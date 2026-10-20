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
    from semlayer.pipeline import open_source_from_uri
    with pytest.raises(SourceError) as e:
        open_source_from_uri("snowflake://nope")
    assert e.value.hint and "duckdb" in e.value.hint


@pytest.mark.parametrize("uri", ["mysql://h/db", "", "not a uri", "s3://bucket"])
def test_unsupported_uris_never_raise_a_bare_builtin(uri):
    from semlayer.pipeline import open_source_from_uri
    with pytest.raises(SourceError):
        open_source_from_uri(uri)


def test_the_cli_prints_error_and_hint_rather_than_a_traceback(capsys):
    from semlayer.cli import main
    assert main(["infer", "snowflake://nope"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error:") and "hint:" in err


def test_a_user_mistake_is_not_recorded_as_internal(tmp_path, monkeypatch):
    """`internal` must mean our bug, or it means nothing."""
    import importlib
    import json
    monkeypatch.setenv("SEMLAYER_HOME", str(tmp_path))
    monkeypatch.delenv("SEMLAYER_TELEMETRY", raising=False)
    from semlayer import telemetry
    importlib.reload(telemetry)
    from semlayer.cli import main
    main(["infer", "snowflake://nope"])
    row = json.loads((tmp_path / "telemetry.jsonl").read_text().splitlines()[-1])
    assert row["outcome"] == "error"
    assert row["error_class"] == "connection"
