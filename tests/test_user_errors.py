"""Ordinary mistakes must not produce tracebacks.

A mistyped filename or an unsupported URI is a user error, and the tool
already has a vocabulary for those -- SemlayerError carries a message and a
hint, and main() prints them as `error:` / `hint:`. These paths were raising
bare builtins instead, so a typo looked like a crash.

Found while building telemetry: everything was being classified `internal`,
which should mean OUR bug. The classification was right and the code was
wrong.
"""

from __future__ import annotations

import pytest

from semlayer.errors import SourceError


def test_validate_reports_a_missing_file_instead_of_raising(tmp_path):
    """`validate` takes several paths; one missing must not kill the run."""
    from semlayer.validate import validate_file
    result = validate_file(tmp_path / "nope.yaml")
    assert not result.ok
    assert any("not found" in e for e in result.errors)


def test_validate_reports_a_directory(tmp_path):
    from semlayer.validate import validate_file
    result = validate_file(tmp_path)
    assert not result.ok
    assert any("not a file" in e for e in result.errors)


def test_validate_keeps_going_after_a_missing_file(tmp_path, capsys):
    """The reason this is reported rather than raised."""
    good = tmp_path / "good.yaml"
    good.write_text("semantic_layer:\n  version: 1\n  tables: []\n")
    from semlayer.cli import main
    main(["validate", str(tmp_path / "nope.yaml"), str(good)])
    out = capsys.readouterr().out
    assert "nope.yaml" in out and "good.yaml" in out


def test_an_unsupported_uri_is_a_source_error_with_a_hint():
    """`snowflake://...` used to stand in for "unsupported" here, and that is
    exactly what the one person who tried a real warehouse pasted. It is a
    supported form now (tests/test_snowflake_connect.py), so this needs a URI
    that genuinely is not one."""
    from semlayer.pipeline import open_source_from_uri
    with pytest.raises(SourceError) as e:
        open_source_from_uri("redshift://cluster/db")
    assert e.value.hint and "duckdb" in e.value.hint


@pytest.mark.parametrize("uri", ["mysql://h/db", "", "not a uri", "s3://bucket"])
def test_unsupported_uris_never_raise_a_bare_builtin(uri):
    from semlayer.pipeline import open_source_from_uri
    with pytest.raises(SourceError):
        open_source_from_uri(uri)


def test_the_cli_prints_error_and_hint_rather_than_a_traceback(capsys, tmp_path):
    """Steady state, so the first-run telemetry notice is not in the way.

    This previously asserted stderr STARTED with "error:" and passed only
    because the developer's real ~/.semlayer already had a notice marker.
    Once every test got an isolated home the notice printed first and the
    assertion failed -- correctly. The notice preceding an error on a
    genuinely first run is right; the test's assumption was not.
    """
    import os
    from pathlib import Path
    Path(os.environ["SEMLAYER_HOME"]).mkdir(parents=True, exist_ok=True)
    (Path(os.environ["SEMLAYER_HOME"]) / "telemetry_notice").touch()

    from semlayer.cli import main
    assert main(["infer", "redshift://cluster/db"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error:") and "hint:" in err
    assert "Traceback" not in err


def test_a_user_mistake_is_not_recorded_as_internal(tmp_path, monkeypatch):
    """`internal` must mean our bug, or it means nothing."""
    import importlib
    import json
    monkeypatch.setenv("SEMLAYER_HOME", str(tmp_path))
    monkeypatch.delenv("SEMLAYER_TELEMETRY", raising=False)
    from semlayer import telemetry
    importlib.reload(telemetry)
    from semlayer.cli import main
    main(["infer", "redshift://cluster/db"])
    # The spool survives because conftest blocks the send, which is also
    # what a user with no network sees.
    lines = (tmp_path / "telemetry.jsonl").read_text().splitlines()
    row = json.loads(lines[-1])
    assert row["outcome"] == "error"
    assert row["error_class"] == "connection"


def test_a_bare_filename_never_becomes_a_connector(tmp_path, monkeypatch):
    """The scheme is everything before the first colon, so a path with no
    scheme would otherwise record a filename."""
    import argparse
    import importlib
    import json
    monkeypatch.setenv("SEMLAYER_HOME", str(tmp_path))
    monkeypatch.delenv("SEMLAYER_TELEMETRY", raising=False)
    from semlayer import telemetry
    importlib.reload(telemetry)
    from semlayer.cli import _note_invocation

    _note_invocation(argparse.Namespace(source="acme_finance_prod.duckdb"))
    telemetry.record("cli", command="infer")
    row = json.loads((tmp_path / "telemetry.jsonl").read_text().splitlines()[-1])
    assert "connector" not in row
    assert "acme_finance_prod" not in json.dumps(row)


def test_the_connector_is_the_scheme_not_the_uri(tmp_path, monkeypatch):
    """`connector` must be the warehouse type and nothing else -- the URI
    carries accounts, hosts, databases and sometimes credentials."""
    import argparse
    import importlib
    monkeypatch.setenv("SEMLAYER_HOME", str(tmp_path))
    monkeypatch.delenv("SEMLAYER_TELEMETRY", raising=False)
    from semlayer import telemetry
    importlib.reload(telemetry)
    from semlayer.cli import _note_invocation

    secret = "postgresql://user:hunter2@db.internal.acme.com:5432/finance_prod"
    _note_invocation(argparse.Namespace(source=secret))
    telemetry.record("cli", command="infer")

    import json
    row = json.loads((tmp_path / "telemetry.jsonl").read_text().splitlines()[-1])
    assert row["connector"] == "postgres"   # postgresql:// normalised
    blob = json.dumps(row)
    for leaked in ("hunter2", "acme", "finance_prod", "db.internal"):
        assert leaked not in blob


def test_a_nonzero_exit_without_an_exception_is_classified(tmp_path, monkeypatch):
    """validate on a missing file returns 1 and raises nothing.

    That path recorded outcome=error with no class at all -- the first real
    error row telemetry received had error_class null, so the table could
    not say whether a user had mistyped a path or the tool had crashed.
    """
    import importlib
    import json
    monkeypatch.setenv("SEMLAYER_HOME", str(tmp_path))
    monkeypatch.delenv("SEMLAYER_TELEMETRY", raising=False)
    from semlayer import telemetry
    importlib.reload(telemetry)
    from semlayer.cli import main
    assert main(["validate", str(tmp_path / "does-not-exist.yaml")]) == 1
    lines = (tmp_path / "telemetry.jsonl").read_text().splitlines()
    row = json.loads(lines[-1])
    assert row["outcome"] == "error"
    assert row["error_class"] == "invalid_input"
