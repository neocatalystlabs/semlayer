"""examples/assisted_build/build.py runs end to end without an LLM and
records what it planned beside what it built."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

OSS = Path(__file__).resolve().parents[1]


def _load_script():
    """The script under its own module name: a plain `import build` resolved
    to fixtures/build.py in CI, where that file had already been imported."""
    path = OSS / "examples" / "assisted_build" / "build.py"
    spec = importlib.util.spec_from_file_location("assisted_build_script", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


build = _load_script()

FIXTURE = OSS / "fixtures" / "build" / "messy_mart.duckdb"
pytestmark = pytest.mark.skipif(not FIXTURE.exists(),
                                reason="fixture not built: python fixtures/build.py")


def test_no_llm_build_writes_a_valid_layer_and_a_reproducible_report(tmp_path, capsys):
    out = tmp_path / "layer.yaml"
    rc = build.main([f"duckdb:{FIXTURE}", "--no-llm", "--no-declared-constraints",
                     "--yes", "-o", str(out), "--query-workers", "4"])
    assert rc == 0
    text = capsys.readouterr().out
    for heading in ("# Probe", "## What this run will do", "## Estimate", "## Building", "## Result"):
        assert heading in text
    assert "valid=True" in text
    report = json.loads((tmp_path / "layer.yaml.report.json").read_text())
    assert report["options"]["query_workers"] == 4
    assert report["options"]["use_declared"] is False
    assert report["probe"]["grade"] in ("weak", "fair", "strong")
    assert report["estimate"]["tables_modelled"] >= 1
    assert set(report["stages"]) >= {"profile", "link", "enrich"}


def test_estimate_scales_with_workers_and_says_so():
    class P:
        tables, columns, duplicate_copies_removable = 200, 3000, 50

    serial = build.estimate(P, llm_workers=1, query_workers=1, local=False, with_llm=True)
    pooled = build.estimate(P, llm_workers=8, query_workers=8, local=False, with_llm=True)
    assert serial["tables_modelled"] == pooled["tables_modelled"] == 150
    assert pooled["seconds"]["link_inclusion"] < serial["seconds"]["link_inclusion"]
    assert pooled["seconds"]["profile_and_describe_llm"] * 8 == pytest.approx(
        serial["seconds"]["profile_and_describe_llm"], abs=8)
    dry = build.estimate(P, llm_workers=8, query_workers=8, local=False, with_llm=False)
    assert dry["usd_llm"] == 0.0 and dry["seconds"]["profile_and_describe_llm"] == 0


def test_declining_the_prompt_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "confirm", lambda args: False)
    out = tmp_path / "layer.yaml"
    rc = build.main([f"duckdb:{FIXTURE}", "--no-llm", "-o", str(out)])
    assert rc == 2
    assert not out.exists()
