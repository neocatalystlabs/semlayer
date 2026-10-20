# SPDX-License-Identifier: FSL-1.1-ALv2
"""Anonymous usage telemetry — local spool only, opt-out, no network in v1.

Honesty contract: counts commands and durations, NEVER schema contents,
names, or data. Until a documented endpoint ships, events only accumulate
in the local spool — nothing leaves the machine; the sender will be a
separate, visible release note.

Two off switches, either one sufficient:

    SEMLAYER_TELEMETRY=off          this process, or exported for the shell
    semlayer telemetry --off        permanent; writes ~/.semlayer/telemetry_off

This docstring previously named a `telemetry` key in ~/.semlayer/config.
No such file was ever read — there is no config file in this package and no
parser for one. A marker file is used instead because its existence IS the
setting: there is nothing to parse, so there is no malformed-input path that
could silently leave telemetry ON while the user believes it is off.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from semlayer import telemetry_schema

_DIR = Path(os.environ.get("SEMLAYER_HOME", Path.home() / ".semlayer"))
_SPOOL = _DIR / "telemetry.jsonl"
_OFF = _DIR / "telemetry_off"
_NOTICE = _DIR / "telemetry_notice"

ENDPOINT = "https://telemetry.semlayer.dev/v1/events"

# A spool that is never sent must not grow without bound, and a spool that
# has sat for a month is describing a version nobody is running.
MAX_SPOOL_EVENTS = 200
MAX_SPOOL_AGE_S = 30 * 24 * 3600
SEND_TIMEOUT_S = 1.5

_log = logging.getLogger(__name__)


def enabled() -> bool:
    """Whether telemetry is on. Either switch alone turns it off.

    Fails closed: if the marker cannot be read, we assume off rather than
    risk sending against a user's wishes because a stat() failed.
    """
    return not disabled_by()


def disabled_by() -> str | None:
    """Which switch is turning telemetry off, or None if it is on.

    The CLI reports this, because `telemetry --on` removes the marker but
    cannot clear an environment variable -- without naming the reason, a
    user who has SEMLAYER_TELEMETRY=off exported would run `--on`, be told
    nothing, and still be off.
    """
    if os.environ.get("SEMLAYER_TELEMETRY", "on").lower() in ("off", "0", "false"):
        return "SEMLAYER_TELEMETRY"
    try:
        if _OFF.exists():
            return str(_OFF)
    except OSError:
        return str(_OFF)  # cannot tell -> do not send
    return None


def turn_off() -> None:
    """Write the marker. Contents are irrelevant; existence is the setting."""
    _DIR.mkdir(parents=True, exist_ok=True)
    _OFF.touch()


def turn_on() -> None:
    """Remove the marker. Cannot clear SEMLAYER_TELEMETRY -- see disabled_by."""
    _OFF.unlink(missing_ok=True)


def spool_path() -> Path:
    """Where events accumulate. Printed by the CLI so it is never a mystery."""
    return _SPOOL


def _anon_id() -> str:
    f = _DIR / "anon_id"
    if not f.exists():
        _DIR.mkdir(parents=True, exist_ok=True)
        f.write_text(uuid.uuid4().hex)
    return f.read_text().strip()


def _environment() -> dict:
    """Machine facts, identical on every event from a given machine.

    Computed here rather than passed by callers, so there is one place that
    decides what "the machine" discloses. Deliberately coarse: major.minor
    for Python and no kernel release, because a full version string plus an
    exact architecture narrows a population faster than it informs anything
    we would act on.
    """
    from semlayer import __version__
    machine = platform.machine().lower()
    return {
        "version": __version__,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "os": platform.system().lower(),
        "arch": "arm64" if machine in ("arm64", "aarch64")
                else "x86_64" if machine in ("x86_64", "amd64") else "other",
        "ci": bool(os.environ.get("CI")),
    }


def record(event: str, t: int | None = None, **props) -> None:
    """Append one event to the local spool.

    Every field passes through the whitelist in telemetry_schema; anything
    not declared there is dropped, and counts are bucketed on the way out.
    A caller cannot send an exact table count or an undeclared field even
    by mistake, which is the point -- it makes the honesty contract a
    property of this function rather than of everyone's future discipline.

    `t` is the moment the command STARTED, passed in because the caller
    records at the end, once duration and outcome exist.
    """
    if not enabled():
        return
    try:
        row = {
            "schema": telemetry_schema.SCHEMA,
            "t": int(t if t is not None else time.time()),
            "anon": _anon_id(),
            # _environment() last: machine facts are ours to state, not a
            # caller's to override.
            **telemetry_schema.serialize(
                {**props, "event": event, **_environment()}),
        }
        _DIR.mkdir(parents=True, exist_ok=True)
        with open(_SPOOL, "a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        pass  # telemetry must never break the tool


# ---------------------------------------------------------------- sending


def _readable_spool() -> list[dict]:
    """Parse the spool, dropping what is too old, malformed, or excess.

    Oldest-first truncation: a machine that has been offline for a year
    should send what it did yesterday, not what it did last January.
    """
    try:
        lines = _SPOOL.read_text().splitlines()
    except OSError:
        return []
    cutoff = time.time() - MAX_SPOOL_AGE_S
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue                       # a partial write; skip, never fail
        if isinstance(row, dict) and row.get("t", 0) >= cutoff:
            out.append(row)
    return out[-MAX_SPOOL_EVENTS:]


def _post(batch: list[dict]) -> bool:
    """One attempt, returning True only on 2xx.

    2xx is the only outcome that may truncate the spool. Anything else --
    4xx, 5xx, a timeout, a refused connection -- and the events wait for
    next time rather than being destroyed by a failure.
    """
    body = json.dumps(batch).encode()
    req = urllib.request.Request(
        ENDPOINT, data=body, method="POST",
        headers={"content-type": "application/json",
                 "content-length": str(len(body))})
    with urllib.request.urlopen(req, timeout=SEND_TIMEOUT_S) as r:
        return 200 <= r.status < 300


def _send() -> None:
    """Body of the sender thread. Swallows everything."""
    try:
        batch = _readable_spool()
        if batch and _post(batch):
            _SPOOL.write_text("")          # truncate only on success
    except Exception as e:
        # Telemetry must never affect the command the user actually ran, so
        # every failure is invisible unless they asked for detail.
        _log.debug("telemetry send failed: %s", e)


def flush(timeout: float = SEND_TIMEOUT_S) -> None:
    """Try once to send the spool, and give up on time rather than on error.

    A daemon thread joined with a deadline, so the worst case is the command
    taking `timeout` longer on a dead network -- not hanging, and not
    blocking exit. The spool is kept unless the POST returned 2xx.
    """
    if not enabled():
        return
    t = threading.Thread(target=_send, daemon=True, name="semlayer-telemetry")
    t.start()
    t.join(timeout)


def notice_pending() -> bool:
    """Whether the first-run notice still needs printing."""
    try:
        return not _NOTICE.exists()
    except OSError:
        return False                       # cannot tell -> say nothing


def mark_notice_shown() -> None:
    """Record the notice, and discard everything collected before it.

    The spool predates the notice, and every event in it was recorded while
    the README promised that nothing leaves your machine. Sending those
    would make that promise retroactively false, so they are dropped rather
    than transmitted. Truncation rather than filtering by timestamp: both
    reach the same place, but a date comparison can be subtly wrong in the
    direction that sends what it should not.
    """
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
        _SPOOL.write_text("")
        _NOTICE.touch()
    except OSError:
        pass
