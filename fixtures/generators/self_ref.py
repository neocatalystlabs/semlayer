"""self_ref fixture: recursive / self-referential structures.

employees: a management hierarchy via mgr_id -> employees.emp_id. A true tree,
5 levels deep, CEO (level 1) has mgr_id IS NULL. ~2K rows.

bom: a bill-of-materials DAG over a `parts` dimension. parent_part_id and
child_part_id both reference parts.part_id. Edges only ever go from a lower
part_level to the next higher part_level, so the graph is acyclic by
construction even though a child may have multiple parents (a true DAG, not
just a tree) — the test suite verifies no cycles exist via a recursive CTE.

Exercises: hierarchies with kind: recursive, self-referential relationships,
cycle-safety.

Deterministic: seeded RNG, no wall-clock dependence.
"""

from __future__ import annotations

import random

try:
    from generators._bulk import bulk_insert
except ImportError:
    from _bulk import bulk_insert

SEED = 20260718

# --- employees: 5-level tree, ~2K rows ---
DEPTS = ["engineering", "sales", "support", "finance", "ops"]
LEVEL_SIZES = [1, 6, 36, 216, 1728]  # L1..L5, sums to 1987
N_EMPLOYEES = sum(LEVEL_SIZES)

# --- bom: 4-level DAG over parts ---
PART_LEVEL_SIZES = [5, 20, 60, 150]  # L1 finished goods .. L4 raw components
N_PARTS = sum(PART_LEVEL_SIZES)


def build(con) -> None:
    rng = random.Random(SEED)

    # --- employees ---
    con.execute("""
        CREATE TABLE employees (
            emp_id INTEGER PRIMARY KEY,
            mgr_id INTEGER,
            dept VARCHAR NOT NULL,
            salary DECIMAL(10,2) NOT NULL
        )
    """)

    employees = []
    prev_level_ids: list[int] = []
    next_id = 1
    for level_idx, size in enumerate(LEVEL_SIZES):
        level_ids = []
        for _ in range(size):
            emp_id = next_id
            next_id += 1
            if level_idx == 0:
                mgr_id = None
            else:
                mgr_id = prev_level_ids[(emp_id - 1) % len(prev_level_ids)]
            dept = DEPTS[rng.randrange(len(DEPTS))]
            base_salary = 220_000 - level_idx * 30_000
            salary = round(base_salary + rng.uniform(-10_000, 10_000), 2)
            employees.append((emp_id, mgr_id, dept, salary))
            level_ids.append(emp_id)
        prev_level_ids = level_ids

    bulk_insert(con, "employees", employees)

    # --- parts + bom ---
    con.execute("""
        CREATE TABLE parts (
            part_id INTEGER PRIMARY KEY,
            part_name VARCHAR NOT NULL,
            part_level INTEGER NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE bom (
            parent_part_id INTEGER NOT NULL,
            child_part_id INTEGER NOT NULL,
            qty INTEGER NOT NULL,
            PRIMARY KEY (parent_part_id, child_part_id)
        )
    """)

    parts = []
    bom = []
    levels: list[list[int]] = []
    pid = 1
    for level_idx, size in enumerate(PART_LEVEL_SIZES):
        level_ids = []
        for _ in range(size):
            parts.append((pid, f"part_{pid:04d}", level_idx + 1))
            level_ids.append(pid)
            pid += 1
        levels.append(level_ids)

    for level_idx in range(1, len(levels)):
        parent_pool = levels[level_idx - 1]
        for child in levels[level_idx]:
            n_parents = rng.randint(1, min(3, len(parent_pool)))
            parents = rng.sample(parent_pool, n_parents)
            for parent in parents:
                bom.append((parent, child, rng.randint(1, 10)))

    bulk_insert(con, "parts", parts)
    bulk_insert(con, "bom", bom)


NAME = "self_ref"
