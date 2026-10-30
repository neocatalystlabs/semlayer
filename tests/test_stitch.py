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


def _members(tmp_path: Path, shadow: bool = False) -> list[str]:
    con = _warehouse(tmp_path / "acme.duckdb")
    if shadow:
        # finance keeps its own cost centres: 8 of platform's 12, plus a budget column
        con.execute("create table finance.dim_cost_center as select cost_center_id, "
                    "cost_center_name, cost_center_id * 1000.0 as budget "
                    "from platform.dim_cost_center where cost_center_id < 8")
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


def test_a_members_copy_of_another_members_dimension_is_the_same_entity(tmp_path):
    """finance/dim_cost_center is 8 of platform's 12 rows plus a budget column.

    Not a fact and its dimension: the same entity. Stitch writes a one_to_one
    `same_entity` edge from the copy to the canonical table (platform's is
    certified AND holds every key), with the columns they share, the column
    only finance has, and the measured overlap both ways. Nothing to review:
    the canonical copy was decided.
    """
    from semlayer.review import collect, for_hub
    from semlayer.setdoc import load_set, set_checks

    members = _members(tmp_path, shadow=True)
    con = duckdb.connect(str(tmp_path / "acme.duckdb"), read_only=True)
    try:
        result = stitch(members, "acme", tmp_path / "acme.yaml", source=DuckDBSource(con),
                        query_workers=1)
    finally:
        con.close()
    (tmp_path / "acme.yaml").write_text(yaml.safe_dump(result.hub, sort_keys=False))
    assert result.copies == 1
    same = [e for e in result.hub["semantic_layer"]["relationships"] if e.get("same_entity")]
    assert len(same) == 1
    e = same[0]
    assert e["from"] == {"table": "finance/dim_cost_center", "columns": ["cost_center_id"]}
    assert e["to"] == {"table": "platform/dim_cost_center", "columns": ["cost_center_id"]}
    assert e["cardinality"] == "one_to_one" and e["canonical"] == "platform/dim_cost_center"
    assert e["shared_columns"] == ["cost_center_id", "cost_center_name"]
    assert e["extends_with"] == ["budget"]
    assert e["key_overlap"] == {"from_in_to": 1.0, "to_in_from": 0.667}
    assert e["confidence"] >= 0.7 and "certified" in e["provenance"][0]["detail"]
    # the fact's own key still crosses to the canonical dimension, not the copy
    seam = {(r["from"]["table"], r["to"]["table"]) for r in
            result.hub["semantic_layer"]["relationships"] if not r.get("same_entity")}
    assert ("finance/fct_gl", "platform/dim_cost_center") in seam
    v = validate_file(tmp_path / "acme.yaml")
    assert v.ok, v.errors
    rs = load_set(tmp_path / "acme.yaml")
    assert [i for i in for_hub(collect(rs.doc), "acme") if i.kind == "same_entity"] == []

    # a canonical that is not an end, or a same_entity that is not one_to_one, is a set error
    e["canonical"] = "sales/dim_customer"
    e["cardinality"] = "many_to_one"
    errors = set_checks(rs)
    rs.doc["semantic_layer"]["relationships"] = result.hub["semantic_layer"]["relationships"]
    errors = set_checks(rs)
    assert any("neither of its ends" in x for x in errors)
    assert any("must be one_to_one" in x for x in errors)


def test_without_a_connection_a_copy_is_proposed_undecided_and_queued(tmp_path):
    from semlayer.review import collect, for_hub
    from semlayer.setdoc import load_set

    members = _members(tmp_path, shadow=True)
    result = stitch(members, "acme", tmp_path / "acme.yaml", source=None)
    (tmp_path / "acme.yaml").write_text(yaml.safe_dump(result.hub, sort_keys=False))
    same = [e for e in result.hub["semantic_layer"]["relationships"] if e.get("same_entity")]
    assert len(same) == 1 and "canonical" not in same[0] and "key_overlap" not in same[0]
    assert same[0]["confidence"] == 0.3 and "NOT tested" in same[0]["provenance"][0]["detail"]
    assert validate_file(tmp_path / "acme.yaml").ok
    items = [i for i in for_hub(collect(load_set(tmp_path / "acme.yaml").doc), "acme")
             if i.kind == "same_entity"]
    assert len(items) == 1 and "undecided" in items[0].claim


