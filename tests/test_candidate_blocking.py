# SPDX-License-Identifier: FSL-1.1-ALv2
"""ParentIndex.ranked() must reproduce the brute-force sort exactly.

The index scores only parents that share something with the child by name
and appends the rest in list order. That is the same order as
`sorted(parents, key=name_similarity, reverse=True)` only if (a) every
parent that scores above zero is inside the block and (b) the tail keeps
its position order, which the stable sort guarantees. (a) is a claim about
`name_similarity`'s branches, so it is checked here against the brute force
on every built fixture and on a hand-picked set of the names that exercised
those branches when they were written (PascalCase keys, TPC-DS alias
prefixes, fused initials, bare `id`).
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest

OSS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OSS / "src"))

from semlayer.link.candidates import (  # noqa: E402
    ParentIndex,
    _type_family,
    name_similarity,
    select_parents_and_children,
)
from semlayer.profile.run import profile_with_stats  # noqa: E402
from semlayer.source import DuckDBSource  # noqa: E402

BUILD = OSS / "fixtures" / "build"
FIXTURES = ["messy_mart", "tpcds_clean", "collision_heavy", "fan_trap", "multi_tenant",
            "self_ref", "snapshot_noval"]


def _brute(parents, ccn, ctn):
    return sorted(parents, key=lambda p: name_similarity(ccn, ctn, p[1], p[0]), reverse=True)


@pytest.mark.parametrize("fixture", FIXTURES)
def test_index_matches_brute_force_on_fixture(fixture):
    db = BUILD / f"{fixture}.duckdb"
    if not db.exists():
        pytest.skip(f"{db.name} not built")
    con = duckdb.connect(str(db), read_only=True)
    try:
        doc, stats = profile_with_stats(DuckDBSource(con))
    finally:
        con.close()
    parents, children = select_parents_and_children(stats, doc)
    assert parents and children
    index = ParentIndex(parents, stats)
    checked = 0
    for ctn, ccn in children:
        brute = _brute(parents, ccn, ctn)
        assert index.ranked(ccn, ctn) == brute, (fixture, ctn, ccn)
        fam = _type_family(stats[ctn].columns[ccn].sql_type)
        assert index.ranked(ccn, ctn, family=fam) == [
            p for p in brute if _type_family(stats[p[0]].columns[p[1]].sql_type) == fam
        ], (fixture, ctn, ccn, fam)
        checked += 1
    assert checked == len(children)


class _Col:
    def __init__(self, sql_type):
        self.sql_type = sql_type


class _Table:
    def __init__(self, cols):
        self.columns = {c: _Col(t) for c, t in cols.items()}


# (table, column, sql type). Names chosen for the branches of name_similarity:
# exact match, PascalCase folding, TPC-DS alias prefix + core suffix, fused
# initials, token-tail with abbreviation, bare id, no-underscore legacy names,
# and parents that share nothing with any child.
_PARENTS = [
    ("customers", "customer_id", "INTEGER"), ("DimDate", "DateKey", "INTEGER"),
    ("date_dim", "d_date_sk", "BIGINT"), ("household_demographics", "hd_demo_sk", "INTEGER"),
    ("customer_address", "ca_address_sk", "INTEGER"), ("ids", "id", "INTEGER"),
    ("CUST_MSTR", "CUSTNO", "INTEGER"), ("legacy", "billcustomerkey", "INTEGER"),
    ("products", "product_id", "INTEGER"), ("stores", "store_code", "VARCHAR"),
    ("colours", "colour_cd", "VARCHAR"), ("things", "thing_uuid", "VARCHAR"),
    ("dim_region", "region_id", "INTEGER"), ("dim_region", "sk_region", "INTEGER"),
]
_CHILDREN = [
    ("orders", "customer_id", "INTEGER"), ("FactSales", "DueDateKey", "INTEGER"),
    ("FactSales", "OrderDateKey", "INTEGER"), ("web_sales", "ws_bill_hdemo_sk", "INTEGER"),
    ("catalog_sales", "cs_bill_addr_sk", "INTEGER"), ("links", "id", "INTEGER"),
    ("ORD_HDR", "CUST_NO", "INTEGER"), ("legacy_orders", "billcustomerkey", "INTEGER"),
    ("order_items", "prod_id", "INTEGER"), ("visits", "store_cd", "VARCHAR"),
    ("shoes", "shoe_size", "INTEGER"), ("dly_sls_agg", "agg_dt_key", "INTEGER"),
    ("fct_orders", "region_id_x", "INTEGER"), ("fct_orders", "sk_region", "INTEGER"),
    ("events", "customer_id_2", "INTEGER"), ("misc", "note_txt", "VARCHAR"),
]


def test_index_matches_brute_force_on_branch_names():
    stats = {}
    for tn, cn, ty in _PARENTS + _CHILDREN:
        stats.setdefault(tn, {})[cn] = ty
    stats = {tn: _Table(cols) for tn, cols in stats.items()}
    parents = [(tn, cn) for tn, cn, _ in _PARENTS]
    index = ParentIndex(parents, stats)
    for ctn, ccn, ty in _CHILDREN:
        brute = _brute(parents, ccn, ctn)
        assert index.ranked(ccn, ctn) == brute, (ctn, ccn)
        fam = _type_family(ty)
        assert index.ranked(ccn, ctn, family=fam) == [
            p for p in brute if _type_family(stats[p[0]].columns[p[1]].sql_type) == fam]
    # and the block really is a block: at least one child leaves most parents out
    assert min(len(index.block(ccn, ctn)) for ctn, ccn, _ in _CHILDREN) < len(parents) // 2
