"""LLM provider with cassette record/replay.

Frugality rules (project policy):
- Default model is the CHEAP tier (Haiku). Frontier escalation is explicit,
  never a default.
- Deterministic decoding (temperature 0 where the model supports it), pinned
  model + prompt versions.
- Every call goes through the cassette: keyed on sha256(model + system + user),
  recorded once, replayed forever. CI never pays for a call twice; a missing
  cassette without an API key is a hard error, not a silent live call.

Concurrency: cassette writes are atomic (tmp + rename) so concurrent runs
can never interleave a partial JSON file (BETA Q3).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from semlayer.errors import LLMError

if TYPE_CHECKING:
    import anthropic as _anthropic_types  # noqa: F401  (typing only)

logger = logging.getLogger(__name__)

CHEAP_MODEL = "claude-haiku-4-5-20251001"
CASSETTE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "cassettes"


@runtime_checkable
class LLMProvider(Protocol):
    """The only LLM coupling the engine has (BETA Q4).

    Bedrock/Vertex/other routes implement this and drop in — pipeline code
    must never import a concrete provider directly.
    """

    model: str

    def complete(self, system: str, user: str, max_tokens: int = 1500) -> str:
        """Return the model's text completion for a system+user prompt pair."""
        ...


class CassetteMiss(LLMError):
    """Raised when no cassette exists and live calls are disabled.

    Tests catch this to skip gracefully in keyless environments; it is an
    LLMError so CLI boundaries handle it uniformly.
    """


class AnthropicProvider:
    """Anthropic-backed `LLMProvider` with the cassette layer.

    `allow_live` defaults to key presence: keyless environments (CI) replay
    only — they can never silently spend money or produce unpinned outputs.
    """

    def __init__(self, model: str = CHEAP_MODEL, cassette_dir: Path | None = None,
                 allow_live: bool | None = None):
        self.model = model
        self.cassette_dir = cassette_dir or CASSETTE_DIR
        self.cassette_dir.mkdir(exist_ok=True)
        self.allow_live = (
            allow_live if allow_live is not None else bool(os.environ.get("ANTHROPIC_API_KEY"))
        )
        self._client: object | None = None  # lazy: anthropic import only when live
        self.calls_live = 0
        self.calls_cached = 0
        self.tokens_in = 0
        self.tokens_out = 0

    def _key(self, system: str, user: str) -> str:
        # Cassette identity. MUST stay byte-stable across releases: changing
        # this invalidates every committed cassette.
        h = hashlib.sha256()
        h.update(self.model.encode())
        h.update(b"\x00")
        h.update(system.encode())
        h.update(b"\x00")
        h.update(user.encode())
        return h.hexdigest()[:32]

    def complete(self, system: str, user: str, max_tokens: int = 1500) -> str:
        """Replay from cassette, or call the API and record atomically."""
        path = self.cassette_dir / f"{self._key(system, user)}.json"
        if path.exists():
            self.calls_cached += 1
            return str(json.loads(path.read_text())["response"])
        if not self.allow_live:
            raise CassetteMiss(
                f"no cassette for this prompt and live calls disabled "
                f"(model={self.model}, key={path.name})",
                hint="set ANTHROPIC_API_KEY to record, or run against committed cassettes",
            )
        text, usage_in, usage_out = self._call_live(system, user, max_tokens)
        self.calls_live += 1
        self.tokens_in += usage_in
        self.tokens_out += usage_out
        logger.debug("llm live call model=%s in=%d out=%d", self.model, usage_in, usage_out)
        self._record(path, system, text, usage_in, usage_out)
        return text

    def _call_live(self, system: str, user: str, max_tokens: int) -> tuple[str, int, int]:
        import anthropic

        if self._client is None:
            self._client = anthropic.Anthropic()
        client: anthropic.Anthropic = self._client  # type: ignore[assignment]
        kwargs: dict = {"model": self.model, "max_tokens": max_tokens, "system": system,
                        "messages": [{"role": "user", "content": user}]}
        try:
            msg = client.messages.create(temperature=0, **kwargs)
        except anthropic.BadRequestError as e:
            if "temperature" not in str(e):
                raise LLMError(f"Anthropic API rejected the request: {e}",
                               hint="check model id and account access") from e
            # newer models dropped the temperature param; cassettes still pin outputs
            msg = client.messages.create(**kwargs)
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        if not text:
            raise LLMError(f"model {self.model} returned no text content",
                           hint="response contained only non-text blocks")
        return text, msg.usage.input_tokens, msg.usage.output_tokens

    def _record(self, path: Path, system: str, text: str, usage_in: int, usage_out: int) -> None:
        # Atomic write: concurrent recorders may race on the same key; rename
        # guarantees readers never observe a partial file (last writer wins,
        # which is safe — identical inputs produce equivalent recordings).
        payload = json.dumps({
            "model": self.model,
            "system_sha": hashlib.sha256(system.encode()).hexdigest()[:12],
            "response": text,
            "usage": {"in": usage_in, "out": usage_out},
        }, indent=1)
        fd, tmp = tempfile.mkstemp(dir=self.cassette_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(payload)
            os.replace(tmp, path)
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            raise

    @property
    def spend_summary(self) -> str:
        """One-line live/cached call and token accounting for logs and reports."""
        return (f"live={self.calls_live} cached={self.calls_cached} "
                f"tokens={self.tokens_in}in/{self.tokens_out}out")
