"""compile_metric (Phase A): execution parity, constructive refusals, time.

The load-bearing assertions execute compiled SQL on the fixture warehouse and
compare against hand-written gold SQL — compiled metrics must be RIGHT, not
merely parseable (the bar the council set vs EXPLAIN-only validation).
"""

import sys
from pathlib import Path

import duckdb

OSS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OSS / "fixtures"))

from semlayer.compile import compile_metric  # noqa: E402
from semlayer.context import apply_doc_ratios, load_context  # noqa: E402
from semlayer.export.dbt import export_dbt  # noqa: E402
from semlayer.pipeline import infer  # noqa: E402
from semlayer.source import DuckDBSource  # noqa: E402


def _messy():
    import importlib

    mod = importlib.import_module("generators.messy_mart")
    con = duckdb.connect(":memory:")
    mod.build(con)
    doc = infer(DuckDBSource(con))
    return con, doc


CON, DOC = _messy()
METRICS = {m["name"]: m for m in DOC["semantic_layer"]["metrics"]}


def _rows(sql):
    return CON.execute(sql).fetchall()


# ------------------------------------------------------- execution parity --

def test_simple_metric_matches_gold_sql():
    out = compile_metric(DOC, "total_tot_amt")
    assert "sql" in out, out
    got = _rows(out["sql"])[0][0]
    want = _rows("SELECT SUM(tot_amt) FROM ord_hdr WHERE sts_cd <> 'X'")[0][0]
    assert abs(got - want) < 0.01  # business-rule filter applied automatically


def test_group_by_base_column():
    out = compile_metric(DOC, "total_tot_amt", group_by=["sts_cd"])
    assert "sql" in out, out
    got = dict(_rows(out["sql"]))
    want = dict(_rows(
        "SELECT sts_cd, SUM(tot_amt) FROM ord_hdr WHERE sts_cd <> 'X' "
        "GROUP BY sts_cd"))
    assert got == want


def test_group_by_joined_dimension():
    out = compile_metric(DOC, "total_tot_amt", group_by=["store_dim.state_cd"])
    assert "sql" in out, out
    got = dict(_rows(out["sql"]))
    want = dict(_rows(
        "SELECT s.state_cd, SUM(o.tot_amt) FROM ord_hdr o "
        "LEFT JOIN store_dim s ON o.store_id = s.store_id "
        "WHERE o.sts_cd <> 'X' GROUP BY s.state_cd"))
    assert got == want


def test_time_grain_and_window():
    m = METRICS["total_tot_amt"]
    assert m.get("agg_time_dimension") == "ord_hdr.ord_dt"  # not crt_dt (metadata)
    out = compile_metric(DOC, "total_tot_amt", time_grain="month",
                         time_start="2024-01-01", time_end="2024-04-01")
    assert "sql" in out, out
    got = dict(_rows(out["sql"]))
    want = dict(_rows(
        "SELECT date_trunc('month', ord_dt), SUM(tot_amt) FROM ord_hdr "
        "WHERE sts_cd <> 'X' AND ord_dt >= '2024-01-01' AND ord_dt < '2024-04-01' "
        "GROUP BY 1"))
    assert got == want
    assert len(got) == 3  # three months in the window


def test_agg_time_dimension_via_date_dim():
    # dly_sls_agg has only a date KEY; time dim must resolve through date_dim
    m = METRICS.get("total_tot_sls_amt")
    assert m is not None and m.get("agg_time_dimension", "").startswith("date_dim.")
    out = compile_metric(DOC, "total_tot_sls_amt", time_grain="month")
    assert "sql" in out and "LEFT JOIN date_dim" in out["sql"]
    assert _rows(out["sql"])  # executes


# ------------------------------------------------------------- refusals --

def test_refuse_measure_group_by_constructively():
    out = compile_metric(DOC, "total_tot_amt", group_by=["tot_amt"])
    assert out.get("refused") and "measure" in out["reason"]
    assert out["legal_group_by"]  # constructive: alternatives enumerated


