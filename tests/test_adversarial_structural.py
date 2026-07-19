"""Adversarial structural fixtures: self_ref, multi_tenant, collision_heavy
(test-plan.md Layer 1/2/4).

self_ref     -- recursive hierarchies (org chart, BOM DAG); cycle-safety.
multi_tenant -- schema-per-tenant collapsing to a 4-table template, not 80 tables.
collision_heavy -- FK false-positive traps: seeded high-inclusion-ratio pairs
                   that must NOT be inferred as foreign keys; real FKs must
                   be orphan-free.
"""

import sys
from pathlib import Path

import duckdb
import pytest
import yaml

OSS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OSS / "fixtures"))
from generators import collision_heavy, multi_tenant, self_ref  # noqa: E402

GOLDS = OSS / "fixtures" / "golds"


def _load_gold(name: str) -> dict:
    return yaml.safe_load((GOLDS / f"{name}.yaml").read_text())


# ---------------------------------------------------------------------------
# self_ref
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def self_ref_con():
    con = duckdb.connect(":memory:")
    self_ref.build(con)
    yield con
    con.close()


def test_self_ref_row_counts(self_ref_con):
    assert self_ref_con.execute("SELECT count(*) FROM employees").fetchone()[0] == self_ref.N_EMPLOYEES
    assert self_ref_con.execute("SELECT count(*) FROM parts").fetchone()[0] == self_ref.N_PARTS
    # exactly one CEO (NULL mgr_id)
    assert self_ref_con.execute(
        "SELECT count(*) FROM employees WHERE mgr_id IS NULL"
    ).fetchone()[0] == 1


def test_self_ref_determinism():
    a, b = duckdb.connect(":memory:"), duckdb.connect(":memory:")
    self_ref.build(a)
    self_ref.build(b)
    for t in ["employees", "parts", "bom"]:
        ra = a.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall()
        rb = b.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall()
        assert ra == rb, f"{t} not deterministic"
    a.close(); b.close()


def test_employee_hierarchy_depth_matches_expected(self_ref_con):
    """Depth reachable from the CEO via a recursive CTE == 5 (LEVEL_SIZES)."""
    depth = self_ref_con.execute("""
        WITH RECURSIVE org AS (
            SELECT emp_id, 1 AS depth FROM employees WHERE mgr_id IS NULL
            UNION ALL
            SELECT e.emp_id, org.depth + 1
            FROM employees e JOIN org ON e.mgr_id = org.emp_id
        )
        SELECT max(depth) FROM org
    """).fetchone()[0]
    assert depth == len(self_ref.LEVEL_SIZES)


def test_employee_hierarchy_reaches_every_row(self_ref_con):
    """The recursive CTE from the CEO must reach every employee (single tree, no orphans)."""
    reached = self_ref_con.execute("""
        WITH RECURSIVE org AS (
            SELECT emp_id FROM employees WHERE mgr_id IS NULL
            UNION ALL
            SELECT e.emp_id FROM employees e JOIN org ON e.mgr_id = org.emp_id
        )
        SELECT count(*) FROM org
    """).fetchone()[0]
    assert reached == self_ref.N_EMPLOYEES


def test_bom_has_no_cycles(self_ref_con):
    """A recursive CTE walking parent->child must never revisit a node (DAG, acyclic)."""
    cyclic = self_ref_con.execute("""
        WITH RECURSIVE explode(root, current, depth, path) AS (
            SELECT parent_part_id, child_part_id, 1, [parent_part_id, child_part_id]
            FROM bom
            UNION ALL
            SELECT explode.root, b.child_part_id, explode.depth + 1, list_append(explode.path, b.child_part_id)
            FROM bom b JOIN explode ON b.parent_part_id = explode.current
            WHERE explode.depth < 20 AND NOT list_contains(explode.path, b.child_part_id)
        )
        SELECT count(*) FROM explode e1
        WHERE EXISTS (
            SELECT 1 FROM bom b WHERE b.parent_part_id = e1.current AND b.child_part_id = e1.root
        )
    """).fetchone()[0]
    assert cyclic == 0

    # depth bound: 4 part levels means max explosion depth is 3 edges
    max_depth = self_ref_con.execute("""
        WITH RECURSIVE explode(root, current, depth) AS (
            SELECT parent_part_id, child_part_id, 1 FROM bom
            UNION ALL
            SELECT explode.root, b.child_part_id, explode.depth + 1
            FROM bom b JOIN explode ON b.parent_part_id = explode.current
        )
        SELECT max(depth) FROM explode
    """).fetchone()[0]
    assert max_depth == len(self_ref.PART_LEVEL_SIZES) - 1


