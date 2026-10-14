"""Enrich stage evals: metric coverage, business-rule discovery, deprecation,
aggregates, and the dbt exporter round-trip (M4)."""

import sys
from pathlib import Path

import duckdb
import pytest
import yaml

OSS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OSS / "fixtures"))

try:
    from dotenv import load_dotenv
    load_dotenv(OSS.parent / ".env")
except ImportError:
    pass

from semlayer.describe import describe_source  # noqa: E402
from semlayer.enrich import enrich_source  # noqa: E402
from semlayer.export.dbt import export_dbt  # noqa: E402
from semlayer.link import link_source  # noqa: E402
from semlayer.llm.provider import AnthropicProvider, CassetteMiss  # noqa: E402
from semlayer.profile.run import profile_with_stats  # noqa: E402
from semlayer.source import DuckDBSource  # noqa: E402
from semlayer.validate import validate_document  # noqa: E402


@pytest.fixture(scope="module")
def enriched():
    import importlib
    mod = importlib.import_module("generators.messy_mart")
    con = duckdb.connect(":memory:")
    mod.build(con)
    llm = AnthropicProvider()
    try:
        doc, stats = profile_with_stats(DuckDBSource(con), llm=llm)
        link_source(DuckDBSource(con), doc, stats, llm=llm, use_declared=False)
        describe_source(doc, stats, llm)
        enrich_source(DuckDBSource(con), doc, stats)
    except CassetteMiss as e:
        pytest.skip(str(e))
    finally:
        con.close()
    doc.pop("_link_audit", None)
    return doc


def test_enriched_doc_is_spec_conformant(enriched):
    r = validate_document(enriched)
    assert r.ok, "\n".join(r.errors[:5])


def test_metric_coverage_vs_gold(enriched):
    """All measurable gold metrics covered (measured 4/4 incl. the filtered
    completed-revenue variant via dictionary-joined decodes)."""
    gold = yaml.safe_load((OSS / "fixtures" / "golds" / "messy_mart.yaml").read_text())
    gm = {(m.get("measure"), m.get("agg", "sum"))
          for m in gold["semantic_layer"].get("metrics", []) if m.get("measure")}
    dm = {(m.get("measure"), m.get("agg", "sum"))
          for m in enriched["semantic_layer"].get("metrics", [])}
    covered = len(gm & dm)
    print(f"\n[enrich] metric coverage {covered}/{len(gm)}")
    assert covered == len(gm)
    # the completed-status filtered variant must exist (gold: completed_order_revenue)
    assert any(m.get("filter") and "= 'C'" in m["filter"]
               for m in enriched["semantic_layer"]["metrics"])


def test_business_rule_discovered(enriched):
    """The reconciliation verifier must rediscover the cancelled-exclusion rule,
    scoped to MONETARY metrics (not event counts) + a measure-scoped required filter."""
    ord_hdr = next(t for t in enriched["semantic_layer"]["tables"] if t["name"] == "ord_hdr")
    rfs = ord_hdr.get("knowledge", {}).get("required_filters", [])
    assert any(r["expr"] == "sts_cd <> 'X'" and r.get("scope") == "measures" for r in rfs), rfs
    metrics = enriched["semantic_layer"]["metrics"]
    rev = next(m for m in metrics if m["measure"] == "ord_hdr.tot_amt" and m["agg"] == "sum"
               and "completed" not in m["name"])
    assert rev.get("filter") == "sts_cd <> 'X'"
    counts = [m for m in metrics if m.get("agg") == "count" and m["measure"].startswith("ord_hdr.")]
    assert all(not m.get("filter") for m in counts), "counts must NOT inherit the revenue rule"


def test_aggregates_detected_and_advisory(enriched):
    aggs = enriched["semantic_layer"].get("aggregate_tables", [])
    by_table = {a["table"]: a for a in aggs}
    assert {"dly_sls_agg", "mth_cust_agg"} <= set(by_table)
    for a in aggs:
        assert a["mapping_source"] == "heuristic"
        assert a["routing"]["status"] == "advisory", "heuristic mappings must never verify routing"


