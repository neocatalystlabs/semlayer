"""The examples/agent_pipeline demo must keep demonstrating what it claims.

These tests replay committed cassettes only -- they never call an API. They
exist because the example is cited in public writing: if a refactor changes
what the agents do, that should fail here rather than quietly make the
published numbers wrong.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

OSS = Path(__file__).resolve().parents[1]
EXAMPLE = OSS / "examples" / "agent_pipeline"
sys.path.insert(0, str(EXAMPLE))

pytestmark = pytest.mark.skipif(
    not (OSS / "fixtures" / "build" / "messy_mart.duckdb").exists(),
    reason="fixture not built: python fixtures/build.py",
)

TRUE_REVENUE = 14_621_503.30
RAW_REVENUE = 16_300_242.85


@pytest.fixture(scope="module")
def results():
    """Both modes, replay-only. Shared: the layer build and MCP spin-up are slow."""
    import pipeline

    llm = _replay_only(pipeline)
    return {m: pipeline.run_pipeline(m, llm) for m in ("raw", "layer")}


def _replay_only(pipeline):
    from semlayer.llm.provider import AnthropicProvider

    return AnthropicProvider(cassette_dir=pipeline.CASSETTES, allow_live=False)


def test_truth_is_computed_not_asserted(results):
    """The yardstick comes from SQL over the fixture, not from a constant here."""
    import pipeline

    truth = pipeline._truth()
    assert truth["revenue"] == pytest.approx(TRUE_REVENUE, abs=0.01)


def test_raw_mode_overstates_revenue(results):
    rev = results["raw"].by_name("metrics").answer["revenue"]
    assert rev == pytest.approx(RAW_REVENUE, abs=0.01)
    # the overstatement is exactly the cancelled orders, nothing else
    assert rev - TRUE_REVENUE == pytest.approx(1_678_739.55, abs=0.01)


def test_layer_mode_matches_truth(results):
    rev = results["layer"].by_name("metrics").answer["revenue"]
    assert rev == pytest.approx(TRUE_REVENUE, abs=0.01)


def test_every_reported_number_was_actually_executed(results):
    """No mode may report a figure that no run_sql returned (see README)."""
    import pipeline

    for mode, r in results.items():
        m = r.by_name("metrics")
        assert pipeline._grounded(m, m.answer["revenue"]), f"{mode}: revenue not executed"
        b = r.by_name("breakdown")
        assert pipeline._grounded(b, b.answer["avg_order_value"]), f"{mode}: aov not executed"


def test_only_layer_mode_checks_its_sql(results):
    def checks(r):
        return sum(1 for run in r.runs for t in run.turns if t.tool == "check_sql")

    assert checks(results["raw"]) == 0
    assert checks(results["layer"]) > 0


def test_the_action_diverges(results):
    """The point of the example: raw files a ticket for a breach that did not happen."""
    raw, layer = (results[m].by_name("alert").answer for m in ("raw", "layer"))
    assert raw["escalate"] is True
    assert raw["ticket"].strip()
    assert layer["escalate"] is False
    assert not layer.get("ticket", "").strip()


def test_prompts_are_identical_across_modes():
    """Only the tool list and the layer instruction may differ, never the task."""
    import pipeline

    for role in ("metrics", "breakdown", "alert"):
        raw = pipeline._system(role, ["t"], False)
        layer = pipeline._system(role, ["t"], True)
        assert raw == layer.replace("\n\n" + pipeline._LAYER_INSTRUCTION, "")


def test_fabricated_results_cannot_reach_the_caller():
    """A reply that calls a tool and then invents its result stops at the call."""
    from agents import _parse

    verb, tool, args = _parse(
        'CALL check_sql {"sql": "SELECT 1"}\n\nRESULT: [{"revenue": 99}]\n\n'
        'ANSWER {"revenue": 99}')
    assert (verb, tool) == ("CALL", "check_sql")
    assert args == {"sql": "SELECT 1"}
