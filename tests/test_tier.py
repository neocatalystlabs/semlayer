# SPDX-License-Identifier: FSL-1.1-ALv2
"""semlayer.tier: the tier of a table from its name and columns.

Three kinds of check. Named cases for every precedence decision the module
documents. A gold-scored run on a 300-table scale_N warehouse built here
(8% core, 22% staging, 45% raw, 25% scratch), where raw and staging must be
perfect and NO core or staging table may be demoted to informational. And
every built fixture, on which nothing may come out raw or scratch, because
that is the condition under which existing behaviour is untouched.
"""

from __future__ import annotations

import collections
import glob
import sys
from pathlib import Path

import duckdb
import pytest

OSS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OSS / "src"))
sys.path.insert(0, str(OSS / "fixtures"))

from semlayer.tier import TIERS, infer_tier, is_informational  # noqa: E402

CASES = [
    # name, columns, expected tier, informational?
    ("raw_crm__customer", [], "raw", True),
    ("landing_orders_20250101", [], "raw", True),
    ("orders_20250101", [], "raw", True),                 # date shard alone
    ("cdc_erp_customer", [], "raw", True),
    ("hist_product", [], "raw", True),
    ("tmp_orders_3", [], "scratch", True),
    ("bak_customer_20250301", [], "scratch", True),       # backup beats date shard
    ("dim_customer_bak", [], "scratch", True),            # backup of a dim is scratch
    ("renewal_draft_11", [], "scratch", True),            # counter stripped, suffix seen
    ("junk_5", [], "scratch", True),                      # whole-name scratch word
    ("customer_copy", [], "scratch", True),
    ("customer_old", [], "scratch", True),
    ("fct_payments_click_hist", [], "core", False),       # core prefix beats raw suffix
    ("int__payments_click_hist", [], "staging", False),   # staging prefix beats raw suffix
    ("stg_customers_raw", [], "staging", False),
    ("stg_crm__customer", [], "staging", False),
    ("int__customer", [], "staging", False),
    ("dim_customer", [], "core", False),
    ("fct_orders_line", [], "core", False),
    ("agg_sales_monthly_by_region", [], "core", False),
    ("cust_snapshot", [], "core", False),                 # SCD2 table, not a raw shard
    ("cdm_source", [], "core", False),                    # OMOP; `_source` is not raw
    ("source_to_concept_map", [], "core", False),         # OMOP; `source_` is not raw
    ("etl_log", [], "core", False),                       # operational, already typed so
    ("digest_test", [], "core", False),                   # `_test` suffix is not scratch
    ("customers", [], "core", False),
    ("Customer", ["CustomerKey", "Name"], "core", False),
    ("mystery", ["id", "etl_batch_id", "record_hash", "payload"], "raw", False),  # weak raw
    ("mystery2", ["id", "etl_batch_id"], "core", False),  # one ETL column is not evidence
    ("raw_events", ["id", "etl_batch_id", "record_hash"], "raw", True),
]


@pytest.mark.parametrize("name,cols,tier,informational", CASES)
def test_named_cases(name, cols, tier, informational):
    claim = infer_tier(name, cols)
    assert claim.tier == tier, (name, claim)
    assert is_informational(claim) is informational, (name, claim)
    assert claim.signals and 0.5 <= claim.confidence <= 0.95
    assert claim.provenance()[0]["detail"] == claim.signals[0]


def test_core_default_is_low_confidence_and_explained():
    c = infer_tier("orders")
    assert (c.tier, c.confidence) == ("core", 0.5)
    assert "no tier convention" in c.signals[0]


def _cols_by_table(db: str) -> dict[str, list[str]]:
    con = duckdb.connect(db, read_only=True)
    try:
        rows = con.execute(
            "select table_name, column_name from information_schema.columns "
            "where table_schema not in ('information_schema','pg_catalog')").fetchall()
    finally:
        con.close()
    d: dict[str, list[str]] = collections.defaultdict(list)
    for t, c in rows:
        d[t].append(c)
    return d


def test_scored_against_gold_tiers(tmp_path):
    """300-table scale_N warehouse: raw and staging perfect, nothing real demoted."""
    from generators import scale

    db = tmp_path / "scale_300.duckdb"
    con = duckdb.connect(str(db))
    try:
        meta = scale.build(con, 300)
    finally:
        con.close()
    gold = meta["tables"]
    cbt = _cols_by_table(str(db))
    conf: collections.Counter = collections.Counter()
    demoted = []
    for t, g in gold.items():
        p = infer_tier(t, cbt[t])
        conf[(g["tier"], p.tier)] += 1
        if is_informational(p) and g["tier"] in ("core", "staging"):
            demoted.append(t)
    assert not demoted, demoted

    def prec_rec(tier):
        tp = conf[(tier, tier)]
        fp = sum(conf[(x, tier)] for x in TIERS if x != tier)
        fn = sum(conf[(tier, x)] for x in TIERS if x != tier)
        return tp / max(1, tp + fp), tp / max(1, tp + fn)

    assert prec_rec("raw") == (1.0, 1.0), conf
    assert prec_rec("staging") == (1.0, 1.0), conf
    p, r = prec_rec("scratch")
    assert p == 1.0 and r >= 0.95, conf
    assert prec_rec("core")[1] == 1.0, conf          # every core table stays core