def test_self_ref_gold_hierarchies_are_recursive():
    gold = _load_gold("self_ref")
    hierarchies = gold["semantic_layer"]["hierarchies"]
    assert len(hierarchies) == 2
    for h in hierarchies:
        assert h["kind"] == "recursive"
        assert "parent_column" in h["recursive"]
        assert "child_column" in h["recursive"]

    rel_names = {(r["from"]["table"], r["to"]["table"]) for r in gold["semantic_layer"]["relationships"]}
    assert ("employees", "employees") in rel_names  # true self-referential relationship


def test_self_ref_gold_has_one_metric():
    gold = _load_gold("self_ref")
    metrics = gold["semantic_layer"]["metrics"]
    assert len(metrics) == 1
    assert metrics[0]["name"] == "headcount_by_dept"


# ---------------------------------------------------------------------------
# multi_tenant
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def multi_tenant_con():
    con = duckdb.connect(":memory:")
    multi_tenant.build(con)
    yield con
    con.close()


def test_all_20_schemas_exist(multi_tenant_con):
    schemas = {
        r[0] for r in multi_tenant_con.execute(
            "SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE 'tenant_%'"
        ).fetchall()
    }
    expected = {multi_tenant.tenant_schema(i) for i in range(1, multi_tenant.N_TENANTS + 1)}
    assert schemas == expected


def test_all_tenant_schemas_have_identical_column_structure(multi_tenant_con):
    """information_schema comparison: every tenant schema's 4 tables have identical
    (table_name, column_name, data_type, ordinal_position)."""
    rows = multi_tenant_con.execute("""
        SELECT table_schema, table_name, column_name, data_type, ordinal_position
        FROM information_schema.columns
        WHERE table_schema LIKE 'tenant_%'
        ORDER BY table_schema, table_name, ordinal_position
    """).fetchall()

    by_schema: dict[str, list[tuple]] = {}
    for schema, table, col, dtype, pos in rows:
        by_schema.setdefault(schema, []).append((table, col, dtype, pos))

    assert len(by_schema) == multi_tenant.N_TENANTS
    reference = by_schema[multi_tenant.tenant_schema(1)]
    assert len(reference) > 0
    for schema, cols in by_schema.items():
        assert cols == reference, f"{schema} column structure diverges from tenant_001"


def test_multi_tenant_row_counts_per_tenant(multi_tenant_con):
    for i in range(1, multi_tenant.N_TENANTS + 1):
        schema = multi_tenant.tenant_schema(i)
        n_orders = multi_tenant_con.execute(f"SELECT count(*) FROM {schema}.orders").fetchone()[0]
        assert n_orders == multi_tenant.N_ORDERS_PER_TENANT


def test_multi_tenant_determinism():
    a, b = duckdb.connect(":memory:"), duckdb.connect(":memory:")
    multi_tenant.build(a)
    multi_tenant.build(b)
    for i in range(1, multi_tenant.N_TENANTS + 1):
        schema = multi_tenant.tenant_schema(i)
        for t in ["customers", "products", "orders", "order_items"]:
            ra = a.execute(f"SELECT * FROM {schema}.{t} ORDER BY ALL").fetchall()
            rb = b.execute(f"SELECT * FROM {schema}.{t} ORDER BY ALL").fetchall()
            assert ra == rb, f"{schema}.{t} not deterministic"
    a.close(); b.close()


def test_multi_tenant_gold_has_exactly_4_tables_with_template():
    gold = _load_gold("multi_tenant")
    tables = gold["semantic_layer"]["tables"]
    assert len(tables) == 4
    names = {t["name"] for t in tables}
    assert names == {"customers", "products", "orders", "order_items"}
    for t in tables:
        assert "tenant_template" in t, f"table {t['name']} missing tenant_template"
        assert t["tenant_template"]["parameter"] == "tenant_schema"
        assert t["tenant_template"]["instances"] == 20
        assert "{tenant_schema}" in t["source"]


# ---------------------------------------------------------------------------
# collision_heavy
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def collision_con():
    con = duckdb.connect(":memory:")
    collision_heavy.build(con)
    yield con
    con.close()


def _inclusion_ratio(con, from_col: str, to_col: str) -> float:
    """Fraction of distinct values of from_col present in to_col's value set."""
    from_tbl, from_c = from_col.split(".")
    to_tbl, to_c = to_col.split(".")
    total, matched = con.execute(f"""
        SELECT
            count(DISTINCT f.{from_c}),
            count(DISTINCT f.{from_c}) FILTER (
                WHERE f.{from_c} IN (SELECT {to_c} FROM {to_tbl})
            )
        FROM {from_tbl} f
        WHERE f.{from_c} IS NOT NULL
    """).fetchone()
    if total == 0:
        return 0.0
    return matched / total


