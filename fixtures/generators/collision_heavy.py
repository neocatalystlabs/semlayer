"""collision_heavy fixture: the FK false-positive trap.

~25 tables, most carrying several small-range integer id/code/status columns
(domains like 1-5, 1-10, 1-20) so that naive inclusion-dependency mining
finds an explosion of spurious IND candidates: any two small contiguous
integer domains where one is a subset of the other look, statistically,
like a foreign key.

Exactly 5 REAL foreign keys exist, where both the data (every child value is
present in the parent) and the naming agree:
    orders.customer_id      -> customers.customer_id
    order_items.order_id    -> orders.order_id
    order_items.product_id  -> products.product_id
    employees.dept_id       -> departments.dept_id
    shipments.order_id      -> orders.order_id

TRAPS below are seeded column pairs with >=0.99 inclusion ratio (in this
fixture's data, exactly 1.0 by construction: the "from" column's domain is a
small contiguous range fully covered by the "to" column's PK range) that are
semantically absurd — a correct engine must NOT declare these as FKs, even
though the statistics alone strongly suggest it. Each entry documents why
the pairing is wrong. This fixture feeds the high-confidence-error-rate
metric (test-plan.md Layer 4): the fraction of seeded traps that a candidate
engine would auto-include.

Deterministic: seeded RNG, no wall-clock dependence.
"""

from __future__ import annotations

import random

try:
    from generators._bulk import bulk_insert, bulk_insert_sql
except ImportError:
    from _bulk import bulk_insert, bulk_insert_sql

SEED = 20260718

# (from_col, to_col, why_wrong) -- "table.column" -> "table.column".
# from_col's value domain is a strict subset of to_col's value domain
# (inclusion ratio ~1.0 in the generated data) purely because both are
# small contiguous integer ranges; there is no real-world relationship.
TRAPS: list[tuple[str, str, str]] = [
    (
        "shoes.shoe_size",
        "departments.dept_id",
        "shoe sizing (1-15) has nothing to do with organizational departments; "
        "the overlap is incidental because dept_id happens to enumerate 1-50.",
    ),
    (
        "tickets.priority_cd",
        "regions.region_id",
        "support-ticket priority (1-5) is not a region; both are just small "
        "contiguous integer codes.",
    ),
    (
        "reviews.rating",
        "carriers.carrier_id",
        "a 1-5 star product review rating is not a shipping carrier identifier.",
    ),
    (
        "coupons.discount_pct_bucket",
        "warehouses.warehouse_id",
        "a discretized discount-percent bucket (1-10) is not a warehouse.",
    ),
    (
        "returns.reason_cd",
        "suppliers.supplier_id",
        "a return-reason code (1-6) is not a supplier identifier.",
    ),
    (
        "campaigns.channel_cd",
        "categories.category_id",
        "a marketing-channel code (1-4) is not a product category.",
    ),
]

# Ranges for every PK / id-like column, so trap "to" columns are guaranteed
# to contain the full small-integer domain of their paired trap "from" column.
RANGES = {
    "customers": 150,
    "products": 100,
    "orders": 600,
    "order_items": 1200,
    "departments": 50,
    "employees": 200,
    "shipments": 400,
    "regions": 10,
    "warehouses": 12,
    "carriers": 8,
    "suppliers": 20,
    "stores": 15,
    "categories": 20,
    "tax_codes": 15,
    "currencies": 10,
    "addresses": 150,
    "inventory": 150,
    "returns": 100,
    "reviews": 150,
    "campaigns": 30,
    "coupons": 50,
    "promotions": 20,
    "tickets": 80,
    "payments": 600,
    "priorities": 5,
    "shoes": 150,
}

TABLE_NAMES = list(RANGES)  # 26 names incl. order_items; declared "~25"


