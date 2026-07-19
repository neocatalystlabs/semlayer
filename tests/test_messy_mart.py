"""messy_mart fixture: determinism, aggregate reconciliation, implicit FK integrity,
SCD2 validity, and gold validation (test plan Layers 1, 2 & 6).

The warehouse DDL declares NO primary/foreign keys anywhere (see
fixtures/generators/messy_mart.py) — the point of this fixture is that real keys
and FK relationships exist in the *data* (inclusion dependencies hold) even though
the catalog says nothing about them. These tests assert those data-level invariants
hold, the same way an inference engine's own checks would.
"""

import sys
from pathlib import Path

import duckdb
import pytest

OSS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OSS / "fixtures"))
from generators import messy_mart  # noqa: E402

from semlayer.validate import validate_file  # noqa: E402


@pytest.fixture(scope="module")
def con():
    con = duckdb.connect(":memory:")
    messy_mart.build(con)
    yield con
    con.close()


def _tables(con):
    return {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}


def test_table_count_and_expected_tables_present(con):
    tables = _tables(con)
    # ~35-table warehouse; spot-check the categories called out in the brief.
    assert len(tables) >= 30
    for expected in [
        "ord_hdr", "ord_ln", "sub_evt", "web_evt",  # facts
        "cust_mstr", "prod_ref", "store_dim", "date_dim",  # dims
        "dly_sls_agg", "mth_cust_agg",  # aggregates
        "ord_hdr_legacy", "cust_mstr_legacy",  # deprecated legacy
        "etl_log", "stg_orders_raw",  # operational/staging
    ]:
        assert expected in tables, f"missing expected table {expected}"


def test_no_declared_constraints(con):
    """DDL must not declare PK/FK/UNIQUE constraints — the warehouse cruft this fixture models."""
    constraints = con.execute(
        "SELECT count(*) FROM duckdb_constraints() WHERE constraint_type != 'CHECK'"
    ).fetchone()[0]
    assert constraints == 0


def test_determinism():
    """Two independent builds must be byte-identical on sample tables (SPEC.md producer rules)."""
    a, b = duckdb.connect(":memory:"), duckdb.connect(":memory:")
    messy_mart.build(a)
    messy_mart.build(b)
    for t in ["ord_hdr", "cust_mstr", "dly_sls_agg"]:
        ra = a.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall()
        rb = b.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall()
        assert ra == rb, f"{t} not deterministic across builds"
    a.close(); b.close()


def test_dly_sls_agg_reconciles_with_base_fact(con):
    """dly_sls_agg must exactly equal a grouped SUM over ord_hdr (excluding cancelled orders)."""
    direct = con.execute("""
        SELECT date_key, store_id, sum(tot_amt) AS tot_sls_amt, count(*) AS ord_cnt
        FROM ord_hdr
        WHERE sts_cd != 'X'
        GROUP BY 1, 2
    """).fetchall()
    agg = con.execute("SELECT agg_dt_key, store_id, tot_sls_amt, ord_cnt FROM dly_sls_agg").fetchall()
    assert len(direct) == len(agg) and len(agg) > 0
    direct_map = {(d, s): (float(amt), cnt) for d, s, amt, cnt in direct}
    for dkey, sid, amt, cnt in agg:
        d_amt, d_cnt = direct_map[(dkey, sid)]
        assert abs(float(amt) - d_amt) < 0.01
        assert cnt == d_cnt


def test_mth_cust_agg_reconciles_with_base_fact(con):
    """mth_cust_agg must exactly equal a grouped SUM over ord_hdr (excluding cancelled orders)."""
    direct = con.execute("""
        SELECT strftime(ord_dt, '%Y-%m') AS yr_mth, cust_id,
               sum(tot_amt) AS tot_spend_amt, count(*) AS ord_cnt
        FROM ord_hdr
        WHERE sts_cd != 'X'
        GROUP BY 1, 2
    """).fetchall()
    agg = con.execute("SELECT yr_mth, cust_id, tot_spend_amt, ord_cnt FROM mth_cust_agg").fetchall()
    assert len(direct) == len(agg) and len(agg) > 0
    direct_map = {(ym, c): (float(amt), cnt) for ym, c, amt, cnt in direct}
    for ym, cid, amt, cnt in agg:
        d_amt, d_cnt = direct_map[(ym, cid)]
        assert abs(float(amt) - d_amt) < 0.01
        assert cnt == d_cnt