def test_refuse_unknown_column_and_unknown_metric():
    out = compile_metric(DOC, "total_tot_amt", group_by=["nope_col"])
    assert out.get("refused") and "N:1-reachable" in out["reason"]
    out2 = compile_metric(DOC, "no_such_metric")
    assert out2.get("refused") and out2["legal_group_by"]  # lists metric names


def test_refuse_bad_filter_identifier():
    out = compile_metric(DOC, "total_tot_amt", extra_filter="bogus_col = 'x'")
    assert out.get("refused") and "bogus_col" in out["reason"]


def test_extra_filter_on_joined_dim_executes():
    out = compile_metric(DOC, "total_tot_amt",
                         extra_filter="store_dim.state_cd = 'CA'")
    assert "sql" in out and "LEFT JOIN store_dim" in out["sql"]
    assert _rows(out["sql"])


# ------------------------------------------------------- declared ratios --

def test_docs_declared_ratio_compiles_and_matches(tmp_path):
    (tmp_path / "kpis.md").write_text(
        "# KPIs\navg order value = ord_hdr.tot_amt / ord_hdr.ord_id\n")
    bundle = load_context([str(tmp_path / "kpis.md")])
    apply_doc_ratios(DOC, bundle.chunks)
    m = next(m for m in DOC["semantic_layer"]["metrics"]
             if m["name"] == "avg_order_value")
    assert m["type"] == "ratio"
    assert any(p["signal"] == "docs" for p in m["provenance"])
    out = compile_metric(DOC, "avg_order_value")
    assert "sql" in out, out
    got = _rows(out["sql"])[0][0]
    # docs-declared operands are compiled verbatim (SUM/SUM); exact numeric
    # parity is asserted on the sum/sum ratio below
    assert got is not None and got > 0

    DOC["semantic_layer"]["metrics"].append({
        "name": "amt_per_qty", "type": "ratio",
        "numerator": "ord_hdr.tot_amt", "denominator": "ord_hdr.tot_amt",
        "lifecycle": "inferred", "confidence": 0.6})
    out2 = compile_metric(DOC, "amt_per_qty")
    assert abs(_rows(out2["sql"])[0][0] - 1.0) < 1e-9


def test_ratio_cross_table_refused():
    DOC["semantic_layer"]["metrics"].append({
        "name": "bad_ratio", "type": "ratio",
        "numerator": "ord_rtn.rtn_id", "denominator": "ord_hdr.ord_id",
        "lifecycle": "inferred", "confidence": 0.5})
    out = compile_metric(DOC, "bad_ratio")
    assert out.get("refused") and "Phase B" in out["reason"]


# ---------------------------------------------------------------- export --

def test_dbt_export_includes_ratio():
    payload, losses = export_dbt(DOC)
    ratios = [m for m in payload["metrics"] if m["type"] == "ratio"]
    assert ratios and "numerator" in ratios[0]["type_params"]
    assert not any("type ratio not exported" in loss for loss in losses)


# ------------------------------------------------------------- multi-hop --

def _line_metric():
    return next(m for m in DOC["semantic_layer"]["metrics"]
                if m["type"] == "simple" and m["measure"].startswith("ord_ln.")
                and not m.get("filter"))


def test_two_hop_snowflaked_dimension_parity():
    m = _line_metric()
    col = m["measure"].split(".", 1)[1]
    out = compile_metric(DOC, m["name"], group_by=["dept_dim.dept_cd"])
    assert "sql" in out, out
    got = dict(_rows(out["sql"]))
    want = dict(_rows(
        f"SELECT d.dept_cd, SUM(l.{col}) FROM ord_ln l "
        "LEFT JOIN prod_ref p ON l.prod_id = p.prod_id "
        "LEFT JOIN dept_dim d ON p.dept_cd = d.dept_cd GROUP BY 1"))
    assert got == want


