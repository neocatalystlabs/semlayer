"""eav fixture: entity-attribute-value anti-pattern.

`entity_attr` stores ~20 distinct attributes for ~5000 entities as
(entity_id, attr_name, attr_value) rows, with attr_value always VARCHAR
regardless of the attribute's "real" type (numbers, dates, booleans, and
enums are all string-encoded). A small proper `entities` table holds the
entity's own identity columns.

This is deliberately NOT column-inferable: the semantics of a "row" depend
on the value of attr_name, not on any column's name or declared type. The
gold layer must say so honestly (table_type: operational, attr_value
semantic_type: unknown) rather than guess.

Deterministic: seeded RNG, no wall-clock dependence.
"""

from __future__ import annotations

import random

try:
    from generators._bulk import bulk_insert
except ImportError:
    from _bulk import bulk_insert

SEED = 20260718
N_ENTITIES = 1_500

# 20 distinct attributes, spanning several "real" underlying types, all
# stored as VARCHAR in attr_value.
ATTRS = [
    ("status", "enum", ["active", "inactive", "pending", "suspended"]),
    ("plan_tier", "enum", ["free", "starter", "pro", "enterprise"]),
    ("signup_date", "date", None),
    ("last_login_ts", "timestamp", None),
    ("age", "int", None),
    ("account_balance", "decimal", None),
    ("is_verified", "bool", ["true", "false"]),
    ("is_trial", "bool", ["true", "false"]),
    ("country_code", "enum", ["US", "CA", "GB", "DE", "FR", "IN", "AU"]),
    ("referral_source", "enum", ["organic", "paid_search", "social", "referral", "email"]),
    ("num_logins", "int", None),
    ("num_purchases", "int", None),
    ("lifetime_value", "decimal", None),
    ("email_opt_in", "bool", ["true", "false"]),
    ("sms_opt_in", "bool", ["true", "false"]),
    ("preferred_language", "enum", ["en", "es", "fr", "de", "ja"]),
    ("support_tickets_open", "int", None),
    ("nps_score", "int", None),
    ("device_type", "enum", ["desktop", "mobile", "tablet"]),
    ("churn_risk_score", "decimal", None),
]

assert len(ATTRS) == 20


def _gen_value(rng: random.Random, kind: str, choices) -> str:
    if kind == "enum" or kind == "bool":
        return rng.choice(choices)
    if kind == "date":
        y = 2018 + rng.randint(0, 7)
        m = 1 + rng.randint(0, 11)
        d = 1 + rng.randint(0, 27)
        return f"{y:04d}-{m:02d}-{d:02d}"
    if kind == "timestamp":
        y = 2024 + rng.randint(0, 1)
        m = 1 + rng.randint(0, 11)
        d = 1 + rng.randint(0, 27)
        hh = rng.randint(0, 23)
        mm = rng.randint(0, 59)
        return f"{y:04d}-{m:02d}-{d:02d} {hh:02d}:{mm:02d}:00"
    if kind == "int":
        return str(rng.randint(0, 500))
    if kind == "decimal":
        return f"{rng.uniform(0, 10000):.2f}"
    raise ValueError(kind)


def build(con) -> None:
    rng = random.Random(SEED)

    con.execute("""
        CREATE TABLE entities (
            entity_id INTEGER PRIMARY KEY,
            entity_type VARCHAR NOT NULL,
            created_ts TIMESTAMP NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE entity_attr (
            entity_attr_id INTEGER PRIMARY KEY,
            entity_id INTEGER NOT NULL,
            attr_name VARCHAR NOT NULL,
            attr_value VARCHAR NOT NULL
        )
    """)

    entities = []
    for eid in range(1, N_ENTITIES + 1):
        etype = rng.choice(["account", "organization"])
        y = 2018 + rng.randint(0, 7)
        m = 1 + rng.randint(0, 11)
        d = 1 + rng.randint(0, 27)
        entities.append((eid, etype, f"{y:04d}-{m:02d}-{d:02d} 00:00:00"))
    bulk_insert(con, "entities", entities)

    rows = []
    ea_id = 1
    for eid in range(1, N_ENTITIES + 1):
        # Not every entity has every attribute (EAV tables are typically
        # sparse) — each attribute present with 80% probability.
        for attr_name, kind, choices in ATTRS:
            if rng.random() < 0.8:
                value = _gen_value(rng, kind, choices)
                rows.append((ea_id, eid, attr_name, value))
                ea_id += 1

    bulk_insert(con, "entity_attr", rows)


NAME = "eav"
