"""Adversarial small-fixture tests: obt, eav, snapshot_noval.

Covers (per fixture): build determinism, gold validation, plus the
fixture-specific canary each fixture exists to exercise:
  - obt: intra-table hierarchy sanity (no relationships needed/declared)
  - eav: attribute distribution sanity (honest non-inference target)
  - snapshot_noval: the over-count canary (SPEC.md-style fan-out-adjacent
    hazard for full-copy snapshots without validity columns)
"""

import sys
from pathlib import Path

import duckdb
import pytest
import yaml

OSS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OSS / "fixtures"))
from generators import eav, obt, snapshot_noval  # noqa: E402

from semlayer.validate import validate_file  # noqa: E402

GOLDS_DIR = OSS / "fixtures" / "golds"


def _assert_deterministic(build_fn, tables: list[str]):
    a, b = duckdb.connect(":memory:"), duckdb.connect(":memory:")
    build_fn(a)
    build_fn(b)
    for t in tables:
        ra = a.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall()
        rb = b.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall()
        assert ra == rb, f"{t} not deterministic"
    a.close()
    b.close()


# --------------------------------------------------------------------------
# Gold validation (all three fixtures)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["obt", "eav", "snapshot_noval"])
def test_gold_validates(name):
    result = validate_file(GOLDS_DIR / f"{name}.yaml")
    assert result.ok, "\n".join(result.errors)


# --------------------------------------------------------------------------
# obt
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def obt_con():
    con = duckdb.connect(":memory:")
    obt.build(con)
    yield con
    con.close()


def test_obt_determinism():
    _assert_deterministic(obt.build, ["events_wide"])


def test_obt_row_and_column_count(obt_con):
    assert obt_con.execute("SELECT count(*) FROM events_wide").fetchone()[0] == obt.N_ROWS
    ncols = len(obt_con.execute("DESCRIBE events_wide").fetchall())
    assert ncols >= 100, f"expected a wide (~120-column) table, got {ncols} columns"


def test_obt_gold_hierarchies_are_intra_table():
    """The gold must declare geography/product hierarchies against events_wide
    itself, and must declare no relationships (nothing joinable exists)."""
    doc = yaml.safe_load((GOLDS_DIR / "obt.yaml").read_text())["semantic_layer"]
    assert doc.get("relationships", []) == []
    assert len(doc["tables"]) == 1
    assert doc["tables"][0]["table_type"] == "denormalized"

    hierarchies = {h["name"]: h for h in doc["hierarchies"]}
    assert hierarchies["geography"]["dimension_table"] == "events_wide"
    geo_columns = [lvl["column"] for lvl in hierarchies["geography"]["levels"]]
    assert geo_columns == ["cust_country", "cust_state", "cust_city"]

    assert hierarchies["product_taxonomy"]["dimension_table"] == "events_wide"
    prod_columns = [lvl["column"] for lvl in hierarchies["product_taxonomy"]["levels"]]
    assert prod_columns == ["prod_department", "prod_category"]


def test_obt_geography_hierarchy_matches_data(obt_con):
    """cust_country -> cust_state is a genuine functional dependency in the
    generated data (each state belongs to exactly one country)."""
    violations = obt_con.execute("""
        SELECT cust_state, count(DISTINCT cust_country) AS n
        FROM events_wide GROUP BY 1 HAVING n > 1
    """).fetchall()
    assert violations == []


# --------------------------------------------------------------------------
# eav
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def eav_con():
    con = duckdb.connect(":memory:")
    eav.build(con)
    yield con
    con.close()


def test_eav_determinism():
    _assert_deterministic(eav.build, ["entities", "entity_attr"])


def test_eav_attribute_distribution_sanity(eav_con):
    """~20 distinct attributes; each present for a plausible share of the
    ~5000 entities (sparse EAV, not fully dense, not near-empty)."""
    distinct_attrs = eav_con.execute(
        "SELECT count(DISTINCT attr_name) FROM entity_attr"
    ).fetchone()[0]
    assert distinct_attrs == len(eav.ATTRS) == 20

    counts = dict(eav_con.execute(
        "SELECT attr_name, count(*) FROM entity_attr GROUP BY 1"
    ).fetchall())
    assert set(counts.keys()) == {name for name, _, _ in eav.ATTRS}
    for attr_name, n in counts.items():
        share = n / eav.N_ENTITIES
        assert 0.6 < share <= 1.0, f"{attr_name} present for implausible share {share:.2f}"