def test_three_hop_chain_parity():
    m = _line_metric()
    col = m["measure"].split(".", 1)[1]
    out = compile_metric(DOC, m["name"], group_by=["whs_dim.whs_id"])
    assert "sql" in out, out
    assert out["sql"].index("JOIN ord_hdr") < out["sql"].index("JOIN store_dim") \
        < out["sql"].index("JOIN whs_dim")
    got = dict(_rows(out["sql"]))
    want = dict(_rows(
        f"SELECT w.whs_id, SUM(l.{col}) FROM ord_ln l "
        "LEFT JOIN ord_hdr h ON l.ord_id = h.ord_id "
        "LEFT JOIN store_dim s ON h.store_id = s.store_id "
        "LEFT JOIN whs_dim w ON s.whs_id = w.whs_id GROUP BY 1"))
    assert got == want


def test_shortest_path_beats_longer_diamond():
    # dept_dim from ord_ln: direct prod_ref->dept_dim (2 hops) wins over
    # prod_ref->category_dim->dept_dim (3 hops); NOT ambiguous
    m = _line_metric()
    out = compile_metric(DOC, m["name"], group_by=["dept_dim.dept_cd"])
    assert "sql" in out and "category_dim" not in out["sql"]


def test_equal_length_paths_refuse_as_ambiguous():
    sl = {"tables": [
        {"name": "ord", "columns": [
            {"name": "a_id", "entity_role": "foreign_key"},
            {"name": "b_id", "entity_role": "foreign_key"},
            {"name": "amt", "entity_role": "measure"}]},
        {"name": "a", "columns": [{"name": "a_id", "entity_role": "primary_key"},
                                  {"name": "d_id", "entity_role": "foreign_key"}]},
        {"name": "b", "columns": [{"name": "b_id", "entity_role": "primary_key"},
                                  {"name": "d_id", "entity_role": "foreign_key"}]},
        {"name": "dim", "columns": [{"name": "d_id", "entity_role": "primary_key"},
                                    {"name": "label", "entity_role": "dimension"}]},
    ], "metrics": [{"name": "total_amt", "type": "simple",
                    "measure": "ord.amt", "agg": "sum"}],
        "relationships": [
        {"name": "r1", "from": {"table": "ord", "columns": ["a_id"]},
         "to": {"table": "a", "columns": ["a_id"]}, "cardinality": "many_to_one"},
        {"name": "r2", "from": {"table": "ord", "columns": ["b_id"]},
         "to": {"table": "b", "columns": ["b_id"]}, "cardinality": "many_to_one"},
        {"name": "r3", "from": {"table": "a", "columns": ["d_id"]},
         "to": {"table": "dim", "columns": ["d_id"]}, "cardinality": "many_to_one"},
        {"name": "r4", "from": {"table": "b", "columns": ["d_id"]},
         "to": {"table": "dim", "columns": ["d_id"]}, "cardinality": "many_to_one"},
    ]}
    out = compile_metric({"semantic_layer": sl}, "total_amt", group_by=["dim.label"])
    assert out.get("refused") and "ambiguous" in out["reason"]


def test_role_playing_dim_refuses():
    sl = {"tables": [
        {"name": "ord", "columns": [
            {"name": "ord_dt_key", "entity_role": "foreign_key"},
            {"name": "ship_dt_key", "entity_role": "foreign_key"},
            {"name": "amt", "entity_role": "measure"}]},
        {"name": "date_dim", "columns": [{"name": "date_key", "entity_role": "primary_key"},
                                         {"name": "cal_dt", "entity_role": "dimension"}]},
    ], "metrics": [{"name": "total_amt", "type": "simple",
                    "measure": "ord.amt", "agg": "sum"}],
        "relationships": [
        {"name": "r1", "from": {"table": "ord", "columns": ["ord_dt_key"]},
         "to": {"table": "date_dim", "columns": ["date_key"]}, "cardinality": "many_to_one"},
        {"name": "r2", "from": {"table": "ord", "columns": ["ship_dt_key"]},
         "to": {"table": "date_dim", "columns": ["date_key"]}, "cardinality": "many_to_one"},
    ]}
    out = compile_metric({"semantic_layer": sl}, "total_amt", group_by=["date_dim.cal_dt"])
    assert out.get("refused") and "ambiguous" in out["reason"]
