"""Two schemas holding the same bare table name produce two distinct tables, not a crash."""

import duckdb

from semlayer.pipeline import infer
from semlayer.source import DuckDBSource
from semlayer.validate import validate_document


def _warehouse():
    con = duckdb.connect(":memory:")
    for schema in ("crm", "erp"):
        con.execute(f"create schema {schema}")
        con.execute(f"create table {schema}.dim_region(region_id integer, region_name varchar)")
        con.execute(f"insert into {schema}.dim_region select i, 'r' || i from range(1, 41) t(i)")
    # erp has a column crm does not: this is the column Link crashed on
    con.execute("alter table erp.dim_region add column segment varchar")
    con.execute("create table erp.fct_orders(order_id integer, region_id integer, amount decimal(10,2))")
    con.execute("insert into erp.fct_orders select i, (i % 40) + 1, i * 1.5 from range(1, 2001) t(i)")
    con.execute("create table crm.dim_account(account_id integer, account_name varchar)")
    con.execute("insert into crm.dim_account select i, 'a' || i from range(1, 21) t(i)")
    return con


def test_colliding_names_are_qualified_and_the_rest_stay_bare():
    con = _warehouse()
    try:
        doc = infer(DuckDBSource(con), llm=None)
    finally:
        con.close()
    names = sorted(t["name"] for t in doc["semantic_layer"]["tables"])
    assert names == ["crm__dim_region", "dim_account", "erp__dim_region", "fct_orders"]
    by = {t["name"]: t for t in doc["semantic_layer"]["tables"]}
    assert by["crm__dim_region"]["source"] == "crm.dim_region"
    assert "segment" in {c["name"] for c in by["erp__dim_region"]["columns"]}
    assert "segment" not in {c["name"] for c in by["crm__dim_region"]["columns"]}
    assert validate_document(doc).ok
    # the fact's region key resolved to ONE of the two regions, by document name
    fk = next(c for c in by["fct_orders"]["columns"] if c["name"] == "region_id")
    assert "__dim_region." in fk.get("foreign_key", {}).get("references", "")
