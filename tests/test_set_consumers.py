# SPDX-License-Identifier: FSL-1.1-ALv2
"""mcp, compile, lint and review over a hub: the three-file scenario end to end.

Finance, sales and platform schemas in one DuckDB file, three member
documents inferred from them, and an analyst's hub with a hand-written
seam. The compiled cross-domain SQL must execute against the warehouse and
agree with a hand-written query.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import yaml

OSS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OSS / "src"))

from semlayer.cli import _load_doc  # noqa: E402
from semlayer.compile import compile_metric  # noqa: E402
from semlayer.lint import lint_sql  # noqa: E402
from semlayer.mcp_server import get_table, list_domains, list_tables  # noqa: E402
from semlayer.pipeline import InferOptions, infer  # noqa: E402
from semlayer.review import collect, for_hub  # noqa: E402
from semlayer.source import DuckDBSource  # noqa: E402
from semlayer.validate import validate_file  # noqa: E402

AGG = {"allowed": ["sum", "avg", "min", "max", "count"], "default": "sum"}


def _warehouse(path: Path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(path))
    con.execute("create schema finance; create schema sales; create schema platform")
    con.execute("create table platform.dim_region(region_id integer, region_name varchar)")
    con.execute("insert into platform.dim_region values (1,'north'),(2,'south'),(3,'west')")
    con.execute("create table platform.dim_cost_center(cost_center_id integer, "
                "cost_center_name varchar, region_id integer)")
    con.execute("insert into platform.dim_cost_center select i, 'cc' || i, 1 + i % 3 "
                "from range(12) t(i)")
    con.execute("create table sales.dim_customer(customer_id integer, segment varchar)")
    con.execute("insert into sales.dim_customer select i, case when i % 2 = 0 then 'smb' "
                "else 'enterprise' end from range(40) t(i)")
    con.execute("create table sales.fct_orders(order_id integer, customer_id integer, "
                "cost_center_id integer, net_revenue double)")
    con.execute("insert into sales.fct_orders select i, i % 40, i % 12, (i % 7) * 10.0 "
                "from range(500) t(i)")
    con.execute("create table finance.fct_gl(gl_line_id integer, cost_center_id integer, "
                "customer_id integer, amount double)")
    con.execute("insert into finance.fct_gl select i, i % 12, i % 40, (i % 5) * 3.0 "
                "from range(300) t(i)")
    return con


def _members(tmp_path: Path) -> Path:
    con = _warehouse(tmp_path / "acme.duckdb")
    try:
        for schema in ("finance", "sales", "platform"):
            doc = infer(DuckDBSource(con), options=InferOptions(schema=schema, query_workers=1))
            sl = doc["semantic_layer"]
            # metrics the way Enrich would write them
            if schema == "sales":
                sl.setdefault("metrics", []).append(
                    {"name": "net_revenue", "type": "simple",
                     "measure": "fct_orders.net_revenue", "agg": "sum"})
            if schema == "finance":
                sl.setdefault("metrics", []).append(
                    {"name": "gl_amount", "type": "simple", "measure": "fct_gl.amount",
                     "agg": "sum"})
            for t in sl["tables"]:
                for c in t["columns"]:
                    if c["name"] in ("net_revenue", "amount"):
                        c["entity_role"] = "measure"
                        c["aggregations"] = AGG
            (tmp_path / f"{schema}.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    finally:
        con.close()
    hub = {"semantic_layer": {
        "spec_version": "0.6.0", "name": "acme",
        "members": [{"name": n, "path": f"{n}.yaml"} for n in ("finance", "sales", "platform")],
        "relationships": [
            {"name": "orders_cc", "from": {"table": "sales/fct_orders", "columns": ["cost_center_id"]},
             "to": {"table": "platform/dim_cost_center", "columns": ["cost_center_id"]},
             "cardinality": "many_to_one", "confidence": 0.94, "lifecycle": "inferred"},
            {"name": "gl_cc", "from": {"table": "finance/fct_gl", "columns": ["cost_center_id"]},
             "to": {"table": "platform/dim_cost_center", "columns": ["cost_center_id"]},
             "cardinality": "many_to_one", "confidence": 0.61, "lifecycle": "inferred"},
            {"name": "gl_customer", "from": {"table": "finance/fct_gl", "columns": ["customer_id"]},
             "to": {"table": "sales/dim_customer", "columns": ["customer_id"]},
             "cardinality": "many_to_one", "confidence": 0.86, "lifecycle": "inferred"},
        ],
    }}
    (tmp_path / "acme.yaml").write_text(yaml.safe_dump(hub, sort_keys=False))
    return tmp_path / "acme.yaml"


def test_scenario_one_end_to_end(tmp_path):
    hub = _members(tmp_path)
    assert validate_file(hub).ok, validate_file(hub).errors
    doc = _load_doc(str(hub))                      # the CLI resolves a hub on load
    sl = doc["semantic_layer"]
    assert {t["name"] for t in sl["tables"]} >= {"finance/fct_gl", "sales/fct_orders",
                                                  "platform/dim_region"}

    # --- mcp: members are domains; the seam shows on table detail
    domains = {d["name"]: d["tables"] for d in list_domains(doc)}
    assert {k: domains[k] for k in ("finance", "sales", "platform")} == {
        "finance": 1, "sales": 2, "platform": 2}        # members lead; Enrich's own domains follow
    assert {t["name"] for t in list_tables(doc, "platform")} == {"platform/dim_cost_center",
                                                                 "platform/dim_region"}
    detail = get_table(doc, "finance/fct_gl")
    assert detail["source"] == "finance.fct_gl"
    assert {r["to"]["table"] for r in detail["relationships"]} >= {"platform/dim_cost_center",
                                                                   "sales/dim_customer"}

    # --- compile: revenue by region crosses the seam and executes
    out = compile_metric(doc, "sales/net_revenue", group_by=["platform/dim_region.region_name"])
    assert "sql" in out, out
    sql = out["sql"]
    assert "FROM sales.fct_orders AS sales_fct_orders" in sql
    assert "LEFT JOIN platform.dim_cost_center AS platform_dim_cost_center" in sql
    assert "/" not in sql.replace("--", "")           # no set name leaks into SQL
    con = duckdb.connect(str(tmp_path / "acme.duckdb"), read_only=True)
    try:
        got = dict(con.execute(sql).fetchall())
        want = dict(con.execute(
            "select r.region_name, sum(o.net_revenue) from sales.fct_orders o "
            "join platform.dim_cost_center cc on o.cost_center_id = cc.cost_center_id "
            "join platform.dim_region r on cc.region_id = r.region_id group by 1").fetchall())
    finally:
        con.close()
    assert got == want

    # --- compile: a metric grouped by its own member's dimension still works
    out = compile_metric(doc, "finance/gl_amount", group_by=["sales/dim_customer.segment"])
    assert "sql" in out and "LEFT JOIN sales.dim_customer AS sales_dim_customer" in out["sql"]

    # --- lint: a seam-supported join passes; an unsupported one is named
    good = ("select r.region_name, sum(o.net_revenue) from sales.fct_orders o "
            "join platform.dim_cost_center cc on o.cost_center_id = cc.cost_center_id "
            "join platform.dim_region r on cc.region_id = r.region_id group by 1")
    findings = lint_sql(doc, good)
    assert not [f for f in findings["findings"] if f["rule"] == "unknown_table"], findings
    bad = ("select count(*) from finance.fct_gl g join sales.fct_orders o "
           "on g.customer_id = o.customer_id")
    rules = {f["rule"] for f in lint_sql(doc, bad)["findings"]}
    assert "unknown_table" not in rules            # both tables are known through their source
    rules = {f["rule"] for f in lint_sql(doc, "select 1 from finance.nope")["findings"]}
    assert "unknown_table" in rules

    # --- review: the hub sees the weak seam edge and nothing from inside the members
    items = for_hub(collect(doc), "acme")
    assert [i.kind for i in items] == ["seam_edge"]
    assert items[0].table == "finance/fct_gl" and "platform/dim_cost_center" in items[0].claim


def test_cross_warehouse_join_is_refused_unless_federated(tmp_path):
    hub = _members(tmp_path)
    lake = yaml.safe_load((tmp_path / "finance.yaml").read_text())
    lake["semantic_layer"]["warehouse"] = "duckdb:lake.duckdb"
    (tmp_path / "finance.yaml").write_text(yaml.safe_dump(lake, sort_keys=False))
    hub_doc = yaml.safe_load(hub.read_text())
    hub_doc["semantic_layer"]["federation"] = {"engine": "duckdb", "bridge": "bridge.duckdb"}
    hub.write_text(yaml.safe_dump(hub_doc, sort_keys=False))
    doc = _load_doc(str(hub))
    out = compile_metric(doc, "finance/gl_amount", group_by=["sales/dim_customer.segment"])
    assert "sql" in out                                            # federated: compiles
    del hub_doc["semantic_layer"]["federation"]
    hub.write_text(yaml.safe_dump(hub_doc, sort_keys=False))
    assert not validate_file(hub).ok                               # the set rule
    doc = _load_doc(str(hub))
    out = compile_metric(doc, "finance/gl_amount", group_by=["sales/dim_customer.segment"])
    assert out.get("refused") and "duckdb:lake.duckdb" in out["reason"] and "federation" in out["reason"]
    # within one warehouse nothing is refused
    out = compile_metric(doc, "finance/gl_amount", group_by=["finance/fct_gl.cost_center_id"])
    assert "sql" in out and "FROM finance.fct_gl AS finance_fct_gl" in out["sql"]


def test_single_schema_sql_is_unchanged(tmp_path):
    """A layer over the default schema compiles to the same bare names as before."""
    con = duckdb.connect(str(tmp_path / "one.duckdb"))
    con.execute("create table orders(order_id integer, amount double, region varchar)")
    con.execute("insert into orders select i, i * 1.0, 'r' || (i % 3) from range(30) t(i)")
    try:
        doc = infer(DuckDBSource(con), options=InferOptions(query_workers=1))
    finally:
        con.close()
    sl = doc["semantic_layer"]
    for c in sl["tables"][0]["columns"]:
        if c["name"] == "amount":
            c["entity_role"] = "measure"
            c["aggregations"] = AGG
    sl["metrics"] = [{"name": "amount", "type": "simple", "measure": "orders.amount", "agg": "sum"}]
    out = compile_metric(doc, "amount", group_by=["region"])
    assert out["sql"].startswith("SELECT orders.region AS region, SUM(orders.amount) AS amount\nFROM orders")