@pytest.mark.parametrize(
    "from_col,to_col,why_wrong", collision_heavy.TRAPS, ids=[t[0] for t in collision_heavy.TRAPS]
)
def test_seeded_trap_is_actually_a_high_inclusion_ratio(collision_con, from_col, to_col, why_wrong):
    """Verify the trap is real: >=0.99 inclusion ratio in the generated data,
    even though (per `why_wrong`) it is semantically absurd."""
    ratio = _inclusion_ratio(collision_con, from_col, to_col)
    assert ratio >= 0.99, f"trap {from_col} -> {to_col} only has ratio {ratio}; not actually a trap"


def test_at_least_6_traps_documented():
    assert len(collision_heavy.TRAPS) >= 6
    for from_col, to_col, why_wrong in collision_heavy.TRAPS:
        assert "." in from_col and "." in to_col
        assert why_wrong  # non-empty rationale


# 11 real relationships: the original 5 "documented" ones plus 6 more the
# generator demonstrably creates (adjudicated against generator source
# 2026-07-18 — randint over exactly the referenced table's range, semantic
# names). Gold curation, not score-chasing.
REAL_FKS = [
    ("orders", "customer_id", "customers", "customer_id"),
    ("order_items", "order_id", "orders", "order_id"),
    ("order_items", "product_id", "products", "product_id"),
    ("employees", "dept_id", "departments", "dept_id"),
    ("shipments", "order_id", "orders", "order_id"),
    ("customers", "region_id", "regions", "region_id"),
    ("stores", "region_cd", "regions", "region_id"),
    ("products", "category_id", "categories", "category_id"),
    ("shipments", "carrier_id", "carriers", "carrier_id"),
    ("inventory", "warehouse_id", "warehouses", "warehouse_id"),
    ("tickets", "priority_cd", "priorities", "priority_id"),
]


@pytest.mark.parametrize(
    "child_tbl,child_col,parent_tbl,parent_col", REAL_FKS,
    ids=[f"{c}.{cc}->{p}.{pc}" for c, cc, p, pc in REAL_FKS],
)
def test_real_fk_is_orphan_free(collision_con, child_tbl, child_col, parent_tbl, parent_col):
    orphans = collision_con.execute(f"""
        SELECT count(*) FROM {child_tbl} c
        LEFT JOIN {parent_tbl} p ON c.{child_col} = p.{parent_col}
        WHERE c.{child_col} IS NOT NULL AND p.{parent_col} IS NULL
    """).fetchone()[0]
    assert orphans == 0


def test_collision_heavy_determinism():
    a, b = duckdb.connect(":memory:"), duckdb.connect(":memory:")
    collision_heavy.build(a)
    collision_heavy.build(b)
    for t in collision_heavy.TABLE_NAMES:
        ra = a.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall()
        rb = b.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall()
        assert ra == rb, f"{t} not deterministic"
    a.close(); b.close()


def test_collision_heavy_gold_declares_exactly_the_real_fks():
    gold = _load_gold("collision_heavy")
    sl = gold["semantic_layer"]
    assert len(sl["relationships"]) == len(REAL_FKS)

    declared = {
        (r["from"]["table"], r["from"]["columns"][0], r["to"]["table"], r["to"]["columns"][0])
        for r in sl["relationships"]
    }
    assert declared == set(REAL_FKS)

    # column-level foreign_key blocks must match the same 5 real FKs, no more.
    fk_cols = set()
    for t in sl["tables"]:
        for c in t["columns"]:
            if c.get("foreign_key"):
                fk_cols.add((t["name"], c["name"]))
    assert fk_cols == {(c, cc) for c, cc, _, _ in REAL_FKS}


def test_collision_heavy_gold_does_not_declare_any_trap_as_fk():
    gold = _load_gold("collision_heavy")
    sl = gold["semantic_layer"]
    # a trap is a specific WRONG (child -> parent) pair; the child column may
    # legitimately have a real FK to a DIFFERENT parent (tickets.priority_cd
    # -> priorities is real; its trap was -> regions).
    traps = {(f, to) for f, to, _ in collision_heavy.TRAPS}
    for t in sl["tables"]:
        for c in t["columns"]:
            fk = c.get("foreign_key")
            if fk:
                pair = (f"{t['name']}.{c['name']}", fk["references"])
                assert pair not in traps, f"gold declares seeded trap {pair} as real FK"
