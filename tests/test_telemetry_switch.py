"""The off switch must work before anything can be sent.

Until a sender exists these assertions are about intent; the moment one
ships they are the difference between an opt-out and a broken promise.
Every test here isolates SEMLAYER_HOME, because the module resolves its
paths at import time and would otherwise touch the developer's real spool.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def tele(tmp_path, monkeypatch):
    """A telemetry module rooted in a temp dir, with no env switch set."""
    monkeypatch.setenv("SEMLAYER_HOME", str(tmp_path))
    monkeypatch.delenv("SEMLAYER_TELEMETRY", raising=False)
    from semlayer import telemetry
    return importlib.reload(telemetry)


def test_on_by_default(tele):
    assert tele.enabled()
    assert tele.disabled_by() is None


def test_turn_off_writes_a_marker_and_stops_recording(tele, tmp_path):
    tele.turn_off()
    assert (tmp_path / "telemetry_off").exists()
    assert not tele.enabled()
    tele.record("cli", command="infer")
    assert not (tmp_path / "telemetry.jsonl").exists()


def test_turn_on_removes_the_marker(tele, tmp_path):
    tele.turn_off()
    tele.turn_on()
    assert not (tmp_path / "telemetry_off").exists()
    assert tele.enabled()


def test_turn_on_is_idempotent_when_never_off(tele):
    tele.turn_on()          # must not raise on a missing file
    assert tele.enabled()


def test_the_marker_survives_a_new_process(tele, tmp_path, monkeypatch):
    """The point of the marker: an env var dies with the shell, this does not."""
    tele.turn_off()
    monkeypatch.setenv("SEMLAYER_HOME", str(tmp_path))
    from semlayer import telemetry
    fresh = importlib.reload(telemetry)
    assert not fresh.enabled()


@pytest.mark.parametrize("value", ["off", "0", "false", "OFF", "False"])
def test_env_var_turns_it_off(tmp_path, monkeypatch, value):
    monkeypatch.setenv("SEMLAYER_HOME", str(tmp_path))
    monkeypatch.setenv("SEMLAYER_TELEMETRY", value)
    from semlayer import telemetry
    t = importlib.reload(telemetry)
    assert not t.enabled()
    assert t.disabled_by() == "SEMLAYER_TELEMETRY"


def test_turn_on_cannot_clear_the_env_var_and_says_so(tmp_path, monkeypatch):
    """The failure this prevents: a user runs --on, is told nothing, stays off."""
    monkeypatch.setenv("SEMLAYER_HOME", str(tmp_path))
    monkeypatch.setenv("SEMLAYER_TELEMETRY", "off")
    from semlayer import telemetry
    t = importlib.reload(telemetry)
    t.turn_on()
    assert not t.enabled()
    assert t.disabled_by() == "SEMLAYER_TELEMETRY"


def test_it_fails_closed_when_the_marker_cannot_be_read(tele, monkeypatch):
    """A stat() failure must not be read as consent to send."""
    def boom(self):
        raise OSError("permission denied")
    monkeypatch.setattr(type(tele._OFF), "exists", boom)
    assert not tele.enabled()


def test_the_telemetry_command_is_not_itself_recorded(tmp_path, monkeypatch, capsys):
    """Logging that someone opted out, as they opt out, defeats the switch."""
    monkeypatch.setenv("SEMLAYER_HOME", str(tmp_path))
    monkeypatch.delenv("SEMLAYER_TELEMETRY", raising=False)
    from semlayer import telemetry
    importlib.reload(telemetry)
    from semlayer.cli import main
    assert main(["telemetry"]) == 0
    capsys.readouterr()
    assert not (tmp_path / "telemetry.jsonl").exists()


def test_another_command_is_still_recorded(tmp_path, monkeypatch, capsys):
    """The exclusion above must be narrow, not a disabled spool."""
    monkeypatch.setenv("SEMLAYER_HOME", str(tmp_path))
    monkeypatch.delenv("SEMLAYER_TELEMETRY", raising=False)
    from semlayer import telemetry
    importlib.reload(telemetry)
    telemetry.record("cli", command="validate")
    assert "validate" in (tmp_path / "telemetry.jsonl").read_text()
