"""tpcds_clean gold fixture tests.

Checks (in addition to the generic gold-validates-via-schema test in
test_spec.py, which already covers this file via the GOLDS glob):

1. The gold's table names exactly match `information_schema.tables` of a
   freshly-generated sf=0.01 TPC-DS database (no missing, no extra tables).
2. For every table, the gold's column names exactly match
   `information_schema.columns` for that table (no missing, no extra columns).
   This is the check that catches typos in a 24-table hand-authored file.
"""

import sys
from pathlib import Path

import duckdb
import pytest
import yaml

from semlayer.validate import validate_file

OSS = Path(__file__).resolve().parent.parent
GOLD_PATH = OSS / "fixtures" / "golds" / "tpcds_clean.yaml"
sys.path.insert(0, str(OSS / "fixtures"))
from generators import tpcds_clean  # noqa: E402


@pytest.fixture(scope="module")
def con():
    con = duckdb.connect(":memory:")
    tpcds_clean.build(con)
    yield con
    con.close()


@pytest.fixture(scope="module")
def gold_doc() -> dict:
    return yaml.safe_load(GOLD_PATH.read_text())


@pytest.fixture(scope="module")
def db_schema(con) -> dict[str, set[str]]:
    rows = con.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_catalog = current_database() AND table_schema = 'main'"
    ).fetchall()
    schema: dict[str, set[str]] = {}
    for table_name, column_name in rows:
        schema.setdefault(table_name, set()).add(column_name)
    return schema


def test_gold_validates_via_semlayer():
    result = validate_file(GOLD_PATH)
    assert result.ok, "\n".join(result.errors)


def test_gold_covers_all_24_tpcds_tables(db_schema):
    assert len(db_schema) == 24, f"expected 24 TPC-DS tables in the generated db, found {len(db_schema)}"


def test_gold_table_names_match_database_exactly(gold_doc, db_schema):
    gold_tables = {t["name"] for t in gold_doc["semantic_layer"]["tables"]}
    db_tables = set(db_schema)
    missing = db_tables - gold_tables
    extra = gold_tables - db_tables
    assert not missing, f"gold is missing tables present in the database: {sorted(missing)}"
    assert not extra, f"gold declares tables not present in the database: {sorted(extra)}"


@pytest.mark.parametrize(
    "table_name",
    [
        "call_center", "catalog_page", "catalog_returns", "catalog_sales", "customer",
        "customer_address", "customer_demographics", "date_dim", "household_demographics",
        "income_band", "inventory", "item", "promotion", "reason", "ship_mode", "store",
        "store_returns", "store_sales", "time_dim", "warehouse", "web_page", "web_returns",
        "web_sales", "web_site",
    ],
)
def test_gold_column_names_match_database_exactly(table_name, gold_doc, db_schema):
    tables_by_name = {t["name"]: t for t in gold_doc["semantic_layer"]["tables"]}
    assert table_name in tables_by_name, f"gold is missing table '{table_name}'"
    gold_columns = {c["name"] for c in tables_by_name[table_name]["columns"]}
    db_columns = db_schema[table_name]
    missing = db_columns - gold_columns
    extra = gold_columns - db_columns
    assert not missing, f"{table_name}: gold is missing columns {sorted(missing)}"
    assert not extra, f"{table_name}: gold declares nonexistent columns {sorted(extra)}"


def test_gold_source_matches_table_name(gold_doc):
    for t in gold_doc["semantic_layer"]["tables"]:
        assert t["source"] == f"main.{t['name']}", (
            f"table {t['name']}: source '{t['source']}' does not follow the 'main.<table>' convention"
        )


def test_fact_tables_have_composite_primary_keys(gold_doc):
    facts = {
        "store_sales", "store_returns", "catalog_sales", "catalog_returns",
        "web_sales", "web_returns", "inventory",
    }
    tables_by_name = {t["name"]: t for t in gold_doc["semantic_layer"]["tables"]}
    for name in facts:
        pk = tables_by_name[name].get("primary_key")
        assert pk and len(pk) >= 2, f"{name}: expected a composite primary_key, got {pk}"


def test_star_join_relationships_present_for_core_facts(gold_doc):
    rels = gold_doc["semantic_layer"]["relationships"]
    from_tables = {r["from"]["table"] for r in rels}
    for fact in ("store_sales", "web_sales", "catalog_sales", "inventory"):
        assert fact in from_tables, f"missing at least one relationship from fact table '{fact}'"
        fact_rels = [r for r in rels if r["from"]["table"] == fact]
        assert all(r["cardinality"] == "many_to_one" for r in fact_rels)
        assert all(r.get("fanout_risk") is False for r in fact_rels)


def test_no_confidence_or_lifecycle_fields_in_clean_gold(gold_doc):
    """Hand-authored gold: clean data, no inference artifacts (per fixture intent)."""

    def walk(obj):
        if isinstance(obj, dict):
            assert "confidence" not in obj, "clean gold must not carry confidence fields"
            assert obj.get("lifecycle") not in {"deprecated", "orphaned"}
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(gold_doc["semantic_layer"])


def test_returns_rate_ratio_metric_declares_input_metrics(gold_doc):
    metrics = {m["name"]: m for m in gold_doc["semantic_layer"]["metrics"]}
    assert "returns_rate" in metrics
    ratio = metrics["returns_rate"]
    assert ratio["type"] == "ratio"
    assert ratio["numerator"] in metrics
    assert ratio["denominator"] in metrics
