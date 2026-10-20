"""The sender. No test here opens a socket.

The properties that matter are all about failure, because the failure modes
are the ones that destroy data: a spool truncated on a response that was not
a success, a command slowed or hung by a dead endpoint, or events collected
under the old "nothing leaves your machine" promise being transmitted after
an upgrade.
"""

from __future__ import annotations

import importlib
import json
import time

import pytest


@pytest.fixture
def tele(tmp_path, monkeypatch):
    monkeypatch.setenv("SEMLAYER_HOME", str(tmp_path))
    monkeypatch.delenv("SEMLAYER_TELEMETRY", raising=False)
    from semlayer import telemetry
    t = importlib.reload(telemetry)
    (tmp_path / "telemetry_notice").touch()      # notice already shown
    return t


def _spool(tele, n=1):
    for i in range(n):
        tele.record("cli", command="validate", outcome="ok", duration_s=i)


# --- truncation is earned, not assumed --------------------------------

def test_a_2xx_truncates_the_spool(tele, monkeypatch, tmp_path):
    monkeypatch.setattr(tele, "_post", lambda batch: True)
    _spool(tele, 3)
    tele.flush()
    assert tele._readable_spool() == []


@pytest.mark.parametrize("outcome", [
    lambda batch: False,                                  # non-2xx
    lambda batch: (_ for _ in ()).throw(OSError("refused")),
    lambda batch: (_ for _ in ()).throw(TimeoutError()),
])
def test_anything_other_than_2xx_keeps_the_spool(tele, monkeypatch, outcome):
    """A failure must never destroy pending events."""
    monkeypatch.setattr(tele, "_post", outcome)
    _spool(tele, 3)
    tele.flush()
    assert len(tele._readable_spool()) == 3


def test_the_off_switch_stops_the_sender_before_any_socket(tele, monkeypatch):
    called = []
    monkeypatch.setattr(tele, "_post", lambda b: called.append(b) or True)
    _spool(tele, 2)
    tele.turn_off()
    tele.flush()
    assert called == []
    assert len(tele._readable_spool()) == 2


# --- the command must not pay for the endpoint ------------------------

def test_a_hanging_endpoint_does_not_hang_the_command(tele, monkeypatch):
    def hang(batch):
        time.sleep(30)
        return True
    monkeypatch.setattr(tele, "_post", hang)
    _spool(tele, 1)
    started = time.monotonic()
    tele.flush(timeout=0.3)
    assert time.monotonic() - started < 2.0
    assert len(tele._readable_spool()) == 1      # kept, since it never finished


# --- spool hygiene ----------------------------------------------------

def test_events_older_than_the_window_are_dropped(tele, tmp_path):
    old = {"schema": 1, "t": int(time.time()) - tele.MAX_SPOOL_AGE_S - 10,
           "anon": "a" * 32, "event": "cli"}
    (tmp_path / "telemetry.jsonl").write_text(json.dumps(old) + "\n")
    _spool(tele, 1)
    kept = tele._readable_spool()
    assert len(kept) == 1 and kept[0]["t"] > old["t"]


def test_the_spool_is_capped_oldest_first(tele):
    _spool(tele, tele.MAX_SPOOL_EVENTS + 25)
    kept = tele._readable_spool()
    assert len(kept) == tele.MAX_SPOOL_EVENTS
    assert kept[-1]["duration_s"] == tele.MAX_SPOOL_EVENTS + 24   # newest kept


def test_a_partial_line_is_skipped_not_fatal(tele, tmp_path):
    """A process killed mid-write leaves half a line. It must not poison
    every future send."""
    _spool(tele, 1)
    with open(tmp_path / "telemetry.jsonl", "a") as f:
        f.write('{"schema": 1, "t": 17604')          # truncated
    assert len(tele._readable_spool()) == 1


# --- the upgrade promise ----------------------------------------------

def test_the_first_run_discards_everything_collected_before_it(tele, tmp_path):
    """Those events were recorded while the README said nothing leaves your
    machine. Sending them would make that promise retroactively false."""
    (tmp_path / "telemetry_notice").unlink()
    _spool(tele, 5)
    assert len(tele._readable_spool()) == 5
    assert tele.notice_pending()
    tele.mark_notice_shown()
    assert tele._readable_spool() == []
    assert not tele.notice_pending()
