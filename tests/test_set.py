# SPDX-License-Identifier: FSL-1.1-ALv2
"""A hub and its members resolve into one document that every consumer understands.

Finance, Sales and Platform each own a file that knows nothing of the
others. The analyst's hub names them as members and holds the seam. After
resolution every object is `<document>/<name>`, every reference is
rewritten the same way, `source` is untouched, and the ordinary validator
checks the whole. The set's own rules catch what no single file can.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

OSS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OSS / "src"))

from semlayer.setdoc import SetError, is_hub, load_set, qualify, set_checks  # noqa: E402
from semlayer.validate import load_document, validate_document, validate_file  # noqa: E402

BUILD = OSS / "fixtures" / "build"


AGG = {"allowed": ["sum", "avg", "min", "max", "count"], "default": "sum"}


def _col(name, sql_type="INTEGER", **extra):
    if extra.get("entity_role") == "measure":
        extra.setdefault("aggregations", AGG)
    return {"name": name, "sql_type": sql_type, **extra}


FINANCE = {"semantic_layer": {
    "spec_version": "0.6.0", "name": "finance", "warehouse": "snowflake:ANALYTICS",
    "tables": [
        {"name": "fct_gl", "source": "finance.fct_gl", "primary_key": ["gl_line_id"],
         "columns": [_col("gl_line_id", entity_role="primary_key"),
                     _col("amount", "DOUBLE", entity_role="measure"),
                     _col("cost_center_id", entity_role="foreign_key"),
                     _col("ap_batch_id", entity_role="foreign_key",
                          foreign_key={"references": "fct_ap_batch.ap_batch_id"})]},
        {"name": "fct_ap_batch", "source": "finance.fct_ap_batch", "primary_key": ["ap_batch_id"],
         "columns": [_col("ap_batch_id", entity_role="primary_key")]},
    ],
    "relationships": [{"name": "gl_to_batch", "from": {"table": "fct_gl", "columns": ["ap_batch_id"]},
                       "to": {"table": "fct_ap_batch", "columns": ["ap_batch_id"]},
                       "cardinality": "many_to_one"}],
    "metrics": [{"name": "gl_amount", "type": "simple", "measure": "fct_gl.amount", "agg": "sum"}],
}}

SALES = {"semantic_layer": {
    "spec_version": "0.6.0", "name": "sales", "warehouse": "snowflake:ANALYTICS",
    "tables": [
        {"name": "fct_orders", "source": "sales.fct_orders", "primary_key": ["order_id"],
         "columns": [_col("order_id", entity_role="primary_key"),
                     _col("customer_id", entity_role="foreign_key",
                          foreign_key={"references": "dim_customer.customer_id"}),
                     _col("cost_center_id", entity_role="foreign_key"),
                     _col("net_revenue", "DOUBLE", entity_role="measure")]},
        {"name": "dim_customer", "source": "sales.dim_customer", "primary_key": ["customer_id"],
         "columns": [_col("customer_id", entity_role="primary_key"), _col("segment", "VARCHAR")]},
    ],
    "relationships": [{"name": "orders_to_customer",
                       "from": {"table": "fct_orders", "columns": ["customer_id"]},
                       "to": {"table": "dim_customer", "columns": ["customer_id"]},
                       "cardinality": "many_to_one"}],
    "metrics": [{"name": "net_revenue", "type": "simple", "measure": "fct_orders.net_revenue",
                 "agg": "sum", "dimensions": ["dim_customer.segment"]}],
}}

PLATFORM = {"semantic_layer": {
    "spec_version": "0.6.0", "name": "platform", "warehouse": "snowflake:ANALYTICS",
    "tables": [
        {"name": "dim_cost_center", "source": "platform.dim_cost_center",
         "primary_key": ["cost_center_id"],
         "columns": [_col("cost_center_id", entity_role="primary_key"),
                     _col("cost_center_name", "VARCHAR"),
                     _col("region_id", entity_role="foreign_key",
                          foreign_key={"references": "dim_region.region_id"})]},
        {"name": "dim_region", "source": "platform.dim_region", "primary_key": ["region_id"],
         "columns": [_col("region_id", entity_role="primary_key"), _col("region_name", "VARCHAR")]},
    ],
    "relationships": [{"name": "cc_to_region",
                       "from": {"table": "dim_cost_center", "columns": ["region_id"]},
                       "to": {"table": "dim_region", "columns": ["region_id"]},
                       "cardinality": "many_to_one"}],
    "hierarchies": [{"name": "cost_center_geo", "kind": "level", "levels": [
        {"name": "region", "column": "dim_region.region_name"},
        {"name": "cost_center", "column": "dim_cost_center.cost_center_name"}]}],
}}

ACME = {"semantic_layer": {
    "spec_version": "0.6.0", "name": "acme",
    "members": [{"name": "finance", "path": "finance.yaml"},
                {"name": "sales", "path": "sales.yaml"},
                {"name": "platform", "path": "platform.yaml"}],
    "relationships": [
        {"name": "gl_cc", "from": {"table": "finance/fct_gl", "columns": ["cost_center_id"]},
         "to": {"table": "platform/dim_cost_center", "columns": ["cost_center_id"]},
         "cardinality": "many_to_one", "confidence": 0.94},
        {"name": "orders_cc", "from": {"table": "sales/fct_orders", "columns": ["cost_center_id"]},
         "to": {"table": "platform/dim_cost_center", "columns": ["cost_center_id"]},
         "cardinality": "many_to_one", "confidence": 0.94},
    ],
    "repo_knowledge": {"routing": [{"intent": "revenue by region",
                                    "use": ["sales/fct_orders", "platform/dim_cost_center",
                                            "platform/dim_region"]}]},
}}


def _write(tmp_path, **docs) -> Path:
    for name, doc in docs.items():
        (tmp_path / f"{name}.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    return tmp_path / "acme.yaml"


def test_members_validate_alone_and_know_nothing_of_the_set(tmp_path):
    hub = _write(tmp_path, finance=FINANCE, sales=SALES, platform=PLATFORM, acme=ACME)
    for name in ("finance", "sales", "platform"):
        r = validate_file(tmp_path / f"{name}.yaml")
        assert r.ok, (name, r.errors)
        assert not is_hub(load_document(tmp_path / f"{name}.yaml"))
    assert is_hub(load_document(hub))


def test_hub_resolves_to_one_qualified_document(tmp_path):
    hub = _write(tmp_path, finance=FINANCE, sales=SALES, platform=PLATFORM, acme=ACME)
    rs = load_set(hub)
    sl = rs.doc["semantic_layer"]
    assert rs.member_names == ["finance", "sales", "platform"]
    names = {t["name"] for t in sl["tables"]}
    assert names == {"finance/fct_gl", "finance/fct_ap_batch", "sales/fct_orders",
                     "sales/dim_customer", "platform/dim_cost_center", "platform/dim_region"}
    # source is untouched: it is what SQL uses
    assert {t["source"] for t in sl["tables"]} >= {"finance.fct_gl", "platform.dim_region"}
    # a member's internal references are rewritten, the seam's are left as written
    rels = {r["name"]: r for r in sl["relationships"]}
    assert rels["gl_to_batch"]["to"]["table"] == "finance/fct_ap_batch"
    assert rels["gl_cc"]["from"]["table"] == "finance/fct_gl"
    fk = next(c for t in sl["tables"] if t["name"] == "sales/fct_orders"
              for c in t["columns"] if c["name"] == "customer_id")["foreign_key"]
    assert fk["references"] == "sales/dim_customer.customer_id"
    metrics = {m["name"]: m for m in sl["metrics"]}
    assert metrics["sales/net_revenue"]["measure"] == "sales/fct_orders.net_revenue"
    assert metrics["sales/net_revenue"]["dimensions"] == ["sales/dim_customer.segment"]
    hier = sl["hierarchies"][0]
    assert hier["name"] == "platform/cost_center_geo"
    assert hier["levels"][0]["column"] == "platform/dim_region.region_name"
    assert sl["repo_knowledge"]["routing"][0]["use"][0] == "sales/fct_orders"
    # and the ordinary validator accepts the whole, with the set's own rules
    assert validate_document(rs.doc).ok, validate_document(rs.doc).errors
    assert set_checks(rs) == []
    assert validate_file(hub).ok


def test_a_hub_without_members_resolves_to_itself(tmp_path):
    _write(tmp_path, finance=FINANCE)
    rs = load_set(tmp_path / "finance.yaml")
    assert rs.doc == FINANCE and rs.member_names == []


def test_dangling_seam_reference_is_an_error(tmp_path):
    bad = yaml.safe_load(yaml.safe_dump(ACME))
    bad["semantic_layer"]["relationships"][0]["to"]["table"] = "platform/dim_cost_centre"
    hub = _write(tmp_path, finance=FINANCE, sales=SALES, platform=PLATFORM, acme=bad)
    r = validate_file(hub)
    assert not r.ok and any("platform/dim_cost_centre" in e for e in r.errors)


def test_member_name_must_match_and_be_unique(tmp_path):
    renamed = yaml.safe_load(yaml.safe_dump(SALES))
    renamed["semantic_layer"]["name"] = "revenue"
    hub = _write(tmp_path, finance=FINANCE, sales=renamed, platform=PLATFORM, acme=ACME)
    r = validate_file(hub)
    assert any("calls itself 'revenue'" in e for e in r.errors)
    dup = yaml.safe_load(yaml.safe_dump(ACME))
    dup["semantic_layer"]["members"][1] = {"name": "finance", "path": "finance.yaml"}
    dup["semantic_layer"]["relationships"] = dup["semantic_layer"]["relationships"][:1]
    dup["semantic_layer"].pop("repo_knowledge")
    hub = _write(tmp_path, acme=dup)
    assert any("not unique" in e for e in validate_file(hub).errors)


def test_missing_member_file_is_a_set_error(tmp_path):
    hub = _write(tmp_path, finance=FINANCE, sales=SALES, acme=ACME)   # no platform.yaml
    with pytest.raises(SetError, match="platform"):
        load_set(hub)
    r = validate_file(hub)
    assert not r.ok and r.errors[0].startswith("set: ")


def test_two_members_may_not_describe_one_source_in_one_warehouse(tmp_path):
    copycat = yaml.safe_load(yaml.safe_dump(SALES))
    copycat["semantic_layer"]["name"] = "sales2"
    copycat["semantic_layer"]["tables"][1]["source"] = "platform.dim_region"
    hub_doc = yaml.safe_load(yaml.safe_dump(ACME))
    hub_doc["semantic_layer"]["members"].append({"name": "sales2", "path": "sales2.yaml"})
    hub = _write(tmp_path, finance=FINANCE, sales=SALES, sales2=copycat, platform=PLATFORM,
                 acme=hub_doc)
    errors = validate_file(hub).errors
    assert any("platform.dim_region is described by both" in e for e in errors), errors


def test_cross_warehouse_relationship_needs_a_federation_engine(tmp_path):
    lake = yaml.safe_load(yaml.safe_dump(FINANCE))
    lake["semantic_layer"]["warehouse"] = "duckdb:lake.duckdb"
    hub_doc = yaml.safe_load(yaml.safe_dump(ACME))
    hub = _write(tmp_path, finance=lake, sales=SALES, platform=PLATFORM, acme=hub_doc)
    errors = validate_file(hub).errors
    assert any("spans warehouses duckdb:lake.duckdb and snowflake:ANALYTICS" in e for e in errors)
    hub_doc["semantic_layer"]["federation"] = {"engine": "duckdb", "bridge": "bridge.duckdb"}
    hub = _write(tmp_path, acme=hub_doc)
    assert validate_file(hub).ok


@pytest.mark.parametrize("fixture", sorted(p.stem for p in BUILD.glob("*.duckdb")))
def test_every_fixture_layer_survives_qualification(fixture):
    """Qualify a real layer and validate it: a reference site the qualifier misses dangles here."""
    gold = OSS / "fixtures" / "golds" / f"{fixture}.yaml"
    if not gold.exists():
        pytest.skip(f"no gold layer for {fixture}")
    doc = load_document(gold)
    before = validate_document(doc)
    if not before.ok:
        pytest.skip(f"gold layer for {fixture} does not validate on its own")
    doc["semantic_layer"] = {**doc["semantic_layer"], **qualify(doc["semantic_layer"], fixture)}
    after = validate_document(doc)
    assert after.ok, after.errors[:5]
    assert all(t["name"].startswith(f"{fixture}/") for t in doc["semantic_layer"]["tables"])


def test_a_hub_cannot_yet_be_a_member_and_names_carry_no_slash(tmp_path):
    """Two doors held open for hubs of hubs: a hub member is refused, not silently
    empty, and `/` never appears in a document name."""
    from semlayer.setdoc import resolve
    leaf = {"semantic_layer": {"spec_version": "0.6.0", "name": "finance", "tables": []}}
    inner = {"semantic_layer": {"spec_version": "0.6.0", "name": "emea",
                                "members": [{"name": "finance", "path": "finance.yaml"}]}}
    top = {"semantic_layer": {"spec_version": "0.6.0", "name": "acme",
                              "members": [{"name": "emea", "path": "emea.yaml"}]}}
    rs = resolve(top, {"emea": inner})
    assert any("cannot yet be a member" in p for p in rs.problems)
    slashed = {"semantic_layer": {"spec_version": "0.6.0", "name": "emea/finance", "tables": []}}
    top2 = {"semantic_layer": {"spec_version": "0.6.0", "name": "acme",
                               "members": [{"name": "emea/finance", "path": "x.yaml"}]}}
    rs = resolve(top2, {"emea/finance": slashed})
    assert any("must not contain '/'" in p for p in rs.problems)
    assert not resolve(top, {"emea": leaf | {"semantic_layer": {**leaf["semantic_layer"],
                                                                 "name": "emea"}}}).problems