def test_deprecation_detected(enriched):
    dep = {t["name"]: t.get("deprecation", {}).get("replacement")
           for t in enriched["semantic_layer"]["tables"] if t.get("lifecycle") == "deprecated"}
    assert dep.get("ord_hdr_legacy") == "ord_hdr"
    assert dep.get("cust_mstr_legacy") == "cust_mstr"


def test_enum_decodes_upgraded_from_dictionary(enriched):
    ord_hdr = next(t for t in enriched["semantic_layer"]["tables"] if t["name"] == "ord_hdr")
    sts = next(c for c in ord_hdr["columns"] if c["name"] == "sts_cd")
    assert sts.get("enum_values"), "sts_cd should carry decodes"
    assert all(e["decode_source"] == "dictionary_join" for e in sts["enum_values"])


def test_routing_avoids_deprecated(enriched):
    routing = enriched["semantic_layer"].get("repo_knowledge", {}).get("routing", [])
    ord_route = next(r for r in routing if "ord hdr" in r["intent"])
    assert any(a["table"] == "ord_hdr_legacy" for a in ord_route.get("avoid", []))


def test_routing_lists_every_reconciled_aggregate(enriched):
    """A fact with two reconciled aggregates must offer both, not the last one seen."""
    sl = enriched["semantic_layer"]
    routing = sl.get("repo_knowledge", {}).get("routing", [])
    by_table = {t["name"]: t for t in sl["tables"]}
    for agg in sl.get("aggregate_tables", []):
        base = agg["aggregates"]
        if by_table[base].get("lifecycle") == "deprecated":
            continue
        route = next((r for r in routing if r["use"] and r["use"][0] == base), None)
        assert route is not None, f"no routing entry for fact {base}"
        assert agg["table"] in route["use"], (
            f"{agg['table']} reconciles with {base} but is missing from its routing"
        )


def test_dbt_export_round_trip(enriched):
    exported, losses = export_dbt(enriched)
    # YAML-serializable and structurally sane
    text = yaml.safe_dump(exported, sort_keys=False)
    back = yaml.safe_load(text)
    sms = {m["name"]: m for m in back["semantic_models"]}
    active = [t for t in enriched["semantic_layer"]["tables"]
              if t.get("lifecycle") not in ("deprecated", "orphaned")]
    assert set(sms) == {t["name"] for t in active}
    # measures survive with mapped agg names
    ord_hdr = sms["ord_hdr"]
    assert any(m["name"] == "tot_amt" and m["agg"] == "sum" for m in ord_hdr["measures"])
    assert any(e["type"] == "primary" for e in ord_hdr["entities"])
    # metrics exported with filters intact
    mnames = {m["name"]: m for m in back["metrics"]}
    assert "total_tot_amt_completed" in mnames
    assert "sts_cd" in mnames["total_tot_amt_completed"]["filter"]
    # losses are REPORTED, never silent
    assert any("hierarchies" in l or "repo knowledge" in l for l in losses)
    assert any("confidence" in l for l in losses)
    # deprecated tables excluded AND reported
    assert any("ord_hdr_legacy" in l for l in losses)


def test_parent_rule_propagates_to_child_fact(enriched):
    """ord_ln.line_amt sums to ord_hdr.tot_amt per order, so the cancelled-exclusion rule is
    inherited by ord_ln as a REQUIRED semi-join filter with per-key evidence."""
    ord_ln = next(t for t in enriched["semantic_layer"]["tables"] if t["name"] == "ord_ln")
    rfs = ord_ln.get("knowledge", {}).get("required_filters", [])
    inh = [f for f in rfs if f["expr"].startswith("ord_ln.ord_id IN (SELECT ord_id FROM ord_hdr")]
    assert inh and inh[0]["enforcement"] == "required" and inh[0]["scope"] == "measures"
    assert "reconciles" in inh[0]["reason"]
    line_rev = next(m for m in enriched["semantic_layer"]["metrics"] if m["name"] == "total_line_amt")
    assert line_rev.get("filter") == inh[0]["expr"]


