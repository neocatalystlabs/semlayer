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
