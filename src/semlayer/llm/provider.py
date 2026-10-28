# SPDX-License-Identifier: FSL-1.1-ALv2
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
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from semlayer.errors import LLMError

if TYPE_CHECKING:
    import anthropic as _anthropic_types  # noqa: F401  (typing only)

logger = logging.getLogger(__name__)

CHEAP_MODEL = "claude-haiku-4-5-20251001"
# Escalation ceiling for a truncated or empty response: 1500 -> 6000 ->
# 24000, then fail loudly. Bounded so a pathological prompt cannot bill
# unboundedly while retrying.
_MAX_TOKENS_CEILING = 24_000
DEFAULT_MAX_TOKENS = 1500


def output_budget(items: int, per_item: int, base: int = 300,
                  floor: int = DEFAULT_MAX_TOKENS) -> int:
    """An output-token ceiling sized to the payload, never below the default.

    A ceiling costs nothing unless the model uses it, while a ceiling that is
    too low costs a whole wasted call: the provider detects truncation and
    repeats the request at four times the budget. Measured live on a
    116-table warehouse at a flat 1500, a 25-column typing escalation
    returned ~2,050 tokens and paid twice, and a 33-column Describe reply
    (~1,440) and a 30-candidate FK chunk (~1,437) sat within 5% of the cap.
    Each call site passes its item count and roughly twice its measured
    tokens per item; the result is clamped to the provider's ceiling.
    """
    return min(_MAX_TOKENS_CEILING, max(floor, base + per_item * max(0, items)))
# Retries on 429 / 5xx / connection errors, with the SDK's exponential
# backoff (0.5 s doubling, capped at 8 s). The SDK default is 2, sized for
# one call at a time. With `--llm-workers` several calls land in the same
# second and a small account's rate limit answers all of them at once, so a
# burst needs more attempts than a lone call did. Six retries is ~16 s of
# backoff before giving up; the error below says what to do then.
_MAX_RETRIES = 6
def _cassette_dir() -> Path:
    """Cassettes live in the repo, not the wheel — same rule as fixtures.

    A pip-installed package resolves __file__ into site-packages, so the repo
    copy is found via the source tree first, then the directory the user is
    standing in (their clone). Without this, a pip-installed benchmark run
    raises CassetteMiss on the first prompt.
    """
    src_tree = Path(__file__).resolve().parent.parent.parent.parent
    for cand in (src_tree, Path.cwd()):
        d = cand / "cassettes"
        # must CONTAIN recordings: __init__ mkdirs this path, so an empty dir
        # left by an earlier run in the wrong directory would otherwise win.
        if d.is_dir() and any(d.glob("*.json")):
            return d
    return src_tree / "cassettes"


CASSETTE_DIR = _cassette_dir()


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
        # Describe pass 1 calls `complete` from a thread pool. The cassette
        # path was already safe (atomic rename); the counters and the lazy
        # client were not, and an unlocked `+=` would only miscount spend,
        # which is still a wrong number in the run report.
        self._lock = threading.Lock()
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
            with self._lock:
                self.calls_cached += 1
            return str(json.loads(path.read_text())["response"])
        if not self.allow_live:
            raise CassetteMiss(
                f"no cassette for this prompt and live calls disabled "
                f"(model={self.model}, key={path.name})",
                hint="set ANTHROPIC_API_KEY to record, or run against committed cassettes",
            )
        text, usage_in, usage_out = self._call_live(system, user, max_tokens)
        with self._lock:
            self.calls_live += 1
            self.tokens_in += usage_in
            self.tokens_out += usage_out
        logger.debug("llm live call model=%s in=%d out=%d", self.model, usage_in, usage_out)
        self._record(path, system, text, usage_in, usage_out)
        return text

    def _create(self, client, kwargs: dict):
        """One messages.create, with every SDK failure translated to LLMError."""
        import anthropic

        try:
            try:
                # extra_body: anthropic>=1.0 dropped temperature from the typed signature
                return client.messages.create(**kwargs, extra_body={"temperature": 0})
            except anthropic.BadRequestError as e:
                if "temperature" not in str(e):
                    raise LLMError(f"Anthropic API rejected the request: {e}",
                                   hint="check model id and account access") from e
                # newer models dropped the temperature param; cassettes still pin outputs
                return client.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            # Only reached after the SDK's own retries are spent. Inside a
            # worker pool this is the one failure the user can fix with a flag,
            # so it must not surface as a raw third-party traceback.
            raise LLMError(
                f"Anthropic rate limit still exceeded after {_MAX_RETRIES} retries "
                f"(model={self.model})",
                hint="lower --llm-workers (1 is fully serial) or raise your account's "
                     "rate limit; nothing was recorded for this prompt, so re-running "
                     "resumes where it stopped") from e
        except anthropic.AuthenticationError as e:
            raise LLMError("Anthropic rejected the API key",
                           hint="check ANTHROPIC_API_KEY") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"could not reach the Anthropic API after {_MAX_RETRIES} retries: "
                           f"{type(e).__name__}",
                           hint="check network access and any proxy; re-running resumes "
                                "from the cassettes already recorded") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API error {e.status_code} (model={self.model})",
                           hint="transient 5xx errors are already retried; if this "
                                "persists, check status.anthropic.com") from e

    def _call_live(self, system: str, user: str, max_tokens: int) -> tuple[str, int, int]:
        import anthropic

        with self._lock:
            if self._client is None:
                self._client = anthropic.Anthropic(max_retries=_MAX_RETRIES)
        client: anthropic.Anthropic = self._client  # type: ignore[assignment]
        kwargs: dict = {"model": self.model, "max_tokens": max_tokens, "system": system,
                        "messages": [{"role": "user", "content": user}]}
        msg = self._create(client, kwargs)
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        # TRUNCATION IS A SILENT WRONG ANSWER, so it is detected here rather
        # than left to each caller. `stop_reason == "max_tokens"` is the only
        # reliable signal: a cut-off response is well-formed text that happens
        # to stop mid-structure, so nothing downstream can tell it apart from a
        # complete one. Measured cost of not checking: on a chattier model 13.2%
        # of responses hit the 1500-token default, and link/validate_llm.py
        # dropped EVERY foreign-key verdict in those chunks on an unparseable
        # array -- FK precision fell to 0.000 on three fixtures with no error
        # raised anywhere. 4-5 merely fit inside the budget; the defect was
        # always there.
        truncated = getattr(msg, "stop_reason", None) == "max_tokens"
        if (not text or truncated) and max_tokens < _MAX_TOKENS_CEILING:
            # models with default thinking can also spend the whole budget on
            # thinking blocks; the same escalation recovers both cases
            logger.warning("llm %s at max_tokens=%d (%s); retrying with %d",
                           self.model, max_tokens,
                           "truncated" if truncated else "no text", max_tokens * 4)
            return self._call_live(system, user, max_tokens * 4)
        if not text:
            raise LLMError(f"model {self.model} returned no text content",
                           hint="response contained only non-text blocks")
        if truncated:
            # Loud, not silent: returning this would record a cassette that
            # pins a half-written answer forever.
            raise LLMError(
                f"model {self.model} truncated its response at the "
                f"{max_tokens}-token ceiling",
                hint="the prompt asks for more output than the ceiling allows; "
                     "reduce the batch size at the call site")
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
