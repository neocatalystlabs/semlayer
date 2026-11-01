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


# --- a replay must be recognisable, and a send must not take bystanders ----

def test_every_event_carries_its_own_id(tele):
    """The dedupe key. Random per event rather than derived from the fields:
    two identical commands a second apart are two events, and a content hash
    would silently merge them."""
    _spool(tele, 3)
    ids = [r["event_id"] for r in tele._readable_spool()]
    assert len(set(ids)) == 3
    assert all(len(i) == 32 for i in ids)


def test_a_resend_carries_the_same_ids(tele, monkeypatch):
    """What makes the receiver able to tell a replay from new usage.

    This is the defect's actual shape: the spool is truncated only after a
    2xx, so a response that arrives after the sender's deadline leaves events
    spooled that the receiver already committed. The client is right to retry
    -- it cannot know -- so the ids have to survive the retry unchanged.
    """
    sent = []
    monkeypatch.setattr(tele, "_post", lambda batch: sent.append(batch) or False)
    _spool(tele, 2)
    tele.flush()
    tele.flush()
    assert len(sent) == 2
    assert [r["event_id"] for r in sent[0]] == [r["event_id"] for r in sent[1]]


def test_a_send_removes_only_what_it_sent(tele, monkeypatch, tmp_path):
    """An event recorded while the POST was in flight must survive it.

    The old code truncated the whole file on success, so a busy machine could
    lose a command for every command it reported. Simulated by recording from
    inside `_post`, which is exactly when the race happens.
    """
    def post_then_record(batch):
        tele.record("cli", command="probe", outcome="ok")
        return True

    monkeypatch.setattr(tele, "_post", post_then_record)
    _spool(tele, 2)
    tele.flush()

    left = tele._readable_spool()
    assert [r["command"] for r in left] == ["probe"]


def test_events_beyond_the_batch_cap_are_not_destroyed(tele, monkeypatch):
    """The batch is capped at MAX_SPOOL_EVENTS; the file is not. Truncating
    everything on a successful send threw away what the cap excluded."""
    monkeypatch.setattr(tele, "MAX_SPOOL_EVENTS", 2)
    monkeypatch.setattr(tele, "_post", lambda batch: True)
    _spool(tele, 5)
    tele.flush()
    # Counted on disk, not through `_readable_spool`, which applies the same
    # cap and would report 2 however many were kept.
    assert len(tele._SPOOL.read_text().splitlines()) == 3


def test_a_successful_send_still_prunes_stale_events(tele, monkeypatch):
    """Pruning used to be a side effect of clearing the file. Now that a send
    removes only its own lines, the stale cutoff has to be applied in the
    rewrite as well, or an old event would sit in the spool forever."""
    monkeypatch.setattr(tele, "_post", lambda batch: True)
    old = int(time.time()) - tele.MAX_SPOOL_AGE_S - 60
    tele.record("cli", t=old, command="validate", outcome="ok")
    _spool(tele, 1)
    assert len(tele._SPOOL.read_text().splitlines()) == 2    # both on disk
    tele.flush()
    assert tele._SPOOL.read_text().strip() == ""


def test_the_post_gives_up_before_the_thread_is_abandoned(tele):
    """Same cause as the duplicate sends: one deadline for both meant a
    response arriving at 1.49s left no time to truncate the spool, and the
    events went out again next run."""
    assert tele.POST_TIMEOUT_S < tele.SEND_TIMEOUT_S