def test_refresh_keeps_verdicts_replaces_inferences_and_orphans_lost_ends(tmp_path):
    """The analyst's word survives a refresh; inference is redone; a lost end is orphaned.

    After the first stitch the analyst rejects fct_gl.customer_id -> sales
    (deprecated) and accepts both cost-centre edges (reviewed). Then the
    members move: sales drops cost_center_id from fct_orders, finance adds a
    region_id. Refresh must keep the rejection and not re-propose it, keep
    the accepted finance edge as reviewed, orphan the accepted sales edge,
    and find the new region key. The hub file stays a hub.
    """
    from semlayer.review import ReviewItem, apply_to_hub
    from semlayer.setdoc import load_set
    from semlayer.stitch import refresh

    members = _members(tmp_path)
    con = duckdb.connect(str(tmp_path / "acme.duckdb"), read_only=True)
    try:
        result = stitch(members, "acme", tmp_path / "acme.yaml", source=DuckDBSource(con),
                        query_workers=1)
    finally:
        con.close()
    hub = result.hub
    rels = {(r["from"]["table"], r["from"]["columns"][0]): r for r in hub["semantic_layer"]["relationships"]}
    def item(key):
        return ReviewItem("seam_edge", key[0], key[1], "", "", ref=f"seam.{rels[key]['name']}")
    assert apply_to_hub(hub, item(("finance/fct_gl", "customer_id")), "reject")
    assert apply_to_hub(hub, item(("finance/fct_gl", "cost_center_id")), "accept")
    assert apply_to_hub(hub, item(("sales/fct_orders", "cost_center_id")), "accept")
    # a member's own claim is not the hub's to settle
    assert not apply_to_hub(hub, ReviewItem("low_confidence", "sales/dim_customer", "segment",
                                            "", ""), "accept")
    (tmp_path / "acme.yaml").write_text(yaml.safe_dump(hub, sort_keys=False))

    # the members move
    sales = yaml.safe_load((tmp_path / "sales.yaml").read_text())
    for t in sales["semantic_layer"]["tables"]:
        if t["name"] == "fct_orders":
            t["columns"] = [c for c in t["columns"] if c["name"] != "cost_center_id"]
    (tmp_path / "sales.yaml").write_text(yaml.safe_dump(sales, sort_keys=False))
    con = duckdb.connect(str(tmp_path / "acme.duckdb"))
    con.execute("alter table finance.fct_gl add column region_id integer")
    con.execute("update finance.fct_gl set region_id = 1 + gl_line_id % 3")
    con.close()
    finance = yaml.safe_load((tmp_path / "finance.yaml").read_text())
    for t in finance["semantic_layer"]["tables"]:
        if t["name"] == "fct_gl":
            t["columns"].append({"name": "region_id", "sql_type": "INTEGER",
                                 "entity_role": "foreign_key",
                                 "cardinality": {"distinct": 3, "lower_bound": 3, "upper_bound": 3,
                                                 "method": "exact", "null_count": 0}})
    (tmp_path / "finance.yaml").write_text(yaml.safe_dump(finance, sort_keys=False))

    con = duckdb.connect(str(tmp_path / "acme.duckdb"), read_only=True)
    try:
        res = refresh(tmp_path / "acme.yaml", source=DuckDBSource(con), query_workers=1)
    finally:
        con.close()
    (tmp_path / "acme.yaml").write_text(yaml.safe_dump(res.hub, sort_keys=False))
    sl = res.hub["semantic_layer"]
    assert sl["members"] and "tables" not in sl                     # still a hub, not the estate
    by = {}
    for r in sl["relationships"]:
        by.setdefault((r["from"]["table"], r["from"]["columns"][0]), []).append(r)
    rejected = by[("finance/fct_gl", "customer_id")]
    assert len(rejected) == 1 and rejected[0]["lifecycle"] == "deprecated"      # kept, not re-proposed
    assert by[("finance/fct_gl", "cost_center_id")][0]["lifecycle"] == "reviewed"
    lost = by[("sales/fct_orders", "cost_center_id")][0]
    assert lost["lifecycle"] == "orphaned" and "refresh" in lost["provenance"][-1]["detail"]
    new = by[("finance/fct_gl", "region_id")][0]
    assert new["to"]["table"] == "platform/dim_region" and new["lifecycle"] == "inferred"
    assert res.kept == 3 and res.orphaned == 1 and res.superseded >= 1
    assert ("finance/fct_gl", "customer_id") in {(u["table"], u["column"]) for u in sl["unresolved"]}
    v = validate_file(tmp_path / "acme.yaml")
    assert v.ok, v.errors
    # a rejected edge is not a join path
    doc = load_set(tmp_path / "acme.yaml").doc
    doc["semantic_layer"].setdefault("metrics", []).append(
        {"name": "finance/gl_amount", "type": "simple", "measure": "finance/fct_gl.amount",
         "agg": "sum"})
    out = compile_metric(doc, "finance/gl_amount", group_by=["sales/dim_customer.segment"])
    assert "sql" not in out or not out["sql"]