def test_ratio_metric_synthesised_with_rule(enriched):
    """avg_tot_amt_per_order = SUM(tot_amt)/COUNT(ord_id) on ord_hdr, carrying the discovered rule."""
    from semlayer.compile import compile_metric
    m = next(m for m in enriched["semantic_layer"]["metrics"] if m["name"] == "avg_tot_amt_per_order")
    assert m["type"] == "ratio" and m["numerator"] == "ord_hdr.tot_amt"
    assert m["denominator"] == "ord_hdr.ord_id" and m.get("filter") == "sts_cd <> 'X'"
    sql = compile_metric(enriched, "avg_tot_amt_per_order")["sql"]
    assert "COUNT(ord_hdr.ord_id)" in sql and "SUM(ord_hdr.tot_amt)" in sql and "sts_cd <> 'X'" in sql


def test_every_metric_states_its_grain(enriched):
    """Metrics copy their base table's grain; reading it too early made it ''."""
    sl = enriched["semantic_layer"]
    grains = {t["name"]: t.get("grain") for t in sl["tables"]}
    for m in sl["metrics"]:
        base = (m.get("measure") or m.get("numerator") or ".").split(".", 1)[0]
        assert m.get("grain"), f"{m['name']} has no grain"
        assert m["grain"] == grains[base], m["name"]


def test_a_reconciled_aggregate_states_its_grain(enriched):
    """No primary key, but reconciliation measured the group columns."""
    sl = enriched["semantic_layer"]
    tables = {t["name"]: t for t in sl["tables"]}
    for a in sl.get("aggregate_tables", []):
        grain = tables[a["table"]].get("grain")
        assert grain, f"{a['table']} reconciled but states no grain"
        for col in a["grain"]:
            assert col in grain, (a["table"], col)


def test_date_dimension_gets_a_time_hierarchy(enriched):
    """The verified calendar attributes are assembled, not left as loose tags."""
    sl = enriched["semantic_layer"]
    hs = [h for h in sl.get("hierarchies", []) if h.get("kind") == "time"]
    assert hs, "a verified date dimension must declare a time hierarchy"
    h = next(x for x in hs if x["name"].endswith("_calendar"))
    levels = {lv["name"]: lv["column"] for lv in h["levels"]}
    assert ["year", "quarter", "month", "day"] == [lv["name"] for lv in h["levels"]], \
        "levels must run most-general to most-granular"
    t = next(x for x in sl["tables"] if x["name"] == h["dimension_table"])
    tagged = {c["name"]: c.get("time_attribute") for c in t["columns"]}
    for level, col in levels.items():
        if level == "day":
            continue
        assert tagged[col] == f"calendar_{level}", (level, col, tagged[col])


