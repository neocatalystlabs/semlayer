"""Where a run has got to: the events, the fraction, and the estimate.

Three things are being checked, and the first is the one that matters most
to whoever is watching: the fraction only ever goes UP. A bar that goes
backwards reads as a bug in the thing being watched, so it is pinned on a
real run rather than argued about.

Then: that every long loop reports at all (a bar that sits still through
76% of a run is not a bar), and that the estimate refuses to guess.
"""

from __future__ import annotations

import json

import pytest

from semlayer import session as sess
from semlayer.progress import ticker


# --- the ticker -----------------------------------------------------------

def test_no_watcher_means_no_work():
    tick = ticker(None, "profile", 10, "profiling")
    assert tick(3) is None               # and nothing raised


def test_done_is_how_many_are_finished():
    """Called BEFORE each unit, so `done` counts completed ones and the
    label counts the one about to run."""
    seen = []
    tick = ticker(seen.append, "profile", 4, "profiling table")
    tick(0)
    tick(3)
    assert seen[0]["done"] == 0
    assert seen[0]["within"] == 0.0
    assert seen[0]["label"] == "profiling table 1 of 4"
    assert seen[1]["within"] == 0.75
    assert seen[1]["label"] == "profiling table 4 of 4"


def test_an_empty_stage_is_complete_not_undefined():
    seen = []
    ticker(seen.append, "profile", 0, "profiling")(0)
    assert seen[0]["within"] == 1.0


def test_the_fraction_is_clamped():
    """A stage that miscounts its own units must not push the bar past the
    end or below the start."""
    seen = []
    ticker(seen.append, "link", 4, "checking", within=lambda d, t: 9.0)(1)
    ticker(seen.append, "link", 4, "checking", within=lambda d, t: -3.0)(1)
    assert [e["within"] for e in seen] == [1.0, 0.0]


# --- a real run -----------------------------------------------------------

@pytest.fixture
def events(built_fixture):
    """Every progress event from a real no-LLM run over 36 tables."""
    from semlayer.pipeline import InferOptions, infer_with_report, open_source_from_uri
    out: list[dict] = []
    source = open_source_from_uri(f"duckdb:{built_fixture('messy_mart')}")
    infer_with_report(source, llm=None, options=InferOptions(name="t"),
                      progress=out.append)
    return out


def test_every_stage_reports_its_boundaries(events):
    starts = {e["stage"] for e in events if e["event"] == "start"}
    done = {e["stage"] for e in events if e["event"] == "done"}
    assert starts == done == {"profile", "link", "enrich"}


def test_profile_reports_every_table(events):
    units = [e for e in events if e["event"] == "unit" and e["stage"] == "profile"]
    assert len(units) == 36
    assert units[0]["total"] == 36
    assert [u["done"] for u in units] == list(range(36))


def test_link_reports_from_inside_its_queries(events):
    """Link is 76% of a run with no model. A bar that cannot move during it
    is not worth having."""
    units = [e for e in events if e["event"] == "unit" and e["stage"] == "link"]
    assert len(units) > 10, "link reported nothing useful"
    assert units[-1]["within"] > 0.9


def test_every_unit_event_can_be_read_aloud(events):
    for e in events:
        if e["event"] == "unit":
            assert e["label"] and " of " in e["label"], e


def test_the_fraction_never_goes_backwards(events):
    """The property worth pinning: a bar that goes backwards reads as a bug
    in whatever is being watched."""
    job = sess.InferJob("duckdb:w.duckdb", None, None, with_llm=False)
    fractions = []
    for e in events:
        job.progress(e)
        fractions.append(job.fraction)
    assert fractions == sorted(fractions), fractions
    # Still "running" as far as this job knows, so it stops short of 100%:
    # the last stage ending is not the run ending.
    assert fractions[-1] == 0.99
    job.state = "done"
    assert job.fraction == 1.0


def test_the_stage_label_does_not_outlive_its_stage(events):
    """Enrich has no per-unit loop, so a label left over from link would be
    read out as what the run is doing now."""
    job = sess.InferJob("duckdb:w.duckdb", None, None, with_llm=False)
    for e in events:
        job.progress(e)
        if e["stage"] == "enrich":
            assert job.label is None
            assert job.as_dict()["doing"] == sess.STAGE_WORDS["enrich"]


# --- describe, which needs a model ---------------------------------------

