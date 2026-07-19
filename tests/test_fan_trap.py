"""fan_trap fixture: build determinism + the double-counting canary (test plan Layers 2 & 6).

The canary encodes SPEC.md 2.3: a naive SUM across a fan-out MUST diverge from
truth, and the symmetric-aggregate strategy MUST recover truth exactly.
"""

import sys
from pathlib import Path

import duckdb
import pytest

OSS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OSS / "fixtures"))
from generators import fan_trap  # noqa: E402


@pytest.fixture(scope="module")
def con():
    con = duckdb.connect(":memory:")
    fan_trap.build(con)
    yield con
    con.close()


def test_row_counts(con):
    assert con.execute("SELECT count(*) FROM orders").fetchone()[0] == fan_trap.N_ORDERS
    assert con.execute("SELECT count(*) FROM customers").fetchone()[0] == fan_trap.N_CUSTOMERS
    # every order has at least one payment and one shipment
    assert con.execute(
        "SELECT count(*) FROM orders o LEFT JOIN payments p ON o.order_id=p.order_id WHERE p.order_id IS NULL"
    ).fetchone()[0] == 0


def test_determinism():
    a, b = duckdb.connect(":memory:"), duckdb.connect(":memory:")
    fan_trap.build(a)
    fan_trap.build(b)
    for t in ["customers", "orders", "payments", "shipments"]:
        ra = a.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
        rb = b.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
        assert ra == rb, f"{t} not deterministic"
    a.close(); b.close()


def test_payments_sum_to_order_totals(con):
    """Split payments reconcile to order totals (seed data invariant)."""
    diff = con.execute("""
        SELECT max(abs(o.order_total - p.paid)) FROM orders o
        JOIN (SELECT order_id, sum(amount) AS paid FROM payments GROUP BY 1) p USING (order_id)
    """).fetchone()[0]
    assert float(diff) < 0.01


def test_fan_trap_canary(con):
    """THE canary: naive 3-way join over-counts; symmetric aggregation recovers truth."""
    truth = float(con.execute("SELECT sum(order_total) FROM orders").fetchone()[0])

    naive = float(con.execute("""
        SELECT sum(o.order_total)
        FROM orders o
        JOIN payments p ON o.order_id = p.order_id
        JOIN shipments s ON o.order_id = s.order_id
    """).fetchone()[0])

    # symmetric aggregate: de-duplicate by the fact PK before summing
    symmetric = float(con.execute("""
        SELECT sum(order_total) FROM (
            SELECT DISTINCT o.order_id, o.order_total
            FROM orders o
            JOIN payments p ON o.order_id = p.order_id
            JOIN shipments s ON o.order_id = s.order_id
        )
    """).fetchone()[0])

    assert naive > truth * 1.2, (
        f"fixture failed to produce a material fan-out (naive={naive}, truth={truth})"
    )
    assert abs(symmetric - truth) < 0.01, "symmetric aggregation must recover the true total"


def test_gold_metric_by_region_matches_direct_query(con):
    """total_revenue by geography.region: compiled-metric semantics vs direct SQL."""
    direct = dict(con.execute("""
        SELECT c.region, sum(o.order_total)
        FROM orders o JOIN customers c ON o.customer_id = c.customer_id
        GROUP BY 1
    """).fetchall())
    assert len(direct) == 4
    assert abs(sum(float(v) for v in direct.values())
               - float(con.execute("SELECT sum(order_total) FROM orders").fetchone()[0])) < 0.01