def test_eav_attr_value_always_string_typed(eav_con):
    coltype = eav_con.execute(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name='entity_attr' AND column_name='attr_value'"
    ).fetchone()[0]
    assert coltype == "VARCHAR"


def test_eav_gold_marks_attr_value_unknown_and_operational():
    doc = yaml.safe_load((GOLDS_DIR / "eav.yaml").read_text())["semantic_layer"]
    tables = {t["name"]: t for t in doc["tables"]}
    ea = tables["entity_attr"]
    assert ea["table_type"] == "operational"
    assert "ai_context" in ea
    assert "eav" in ea["ai_context"].lower() or "entity-attribute-value" in ea["ai_context"].lower()
    cols = {c["name"]: c for c in ea["columns"]}
    assert cols["attr_value"]["semantic_type"] == "unknown"


# --------------------------------------------------------------------------
# snapshot_noval
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def snap_con():
    con = duckdb.connect(":memory:")
    snapshot_noval.build(con)
    yield con
    con.close()


def test_snapshot_determinism():
    _assert_deterministic(snapshot_noval.build, ["cust_snapshot", "acct_status"])


def test_snapshot_row_counts(snap_con):
    n = snap_con.execute("SELECT count(*) FROM cust_snapshot").fetchone()[0]
    assert n == snapshot_noval.N_CUSTOMERS * snapshot_noval.N_SNAPSHOT_DAYS


def test_snapshot_no_validity_columns_present(snap_con):
    cols = {r[0] for r in snap_con.execute("DESCRIBE cust_snapshot").fetchall()}
    assert "valid_from" not in cols and "valid_to" not in cols
    acct_cols = {r[0] for r in snap_con.execute("DESCRIBE acct_status").fetchall()}
    assert "valid_from" not in acct_cols and "valid_to" not in acct_cols
    assert "is_current" in acct_cols


def test_snapshot_overcount_canary(snap_con):
    """THE canary: summing account_balance across all snap_dt rows massively
    over-counts vs. summing at the latest snapshot date alone."""
    latest_total = float(snap_con.execute("""
        SELECT sum(account_balance) FROM cust_snapshot
        WHERE snap_dt = (SELECT max(snap_dt) FROM cust_snapshot)
    """).fetchone()[0])

    naive_total = float(snap_con.execute(
        "SELECT sum(account_balance) FROM cust_snapshot"
    ).fetchone()[0])

    n_dates = snap_con.execute(
        "SELECT count(DISTINCT snap_dt) FROM cust_snapshot"
    ).fetchone()[0]
    assert n_dates == snapshot_noval.N_SNAPSHOT_DAYS

    # naive over-counts by roughly n_dates x; assert it's at least an order
    # of magnitude too high to make the canary robust to daily drift noise.
    assert naive_total > latest_total * (n_dates * 0.5), (
        f"fixture failed to produce a material over-count "
        f"(naive={naive_total}, latest={latest_total}, n_dates={n_dates})"
    )


def test_snapshot_gold_declares_advisory_latest_filter():
    doc = yaml.safe_load((GOLDS_DIR / "snapshot_noval.yaml").read_text())["semantic_layer"]
    tables = {t["name"]: t for t in doc["tables"]}
    snap = tables["cust_snapshot"]
    assert snap["table_type"] == "snapshot_scd2"
    assert "snapshot date" in snap["grain"]

    filters = snap["knowledge"]["required_filters"]
    assert any(f["enforcement"] == "advisory" and "snap_dt" in f["expr"] for f in filters)

    acct = tables["acct_status"]
    assert acct["scd"]["type"] == 2
    assert acct["scd"]["is_current_flag"] == "is_current"


def test_snapshot_metric_filters_to_latest_snapshot():
    doc = yaml.safe_load((GOLDS_DIR / "snapshot_noval.yaml").read_text())["semantic_layer"]
    metrics = {m["name"]: m for m in doc["metrics"]}
    m = metrics["latest_total_balance"]
    assert m["measure"] == "cust_snapshot.account_balance"
    assert "snap_dt" in m["filter"] and "max(snap_dt)" in m["filter"]
