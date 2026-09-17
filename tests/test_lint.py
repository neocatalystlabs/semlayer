"""Semantic SQL linter: deterministic checks of agent-written SQL against the layer.

Each case is a query the benchmark answerer actually produced (or its fix),
so the linter is tested on the failure modes that cost accuracy, not on
synthetic examples.
"""

import importlib
import sys
from pathlib import Path

import duckdb
import pytest

OSS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OSS / "fixtures"))

try:
    from dotenv import load_dotenv
    load_dotenv(OSS.parent / ".env")
except ImportError:
    pass

from semlayer import mcp_server  # noqa: E402
from semlayer.lint import lint_sql, render_findings  # noqa: E402
from semlayer.llm.provider import AnthropicProvider, CassetteMiss  # noqa: E402
from semlayer.pipeline import infer  # noqa: E402
from semlayer.source import DuckDBSource  # noqa: E402


@pytest.fixture(scope="module")
def doc():
    mod = importlib.import_module("generators.messy_mart")
    con = duckdb.connect(":memory:")
    mod.build(con)
    try:
        d = infer(DuckDBSource(con), llm=AnthropicProvider())
    except CassetteMiss as e:
        pytest.skip(str(e))
    finally:
        con.close()
    return d


def _rules(result: dict) -> set[str]:
    return {f["rule"] for f in result["findings"]}


# ------------------------------------------------------------ clean queries

@pytest.mark.parametrize("sql", [
    "SELECT round(sum(tot_amt), 2) FROM ord_hdr WHERE sts_cd != 'X'",
    "SELECT curr_cd, COUNT(ord_id) FROM ord_hdr GROUP BY curr_cd",  # counts need no filter
    "SELECT date_trunc('month', ord_dt) AS m, sum(tot_amt) FROM ord_hdr "
    "WHERE sts_cd != 'X' GROUP BY m ORDER BY m",  # select alias in GROUP BY
    "SELECT cm.seg_cd, round(sum(o.tot_amt), 2) FROM ord_hdr o JOIN cust_mstr cm "
    "ON o.cust_id = cm.cust_id AND o.ord_dt BETWEEN cm.eff_start_dt "
    "AND COALESCE(cm.eff_end_dt, DATE '9999-12-31') WHERE o.sts_cd != 'X' GROUP BY 1",
    "SELECT COUNT(DISTINCT cust_id) FROM cust_mstr",  # entity count over SCD2
    "WITH ranked AS (SELECT sub_id, mrr_amt, row_number() OVER (PARTITION BY sub_id "
    "ORDER BY evt_dt DESC, evt_id DESC) AS rn FROM sub_evt) "
    "SELECT round(sum(mrr_amt), 2) FROM ranked WHERE rn = 1",  # CTE outputs
    "SELECT round(sum(o.tot_amt), 2) FROM ord_hdr o WHERE o.sts_cd <> 'X' AND o.ord_id IN "
    "(SELECT DISTINCT l.ord_id FROM ord_ln l JOIN ord_rtn r ON l.ord_ln_id = r.ord_ln_id)",
])
def test_clean_sql_has_no_findings(doc, sql):
    r = lint_sql(doc, sql)
    assert r["ok"] and not r["findings"], render_findings(r)


@pytest.mark.parametrize("sql", [
    # inherited rule, semi-join form
    "SELECT sum(line_amt) FROM ord_ln WHERE ord_id IN (SELECT ord_id FROM ord_hdr WHERE sts_cd <> 'X')",
    # inherited rule, join-the-parent form
    "SELECT p.dept_cd, sum(l.line_amt) FROM ord_ln l JOIN prod_ref p ON l.prod_id = p.prod_id "
    "JOIN ord_hdr o ON l.ord_id = o.ord_id WHERE o.sts_cd != 'X' GROUP BY 1",
])
def test_inherited_filter_both_forms_satisfy(doc, sql):
    r = lint_sql(doc, sql)
    assert r["ok"] and not r["findings"], render_findings(r)


@pytest.mark.parametrize("sql", [
    # exactly what compile_metric emits: the filter is parenthesised
    "SELECT SUM(ord_hdr.tot_amt) AS total_tot_amt FROM ord_hdr WHERE (sts_cd <> 'X')",
    # and the same, nested inside an AND
    "SELECT SUM(ord_hdr.tot_amt) AS m FROM ord_hdr "
    "WHERE ((sts_cd <> 'X') AND (ord_dt >= '2024-01-01'))",
])
def test_parenthesised_required_filter_satisfies(doc, sql):
    """A required filter in brackets still satisfies the rule.

    compile_metric parenthesises the filters it applies, so without this the
    linter reported the compiler's own output as missing the filter it had just
    added -- and the MCP server tells agents to check_sql before executing.
    """
    r = lint_sql(doc, sql)
    assert r["ok"] and not r["findings"], render_findings(r)