def build(con) -> None:
    rng = random.Random(SEED)

    # --- lookup / dimension-ish tables with no real relationships ---
    con.execute("CREATE TABLE regions (region_id INTEGER PRIMARY KEY, region_name VARCHAR)")
    con.execute("CREATE TABLE warehouses (warehouse_id INTEGER PRIMARY KEY, warehouse_name VARCHAR)")
    con.execute("CREATE TABLE carriers (carrier_id INTEGER PRIMARY KEY, carrier_name VARCHAR)")
    con.execute("CREATE TABLE suppliers (supplier_id INTEGER PRIMARY KEY, supplier_name VARCHAR)")
    con.execute("CREATE TABLE categories (category_id INTEGER PRIMARY KEY, category_name VARCHAR)")
    con.execute("CREATE TABLE tax_codes (tax_code_id INTEGER PRIMARY KEY, rate DECIMAL(5,4))")
    con.execute("CREATE TABLE currencies (currency_id INTEGER PRIMARY KEY, currency_code VARCHAR)")
    con.execute("CREATE TABLE priorities (priority_id INTEGER PRIMARY KEY, priority_name VARCHAR)")

    for tbl, name_col, n in [
        ("regions", "region_name", RANGES["regions"]),
        ("warehouses", "warehouse_name", RANGES["warehouses"]),
        ("carriers", "carrier_name", RANGES["carriers"]),
        ("suppliers", "supplier_name", RANGES["suppliers"]),
        ("categories", "category_name", RANGES["categories"]),
        ("priorities", "priority_name", RANGES["priorities"]),
    ]:
        rows = [(i, f"{tbl[:-1]}_{i}") for i in range(1, n + 1)]
        bulk_insert(con, f"{tbl}", rows)

    bulk_insert_sql(con, 
        "INSERT INTO tax_codes VALUES (?, ?)",
        [(i, round(rng.uniform(0.01, 0.15), 4)) for i in range(1, RANGES["tax_codes"] + 1)],
    )
    bulk_insert_sql(con, 
        "INSERT INTO currencies VALUES (?, ?)",
        [(i, f"CUR{i:02d}") for i in range(1, RANGES["currencies"] + 1)],
    )

    con.execute("""
        CREATE TABLE stores (
            store_id INTEGER PRIMARY KEY,
            store_name VARCHAR,
            region_cd INTEGER
        )
    """)
    bulk_insert_sql(con, 
        "INSERT INTO stores VALUES (?, ?, ?)",
        [
            (i, f"store_{i}", rng.randint(1, RANGES["regions"]))
            for i in range(1, RANGES["stores"] + 1)
        ],
    )

    con.execute("""
        CREATE TABLE addresses (
            address_id INTEGER PRIMARY KEY,
            zone_cd INTEGER,
            street VARCHAR
        )
    """)
    bulk_insert_sql(con, 
        "INSERT INTO addresses VALUES (?, ?, ?)",
        [
            (i, rng.randint(1, RANGES["regions"]), f"{i} Main St")
            for i in range(1, RANGES["addresses"] + 1)
        ],
    )

    # --- core entities (real FK targets) ---
    con.execute("""
        CREATE TABLE departments (
            dept_id INTEGER PRIMARY KEY,
            dept_name VARCHAR NOT NULL
        )
    """)
    bulk_insert_sql(con, 
        "INSERT INTO departments VALUES (?, ?)",
        [(i, f"dept_{i}") for i in range(1, RANGES["departments"] + 1)],
    )

    con.execute("""
        CREATE TABLE customers (
            customer_id INTEGER PRIMARY KEY,
            name VARCHAR NOT NULL,
            region_id INTEGER
        )
    """)
    bulk_insert_sql(con, 
        "INSERT INTO customers VALUES (?, ?, ?)",
        [
            (i, f"customer_{i}", rng.randint(1, RANGES["regions"]))
            for i in range(1, RANGES["customers"] + 1)
        ],
    )

    con.execute("""
        CREATE TABLE products (
            product_id INTEGER PRIMARY KEY,
            name VARCHAR NOT NULL,
            category_id INTEGER
        )
    """)
    bulk_insert_sql(con, 
        "INSERT INTO products VALUES (?, ?, ?)",
        [
            (i, f"product_{i}", rng.randint(1, RANGES["categories"]))
            for i in range(1, RANGES["products"] + 1)
        ],
    )

    con.execute("""
        CREATE TABLE employees (
            employee_id INTEGER PRIMARY KEY,
            dept_id INTEGER NOT NULL,
            pay_grade INTEGER
        )
    """)
    bulk_insert_sql(con, 
        "INSERT INTO employees VALUES (?, ?, ?)",
        [
            (i, rng.randint(1, RANGES["departments"]), rng.randint(1, 10))
            for i in range(1, RANGES["employees"] + 1)
        ],
    )

    # --- fact-ish tables (2 real FKs each on orders/order_items/shipments) ---
    con.execute("""
        CREATE TABLE orders (
            order_id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL,
            status_cd INTEGER
        )
    """)
    bulk_insert_sql(con, 
        "INSERT INTO orders VALUES (?, ?, ?)",
        [
            (i, rng.randint(1, RANGES["customers"]), rng.randint(1, 5))
            for i in range(1, RANGES["orders"] + 1)
        ],
    )

    con.execute("""
        CREATE TABLE order_items (
            order_item_id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            qty INTEGER
        )
    """)
    bulk_insert_sql(con, 
        "INSERT INTO order_items VALUES (?, ?, ?, ?)",
        [
            (
                i,
                rng.randint(1, RANGES["orders"]),
                rng.randint(1, RANGES["products"]),
                rng.randint(1, 10),
            )
            for i in range(1, RANGES["order_items"] + 1)
        ],
    )

    con.execute("""
        CREATE TABLE shipments (
            shipment_id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL,
            carrier_id INTEGER
        )
    """)
    bulk_insert_sql(con, 
        "INSERT INTO shipments VALUES (?, ?, ?)",
        [
            (i, rng.randint(1, RANGES["orders"]), rng.randint(1, RANGES["carriers"]))
            for i in range(1, RANGES["shipments"] + 1)
        ],
    )

    con.execute("""
        CREATE TABLE inventory (
            inventory_id INTEGER PRIMARY KEY,
            warehouse_id INTEGER,
            qty INTEGER
        )
    """)
    bulk_insert_sql(con, 
        "INSERT INTO inventory VALUES (?, ?, ?)",
        [
            (i, rng.randint(1, RANGES["warehouses"]), rng.randint(0, 1000))
            for i in range(1, RANGES["inventory"] + 1)
        ],
    )

    con.execute("""
        CREATE TABLE payments (
            payment_id INTEGER PRIMARY KEY,
            method_cd INTEGER
        )
    """)
    bulk_insert_sql(con, 
        "INSERT INTO payments VALUES (?, ?)",
        [(i, rng.randint(1, 4)) for i in range(1, RANGES["payments"] + 1)],
    )

    con.execute("""
        CREATE TABLE promotions (
            promotion_id INTEGER PRIMARY KEY,
            promo_type_cd INTEGER
        )
    """)
    bulk_insert_sql(con, 
        "INSERT INTO promotions VALUES (?, ?)",
        [(i, rng.randint(1, 5)) for i in range(1, RANGES["promotions"] + 1)],
    )

    # --- seeded-trap "from" tables (small-domain codes) ---
    con.execute("""
        CREATE TABLE shoes (
            shoe_id INTEGER PRIMARY KEY,
            shoe_size INTEGER
        )
    """)
    bulk_insert_sql(con, 
        "INSERT INTO shoes VALUES (?, ?)",
        [(i, rng.randint(1, 15)) for i in range(1, RANGES["shoes"] + 1)],
    )

    con.execute("""
        CREATE TABLE tickets (
            ticket_id INTEGER PRIMARY KEY,
            priority_cd INTEGER
        )
    """)
    bulk_insert_sql(con, 
        "INSERT INTO tickets VALUES (?, ?)",
        [(i, rng.randint(1, 5)) for i in range(1, RANGES["tickets"] + 1)],
    )

    con.execute("""
        CREATE TABLE reviews (
            review_id INTEGER PRIMARY KEY,
            rating INTEGER
        )
    """)
    bulk_insert_sql(con, 
        "INSERT INTO reviews VALUES (?, ?)",
        [(i, rng.randint(1, 5)) for i in range(1, RANGES["reviews"] + 1)],
    )

    con.execute("""
        CREATE TABLE coupons (
            coupon_id INTEGER PRIMARY KEY,
            discount_pct_bucket INTEGER
        )
    """)
    bulk_insert_sql(con, 
        "INSERT INTO coupons VALUES (?, ?)",
        [(i, rng.randint(1, 10)) for i in range(1, RANGES["coupons"] + 1)],
    )

    con.execute("""
        CREATE TABLE returns (
            return_id INTEGER PRIMARY KEY,
            reason_cd INTEGER
        )
    """)
    bulk_insert_sql(con, 
        "INSERT INTO returns VALUES (?, ?)",
        [(i, rng.randint(1, 6)) for i in range(1, RANGES["returns"] + 1)],
    )

    con.execute("""
        CREATE TABLE campaigns (
            campaign_id INTEGER PRIMARY KEY,
            channel_cd INTEGER
        )
    """)
    bulk_insert_sql(con, 
        "INSERT INTO campaigns VALUES (?, ?)",
        [(i, rng.randint(1, 4)) for i in range(1, RANGES["campaigns"] + 1)],
    )


NAME = "collision_heavy"