def test_ord_hdr_tot_amt_matches_ord_ln_sum(con):
    """ord_hdr.tot_amt is derived from SUM(ord_ln.line_amt) — an internal reconciliation invariant."""
    diff = con.execute("""
        SELECT max(abs(h.tot_amt - l.line_sum))
        FROM ord_hdr h
        JOIN (SELECT ord_id, sum(line_amt) AS line_sum FROM ord_ln GROUP BY 1) l USING (ord_id)
    """).fetchone()[0]
    assert float(diff) < 0.01


@pytest.mark.parametrize("child,child_col,parent,parent_col", [
    ("ord_ln", "ord_id", "ord_hdr", "ord_id"),
    ("ord_ln", "prod_id", "prod_ref", "prod_id"),
    ("ord_hdr", "store_id", "store_dim", "store_id"),
])
def test_implicit_fk_integrity_no_orphans(con, child, child_col, parent, parent_col):
    """No PK/FK is declared in the DDL, but the data must still be a clean inclusion dependency."""
    orphans = con.execute(f"""
        SELECT count(*) FROM {child} c
        LEFT JOIN {parent} p ON c.{child_col} = p.{parent_col}
        WHERE c.{child_col} IS NOT NULL AND p.{parent_col} IS NULL
    """).fetchone()[0]
    assert orphans == 0, f"{child}.{child_col} has values absent from {parent}.{parent_col}"


def test_scd2_validity_windows_do_not_overlap(con):
    """No two versions of the same cust_mstr natural key may have overlapping [eff_start_dt, eff_end_dt)."""
    overlaps = con.execute("""
        SELECT count(*)
        FROM cust_mstr a
        JOIN cust_mstr b
          ON a.cust_id = b.cust_id AND a.cust_sk < b.cust_sk
        WHERE a.eff_start_dt <= coalesce(b.eff_end_dt, DATE '9999-12-31')
          AND b.eff_start_dt <= coalesce(a.eff_end_dt, DATE '9999-12-31')
    """).fetchone()[0]
    assert overlaps == 0


def test_scd2_exactly_one_current_row_per_natural_key(con):
    bad = con.execute("""
        SELECT cust_id, sum(is_curr_flg) AS n_current
        FROM cust_mstr
        GROUP BY cust_id
        HAVING sum(is_curr_flg) != 1
    """).fetchall()
    assert bad == []


def test_enum_decode_coverage_mixed_by_design(con):
    """sts_cd has a decode dictionary; web_evt.evt_typ_cd deliberately does not (messiness requirement)."""
    sts_codes_in_fact = {r[0] for r in con.execute("SELECT DISTINCT sts_cd FROM ord_hdr").fetchall()}
    sts_codes_decoded = {r[0] for r in con.execute("SELECT sts_cd FROM sts_cd_dim").fetchall()}
    assert sts_codes_in_fact <= sts_codes_decoded
    assert "evt_typ_dim" not in {"evt_typ_dim"} or True  # sanity no-op; real check below
    tables = _tables(con)
    assert "web_evt_typ_dim" not in tables  # no decode dictionary exists for web_evt.evt_typ_cd


def test_gold_validates():
    result = validate_file(OSS / "fixtures" / "golds" / "messy_mart.yaml")
    assert result.ok, "\n".join(result.errors)


def test_gold_tables_match_fixture_tables(con):
    """Every table declared in the gold semantic layer must exist in the built warehouse."""
    import yaml

    doc = yaml.safe_load((OSS / "fixtures" / "golds" / "messy_mart.yaml").read_text())
    gold_tables = {t["name"] for t in doc["semantic_layer"]["tables"]}
    warehouse_tables = _tables(con)
    missing = gold_tables - warehouse_tables
    assert not missing, f"gold references tables absent from the warehouse: {missing}"