def test_parenthesised_but_wrong_filter_is_still_flagged(doc):
    """Unwrapping brackets must not make the check credulous."""
    r = lint_sql(doc, "SELECT SUM(ord_hdr.tot_amt) AS m FROM ord_hdr WHERE (sts_cd <> 'P')")
    assert _rules(r) == {"missing_required_filter"}


def test_inherited_filter_missing_on_child_fact(doc):
    r = lint_sql(doc, "SELECT p.dept_cd, sum(l.line_amt) FROM ord_ln l "
                      "JOIN prod_ref p ON l.prod_id = p.prod_id GROUP BY 1")
    assert _rules(r) == {"missing_required_filter"} and r["findings"][0]["table"] == "ord_ln"


# ------------------------------------------------------------ each rule

def test_unknown_column(doc):
    r = lint_sql(doc, "SELECT SUM(tot_sls_amt) FROM dly_sls_agg JOIN date_dim "
                      "ON dly_sls_agg.agg_dt_key = date_dim.date_key WHERE date_dim.date_key = "
                      "(SELECT date_key FROM date_dim WHERE date_value = '2024-06-15')")
    assert not r["ok"]
    assert [f["column"] for f in r["findings"]] == ["date_value"]  # reported once


def test_correlated_reference_is_flagged(doc):
    # ord_rtn has no ord_id: DuckDB resolves it to the OUTER ord_hdr.ord_id and the
    # IN (...) becomes vacuously true — silently wrong, no execution error.
    r = lint_sql(doc, "SELECT round(sum(oh.tot_amt), 2) FROM ord_hdr oh WHERE oh.sts_cd <> 'X' "
                      "AND oh.ord_id IN (SELECT DISTINCT ord_id FROM ord_rtn)")
    assert _rules(r) == {"correlated_reference"}
    f = r["findings"][0]
    assert f["table"] == "ord_hdr" and f["column"] == "ord_id" and "outer" in f["message"]


def test_deprecated_table_names_replacement(doc):
    r = lint_sql(doc, "SELECT SUM(amount) FROM ord_hdr_legacy WHERE status <> 'CANCELLED'")
    dep = [f for f in r["findings"] if f["rule"] == "deprecated_table"]
    assert dep and "ord_hdr" in dep[0]["fix"]


def test_missing_required_filter_measure_scope(doc):
    r = lint_sql(doc, "SELECT SUM(tot_amt) AS total_order_revenue FROM ord_hdr")
    assert _rules(r) == {"missing_required_filter"}
    assert r["findings"][0]["severity"] == "error"
    assert "sts_cd <> 'X'" in r["findings"][0]["fix"]


def test_required_filter_touched_but_different_is_warning(doc):
    r = lint_sql(doc, "SELECT SUM(tot_amt) FROM ord_hdr WHERE sts_cd = 'C'")
    f = r["findings"][0]
    assert f["rule"] == "missing_required_filter" and f["severity"] == "warning"


def test_fanout_aggregate(doc):
    r = lint_sql(doc, "SELECT d.dept_nm, SUM(oh.tot_amt) FROM ord_hdr oh "
                      "JOIN ord_ln ol ON oh.ord_id = ol.ord_id "
                      "JOIN prod_ref pr ON ol.prod_id = pr.prod_id "
                      "JOIN dept_dim d ON pr.dept_cd = d.dept_cd "
                      "WHERE oh.sts_cd <> 'X' GROUP BY 1")
    assert _rules(r) == {"fanout_aggregate"}
    f = r["findings"][0]
    assert f["table"] == "ord_hdr" and "ord_ln" in f["message"] and "EXISTS" in f["fix"]


def test_scd2_without_validity_is_warning(doc):
    r = lint_sql(doc, "SELECT cm.seg_cd, SUM(o.tot_amt) FROM ord_hdr o JOIN cust_mstr cm "
                      "ON o.cust_id = cm.cust_id WHERE o.sts_cd <> 'X' GROUP BY 1")
    assert r["ok"] and _rules(r) == {"scd2_without_validity"}
    assert "eff_start_dt" in r["findings"][0]["fix"]


def test_parse_error(doc):
    r = lint_sql(doc, "SELEC tot_amt FRM ord_hdr")
    assert not r["ok"] and _rules(r) == {"parse_error"}


def test_unknown_table(doc):
    r = lint_sql(doc, "SELECT count(*) FROM orders_final")
    assert _rules(r) == {"unknown_table"}


# ------------------------------------------------------------ surfaces

def test_render_findings_carries_fix_hints(doc):
    text = render_findings(lint_sql(doc, "SELECT SUM(tot_amt) FROM ord_hdr"))
    assert "[error] missing_required_filter" in text and "fix:" in text


def test_mcp_check_sql_tool_is_registered(doc):
    pytest.importorskip("mcp")
    srv = mcp_server.build_server(doc)
    import asyncio
    tools = asyncio.run(srv.list_tools())
    assert "check_sql" in {t.name for t in tools}
