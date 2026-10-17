"""probe's own guarantees: it must not leak, and its grade must mean something.

The accuracy claim -- that the grade predicts what the engine achieves -- is
measured in test_probe_grade_predicts_accuracy.py, which runs two full
inferences and is opt-in. These are the cheap invariants.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb
import pytest

from semlayer.probe import GRADE_CONFIDENCE_CAP, Probe, render, tier1, tier2
from semlayer.source import DuckDBSource

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))


@pytest.fixture
def wide(monkeypatch):
    monkeypatch.setenv("SEMLAYER_WIDE_TABLES", "120")
    from generators import wide as gen

    def make(opaque: bool):
        con = duckdb.connect(":memory:")
        (gen.build_opaque if opaque else gen.build)(con)
        return con

    return make


def test_telemetry_carries_no_identifier(wide):
    """Not names, and not hashes of names either.

    A hash still carries the shape of a schema -- how many distinct tables,
    which repeat -- and a user who allowed usage pings did not agree to that
    being described. The payload is counts, fractions and booleans, and the
    CLI prints it so the claim is checkable rather than promised.
    """
    con = wide(opaque=True)
    try:
        payload = json.dumps(tier1(DuckDBSource(con)).telemetry())
    finally:
        con.close()
    for leak in ("T_0", "MSTR", "CUST", "EVEN", "_id", "scratch"):
        assert leak.lower() not in payload.lower(), f"identifier leaked: {leak}"
    assert all(isinstance(v, (int, float, bool, str)) for v in json.loads(payload).values())


def test_naming_convention_changes_the_grade(wide):
    """The same warehouse, named two ways, must not grade the same."""
    con = wide(opaque=False)
    try:
        conventional = tier1(DuckDBSource(con))
    finally:
        con.close()
    con = wide(opaque=True)
    try:
        opaque = tier1(DuckDBSource(con))
    finally:
        con.close()

    assert conventional.stage_prefix_ratio > 0.3
    assert opaque.stage_prefix_ratio == 0.0
    assert conventional.grade != opaque.grade
    assert conventional.confidence_cap > opaque.confidence_cap


def test_declared_primary_keys_alone_do_not_earn_a_strong_grade():
    """A PK says a column identifies rows in ITS OWN table.

    It says nothing about which of three copies a child column meant, and
    warehouses routinely declare primary keys without foreign keys. An earlier
    version counted them together and graded a 2,000-table warehouse `strong`
    on 54 declared PKs and no FKs at all.
    """
    p = Probe(tables=2000, columns=15000, declared_pks=54, declared_fks=0)
    assert p.grade == "weak"
    p.declared_fks = 200
    assert p.grade == "strong"


def test_tier2_sees_shared_key_sets_whatever_they_are_called(wide):
    """Identity by value, not by name -- which is the entire point of tier 2."""
    con = wide(opaque=True)
    try:
        src = DuckDBSource(con)
        p = tier2(src, tier1(src))
    finally:
        con.close()
    assert p.tier2_ran and p.sampled_tables > 0
    assert p.shared_key_set_ratio > 0.2, "the fixture builds three copies of every entity"


def test_render_says_what_is_missing():
    p = Probe(tables=10, columns=50)
    out = render(p)
    assert "NO" in out and "not measured" in out
    assert f"{GRADE_CONFIDENCE_CAP['weak']:.2f}" in out


# --- what the real warehouses taught it ------------------------------------
# Each of these encodes a case where probe was WRONG on a real schema. They
# are unit-level so they run in CI; the warehouses that produced them are
# acquired and cannot ship.

def test_shared_keys_need_a_unique_key_on_both_sides():
    """A foreign key is not an ambiguity.

    On a GTFS feed tier 2 flagged `routes.route_id == trips.route_id` as
    tables sharing a key set. That is an FK: every child shares values with
    its parent by definition, so sharing is normal in any normalised schema.
    What is dangerous is several tables that could each be the PARENT.
    """
    con = duckdb.connect(":memory:")
    try:
        con.execute("CREATE TABLE routes (route_id VARCHAR, nm VARCHAR)")
        con.execute("INSERT INTO routes VALUES ('A','x'),('B','y'),('C','z')")
        # a child: same values, NOT unique
        con.execute("CREATE TABLE trips (route_id VARCHAR, trip_id VARCHAR)")
        con.execute("INSERT INTO trips VALUES ('A','1'),('A','2'),('B','3'),('C','4')")
        src = DuckDBSource(con)
        p = tier2(src, tier1(src))
    finally:
        con.close()
    assert p.shared_key_set_ratio == 0.0, "an FK child is not a competing parent"


def test_dense_integer_sequences_are_not_evidence():
    """Two tables counting from one is arithmetic, not a finding.

    AdventureWorks numbers surrogate keys 1..N, so unrelated dimensions hash
    to identical value sets. That reported 24% shared key sets on a clean
    star schema and graded it `weak` when its foreign keys score 1.000.
    """
    con = duckdb.connect(":memory:")
    try:
        for t in ("dim_a", "dim_b"):
            con.execute(f"CREATE TABLE {t} (id INTEGER, nm VARCHAR)")
            con.executemany(f"INSERT INTO {t} VALUES (?, ?)",
                            [(i, f"v{i}") for i in range(1, 51)])
        src = DuckDBSource(con)
        p = tier2(src, tier1(src))
    finally:
        con.close()
    assert p.shared_key_set_ratio == 0.0, "1..50 twice is not a shared entity"


def test_a_weak_tier1_grade_says_it_is_provisional():
    """Tier 1 cannot separate the two reasons a warehouse has no convention.

    Built through tier1 rather than by constructing a Probe, because the
    caveat lives in the notes tier1 writes -- a direct construction has none,
    and a test that passed on one would be testing the dataclass.
    """
    con = duckdb.connect(":memory:")
    try:
        con.execute("CREATE TABLE CUST_MASTER_NEW (CUST_NO INTEGER, nm VARCHAR)")
        con.execute("INSERT INTO CUST_MASTER_NEW VALUES (7,'a'),(9,'b')")
        p = tier1(DuckDBSource(con))
    finally:
        con.close()

    assert p.grade == "weak"
    assert not p.tier2_ran
    out = render(p)
    assert "PROVISIONAL" in out
    assert any("--sample" in n for n in p.notes), "it must say how to resolve it"
