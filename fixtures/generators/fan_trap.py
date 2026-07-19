"""fan_trap fixture: the classic double-counting schema.

orders (fact) --< payments   (1:N)
orders (fact) --< shipments  (1:N)
orders >-- customers (N:1 dim)

Joining orders to BOTH payments and shipments multiplies order rows
(N_payments x N_shipments per order), so a naive SUM(order_total) over the
3-way join over-counts. The gold semantic layer marks both relationships
fanout_risk: true and declares a symmetric_aggregate strategy; the test
suite asserts the naive sum diverges and the symmetric sum matches truth.

Deterministic: seeded RNG, no wall-clock dependence.
"""

from __future__ import annotations

import random

try:
    from generators._bulk import bulk_insert
except ImportError:
    from _bulk import bulk_insert

SEED = 20260718
N_CUSTOMERS = 50
N_ORDERS = 1000

REGIONS = ["north", "south", "east", "west"]


def build(con) -> None:
    rng = random.Random(SEED)

    con.execute("""
        CREATE TABLE customers (
            customer_id INTEGER PRIMARY KEY,
            name VARCHAR NOT NULL,
            region VARCHAR NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE orders (
            order_id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL,
            order_date DATE NOT NULL,
            order_total DECIMAL(12,2) NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE payments (
            payment_id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL,
            amount DECIMAL(12,2) NOT NULL,
            method VARCHAR NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE shipments (
            shipment_id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL,
            weight_kg DECIMAL(8,2) NOT NULL
        )
    """)

    first = ["Ava", "Ben", "Chloe", "Dev", "Elena", "Farid", "Grace", "Hiro",
             "Iris", "Jonas", "Kavya", "Liam", "Mona", "Nils", "Priya", "Quinn"]
    last = ["Anderson", "Bhatt", "Chen", "Dubois", "Ekwueme", "Fischer",
            "Garcia", "Haddad", "Ivanova", "Johansson", "Kim", "Lopez"]
    customers = [
        (i, f"{first[i % len(first)]} {last[(i * 7) % len(last)]}",
         REGIONS[i % len(REGIONS)])
        for i in range(1, N_CUSTOMERS + 1)
    ]
    bulk_insert(con, "customers", customers)

    orders, payments, shipments = [], [], []
    pay_id = ship_id = 1
    for oid in range(1, N_ORDERS + 1):
        cust = rng.randint(1, N_CUSTOMERS)
        day = rng.randint(0, 364)
        total = round(rng.uniform(10, 500), 2)
        orders.append((oid, cust, f"2025-{1 + day // 31:02d}-{1 + day % 28:02d}", total))
        n_pay = rng.randint(1, 3)
        remaining = total
        for k in range(n_pay):
            amt = round(remaining / (n_pay - k), 2)
            remaining = round(remaining - amt, 2)
            payments.append((pay_id, oid, amt, rng.choice(["card", "ach", "wire"])))
            pay_id += 1
        for _ in range(rng.randint(1, 2)):
            shipments.append((ship_id, oid, round(rng.uniform(0.1, 20), 2)))
            ship_id += 1

    bulk_insert(con, "orders", orders)
    bulk_insert(con, "payments", payments)
    bulk_insert(con, "shipments", shipments)


NAME = "fan_trap"
