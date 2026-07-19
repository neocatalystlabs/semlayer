"""obt fixture: one-big-table denormalization.

A single ~120-column `events_wide` table flattens order/customer/product/store
attributes into one row per event (no joinable dimension tables). This tests
intra-table semantics: hierarchies (geography, product) must be declared from
columns WITHIN the table rather than inferred through relationships, because
there is nothing else to join against.

Deterministic: seeded RNG, no wall-clock dependence.
"""

from __future__ import annotations

import random

try:
    from generators._bulk import bulk_insert_sql
except ImportError:
    from _bulk import bulk_insert_sql

SEED = 20260718
N_ROWS = 3_000

REGIONS = {
    "USA": {
        "CA": ["Los Angeles", "San Francisco", "San Diego"],
        "NY": ["New York City", "Buffalo", "Albany"],
        "TX": ["Houston", "Austin", "Dallas"],
    },
    "CAN": {
        "ON": ["Toronto", "Ottawa"],
        "BC": ["Vancouver", "Victoria"],
    },
}

PRODUCT_TREE = {
    "Electronics": ["Phones", "Laptops", "Audio"],
    "Home": ["Kitchen", "Furniture", "Decor"],
    "Apparel": ["Mens", "Womens", "Kids"],
}

CHANNELS = ["web", "mobile_app", "store", "call_center"]
PAYMENT_METHODS = ["card", "ach", "wire", "gift_card"]
ORDER_STATUSES = ["placed", "shipped", "delivered", "cancelled", "returned"]
STORE_FORMATS = ["flagship", "mall", "outlet", "pop_up"]


def _rand_name(rng: random.Random, prefix: str, n: int) -> str:
    return f"{prefix}_{n:06d}"


