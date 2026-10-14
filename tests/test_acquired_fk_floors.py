"""FK precision/recall floors on the two ACQUIRED fixtures.

Why this file exists: `test_link.py`'s FK_FLOORS covers five fixtures and all
five are ones we generated. The two datasets with vendor-declared gold and
real-world naming -- `dbt_synthea` (snake_case OMOP, 116 tables) and
`adventureworks_dw` (PascalCase star, 29 tables) -- had no precision floor at
all, so 32 wrong join targets could sit in shipped output on dbt_synthea
without any gate noticing (prd/link-blocking-design.md §3c).

**Scoring a PARTIAL gold honestly.** Neither vendor declares every true
foreign key, so an edge we infer that the gold does not list is not
automatically wrong -- it may be a real key nobody wrote a test for. Counting
it as a false positive would measure the gold's completeness, not our
precision. So precision is scored **only over child columns the gold mentions**:
for those columns the vendor HAS stated the intended parent, so a different
parent is a genuine error. Edges on columns the gold says nothing about are
excluded from both numerator and denominator.

That makes these numbers comparable across runs and honest about what they
cover; it does not make them a complete precision measure, and they should not
be quoted as one.
"""

from __future__ import annotations

import json
import pathlib
import sys

import duckdb
import pytest

OSS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OSS / "src"))

from semlayer.lineage import load_dbt_manifest  # noqa: E402
from semlayer.link import link_source  # noqa: E402
from semlayer.profile.run import profile_with_stats  # noqa: E402
from semlayer.source import DuckDBSource  # noqa: E402

EXT = OSS / "fixtures" / "external" / "data"

# (fixture, use the ETL manifest) -> (precision floor, recall floor). Measured
# 2026-10-13, deterministic (llm=None) so the numbers do not move with a
# cassette. Recall is of INFERABLE edges: dbt_synthea declares 232 but 84 are
# over empty tables or all-NULL columns, which no method can discover
# (docs/calibration.md).
#
# Both configurations are gated for dbt_synthea because they are different
# products: a user with a dbt project gets the manifest path, a user pointed at
# a bare warehouse gets statistics alone. The same-layer rule in
# _best_parent_per_child lives only on the manifest path, so without both rows
# a regression in either is invisible.
# Measured: adventureworks P=1.000 R=0.975; dbt_synthea without a manifest
# P=0.504 R=0.432, with one P=0.908 R=0.865. The gap between those two rows is
# the ETL-reading path earning its keep, and an ablation attributes +18.3 points
# of recall and +14.9 of precision to the same-layer rule specifically.
#
# The no-manifest row is floored LOW on purpose: half the parents it asserts on
# gold-mentioned columns are wrong, and pretending otherwise by floor-setting
# would hide the open defect that prd/link-blocking-design.md §4.3 tracks. It
# is a regression guard, not a statement that 0.5 is acceptable.
FLOORS = {
    ("dbt_synthea", False): (0.45, 0.40),
    ("dbt_synthea", True): (0.85, 0.80),
    ("adventureworks_dw", False): (0.95, 0.95),
}


def _declared_dbt(path: pathlib.Path) -> set[tuple[str, str, str, str]]:
    """Single-column edges from dbt `relationships` tests."""
    man = json.loads(path.read_text())
    out = set()
    for node in man.get("nodes", {}).values():
        if node.get("resource_type") != "test":
            continue
        meta = node.get("test_metadata") or {}
        if not str(meta.get("name", "")).startswith("relationships"):
            continue
        kw = meta.get("kwargs") or {}
        ccol = str(kw.get("column_name") or node.get("column_name") or "")
        field = str(kw.get("field") or "")
        to = str(kw.get("to") or "")
        parent = to.split("'")[1] if "'" in to else ""
        child = ""
        for dep in (node.get("depends_on", {}).get("nodes") or []):
            if dep.startswith("model.") and dep.split(".")[-1] != parent:
                child = dep.split(".")[-1]
                break
        if ccol and field and parent and child:
            out.add((child, ccol, parent, field))
    return out


def _declared_json(path: pathlib.Path) -> set[tuple[str, str, str, str]]:
    """Single-column edges from a vendor loader's FK constraints."""
    d = json.loads(path.read_text())
    return {(e["from_table"], e["from_columns"][0], e["to_table"], e["to_columns"][0])
            for e in d["foreign_keys"]
            if len(e["from_columns"]) == 1 and len(e["to_columns"]) == 1}


def _gold(fixture: str) -> set[tuple[str, str, str, str]]:
    man = EXT / fixture / "manifest.json"
    if man.exists():
        return _declared_dbt(man)
    return _declared_json(EXT / fixture / "declared_keys.json")


def _infer(fixture: str, use_manifest: bool):
    db = EXT / fixture / f"{fixture}.duckdb"
    man = EXT / fixture / "manifest.json"
    lineage = load_dbt_manifest(man) if (use_manifest and man.exists()) else None
    con = duckdb.connect(str(db), read_only=True)
    try:
        src = DuckDBSource(con)
        doc, stats = profile_with_stats(src, llm=None, lineage=lineage)
        link_source(src, doc, stats, llm=None, use_declared=False, lineage=lineage)
    finally:
        con.close()
    return doc, stats


@pytest.mark.parametrize(("fixture", "use_manifest"), sorted(FLOORS))
def test_acquired_fk_floor(fixture, use_manifest):
    if not (EXT / fixture / f"{fixture}.duckdb").exists():
        pytest.skip(f"{fixture} not fetched (fixtures/external/fetch_{fixture}.py)")

    gold = _gold(fixture)
    doc, stats = _infer(fixture, use_manifest)
    sl = doc["semantic_layer"]

    emitted = {(r["from"]["table"], r["from"]["columns"][0],
                r["to"]["table"], r["to"]["columns"][0])
               for r in sl.get("relationships", [])
               if len(r["from"]["columns"]) == 1 and len(r["to"]["columns"]) == 1}
    # Same-table gold edges are satisfied by recursive-hierarchy detection,
    # exactly as test_link.py credits them: the edge is found, it is just
    # recorded as a hierarchy rather than a relationship.
    emitted |= {(h["dimension_table"], h["recursive"]["parent_column"],
                 h["dimension_table"], h["recursive"]["child_column"])
                for h in sl.get("hierarchies", [])
                if h.get("kind") == "recursive"}

    # inferable: the child table has rows and the child column has values
    def inferable(e):
        ts = stats.get(e[0])
        cs = ts.columns.get(e[1]) if ts else None
        return bool(ts and cs and ts.row_count > 0 and cs.n_distinct > 0)

    truth = {e for e in gold if inferable(e)}
    scored_cols = {(e[0], e[1]) for e in truth}          # see module docstring
    judged = {e for e in emitted if (e[0], e[1]) in scored_cols}

    tp = len(judged & truth)
    precision = tp / len(judged) if judged else 1.0
    recall = tp / len(truth) if truth else 1.0
    pf, rf = FLOORS[(fixture, use_manifest)]
    print(f"\n[{fixture}:manifest={use_manifest}] P={precision:.3f} R={recall:.3f} "
          f"judged={len(judged)} truth={len(truth)} emitted={len(emitted)}")
    assert precision >= pf, (
        f"FK precision regression on {fixture}: {precision:.3f} < {pf}; "
        f"wrong parents e.g. {sorted(judged - truth)[:3]}")
    assert recall >= rf, (
        f"FK recall regression on {fixture}: {recall:.3f} < {rf}; "
        f"missed e.g. {sorted(truth - judged)[:3]}")