def test_dimension_business_key_is_a_natural_key_not_a_foreign_key():
    """A dimension's business key must not be typed as a foreign key.

    The star-schema pair: a surrogate primary key plus the source system's own
    identifier beside it. Nothing emitted `natural_key` before -- the typing
    rule is "unique -> key, otherwise foreign key", so a business key came out
    `unique_key` when the table was small enough for it to be unique and
    `foreign_key` when it was not, which made the answer depend on the row
    count. Gold asserts 31 of these across the corpus, 29 on plain dimensions.

    A foreign key POINTS somewhere, so the discriminator is that Link found no
    relationship for the column.
    """
    from semlayer.enrich.run import _detect_natural_keys

    sl = {
        "tables": [
            {"name": "store_dim", "table_type": "dimension",
             "primary_key": ["store_sk"],
             "columns": [
                 {"name": "store_sk", "semantic_type": "identifier",
                  "entity_role": "primary_key"},
                 {"name": "store_id", "semantic_type": "identifier",
                  "entity_role": "unique_key"},
                 {"name": "region_sk", "semantic_type": "identifier",
                  "entity_role": "foreign_key"},
             ]},
            {"name": "stg_raw", "table_type": "staging",
             "primary_key": ["row_sk"],
             "columns": [
                 {"name": "row_sk", "semantic_type": "identifier",
                  "entity_role": "primary_key"},
                 {"name": "raw_ord_id", "semantic_type": "identifier",
                  "entity_role": "foreign_key"},
             ]},
        ],
        "relationships": [
            {"from": {"table": "store_dim", "columns": ["region_sk"]},
             "to": {"table": "region_dim", "columns": ["region_sk"]}},
        ],
    }
    _detect_natural_keys(sl)
    cols = {c["name"]: c["entity_role"] for c in sl["tables"][0]["columns"]}
    assert cols["store_id"] == "natural_key"
    assert cols["region_sk"] == "foreign_key"   # it points somewhere
    assert cols["store_sk"] == "primary_key"    # untouched
    # staging tables are ETL bookkeeping, not dimensional models
    assert sl["tables"][1]["columns"][1]["entity_role"] == "foreign_key"


def test_a_summary_table_is_found_by_shape_not_by_name():
    """Aggregate detection must not require our own naming convention.

    The gate was `_AGG_NAME_RE.search(table_name)`, so rule recovery -- the
    capability we describe as central -- only ran on summaries named the way we
    named ours (`dly_sls_agg`, `mth_cust_agg`). CMS calls its per-provider
    totals `Prov`; a real warehouse might call a rollup `sales_monthly`. Every
    one was skipped in silence.
    """
    from semlayer.enrich.run import _AGG_NAME_RE, _is_coarser

    class _TS:
        def __init__(self, n):
            self.row_count = n

    # the shape test that replaced the name test
    stats = {"sales_monthly": _TS(120), "ord_hdr": _TS(8000)}
    assert _is_coarser(stats, "sales_monthly", "ord_hdr")
    assert not _is_coarser(stats, "ord_hdr", "sales_monthly")
    assert not _is_coarser(stats, "missing", "ord_hdr")
    # a name no vocabulary of ours would match
    assert not _AGG_NAME_RE.search("sales_monthly")


def test_reconciliation_skips_measures_sql_cannot_sum():
    """A text column named like money is still a text column.

    semantic_type is inferred partly from the NAME, so CMS's VARCHAR
    `Tot_Mdcr_Pymt_Amt` was typed monetary_value; reconciliation emitted SUM()
    over it and DuckDB raised, which crashed `infer` outright. Real warehouses
    store numbers as text constantly -- CSV loads, staging tables, anything
    built by hand -- and a warehouse we cannot reconcile must still profile.
    """
    from semlayer.enrich.run import _summable

    assert _summable({"sql_type": "DECIMAL(19,4)"})
    assert _summable({"sql_type": "BIGINT"})
    assert _summable({"sql_type": "DOUBLE"})
    assert not _summable({"sql_type": "VARCHAR"})
    assert not _summable({"sql_type": "DATE"})
    assert not _summable({})


