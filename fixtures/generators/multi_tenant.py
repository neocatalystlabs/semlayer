"""multi_tenant fixture: schema-per-tenant multi-tenancy.

20 schemas (tenant_001..tenant_020), each holding an *identical* 4-table
mini-mart (orders, customers, products, order_items) — same DDL, different
seeded data per tenant. This is the classic case where a naive inference
engine emits 80 near-duplicate table entries instead of recognizing one
template collapsed across 20 tenant instances.

The gold semantic layer models this as FOUR table entries (not 80), each
carrying a `tenant_template: {parameter: tenant_schema, instances: 20}`
block and a source pattern like "{tenant_schema}.orders" — the spec's
`source` field is a free-form string, so the templated placeholder is valid
without any schema change (see notes in the gold file).

Deterministic: seeded RNG per tenant, no wall-clock dependence.
"""

from __future__ import annotations

import random

try:
    from generators._bulk import bulk_insert
except ImportError:
    from _bulk import bulk_insert

SEED = 20260718
N_TENANTS = 20
N_CUSTOMERS_PER_TENANT = 30
N_PRODUCTS_PER_TENANT = 20
N_ORDERS_PER_TENANT = 150

REGIONS = ["north", "south", "east", "west"]
CATEGORIES = ["widgets", "gadgets", "gizmos", "doohickeys"]
STATUSES = ["pending", "shipped", "delivered", "cancelled"]

DDL = {
    "customers": """
        CREATE TABLE {schema}.customers (
            customer_id INTEGER PRIMARY KEY,
            name VARCHAR NOT NULL,
            region VARCHAR NOT NULL
        )
    """,
    "products": """
        CREATE TABLE {schema}.products (
            product_id INTEGER PRIMARY KEY,
            name VARCHAR NOT NULL,
            category VARCHAR NOT NULL,
            unit_price DECIMAL(10,2) NOT NULL
        )
    """,
    "orders": """
        CREATE TABLE {schema}.orders (
            order_id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL,
            order_date DATE NOT NULL,
            status VARCHAR NOT NULL
        )
    """,
    "order_items": """
        CREATE TABLE {schema}.order_items (
            order_item_id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            qty INTEGER NOT NULL,
            unit_price DECIMAL(10,2) NOT NULL
        )
    """,
}

TABLE_ORDER = ["customers", "products", "orders", "order_items"]


def tenant_schema(i: int) -> str:
    return f"tenant_{i:03d}"


def _build_tenant(con, schema: str, tenant_idx: int) -> None:
    rng = random.Random(SEED + tenant_idx)

    con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    for tbl in TABLE_ORDER:
        con.execute(DDL[tbl].format(schema=schema))

    customers = [
        (i, f"customer_{i:04d}", REGIONS[rng.randrange(len(REGIONS))])
        for i in range(1, N_CUSTOMERS_PER_TENANT + 1)
    ]
    bulk_insert(con, f"{schema}.customers", customers)

    products = [
        (
            i,
            f"product_{i:04d}",
            CATEGORIES[rng.randrange(len(CATEGORIES))],
            round(rng.uniform(5, 500), 2),
        )
        for i in range(1, N_PRODUCTS_PER_TENANT + 1)
    ]
    bulk_insert(con, f"{schema}.products", products)

    orders = []
    order_items = []
    item_id = 1
    for oid in range(1, N_ORDERS_PER_TENANT + 1):
        cust = rng.randint(1, N_CUSTOMERS_PER_TENANT)
        day = rng.randint(0, 364)
        status = STATUSES[rng.randrange(len(STATUSES))]
        orders.append((oid, cust, f"2025-{1 + day // 31:02d}-{1 + day % 28:02d}", status))
        for _ in range(rng.randint(1, 3)):
            prod = rng.randint(1, N_PRODUCTS_PER_TENANT)
            qty = rng.randint(1, 5)
            price = round(rng.uniform(5, 500), 2)
            order_items.append((item_id, oid, prod, qty, price))
            item_id += 1

    bulk_insert(con, f"{schema}.orders", orders)
    bulk_insert(con, f"{schema}.order_items", order_items)


def build(con) -> None:
    for i in range(1, N_TENANTS + 1):
        _build_tenant(con, tenant_schema(i), i)


NAME = "multi_tenant"
