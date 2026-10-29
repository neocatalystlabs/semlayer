"""What foreign-key discovery actually costs, per child column.

This exists because a design note corrected two reviewers with a figure that was
divided by the wrong denominator. 23,198 inclusion tests over 1,312 columns is
17.7 "per column", but only 667 of those columns are children that issue any
test at all: the real rate is ~35 per tested child, and the MEDIAN child sits
at the MAX_QUERIES_PER_CHILD cap. Planning that assumes a typical child is
cheap is planning for a warehouse we have never measured.

The figures are the ones docs/scale.md publishes, so they are asserted rather
than printed. Skipped unless the acquired fixture is present.

**This is a guard, not a regression detector.** It asserts exact child counts
and medians, so any deliberate change to candidate generation will fail it.
That is the intent: these numbers are published, and a change that moves them
should move the docs in the same commit.
"""

from __future__ import annotations

import collections
import pathlib
import statistics

import duckdb
import pytest

import semlayer.link.candidates as C
from semlayer.link.run import link_source
from semlayer.pipeline import profile_with_stats
from semlayer.source import DuckDBSource

EXT = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "external" / "data"

# (fixture, tested children, total tests, median per tested child, share at cap)
# Measured 2026-10-16, deterministic (llm=None, no manifest). Reproduced
# independently by a reviewer within 1.5% before being written down.
EXPECTED = {
    "dbt_synthea": (667, 23_198, 40.0, 0.74),
    "adventureworks_dw": (144, 2_008, 15.5, 0.00),
}


@pytest.mark.parametrize("fixture", sorted(EXPECTED))
def test_fk_query_cost_per_child(fixture):
    db = EXT / fixture / f"{fixture}.duckdb"
    if not db.exists():
        pytest.skip(f"{fixture} not fetched")

    con = duckdb.connect(str(db), read_only=True)
    try:
        src = DuckDBSource(con)
        doc, stats = profile_with_stats(src, llm=None, lineage=None)

        per_child: collections.Counter = collections.Counter()
        real = C._inclusion_batch

        def counted(ex, sbt, ctn, ccn, parents, _r=real):
            per_child[(ctn, ccn)] += len(parents)
            return _r(ex, sbt, ctn, ccn, parents)

        C._inclusion_batch = counted
        try:
            link_source(src, doc, stats, llm=None, use_declared=False, lineage=None)
        finally:
            C._inclusion_batch = real
    finally:
        con.close()

    children, total, median, cap_share = EXPECTED[fixture]
    vals = sorted(per_child.values())
    at_cap = sum(1 for v in vals if v >= C.MAX_QUERIES_PER_CHILD)

    assert len(per_child) == children, f"tested children moved: {len(per_child)}"
    assert abs(sum(vals) - total) / total < 0.02, f"test count moved: {sum(vals)}"
    assert statistics.median(vals) == pytest.approx(median, abs=0.5)
    assert at_cap / len(vals) == pytest.approx(cap_share, abs=0.02)


# --- where the budget actually goes ----------------------------------------
# Half the children never find a parent and spend the full cap discovering it.
# Every stopping rule we have considered acts on the 41% of
# tests that happen AFTER a match; the 57% spent by children that match
# nothing is untouchable by any of them. Recorded here because it redirects
# the optimisation: predicting a no-match child before the first query is
# worth more than every stopping rule combined.

NO_MATCH = {
    # fixture: (no-match children, their share of all tests, tests after first match)
    # 343 -> 341 on 2026-10-28, when the zero-score tail of each child's
    # ranking started at the narrowest parent that can hold the child's values
    # instead of walking every parent in table order: claims.PATIENTID found
    # patients.Id (same 1,171 values, no shared name token) and
    # device_exposure.unique_device_id and payer_plan_period.plan_source_value
    # found an included parent for the first time. Shares did not move.
    "dbt_synthea": (341, 0.57, 0.41),
}


@pytest.mark.parametrize("fixture", sorted(NO_MATCH))
def test_budget_is_spent_by_children_that_match_nothing(fixture):
    db = EXT / fixture / f"{fixture}.duckdb"
    if not db.exists():
        pytest.skip(f"{fixture} not fetched")

    con = duckdb.connect(str(db), read_only=True)
    try:
        src = DuckDBSource(con)
        doc, stats = profile_with_stats(src, llm=None, lineage=None)
        tests: dict = collections.defaultdict(list)
        real = C._inclusion_batch

        def counted(ex, sbt, ctn, ccn, parents, _r=real):
            rs = _r(ex, sbt, ctn, ccn, parents)
            tests[(ctn, ccn)].extend(rs)
            return rs

        C._inclusion_batch = counted
        try:
            link_source(src, doc, stats, llm=None, use_declared=False, lineage=None)
        finally:
            C._inclusion_batch = real
    finally:
        con.close()

    total = sum(len(v) for v in tests.values())
    # The timing block Link reports must count what was actually issued.
    timing = C.last_timing()
    # link_source issues a few inclusion tests of its own outside candidate
    # generation (self-reference checks), so the harness sees slightly more.
    assert 0 <= total - timing["tests"] <= 8, (total, timing["tests"])
    assert 0 <= len(tests) - timing["statements"] <= 8
    assert set(timing) >= {"parents", "children", "index_s", "plan_s", "query_s"}
    no_match = {k: v for k, v in tests.items() if not any(r >= C.MIN_INCLUSION for r in v)}
    after_first = 0
    for v in tests.values():
        for i, r in enumerate(v):
            if r >= C.MIN_INCLUSION:
                after_first += len(v) - i - 1
                break

    n_children, nm_share, after_share = NO_MATCH[fixture]
    assert len(no_match) == n_children
    assert sum(len(v) for v in no_match.values()) / total == pytest.approx(nm_share, abs=0.02)
    assert after_first / total == pytest.approx(after_share, abs=0.02)
