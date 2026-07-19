"""Anonymous usage telemetry — local spool only, opt-out, no network in v1.

Honesty contract (PRD §14): counts commands and durations, NEVER schema
contents, names, or data. Disable with SEMLAYER_TELEMETRY=off or the
`telemetry` key in ~/.semlayer/config. Until a documented endpoint ships
with the cloud product, events only accumulate in the local spool — nothing
leaves the machine; the sender will be a separate, visible release note.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

_DIR = Path(os.environ.get("SEMLAYER_HOME", Path.home() / ".semlayer"))
_SPOOL = _DIR / "telemetry.jsonl"


def enabled() -> bool:
    """Whether telemetry is on (opt-out via env var or config)."""
    return os.environ.get("SEMLAYER_TELEMETRY", "on").lower() not in ("off", "0", "false")


def _anon_id() -> str:
    f = _DIR / "anon_id"
    if not f.exists():
        _DIR.mkdir(parents=True, exist_ok=True)
        f.write_text(uuid.uuid4().hex)
    return f.read_text().strip()


def record(event: str, **props) -> None:
    """Append one event to the local spool.

    Props must be counts/durations/enums only — never identifiers or content.
    """
    if not enabled():
        return
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
        with open(_SPOOL, "a") as f:
            f.write(json.dumps({
                "t": int(time.time()), "anon": _anon_id(), "event": event, **props
            }) + "\n")
    except OSError:
        pass  # telemetry must never break the tool
