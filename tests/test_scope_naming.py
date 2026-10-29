# SPDX-License-Identifier: FSL-1.1-ALv2
"""--schema scopes the run, names the document and the file; documents say their warehouse.

Three teams each scoping to their own schema must get three differently
named documents in three differently named files without asking, because
the document's `name` is what other documents reference once the layers
are combined, and the file name is what a platform engineer sees.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest

OSS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OSS / "src"))

from semlayer.cli import default_out  # noqa: E402
from semlayer.errors import SemlayerError  # noqa: E402
from semlayer.pipeline import InferOptions, infer_with_report  # noqa: E402
from semlayer.profile.run import profile_with_stats  # noqa: E402
from semlayer.source import DuckDBSource, warehouse_uri  # noqa: E402
from semlayer.validate import validate_document  # noqa: E402


def _two_schemas(path: Path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(path))
    con.execute("create schema finance; create schema sales")
    con.execute("create table finance.fct_gl(gl_line_id integer, amount double, cost_center_id integer)")
    con.execute("insert into finance.fct_gl select i, i * 1.5, i % 7 from range(200) t(i)")
    con.execute("create table sales.fct_orders(order_id integer, net_revenue double, cost_center_id integer)")
    con.execute("insert into sales.fct_orders select i, i * 2.0, i % 7 from range(300) t(i)")
    con.execute("create table sales.dim_customer(customer_id integer, customer_name varchar)")
    con.execute("insert into sales.dim_customer select i, 'c' || i from range(50) t(i)")
    return con


def test_schema_scopes_the_run_and_names_the_document(tmp_path):
    con = _two_schemas(tmp_path / "acme.duckdb")
    try:
        doc, _ = profile_with_stats(DuckDBSource(con), schema={"finance"})
    finally:
        con.close()
    sl = doc["semantic_layer"]
    assert [t["name"] for t in sl["tables"]] == ["fct_gl"]
    assert sl["tables"][0]["source"] == "finance.fct_gl"
    assert sl["name"] == "finance"
    assert sl["warehouse"] == "duckdb:acme.duckdb"      # basename, not the path: portable
    assert validate_document(doc).ok


def test_name_overrides_and_two_schemas_do_not_pick_one(tmp_path):
    con = _two_schemas(tmp_path / "acme.duckdb")
    try:
        doc, _ = profile_with_stats(DuckDBSource(con), schema={"finance", "sales"})
        named, _ = profile_with_stats(DuckDBSource(con), schema={"sales"}, name="revenue")
    finally:
        con.close()
    assert doc["semantic_layer"]["name"] == "profiled"
    assert len(doc["semantic_layer"]["tables"]) == 3
    assert named["semantic_layer"]["name"] == "revenue"
    assert [t["name"] for t in named["semantic_layer"]["tables"]] == ["dim_customer", "fct_orders"]


def test_unknown_schema_is_an_error_not_an_empty_layer(tmp_path):
    con = _two_schemas(tmp_path / "acme.duckdb")
    try:
        with pytest.raises(SemlayerError, match="marketing"):
            profile_with_stats(DuckDBSource(con), schema={"marketing"})
    finally:
        con.close()


def test_options_carry_schema_and_name_into_the_report(tmp_path):
    con = _two_schemas(tmp_path / "acme.duckdb")
    try:
        doc, report = infer_with_report(DuckDBSource(con), options=InferOptions(
            schema="finance", use_declared=False, query_workers=1))
    finally:
        con.close()
    assert report["options"]["schema"] == ["finance"]
    assert report["options"]["name"] is None
    assert doc["semantic_layer"]["name"] == "finance"
    assert InferOptions(schema=["a", "b"]).schema == frozenset({"a", "b"})


def test_output_file_follows_the_schema():
    assert default_out(None, {"finance"}) == "finance.yaml"
    assert default_out(None, {"finance", "sales"}) == "semantic_layer.yaml"
    assert default_out(None, None) == "semantic_layer.yaml"
    assert default_out("layer.yaml", {"finance"}) == "layer.yaml"


def test_warehouse_uri_is_credential_free_and_portable():
    con = duckdb.connect()
    try:
        assert warehouse_uri(DuckDBSource(con)) == "duckdb:memory"
    finally:
        con.close()

    class SnowflakeSource:
        database = "ANALYTICS"

    class BigQuerySource:
        project = "acme-dw"

    class Other:
        pass
    assert warehouse_uri(SnowflakeSource()) == "snowflake:ANALYTICS"
    assert warehouse_uri(BigQuerySource()) == "bigquery:acme-dw"
    assert warehouse_uri(Other()) == "other"
