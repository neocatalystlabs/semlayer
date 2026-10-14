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


def test_snowflake_requires_an_explicit_session_role(monkeypatch):
    """The session role must be set, never inherited from DEFAULT_ROLE.

    It was never passed to snowflake.connector.connect at all, so the session
    ran under whatever the account's DEFAULT_ROLE happened to be. That worked
    only because `semlayer init` creates a service user with DEFAULT_ROLE set
    to the reader role it also creates, and broke silently the moment anyone
    ran semlayer as their own account -- profiling under whatever privileges
    they hold. Our minimal-grant claim is about the role in effect, so
    inheriting one makes the claim unverifiable.
    """
    import pytest

    from semlayer.connectors.snowflake import SnowflakeSource
    from semlayer.errors import SemlayerError

    for var in ("SNOWFLAKE_READER_ROLE", "SNOWFLAKE_ROLE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "acct")
    monkeypatch.setenv("SNOWFLAKE_READER_USER", "u")
    monkeypatch.setenv("SNOWFLAKE_READER_PASSWORD", "p")

    with pytest.raises(SemlayerError, match="session role"):
        SnowflakeSource.from_env(persona="reader")
