"""Duplicate-group detection: the largest single saving in the planner.

Measured across 287 real warehouses: 67% of an analytics warehouse's tables
are versioned or partitioned copies (ga360 100%, ebi_chembl 94%), against ~0%
of an application schema (one ERP's 799 tables: zero). Profiling the four
hundredth copy of a table whose shape you already have is waste.

The tests that matter here are the NEGATIVE ones. Signature identity alone is
not duplication, and a rule that collapses same-shaped tables is destructive
rather than merely imprecise -- it discards meaning that cannot be recovered.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from semlayer.probe import _copy_stem, duplicate_groups, representative


@dataclass
class _Col:
    name: str


@dataclass
class _Tab:
    name: str
    columns: list


def T(name, *cols):
    return _Tab(name, [_Col(c) for c in cols])


# --- what must group -------------------------------------------------

def test_versioned_copies_group():
    """The ChEMBL pattern, verbatim from the corpus."""
    g = duplicate_groups([
        T("ATC_CLASSIFICATION", "level1", "level2"),
        T("ATC_CLASSIFICATION_23", "level1", "level2"),
        T("ATC_CLASSIFICATION_30", "level1", "level2"),
    ])
    assert len(g) == 1 and len(g[0]) == 3


def test_date_partitioned_copies_group():
    """The GA360 pattern: one table per day, identical shape."""
    g = duplicate_groups([
        T(f"GA_SESSIONS_2016112{i}", "visitId", "fullVisitorId") for i in range(5)
    ])
    assert len(g) == 1 and len(g[0]) == 5


@pytest.mark.parametrize("names", [
    ("orders", "orders_v2"),
    ("orders", "orders_backup"),
    ("orders", "orders_2023"),
    ("orders", "orders_old"),
    ("orders", "ordersV2"),          # no separator
])
def test_copy_suffixes_group(names):
    g = duplicate_groups([T(n, "id", "total") for n in names])
    assert len(g) == 1, f"{names} should be one group"


# --- what must NOT group, which is the point -------------------------

def test_same_shape_different_meaning_does_not_group():
    """Mooney_Family: 67 tables all exactly (name1, name2). aunt and uncle are
    distinct relations; collapsing them discards 66 tables of meaning."""
    g = duplicate_groups([
        T("aunt", "name1", "name2"),
        T("uncle", "name1", "name2"),
        T("brother", "name1", "name2"),
    ])
    assert g == []


def test_same_stem_different_shape_does_not_group():
    """A table that gained a column is not a copy of the one that did not."""
    g = duplicate_groups([
        T("orders", "id", "total"),
        T("orders_v2", "id", "total", "currency"),
    ])
    assert g == []


def test_an_application_schema_finds_nothing():
    """An ERP schema: 0 of 799. A rule that fires here is a false positive."""
    g = duplicate_groups([
        T("res_users", "id", "login"),
        T("res_partner", "id", "name"),
        T("account_move", "id", "ref"),
        T("account_move_line", "id", "move_id"),
    ])
    assert g == []


# --- the stem rule ---------------------------------------------------

@pytest.mark.parametrize("name,stem", [
    ("ATC_CLASSIFICATION_30", "atc_classification"),
    ("GA_SESSIONS_20161121", "ga_sessions"),
    ("orders_2023_v2", "orders"),          # two passes
    ("orders_backup", "orders"),
    ("aunt", "aunt"),
    ("uncle", "uncle"),
    ("schema.orders_v1", "orders"),        # schema stripped
])
def test_copy_stem(name, stem):
    assert _copy_stem(name) == stem


def test_groups_are_largest_first():
    g = duplicate_groups(
        [T("a", "x"), T("a_1", "x")] +
        [T(f"b_{i}", "y") for i in range(4)] + [T("b", "y")]
    )
    assert [len(x) for x in g] == [5, 2]


# --- which member gets profiled --------------------------------------

def _rep(*tables):
    g = duplicate_groups(list(tables))
    assert len(g) == 1, "fixture should form exactly one group"
    return representative(g[0]).name


@pytest.mark.parametrize("names,expected", [
    # The unsuffixed member is canonical: an annual archive beside a live
    # table is the common shape, not a v2 that left the plain name behind.
    (("orders", "orders_2023"), "orders"),
    (("orders", "orders_v2"), "orders"),
    # A dead marker is never chosen while anything else is available.
    (("orders", "orders_backup"), "orders"),
    (("orders_v2", "orders_backup"), "orders_v2"),
    # No unsuffixed member: most recent wins, which for dates and version
    # numbers is the lexicographically greatest name.
    (("ga_sessions_20161121", "ga_sessions_20161125"), "ga_sessions_20161125"),
    (("atc_classification_23", "atc_classification_30"), "atc_classification_30"),
    # Everything is dead: still has to return one of them.
    (("orders_old", "orders_backup"), "orders_old"),
])
def test_representative(names, expected):
    assert _rep(*(T(n, "id", "total") for n in names)) == expected


def test_representative_is_a_member_of_the_group():
    """Whatever the rules do, the answer must be a table we actually saw."""
    tabs = [T(f"snap_{i}", "a", "b") for i in range(6)]
    g = duplicate_groups(tabs)
    assert representative(g[0]) in g[0]
