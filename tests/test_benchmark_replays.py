"""`python -m semlayer.benchmark` must reproduce its table with no API key.

docs/benchmark.md opens by claiming the results are reproducible from committed
recordings. That claim was false twice: cassettes for `tpcds_clean` were
re-recorded locally for a report in October and never committed, so a third
party running the documented command got a CassetteMiss traceback instead of a
table -- and docs/calibration.md shows the same thing had already happened once
before, with an earlier draft's number flattered by the fixtures that did
replay.

The pattern is structural, not careless: a maintainer with a key never sees the
failure, because their run records what is missing and passes. Only a keyless
run can tell. So this test is the claim's only real guard, and it is why the
benchmark's reproducibility is now enforced rather than asserted.

Enforced AT A RELEASE, not on every commit (decided 2026-10-30). The table in
docs/benchmark.md describes a named release; the engine keeps evolving between
releases, and each type change on messy_mart alters the layer the benchmark
embeds in its prompts, which would force a paid re-record per PR to keep a
document current. Cassettes are re-recorded for functional reasons only. So
these tests run when SEMLAYER_RELEASE_CHECK=1 (the release workflow sets it,
and whoever publishes new numbers runs it) and are skipped in ordinary CI.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest

OSS = pathlib.Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not os.environ.get("SEMLAYER_RELEASE_CHECK"),
    reason="benchmark replays are a release-time check: docs/benchmark.md describes a named "
           "release, not HEAD; set SEMLAYER_RELEASE_CHECK=1 to run them")


@pytest.mark.slow
@pytest.mark.skipif(not (OSS / "fixtures" / "build" / "messy_mart.duckdb").exists(),
                    reason="fixtures not built: python fixtures/build.py")
def test_benchmark_reproduces_with_no_api_key():
    env = dict(os.environ)
    env["ANTHROPIC_API_KEY"] = ""        # EMPTY, not unset: see CONTRIBUTING.md
    r = subprocess.run([sys.executable, "-m", "semlayer.benchmark"],
                       cwd=OSS, env=env, capture_output=True, text=True)
    assert r.returncode == 0, (
        "the documented reproduce command failed without a key, so the "
        "reproducibility claim in docs/benchmark.md is false:\n"
        + r.stderr[-1500:])
    # a table, not a traceback
    assert "| Fixture | CQs | Answerer |" in r.stdout
    for fixture in ("fan_trap", "messy_mart", "tpcds_clean"):
        assert f"| {fixture} |" in r.stdout, f"{fixture} missing from the table"
    assert "CassetteMiss" not in r.stdout + r.stderr


@pytest.mark.slow
@pytest.mark.skipif(not (OSS / "fixtures" / "build" / "messy_mart.duckdb").exists(),
                    reason="fixtures not built")
def test_published_table_matches_what_the_command_prints():
    """The committed doc must carry the numbers the command actually produces."""
    env = dict(os.environ)
    env["ANTHROPIC_API_KEY"] = ""
    r = subprocess.run([sys.executable, "-m", "semlayer.benchmark"],
                       cwd=OSS, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]
    printed = {ln.split("|")[1].strip(): ln.strip()
               for ln in r.stdout.splitlines()
               if ln.startswith("| ") and ln.count("|") >= 7}
    doc = (OSS / "docs" / "benchmark.md").read_text()
    for fixture, row in printed.items():
        if fixture in ("Fixture", "---"):
            continue
        assert row in doc, (
            f"docs/benchmark.md does not contain the row the command prints for "
            f"{fixture}:\n  printed: {row}\n"
            "re-run `python -m semlayer.benchmark` and paste the table in.")


@pytest.mark.slow
@pytest.mark.skipif(not (OSS / "fixtures" / "build" / "messy_mart.duckdb").exists(),
                    reason="fixtures not built")
def test_the_sonnet_row_reproduces_too():
    """Every PUBLISHED row needs a guard, not just the default invocation.

    benchmark.md carries a claude-sonnet-5 row, and `python -m semlayer.benchmark`
    does not produce it: that row needs an explicit answer_model. So the first
    version of this file passed while the Sonnet row was unreproducible -- the
    third instance of the same defect, let through by a guard that only covered
    the rows it happened to run. A published number with no keyless check is how
    this recurs.
    """
    import subprocess
    code = (
        "import sys; sys.path.insert(0, 'src'); sys.path.insert(0, '.')\n"
        "from semlayer.benchmark import run_fixture\n"
        "r = run_fixture('messy_mart', answer_model='claude-sonnet-5',\n"
        "                conditions=['schema_only', 'semantic', 'semantic+lint'])\n"
        "print({c: v['passed'] for c, v in r['conditions'].items()}, r['n_cqs'])\n")
    env = dict(os.environ)
    env["ANTHROPIC_API_KEY"] = ""
    r = subprocess.run([sys.executable, "-c", code], cwd=OSS, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, (
        "the published Sonnet row does not replay without a key:\n"
        + r.stderr[-1200:])
    # the row in docs/benchmark.md, as published
    assert "'schema_only': 21" in r.stdout, r.stdout
    assert "'semantic': 33" in r.stdout, r.stdout
    doc = (OSS / "docs" / "benchmark.md").read_text()
    assert "| messy_mart | 38 | claude-sonnet-5 | 0.55 | 0.87 | 0.87 | — |" in doc, (
        "docs/benchmark.md's Sonnet row does not match what replays")
