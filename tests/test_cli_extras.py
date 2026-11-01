"""M1 leftovers: init grant scripts + telemetry opt-out."""

import subprocess
import sys
from pathlib import Path

OSS = Path(__file__).resolve().parent.parent


def test_init_snowflake_renders_minimal_grant():
    from semlayer.init_cmd import render
    sql = render("snowflake", database="proddb", warehouse_name="wh1")
    assert "GRANT SELECT ON FUTURE TABLES IN DATABASE proddb" in sql
    assert "GRANT USAGE ON WAREHOUSE wh1" in sql
    # the drift grant must be commented OPT-IN, never active by default
    active = [line for line in sql.splitlines()
              if "IMPORTED PRIVILEGES" in line and not line.strip().startswith("--")]
    assert not active


def test_init_bigquery_renders_minimal_grant():
    from semlayer.init_cmd import render
    sh = render("bigquery", project="p1")
    assert "roles/bigquery.dataViewer" in sh
    active = [line for line in sh.splitlines()
              if "resourceViewer" in line and not line.strip().startswith("#")]
    assert not active


def test_telemetry_opt_out(tmp_path, monkeypatch):
    monkeypatch.setenv("SEMLAYER_HOME", str(tmp_path))
    monkeypatch.setenv("SEMLAYER_TELEMETRY", "off")
    import importlib
    from semlayer import telemetry
    importlib.reload(telemetry)
    telemetry.record("test_event", n=1)
    assert not (tmp_path / "telemetry.jsonl").exists()


def test_telemetry_records_no_content(tmp_path, monkeypatch):
    monkeypatch.setenv("SEMLAYER_HOME", str(tmp_path))
    monkeypatch.delenv("SEMLAYER_TELEMETRY", raising=False)
    import importlib
    from semlayer import telemetry
    importlib.reload(telemetry)
    telemetry.record("cli", command="validate")
    line = (tmp_path / "telemetry.jsonl").read_text()
    assert "cli" in line and "anon" in line


def test_snowflake_does_not_require_a_role_to_be_named(monkeypatch):
    """A missing role is no longer a wall. It used to be.

    The requirement was protecting a real property -- a minimal-grant claim
    is about the role in effect, and inheriting DEFAULT_ROLE profiles under
    whatever privileges the account holds. But the report recorded the role
    we had been handed, so the claim was never checked, while the refusal
    made a first run impossible for anyone without a role already created.
    `_effective_role` checks it instead, by asking CURRENT_ROLE().

    What this pins down is that validation no longer stops here: with no role
    anywhere, the run reaches the driver and is refused by it instead. The
    driver is stubbed so that holds whether or not `[warehouses]` is
    installed -- without the stub this test would pass in CI for the wrong
    reason and attempt a real connection on a developer's machine.
    """
    import types

    import pytest

    from semlayer.connectors.snowflake import SnowflakeSource
    from semlayer.errors import SourceError

    class _Refused(Exception):
        pass

    mod = types.ModuleType("snowflake")
    conn = types.ModuleType("snowflake.connector")
    errors = types.ModuleType("snowflake.connector.errors")
    errors.Error = _Refused

    def connect(**kw):
        assert "role" not in kw, "no role was set, so none may be sent"
        raise _Refused("incorrect username or password")

    conn.connect = connect
    conn.errors = errors
    mod.connector = conn
    for name, m in (("snowflake", mod), ("snowflake.connector", conn),
                    ("snowflake.connector.errors", errors)):
        monkeypatch.setitem(sys.modules, name, m)

    for var in ("SNOWFLAKE_READER_ROLE", "SNOWFLAKE_ROLE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "acct")
    monkeypatch.setenv("SNOWFLAKE_READER_USER", "u")
    monkeypatch.setenv("SNOWFLAKE_READER_PASSWORD", "p")

    with pytest.raises(SourceError) as e:
        SnowflakeSource.from_env(persona="reader")
    assert "refused the connection" in str(e.value), (
        "a missing role must no longer be refused before the driver is reached")
    assert "session role" not in str(e.value)
