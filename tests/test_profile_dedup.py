"""Profiling skips duplicate copies and says so in `coverage`.

Measured across 287 real warehouses: 67% of an analytics warehouse's tables
are versioned or partitioned copies. Skipping them is the largest single
saving in the planner -- but only if the document still records that they
exist, because an omission is indistinguishable from "no such table".
"""

import duckdb
import pytest

from semlayer.profile import profile_source
from semlayer.source import DuckDBSource
from semlayer.validate import validate_document


def _source(*names):
    con = duckdb.connect(":memory:")
    for n in names:
        con.execute(f"create table {n} (order_id integer, total double)")
        con.execute(f"insert into {n} values (1, 10.0), (2, 20.0)")
    return con


@pytest.fixture
def copies():
    con = _source("orders", "orders_v2", "orders_2023", "customers")
    yield DuckDBSource(con)
    con.close()


def test_only_the_representative_is_profiled(copies):
    doc = profile_source(copies)["semantic_layer"]
    assert sorted(t["name"] for t in doc["tables"]) == ["customers", "orders"]


def test_coverage_records_what_was_skipped(copies):
    cov = profile_source(copies)["semantic_layer"]["coverage"]
    assert cov["tables_seen"] == 4
    assert cov["tables_modelled"] == 2
    skipped = {s["name"]: s for s in cov["skipped"]}
    assert set(skipped) == {"main.orders_2023", "main.orders_v2"}
    assert all(s["reason"] == "duplicate" for s in skipped.values())
    assert all(s["same_shape_as"] == "main.orders" for s in skipped.values())


def test_the_arithmetic_closes(copies):
    cov = profile_source(copies)["semantic_layer"]["coverage"]
    assert cov["tables_seen"] - cov["tables_modelled"] == len(cov["skipped"])


def test_still_spec_conformant(copies):
    result = validate_document(profile_source(copies))
    assert result.errors == [], result.errors


def test_only_disables_dedup():
    """--only IS the scope. Dropping one of two tables named by hand would be
    wrong however duplicated they look."""
    con = _source("orders", "orders_v2")
    try:
        doc = profile_source(DuckDBSource(con),
                             only={"orders", "orders_v2"})["semantic_layer"]
        assert sorted(t["name"] for t in doc["tables"]) == ["orders", "orders_v2"]
        assert "skipped" not in doc["coverage"]
    finally:
        con.close()


def test_a_schema_with_no_copies_is_untouched():
    """An ERP schema: 0 of 799. The block is emitted, and claims nothing."""
    con = _source("res_users", "account_move")
    try:
        cov = profile_source(DuckDBSource(con))["semantic_layer"]["coverage"]
        assert cov == {"tables_seen": 2, "tables_modelled": 2}
    finally:
        con.close()


# --- the consumer side (SPEC.md §2.12) -------------------------------

def _doc_with_coverage():
    return {"semantic_layer": {
        "spec_version": "0.4.0", "name": "t",
        "tables": [{"name": "orders", "columns": [{"name": "order_id"}]}],
        "coverage": {"tables_seen": 3, "tables_modelled": 1, "skipped": [
            {"name": "main.orders_v2", "reason": "duplicate",
             "same_shape_as": "main.orders"},
            {"name": "main.staging_tmp", "reason": "filtered"},
        ]},
    }}


def test_a_skipped_table_is_not_reported_as_unknown():
    """The one answer §2.12 forbids: an agent told 'no such table' concludes
    the warehouse does not have it, and stops looking."""
    from semlayer.mcp_server import get_table
    got = get_table(_doc_with_coverage(), "orders_v2")
    assert "error" not in got
    assert got["NOT_MODELLED"] is True
    assert got["same_shape_as"] == "main.orders"


def test_skipped_lookup_accepts_either_spelling():
    from semlayer.mcp_server import get_table
    doc = _doc_with_coverage()
    for name in ("main.orders_v2", "orders_v2", "ORDERS_V2"):
        assert get_table(doc, name).get("NOT_MODELLED") is True, name


def test_a_skipped_table_without_a_twin_says_only_that_it_exists():
    from semlayer.mcp_server import get_table
    got = get_table(_doc_with_coverage(), "staging_tmp")
    assert got["reason"] == "filtered"
    assert "same_shape_as" not in got and "guidance" not in got


def test_a_genuinely_absent_table_is_still_unknown():
    from semlayer.mcp_server import get_table
    assert "error" in get_table(_doc_with_coverage(), "no_such_thing")


def test_no_coverage_block_still_answers_unknown():
    from semlayer.mcp_server import get_table
    doc = {"semantic_layer": {"spec_version": "0.1.0", "name": "t",
                              "tables": [{"name": "orders", "columns": []}]}}
    assert "error" in get_table(doc, "orders_v2")


# --- declared partitions are not duplicates --------------------------

class _Part:
    def __init__(self, schema, name):
        self.schema, self.name = schema, name


class _PartitionedSource:
    """A source whose catalogue declares `evt_2024_0N` as partitions of `evt`.

    Shaped like the Postgres connector's contract: `list_partitions()` keyed by
    (schema, parent) with child objects carrying schema and name.
    """

    def __init__(self, inner, parent, children):
        self._inner, self._parent, self._children = inner, parent, children

    def __getattr__(self, item):
        return getattr(self._inner, item)

    def list_partitions(self):
        return {("main", self._parent): [_Part("main", c) for c in self._children]}


def _partitioned():
    con = _source("evt", "evt_2024_01", "evt_2024_02", "evt_2024_03")
    return con, _PartitionedSource(DuckDBSource(con), "evt",
                                   ["evt_2024_01", "evt_2024_02", "evt_2024_03"])


def test_partitions_are_profiled_as_ordinary_tables():
    """`--state` off must mean what it always meant. A partition child looks
    exactly like a copy, and absorbing it here would silently change the
    default path for every Postgres user with a partitioned table."""
    con, src = _partitioned()
    try:
        doc = profile_source(src)["semantic_layer"]
        assert {t["name"] for t in doc["tables"]} == {
            "evt", "evt_2024_01", "evt_2024_02", "evt_2024_03"}
        assert "skipped" not in doc["coverage"]
    finally:
        con.close()


def test_a_real_copy_beside_partitions_is_still_skipped():
    """Exempting partitions must not exempt the whole warehouse."""
    con = _source("evt", "evt_2024_01", "orders", "orders_v2")
    src = _PartitionedSource(DuckDBSource(con), "evt", ["evt_2024_01"])
    try:
        doc = profile_source(src)["semantic_layer"]
        names = {t["name"] for t in doc["tables"]}
        assert "evt_2024_01" in names
        assert "orders_v2" not in names
        assert [s["name"] for s in doc["coverage"]["skipped"]] == ["main.orders_v2"]
    finally:
        con.close()


def test_a_source_that_cannot_list_partitions_still_dedups():
    """DuckDB has no `list_partitions`; the exemption must not require one."""
    con = _source("orders", "orders_v2")
    try:
        doc = profile_source(DuckDBSource(con))["semantic_layer"]
        assert {t["name"] for t in doc["tables"]} == {"orders"}
    finally:
        con.close()
