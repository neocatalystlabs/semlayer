# SPDX-License-Identifier: FSL-1.1-ALv2
"""stitch infers the seam across members through the analyst's connection, and writes the hub.

Three schemas in one DuckDB file, three members inferred from them. Finance
and sales each carry a `cost_center_id` with no parent in their own file,
and finance a `customer_id`; platform holds the cost centres and sales the
customers. Stitch must find all three, test them, write the hub, and list
the key that matches nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import yaml

OSS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OSS / "src"))

from test_set_consumers import _warehouse  # noqa: E402

from semlayer.cli import _load_doc  # noqa: E402
from semlayer.compile import compile_metric  # noqa: E402
from semlayer.pipeline import InferOptions, infer  # noqa: E402
from semlayer.source import DuckDBSource  # noqa: E402
from semlayer.stitch import stitch, unresolved_references  # noqa: E402
from semlayer.validate import validate_file  # noqa: E402

AGG = {"allowed": ["sum", "avg", "min", "max", "count"], "default": "sum"}


def _members(tmp_path: Path) -> list[str]:
    con = _warehouse(tmp_path / "acme.duckdb")
    con.execute("alter table sales.fct_orders add column sales_rep_id integer")
    # values no other member holds: a key stitch must list, not guess
    con.execute("update sales.fct_orders set sales_rep_id = 1000 + order_id % 9")
    con.execute("update platform.dim_cost_center set region_id = region_id")  # keep shape
    try:
        for schema in ("finance", "sales", "platform"):
            doc = infer(DuckDBSource(con), options=InferOptions(schema=schema, query_workers=1))
            sl = doc["semantic_layer"]
            if schema == "sales":
                sl["metrics"] = [{"name": "net_revenue", "type": "simple",
                                  "measure": "fct_orders.net_revenue", "agg": "sum"}]
            if schema == "platform":
                sl["tables"][0]["lifecycle"] = "certified"
            for t in sl["tables"]:
                for c in t["columns"]:
                    if c["name"] in ("net_revenue", "amount"):
                        c["entity_role"] = "measure"
                        c["aggregations"] = AGG
                    # the engine already marks these as keys with no target; be explicit
                    if c["name"] in ("cost_center_id", "customer_id", "sales_rep_id") \
                            and not c.get("foreign_key") and t["name"] not in ("dim_customer",
                                                                                "dim_cost_center"):
                        c["entity_role"] = "foreign_key"
            (tmp_path / f"{schema}.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    finally:
        con.close()
    return [str(tmp_path / f"{n}.yaml") for n in ("finance", "sales", "platform")]


def test_stitch_finds_the_seam_and_lists_what_it_could_not(tmp_path):
    members = _members(tmp_path)
    con = duckdb.connect(str(tmp_path / "acme.duckdb"), read_only=True)
    try:
        result = stitch(members, "acme", tmp_path / "acme.yaml", source=DuckDBSource(con),
                        query_workers=1)
    finally:
        con.close()
    hub = result.hub["semantic_layer"]
    (tmp_path / "acme.yaml").write_text(yaml.safe_dump(result.hub, sort_keys=False))
    assert result.mode == "inclusion" and result.tested > 0
    edges = {(e["from"]["table"], e["from"]["columns"][0], e["to"]["table"])
             for e in hub["relationships"]}
    assert ("finance/fct_gl", "cost_center_id", "platform/dim_cost_center") in edges
    assert ("sales/fct_orders", "cost_center_id", "platform/dim_cost_center") in edges
    assert ("finance/fct_gl", "customer_id", "sales/dim_customer") in edges
    assert len(hub["relationships"]) == 3
    for e in hub["relationships"]:
        assert e["confidence"] >= 0.6                      # all three have name evidence
        assert "inclusion 1.0" in e["provenance"][0]["detail"]
    # members were not touched
    assert "relationships" not in yaml.safe_load(Path(members[0]).read_text())["semantic_layer"] \
        or all("/" not in r["to"]["table"]
               for r in yaml.safe_load(Path(members[0]).read_text())["semantic_layer"]["relationships"])
    # the key nothing owns is listed, not forgotten
    assert [(u["table"], u["column"]) for u in hub["unresolved"]] == [("sales/fct_orders",
                                                                       "sales_rep_id")]
    # the hub validates as a set and compiles across the seam it just wrote
    assert validate_file(tmp_path / "acme.yaml").ok, validate_file(tmp_path / "acme.yaml").errors
    doc = _load_doc(str(tmp_path / "acme.yaml"))
    out = compile_metric(doc, "sales/net_revenue", group_by=["platform/dim_region.region_name"])
    assert "sql" in out and "platform.dim_cost_center AS platform_dim_cost_center" in out["sql"]


def test_names_only_mode_proposes_but_marks_untested(tmp_path):
    members = _members(tmp_path)
    result = stitch(members, "acme", tmp_path / "acme.yaml", source=None)
    hub = result.hub["semantic_layer"]
    assert result.mode == "names_only" and result.tested == 0
    edges = {(e["from"]["table"], e["to"]["table"]) for e in hub["relationships"]}
    assert ("finance/fct_gl", "platform/dim_cost_center") in edges
    for e in hub["relationships"]:
        assert e["confidence"] == 0.3
        assert "NOT tested" in e["provenance"][0]["detail"]
    (tmp_path / "acme.yaml").write_text(yaml.safe_dump(result.hub, sort_keys=False))
    assert validate_file(tmp_path / "acme.yaml").ok


def test_unresolved_references_are_key_shaped_columns_with_no_target():
    """Link's child criteria, minus resolved keys, ETL bookkeeping and plain attributes."""
    def col(name, role, sql_type="INTEGER", distinct=40, **extra):
        return {"name": name, "sql_type": sql_type, "entity_role": role,
                "cardinality": {"distinct": distinct, "null_count": 0}, **extra}
    sl = {"name": "f", "tables": [
        {"name": "f/t", "source": "f.t", "primary_key": ["t_id"], "columns": [
            col("t_id", "primary_key", distinct=300),
            col("a_id", "foreign_key"),                                   # unresolved
            col("b_id", "foreign_key", foreign_key={"references": "f/u.b_id"}),   # resolved
            col("segment", "dimension", sql_type="VARCHAR", distinct=3),  # attribute, not a key
            col("etl_batch_id", "foreign_key", distinct=50),              # ETL bookkeeping
            col("status_cd", "dimension", sql_type="VARCHAR", distinct=4)]},   # key-NAMED: kept
        {"name": "f/u", "source": "f.u", "primary_key": ["b_id"],
         "columns": [col("b_id", "primary_key", distinct=60)]},
        {"name": "f/raw", "source": "f.raw", "modeling": "inventory_only",
         "columns": [col("x_id", "foreign_key")]}]}
    assert unresolved_references(sl) == [("f/t", "a_id"), ("f/t", "status_cd")]
