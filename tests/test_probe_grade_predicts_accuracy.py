"""Does the evidence grade predict the accuracy the engine actually reaches?

`probe` tells a user how far to trust the output before any inference runs.
That claim is only worth making if the grade tracks reality, so it is measured
here rather than asserted: the same warehouse is built twice, differing in
nothing but what the tables and columns are CALLED, and both are inferred and
scored against the generator's own ground truth.

Measured 2026-10-16 at 300 tables:

    wide_conventional   grade fair   FK precision 1.000   recall 1.000
    wide_opaque         grade weak   FK precision 0.394   recall 0.284

A 0.606 precision gap from naming alone. That number is what a naming
convention is worth to this engine, and it is why three separate mechanisms
(the ETL-layer rule, the FK prior, margin stopping) all reading names is a
single point of failure rather than three defences.

Slow by design -- it runs two full inferences -- so it is opt-in:

    SEMLAYER_SLOW=1 pytest tests/test_probe_grade_predicts_accuracy.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import duckdb
import pytest

from semlayer.link.run import link_source
from semlayer.pipeline import profile_with_stats
from semlayer.probe import tier1
from semlayer.source import DuckDBSource

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))

pytestmark = pytest.mark.skipif(
    not os.environ.get("SEMLAYER_SLOW"),
    reason="two full inferences; set SEMLAYER_SLOW=1",
)

# (entry point, expected grade, precision floor, recall floor)
CASES = {
    "conventional": ("build", "fair", 0.95, 0.95),
    "opaque": ("build_opaque", "weak", 0.30, 0.20),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_grade_predicts_accuracy(case, monkeypatch):
    monkeypatch.setenv("SEMLAYER_WIDE_TABLES", "300")
    from generators import wide

    entry, want_grade, pfloor, rfloor = CASES[case]
    con = duckdb.connect(":memory:")
    try:
        gold = set(getattr(wide, entry)(con))
        src = DuckDBSource(con)
        assert tier1(src).grade == want_grade

        doc, stats = profile_with_stats(src, llm=None, lineage=None)
        link_source(src, doc, stats, llm=None, use_declared=False, lineage=None)
    finally:
        con.close()

    emitted = {(r["from"]["table"], r["from"]["columns"][0],
                r["to"]["table"], r["to"]["columns"][0])
               for r in doc["semantic_layer"].get("relationships", [])
               if len(r["from"]["columns"]) == 1 and len(r["to"]["columns"]) == 1}
    scored = {(c, col) for c, col, _, _ in gold}
    judged = {e for e in emitted if (e[0], e[1]) in scored}
    tp = len(judged & gold)

    precision = tp / len(judged) if judged else 1.0
    recall = tp / len(gold)
    assert precision >= pfloor, f"{case} precision {precision:.3f}"
    assert recall >= rfloor, f"{case} recall {recall:.3f}"