def build(con) -> None:
    rng = random.Random(SEED)

    country_choices = list(REGIONS.keys())
    dept_choices = list(PRODUCT_TREE.keys())

    cols = [
        # --- event / order identity ---
        "event_id INTEGER PRIMARY KEY",
        "order_id INTEGER NOT NULL",
        "event_ts TIMESTAMP NOT NULL",
        "event_type VARCHAR NOT NULL",
        "channel VARCHAR NOT NULL",
        # --- customer attributes (flattened) ---
        "cust_id INTEGER NOT NULL",
        "cust_name VARCHAR NOT NULL",
        "cust_email VARCHAR NOT NULL",
        "cust_signup_dt DATE NOT NULL",
        "cust_tier VARCHAR NOT NULL",
        "cust_country VARCHAR NOT NULL",
        "cust_state VARCHAR NOT NULL",
        "cust_city VARCHAR NOT NULL",
        "cust_postal VARCHAR NOT NULL",
        "cust_lifetime_orders INTEGER NOT NULL",
        # --- product attributes (flattened) ---
        "prod_id INTEGER NOT NULL",
        "prod_name VARCHAR NOT NULL",
        "prod_sku VARCHAR NOT NULL",
        "prod_department VARCHAR NOT NULL",
        "prod_category VARCHAR NOT NULL",
        "prod_brand VARCHAR NOT NULL",
        "prod_color VARCHAR NOT NULL",
        "prod_size VARCHAR NOT NULL",
        "prod_weight_kg DECIMAL(8,2) NOT NULL",
        "prod_unit_cost DECIMAL(12,2) NOT NULL",
        "prod_list_price DECIMAL(12,2) NOT NULL",
        # --- store attributes (flattened) ---
        "store_id INTEGER NOT NULL",
        "store_name VARCHAR NOT NULL",
        "store_format VARCHAR NOT NULL",
        "store_country VARCHAR NOT NULL",
        "store_state VARCHAR NOT NULL",
        "store_city VARCHAR NOT NULL",
        # --- order economics ---
        "qty INTEGER NOT NULL",
        "unit_price DECIMAL(12,2) NOT NULL",
        "discount_pct DECIMAL(5,2) NOT NULL",
        "order_total DECIMAL(12,2) NOT NULL",
        "tax_amount DECIMAL(12,2) NOT NULL",
        "shipping_amount DECIMAL(12,2) NOT NULL",
        "payment_method VARCHAR NOT NULL",
        "order_status VARCHAR NOT NULL",
        "is_gift BOOLEAN NOT NULL",
        "is_returned BOOLEAN NOT NULL",
        "coupon_code VARCHAR",
        "loyalty_points_earned INTEGER NOT NULL",
    ]

    # Pad out to ~120 columns with additional plausible denormalized
    # attributes (marketing/attribution/device metadata commonly flattened
    # into wide event tables).
    padding_specs = []
    for i in range(1, 41):
        padding_specs.append((f"attr_text_{i:02d}", "VARCHAR"))
    for i in range(1, 21):
        padding_specs.append((f"attr_num_{i:02d}", "DECIMAL(10,2)"))
    for i in range(1, 21):
        padding_specs.append((f"attr_flag_{i:02d}", "BOOLEAN"))

    for name, sql_type in padding_specs:
        cols.append(f"{name} {sql_type}")

    ddl = "CREATE TABLE events_wide (\n    " + ",\n    ".join(cols) + "\n)"
    con.execute(ddl)

    base_col_names = [c.split()[0] for c in cols]
    padding_names = [n for n, _ in padding_specs]
    n_placeholders = len(base_col_names)
    insert_sql = (
        "INSERT INTO events_wide VALUES (" + ", ".join(["?"] * n_placeholders) + ")"
    )

    # Pre-generate a pool of customers/products/stores that repeat across
    # events, so the table has realistic within-table functional dependencies
    # (e.g. cust_id -> cust_country) despite being fully denormalized.
    n_customers = 2000
    n_products = 500
    n_stores = 60

    # Customer/product/store attribute tuples are stored pre-ordered to
    # match their slice of base_col_names, so per-event rows can be built by
    # tuple concatenation instead of dict construction (much faster at
    # ~50K rows).
    customers = []
    for cid in range(1, n_customers + 1):
        country = rng.choice(country_choices)
        state = rng.choice(list(REGIONS[country].keys()))
        city = rng.choice(REGIONS[country][state])
        customers.append((
            cid,
            _rand_name(rng, "cust", cid),
            f"cust{cid}@example.com",
            f"20{18 + rng.randint(0, 6):02d}-{1 + rng.randint(0, 11):02d}-{1 + rng.randint(0, 27):02d}",
            rng.choice(["bronze", "silver", "gold", "platinum"]),
            country,
            state,
            city,
            f"{rng.randint(10000, 99999)}",
            rng.randint(1, 200),
        ))

    products = []
    for pid in range(1, n_products + 1):
        dept = rng.choice(dept_choices)
        category = rng.choice(PRODUCT_TREE[dept])
        cost = round(rng.uniform(2, 200), 2)
        list_price = round(cost * rng.uniform(1.3, 3.0), 2)
        products.append((
            pid,
            _rand_name(rng, "prod", pid),
            f"SKU-{pid:06d}",
            dept,
            category,
            rng.choice(["Acme", "Globex", "Initech", "Umbrella", "Soylent"]),
            rng.choice(["black", "white", "red", "blue", "green"]),
            rng.choice(["XS", "S", "M", "L", "XL"]),
            round(rng.uniform(0.1, 25), 2),
            cost,
            list_price,
        ))

    stores = []
    for sid in range(1, n_stores + 1):
        country = rng.choice(country_choices)
        state = rng.choice(list(REGIONS[country].keys()))
        city = rng.choice(REGIONS[country][state])
        stores.append((
            sid,
            _rand_name(rng, "store", sid),
            rng.choice(STORE_FORMATS),
            country,
            state,
            city,
        ))

    n_padding = len(padding_names)
    padding_kinds = []
    for name in padding_names:
        if name.startswith("attr_text_"):
            padding_kinds.append("text")
        elif name.startswith("attr_num_"):
            padding_kinds.append("num")
        else:
            padding_kinds.append("flag")

    TEXT_CHOICES = (None, "A", "B", "C", "D")
    DISCOUNT_CHOICES = (0, 0, 0, 5, 10, 15, 20)
    COUPON_CHOICES = (None, None, None, "SAVE10", "WELCOME5")

    rows = []
    for eid in range(1, N_ROWS + 1):
        cust = customers[rng.randrange(n_customers)]
        prod = products[rng.randrange(n_products)]
        store = stores[rng.randrange(n_stores)]

        qty = rng.randint(1, 5)
        unit_price = prod[10]  # prod_list_price
        discount_pct = round(rng.choice(DISCOUNT_CHOICES) + rng.uniform(0, 0.99), 2)
        gross = round(unit_price * qty, 2)
        discount_amt = round(gross * discount_pct / 100, 2)
        order_total = round(gross - discount_amt, 2)
        tax_amount = round(order_total * 0.08, 2)
        shipping_amount = round(rng.uniform(0, 15), 2)

        day = rng.randint(0, 364)
        ts = f"2025-{1 + day // 31:02d}-{1 + day % 28:02d} {rng.randint(0,23):02d}:{rng.randint(0,59):02d}:00"

        padding_vals = [None] * n_padding
        for i, kind in enumerate(padding_kinds):
            if kind == "text":
                padding_vals[i] = rng.choice(TEXT_CHOICES)
            elif kind == "num":
                padding_vals[i] = round(rng.uniform(0, 100), 2) if rng.random() > 0.2 else None
            else:
                padding_vals[i] = rng.random() < 0.5

        row = (
            (eid, 100000 + eid, ts, "purchase", rng.choice(CHANNELS))
            + cust
            + prod
            + store
            + (
                qty, unit_price, discount_pct, order_total, tax_amount, shipping_amount,
                rng.choice(PAYMENT_METHODS), rng.choice(ORDER_STATUSES),
                rng.random() < 0.05, rng.random() < 0.03,
                rng.choice(COUPON_CHOICES), int(order_total),
            )
            + tuple(padding_vals)
        )
        rows.append(row)

    bulk_insert_sql(con, insert_sql, rows)


NAME = "obt"