class _Stub:
    """A model that answers instantly, so the two-pass shape can be tested."""

    model = "stub"

    def complete(self, system, prompt, max_tokens=None):
        return json.dumps({"description": "a table", "columns": {}})


def test_describe_reports_both_passes(built_fixture):
    from semlayer.describe import describe_source
    from semlayer.pipeline import InferOptions, infer_with_report, open_source_from_uri

    source = open_source_from_uri(f"duckdb:{built_fixture('fan_trap')}")
    doc, _ = infer_with_report(source, llm=None, options=InferOptions(name="t"))
    stats: dict = {}
    from semlayer.profile.run import profile_with_stats
    doc, stats = profile_with_stats(source, name="t")
    seen: list[dict] = []
    describe_source(doc, stats, _Stub(), progress=seen.append)
    units = [e for e in seen if e["event"] == "unit"]
    assert units, "describe reported nothing"
    assert {"pass 1 of 2" in u["label"] for u in units} == {True, False}
    # each pass covers half the stage, and nothing exceeds it
    assert max(u["within"] for u in units) <= 1.0
    assert [u["within"] for u in units] == sorted(u["within"] for u in units)


# --- the estimate ---------------------------------------------------------

def _job(fraction: float, elapsed: int, with_llm: bool = False):
    """A job that is `fraction` through and has been running `elapsed`.

    The placement asserts itself. The first version of this helper always
    marked profile complete, so it could not express a fraction below
    profile's own weight -- and the test for "no estimate yet" was passing
    0.01 and getting 0.18, which made the assertion meaningless.
    """
    job = sess.InferJob("duckdb:w.duckdb", None, None, with_llm=with_llm)
    job.started_at = sess.time.time() - elapsed
    if fraction <= job.weights["profile"]:
        job.stage = "profile"
        job._within = fraction / job.weights["profile"]
    else:
        job.stages = {"profile": 1.0}
        job.stage = "link"
        job._within = (fraction - job.weights["profile"]) / job.weights["link"]
    assert abs(job.fraction - fraction) < 0.01, (job.fraction, fraction)
    return job


def test_no_estimate_in_the_first_seconds():
    assert _job(0.5, elapsed=2).eta_s is None


def test_no_estimate_before_there_is_anything_to_extrapolate_from():
    assert _job(0.01, elapsed=30).eta_s is None


def test_an_estimate_once_the_run_has_shown_its_pace():
    eta = _job(0.5, elapsed=60).eta_s
    assert eta is not None
    assert 50 <= eta <= 70, eta


def test_no_estimate_when_it_is_nearly_over():
    """"Roughly ten seconds left" on a run about to finish is noise, and on
    a short run the floor was being reported for its whole second half."""
    assert _job(0.9, elapsed=20).eta_s is None


def test_a_running_job_never_reports_a_hundred_percent():
    """The last stage ending is not the run ending -- the document still has
    to be written and swapped in. A bar stuck at 100% while the thing is
    still going is the commonest way a progress bar lies."""
    job = sess.InferJob("duckdb:w.duckdb", None, None, with_llm=False)
    job.stages = dict.fromkeys(job.weights, 1.0)
    assert job.running
    assert job.fraction == 0.99
    job.state = "done"
    assert job.fraction == 1.0


def test_the_estimate_is_rounded_not_precise():
    """A number to the second invites being held to it."""
    for elapsed in range(20, 90, 7):
        eta = _job(0.5, elapsed=elapsed).eta_s
        assert eta is None or eta % 10 == 0, eta


def test_no_estimate_once_it_is_finished():
    job = _job(0.5, elapsed=60)
    job.state = "done"
    assert job.eta_s is None


# --- the weights ----------------------------------------------------------

def test_the_weights_depend_on_whether_a_model_runs():
    """Describe is 43% of a run with a model and does not happen without
    one, so one set of weights cannot serve both."""
    assert sess.InferJob("x", None, None, with_llm=True).weights is sess.STAGE_WEIGHTS_LLM
    assert sess.InferJob("x", None, None, with_llm=False).weights is \
        sess.STAGE_WEIGHTS_NO_LLM
    assert "describe" in sess.STAGE_WEIGHTS_LLM
    assert "describe" not in sess.STAGE_WEIGHTS_NO_LLM


@pytest.mark.parametrize("weights", [sess.STAGE_WEIGHTS_LLM,
                                     sess.STAGE_WEIGHTS_NO_LLM])
def test_the_weights_add_up(weights):
    assert abs(sum(weights.values()) - 1.0) < 0.01, weights
