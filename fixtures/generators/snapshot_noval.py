"""snapshot_noval fixture: snapshot tables WITHOUT validity columns.

Two tables, both "type-2-ish" without the columns that would normally make
type-2 tracking safe:

- cust_snapshot: a daily full-copy snapshot keyed by (snap_dt, cust_id) — 90
  snapshot dates x 2000 customers. There is no valid_from/valid_to; the only
  way to reconstruct history is to compare consecutive snap_dt rows, and the
  only way to get "current state" is to filter to the latest snap_dt. A
  naive SUM(...) across all snap_dt rows over-counts by ~90x (the classic
  full-copy-snapshot canary) because every customer's balance is repeated on
  every date it didn't change.

- acct_status: only an is_current flag (no valid_from/valid_to at all) —
  tests SCD classification from a bare current-row marker.

Deterministic: seeded RNG, no wall-clock dependence.
"""

from __future__ import annotations

import datetime
import random

try:
    from generators._bulk import bulk_insert
except ImportError:
    from _bulk import bulk_insert

SEED = 20260718
N_CUSTOMERS = 600
N_SNAPSHOT_DAYS = 30

TIERS = ["bronze", "silver", "gold", "platinum"]
STATUSES = ["active", "inactive", "closed"]


def build(con) -> None:
    rng = random.Random(SEED)

    con.execute("""
        CREATE TABLE cust_snapshot (
            snap_dt DATE NOT NULL,
            cust_id INTEGER NOT NULL,
            cust_tier VARCHAR NOT NULL,
            account_balance DECIMAL(14,2) NOT NULL,
            lifetime_orders INTEGER NOT NULL,
            PRIMARY KEY (snap_dt, cust_id)
        )
    """)
    con.execute("""
        CREATE TABLE acct_status (
            account_id INTEGER NOT NULL,
            cust_id INTEGER NOT NULL,
            status VARCHAR NOT NULL,
            is_current BOOLEAN NOT NULL
        )
    """)

    # Each customer has a slowly-evolving state; every day gets a full copy
    # of every customer's current state (a full-copy snapshot with no
    # validity interval columns).
    state = {}
    for cid in range(1, N_CUSTOMERS + 1):
        state[cid] = {
            "cust_tier": rng.choice(TIERS),
            "account_balance": round(rng.uniform(0, 5000), 2),
            "lifetime_orders": rng.randint(0, 50),
        }

    base_date = datetime.date(2025, 1, 1)
    snap_rows = []
    for day in range(N_SNAPSHOT_DAYS):
        snap_dt = (base_date + datetime.timedelta(days=day)).isoformat()
        for cid in range(1, N_CUSTOMERS + 1):
            s = state[cid]
            # Small daily drift so consecutive snapshots aren't byte-identical.
            if rng.random() < 0.1:
                s["account_balance"] = round(s["account_balance"] + rng.uniform(-50, 100), 2)
            if rng.random() < 0.02:
                s["lifetime_orders"] += 1
            if rng.random() < 0.01:
                s["cust_tier"] = rng.choice(TIERS)
            snap_rows.append((
                snap_dt, cid, s["cust_tier"], s["account_balance"], s["lifetime_orders"],
            ))

    bulk_insert(con, "cust_snapshot", snap_rows)

    # acct_status: current-row-only table, one row per account, no history
    # retained. Occasionally an account has a superseded (is_current=false)
    # row alongside its current row to model a bare-flag SCD2 table.
    acct_rows = []
    aid = 1
    for cid in range(1, N_CUSTOMERS + 1):
        n_versions = 1
        if rng.random() < 0.15:
            n_versions = 2
        for v in range(n_versions):
            is_current = v == n_versions - 1
            status = rng.choice(STATUSES) if is_current else rng.choice(STATUSES[:2])
            acct_rows.append((aid, cid, status, is_current))
            aid += 1

    bulk_insert(con, "acct_status", acct_rows)


NAME = "snapshot_noval"