@pytest.mark.parametrize("db", sorted(glob.glob(str(OSS / "fixtures" / "build" / "*.duckdb"))))
def test_built_fixtures_are_never_demoted(db):
    """On every existing fixture the tier is core or staging, so nothing changes."""
    flagged = [(t, infer_tier(t, cols).tier) for t, cols in _cols_by_table(db).items()
               if infer_tier(t, cols).tier in ("raw", "scratch")]
    assert not flagged, flagged


def _assert_listed_not_profiled(by_name: dict, inv: set) -> None:
    """Inventory-only columns carry no cardinality (never scanned) and say so; modelled ones do."""
    for n in inv:
        for c in by_name[n]["columns"]:
            assert "cardinality" not in c, (n, c["name"])
            assert c["provenance"][0]["detail"].startswith("inventory-only tier"), (n, c["name"])
    for n, tbl in by_name.items():
        if n not in inv:
            assert all("cardinality" in c for c in tbl["columns"]), n


def test_inventory_only_tables_are_listed_not_modelled(tmp_path):
    """End to end on a small scale_N warehouse, no LLM.

    Every table is in the layer. Raw and scratch tables carry their shape,
    tier and `modeling: inventory_only`, and nothing else: no relationship
    touches them, they get no table type, description or metric, and the
    review queue carries one line per tier for them rather than one per
    column. The document validates against the spec.
    """
    import duckdb as _duckdb
    from generators import scale

    from semlayer.pipeline import InferOptions, infer_with_report
    from semlayer.review import collect
    from semlayer.source import DuckDBSource
    from semlayer.validate import validate_document

    db = tmp_path / "scale_150.duckdb"
    con = _duckdb.connect(str(db))
    try:
        meta = scale.build(con, 150)
    finally:
        con.close()
    con = _duckdb.connect(str(db), read_only=True)
    try:
        doc, _report = infer_with_report(DuckDBSource(con), llm=None,
                                         options=InferOptions(query_workers=2))
    finally:
        con.close()
    assert validate_document(doc).ok, validate_document(doc).errors
    sl = doc["semantic_layer"]
    assert sl["spec_version"] == "0.6.0"
    by_name = {t["name"]: t for t in sl["tables"]}
    folded = {s["name"].rsplit(".", 1)[-1] for s in sl.get("coverage", {}).get("skipped", [])}
    # every generated table is either in the layer or recorded as a folded copy
    assert set(meta["tables"]) <= set(by_name) | folded
    inv = {n for n, t in by_name.items() if t.get("modeling") == "inventory_only"}
    assert inv, "the raw and scratch tiers should produce inventory-only tables"
    for n in inv:
        t = by_name[n]
        assert t["tier"]["value"] in ("raw", "scratch") and t["tier"]["confidence"] >= 0.8
        assert t["tier"]["provenance"], n
        assert t["columns"] and all("sql_type" in c for c in t["columns"])
        assert t.get("table_type") == "unknown" and "description" not in t, n
    for n, t in by_name.items():
        if n not in inv:
            assert t.get("modeling") == "full" and t["tier"]["value"] in ("core", "staging"), n
    # gold tiers agree with the engine's: nothing core or staging was set aside
    for n in inv:
        assert meta["tables"][n]["tier"] in ("raw", "scratch"), n
    for r in sl.get("relationships", []):
        assert r["from"]["table"] not in inv and r["to"]["table"] not in inv, r
    for m in sl.get("metrics", []):
        assert (m.get("measure") or m.get("numerator") or "").split(".")[0] not in inv, m
    items = collect(doc)
    _assert_listed_not_profiled(by_name, inv)
    per_column_on_inv = [i for i in items if i.table in inv and i.kind != "inventory_only"]
    assert not per_column_on_inv, per_column_on_inv[:3]
    rollups = [i for i in items if i.kind == "inventory_only"]
    assert {i.ref for i in rollups} <= {"tier.raw", "tier.scratch"} and rollups
    assert sum(int(i.claim.split()[0]) for i in rollups) == len(inv)