def test_a_rollup_claim_needs_grain_coverage_not_matching_column_names():
    """"Looks like a rollup" must mean one row per distinct grain in the detail.

    Counting mapped column NAMES is not enough: any two tables sharing two
    column names pass that, which produced seven notes on AdventureWorksDW
    claiming `DimEmployee` was a rollup of `ProspectiveBuyer` and
    `FactInternetSales` a rollup of `FactResellerSales`. Confidently wrong
    output is worse than silence.

    One-directional on purpose: a rollup MAY carry rows for grain combinations
    with no detail rows -- CMS has 13 of 400 providers with every service line
    suppressed -- but may not have fewer rows than the detail's distinct
    combinations, since then it is not a rollup of that table.
    """
    from semlayer.enrich.run import _GRAIN_SLACK, _grain_covers

    class _TS:
        def __init__(self, n):
            self.row_count = n
            self.table = type("M", (), {"schema": "main", "name": "f"})()

    class _Src:
        def __init__(self, distinct):
            self.distinct = distinct

        def query(self, _sql):
            return [(self.distinct,)]

    def covers(agg_rows, fact_distinct, mapped=2):
        stats = {"a": _TS(agg_rows), "f": _TS(999)}
        gmap = {f"c{i}": f"c{i}" for i in range(mapped)}
        return _grain_covers(_Src(fact_distinct), lambda s, n: f'"{s}"."{n}"',
                             stats, {"name": "a"}, {"name": "f"}, gmap)

    assert covers(400, 400)          # exact: a clean rollup
    assert covers(400, 387)          # CMS: suppressed groups still covered
    assert not covers(400, 300)      # 25% short: not a rollup of this table
    assert not covers(387, 400)      # fewer rows than distinct grain: impossible
    assert not covers(400, 400, mapped=1)   # one mapped column is not a grain
    assert _GRAIN_SLACK < 0.1        # slack stays tight


def test_level_hierarchies_need_containment_not_just_a_dependency():
    """`kind: level` was the one hierarchy kind nothing produced.

    Link makes `recursive`, Enrich makes `time`; gold asserted 8 level
    hierarchies and the engine emitted zero, so the gap was scoreable and
    unscored.

    The guards exist because the first version emitted junk. A key and its
    label (`cust_id > cust_nm`), one level in two encodings (`mth_nbr >
    mth_nm`, whose FD holds in BOTH directions and so proves nothing), and an
    email as the finest level all formed "hierarchies".
    """
    from semlayer.enrich.run import (
        _GEO_ORDER,
        _MIN_LEVEL_GROWTH,
        _NEVER_LEVEL_TYPES,
        _NEVER_LEVELS,
        _level_candidates,
    )

    class _C:
        def __init__(self, n, rows, uniq=False):
            self.n_distinct, self.row_count, self.is_unique = n, rows, uniq
            self.cardinality_ratio = n / rows

    class _TS:
        def __init__(self, cols):
            self.columns = cols
            self.row_count = 400

    cols = {
        "state_cd": _C(12, 400), "city_nm": _C(12, 400),
        "cust_id": _C(400, 400, uniq=True),      # unique: the grain, not a level
        "cust_email": _C(380, 400),              # PII identifies, never groups
        "ext_sku": _C(399, 400),                 # untyped and near-unique
        "postal_cd": _C(390, 400),               # near-unique AND a real geo level
    }
    t = {"name": "d", "columns": [
        {"name": "state_cd", "semantic_type": "geo_region", "entity_role": "dimension"},
        {"name": "city_nm", "semantic_type": "geo_city", "entity_role": "dimension"},
        {"name": "cust_id", "semantic_type": "identifier", "entity_role": "primary_key"},
        {"name": "cust_email", "semantic_type": "pii_email", "entity_role": "dimension"},
        {"name": "ext_sku", "semantic_type": "unknown", "entity_role": "dimension"},
        {"name": "postal_cd", "semantic_type": "geo_postal", "entity_role": "dimension"},
    ]}
    kept = {c["name"] for c in _level_candidates(t, _TS(cols))}
    assert kept == {"state_cd", "city_nm", "postal_cd"}, kept

    assert "primary_key" in _NEVER_LEVELS and "natural_key" in _NEVER_LEVELS
    assert "flag" in _NEVER_LEVEL_TYPES and "name" in _NEVER_LEVEL_TYPES
    # ordering comes from the family, since messy_mart has 12 states and 12
    # cities and cardinality is therefore silent
    assert _GEO_ORDER["geo_country"] < _GEO_ORDER["geo_region"] < _GEO_ORDER["geo_city"]
    assert _MIN_LEVEL_GROWTH > 1.0
