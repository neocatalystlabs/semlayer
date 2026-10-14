"""Live Postgres connector tests, against a real server.

`pgserver` ships a PostgreSQL binary, so this needs no Docker, no service and
no credentials -- which is the only reason these can be real integration tests
rather than mocks. If it is not installed the module skips: CI without the
dev extra still passes, it just stops covering this connector.

A mocked version of these tests would have passed against a connector that
read `information_schema.columns` for comments, which Postgres leaves empty.
That is the whole argument for doing it this way.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pgserver")
pytest.importorskip("psycopg")

from semlayer.connectors.postgres import PostgresSource
from semlayer.pipeline import infer_with_report, open_source_from_uri

DDL = """
DROP SCHEMA IF EXISTS mart CASCADE;
CREATE SCHEMA mart;
CREATE TABLE mart.cust_dim (cust_id INT PRIMARY KEY, cust_nm TEXT,
                            cntry_cd TEXT, state_cd TEXT, city_nm TEXT);
CREATE TABLE mart.ord_hdr (ord_id INT PRIMARY KEY, cust_id INT REFERENCES mart.cust_dim,
                           sts_cd TEXT, tot_amt NUMERIC(10,2), ord_dt DATE);
CREATE TABLE mart.ord_ln (ord_id INT, ln_num INT, qty INT, line_amt NUMERIC(10,2),
                          PRIMARY KEY (ord_id, ln_num),
                          FOREIGN KEY (ord_id) REFERENCES mart.ord_hdr);
COMMENT ON TABLE mart.ord_hdr IS 'one row per order header';
COMMENT ON COLUMN mart.ord_hdr.sts_cd IS 'X means cancelled; never counts toward revenue';
"""


@pytest.fixture(scope="session")
def pg_uri(tmp_path_factory):
    """A running PostgreSQL with a small three-table mart.

    pgserver shuts the server down when the last client in the process exits,
    so `get_server` must be called here rather than in a subprocess.
    """
    import pgserver
    import psycopg

    srv = pgserver.get_server(str(tmp_path_factory.mktemp("pgdata")))
    uri = srv.get_uri()
    with psycopg.connect(uri, autocommit=True) as c:
        c.execute(DDL)
        rows = [(i, f"cust{i}", "US" if i % 3 else "CA", f"S{i % 12}", f"C{i % 12}")
                for i in range(1, 61)]
        with c.cursor() as cur:
            cur.executemany("INSERT INTO mart.cust_dim VALUES (%s,%s,%s,%s,%s)", rows)
            cur.executemany(
                "INSERT INTO mart.ord_hdr VALUES (%s,%s,%s,%s,%s)",
                [(i, (i % 60) + 1, "X" if i % 17 == 0 else "A", 100 + i, "2024-01-01")
                 for i in range(1, 201)],
            )
            cur.executemany(
                "INSERT INTO mart.ord_ln VALUES (%s,%s,%s,%s)",
                [(i, ln, ln * 2, 10.0 * ln)
                 for i in range(1, 201) for ln in range(1, (i % 4) + 2)],
            )
        c.execute("ANALYZE")
    return uri


@pytest.fixture(scope="session")
def src(pg_uri):
    s = open_source_from_uri(pg_uri)
    yield s
    s.close()


def test_uri_dispatch_returns_postgres_source(src):
    assert isinstance(src, PostgresSource)


def test_lists_user_tables_and_excludes_catalogs(src):
    names = {t.qualified for t in src.list_tables()}
    assert names == {"mart.cust_dim", "mart.ord_hdr", "mart.ord_ln"}
    assert not any(t.schema.startswith("pg_") for t in src.list_tables())


def test_column_order_follows_ordinal_position(src):
    cust = next(t for t in src.list_tables() if t.name == "cust_dim")
    assert [c.name for c in cust.columns] == [
        "cust_id", "cust_nm", "cntry_cd", "state_cd", "city_nm",
    ]


def test_reads_table_and_column_comments(src):
    # Postgres keeps these in pg_description, NOT information_schema -- a
    # connector that queried information_schema.columns would return {} here.
    assert src.list_comments() == {
        ("mart", "ord_hdr", None): "one row per order header",
        ("mart", "ord_hdr", "sts_cd"): "X means cancelled; never counts toward revenue",
    }


def test_qualify_quotes_identifiers(src):
    assert src.qualify("mart", "ord_hdr") == '"mart"."ord_hdr"'


def test_row_cap_is_limit(src):
    assert src.row_cap_clause(5) == " LIMIT 5"


def test_query_executes_pushdown_sql(src):
    from semlayer.source import row_cap

    assert src.query(f"SELECT count(*) FROM {src.qualify('mart', 'ord_ln')}") == [(500,)]
    assert len(src.query(f"SELECT ord_id FROM {src.qualify('mart', 'ord_ln')}{row_cap(src, 7)}")) == 7


@pytest.fixture(scope="session")
def layer(src):
    doc, _ = infer_with_report(src, llm=None)
    return doc["semantic_layer"] if "semantic_layer" in doc else doc


def _table(layer, name):
    return next(t for t in layer["tables"] if t["name"] == name)


def test_infers_composite_primary_key(layer):
    assert _table(layer, "ord_ln")["primary_key"] == ["ord_id", "ln_num"]


def test_infers_both_foreign_keys(layer):
    edges = {(r["from"]["table"], r["to"]["table"]) for r in layer["relationships"]}
    assert edges == {("ord_hdr", "cust_dim"), ("ord_ln", "ord_hdr")}


def test_infers_geography_level_hierarchy(layer):
    levels = [
        [lv["column"] for lv in h["levels"]]
        for h in layer.get("hierarchies", [])
        if h["kind"] == "level"
    ]
    assert ["cntry_cd", "state_cd", "city_nm"] in levels


def test_catalog_comments_become_descriptions_with_provenance(layer):
    hdr = _table(layer, "ord_hdr")
    assert hdr["description"] == "one row per order header"
    assert any(p["signal"] == "catalog_prior" for p in hdr["provenance"])
    sts = next(c for c in hdr["columns"] if c["name"] == "sts_cd")
    assert sts["description"] == "X means cancelled; never counts toward revenue"
    assert any(p["signal"] == "catalog_prior" for p in sts["provenance"])


# --- minimal-grant proofs -------------------------------------------------
# `semlayer init postgres` emits a grant script and the docs make two claims
# about it: the reader cannot write, and it can read planner statistics with
# no extra grant. Both are asserted here against a real role rather than
# asserted in prose, because the Snowflake equivalents can only run against
# someone's live account and these can run in CI.

WRITE_ATTEMPTS = [
    "INSERT INTO mart.ord_ln VALUES (9999, 1, 1, 1)",
    "UPDATE mart.ord_hdr SET sts_cd = 'Z'",
    "DELETE FROM mart.cust_dim",
    "CREATE TABLE mart.evil (x int)",
    "CREATE TABLE public.evil (x int)",
    "DROP TABLE mart.ord_ln",
]


@pytest.fixture(scope="session")
def reader_dsn(pg_uri):
    """A role created by exactly the grants `semlayer init postgres` prints."""
    import psycopg

    from semlayer.init_cmd import render

    script = render("postgres", database="postgres", schema="mart")
    statements = [
        ln.rstrip(";") for ln in script.splitlines()
        if ln.strip() and not ln.strip().startswith("--")
    ]
    # The emitted CREATE ROLE carries a placeholder password; trust auth on the
    # local socket means we do not need a real one.
    statements = [s for s in statements if not s.startswith("CREATE ROLE")]
    with psycopg.connect(pg_uri, autocommit=True) as c:
        c.execute("DROP ROLE IF EXISTS semlayer_reader")
        c.execute("CREATE ROLE semlayer_reader LOGIN")
        for stmt in statements:
            c.execute(stmt)
    assert any(s.startswith("GRANT SELECT ON ALL TABLES") for s in statements)
    assert any(s.startswith("ALTER DEFAULT PRIVILEGES") for s in statements)
    return pg_uri.replace("postgres:@", "semlayer_reader:@")


def test_reader_role_is_not_superuser(reader_dsn):
    import psycopg

    with psycopg.connect(reader_dsn, autocommit=True) as c:
        user, is_super = c.execute(
            "SELECT current_user, (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)"
        ).fetchone()
    assert (user, is_super) == ("semlayer_reader", False)


def test_reader_can_run_full_inference(reader_dsn):
    src = open_source_from_uri(reader_dsn)
    try:
        doc, _ = infer_with_report(src, llm=None)
    finally:
        src.close()
    sl = doc["semantic_layer"] if "semantic_layer" in doc else doc
    assert {t["name"] for t in sl["tables"]} == {"cust_dim", "ord_hdr", "ord_ln"}
    assert len(sl["relationships"]) == 2


@pytest.mark.parametrize("stmt", WRITE_ATTEMPTS)
def test_reader_cannot_write(reader_dsn, stmt):
    import psycopg

    with psycopg.connect(reader_dsn, autocommit=True) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute(stmt)


def test_reader_reads_planner_statistics_without_extra_grants(reader_dsn):
    """pg_stats needs no pg_read_all_stats: it filters to SELECT-able tables.

    This is the Postgres equivalent of Oracle's data-dictionary trick -- row
    counts and distinct counts without scanning -- and the grant script says
    so, so the claim is tested.
    """
    import psycopg

    with psycopg.connect(reader_dsn, autocommit=True) as c:
        stats = c.execute(
            "SELECT tablename, attname, n_distinct, null_frac FROM pg_stats "
            "WHERE schemaname = 'mart'"
        ).fetchall()
        reltuples = dict(c.execute(
            "SELECT relname, reltuples FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'mart' AND relkind = 'r'"
        ).fetchall())
    assert len(stats) == 14, "every column's statistics must be visible"
    assert reltuples == {"cust_dim": 60.0, "ord_hdr": 200.0, "ord_ln": 500.0}
    # Postgres encodes n_distinct as a NEGATIVE number when it is a ratio of
    # the row count: -1.0 means "distinct in every row", i.e. a key. Reading it
    # as a count would make every unique column look like it had one value.
    by_col = {(t, a): n for t, a, n, _ in stats}
    assert by_col[("cust_dim", "cust_id")] == -1.0
    assert by_col[("ord_hdr", "sts_cd")] == 2.0


def test_drift_detects_ddl_on_postgres(src, layer):
    """`semlayer drift <doc> postgres` is advertised, so the path is exercised.

    Snapshot diffing is the universal fallback tier (PRD boundary decision 4) --
    it needs nothing but `list_tables`, which is exactly why it should work on
    any new connector without connector-specific code. Asserted rather than
    assumed.
    """
    import psycopg

    from semlayer import drift

    # The DDL runs in a throwaway schema, NOT on mart. An earlier version of
    # this test altered mart.cust_dim and "restored" the column with ALTER
    # TABLE ADD COLUMN -- which brings the column back empty, silently nulling
    # 60 rows and costing any later test a hierarchy level. Mutating a shared
    # fixture and putting it back is not the same as not mutating it.
    with psycopg.connect(src.con.info.dsn, autocommit=True) as c:
        c.execute("CREATE SCHEMA dtest")
        c.execute("CREATE TABLE dtest.thing (thing_id INT PRIMARY KEY, old_cd TEXT)")
    try:
        before = drift.snapshot(src)
        with psycopg.connect(src.con.info.dsn, autocommit=True) as c:
            c.execute("ALTER TABLE dtest.thing ADD COLUMN promo_cd TEXT")
            c.execute("ALTER TABLE dtest.thing DROP COLUMN old_cd")
        events = drift.diff_snapshots(before, drift.snapshot(src))
    finally:
        with psycopg.connect(src.con.info.dsn, autocommit=True) as c:
            c.execute("DROP SCHEMA dtest CASCADE")
    found = {(e.kind, e.table, e.column) for e in events}
    assert found == {
        ("column_added", "thing", "promo_cd"),
        ("column_dropped", "thing", "old_cd"),
    }


def test_views_are_listed_and_catalog_schemas_are_not(src):
    """The connector claims views are included; a presentation layer is often views.

    Created in a throwaway schema so the shared mart fixture, whose table and
    relationship counts other tests assert exactly, is left alone.
    """
    import psycopg

    with psycopg.connect(src.con.info.dsn, autocommit=True) as c:
        c.execute("CREATE SCHEMA vtest")
        c.execute("CREATE VIEW vtest.active_orders AS "
                  "SELECT ord_id, tot_amt FROM mart.ord_hdr WHERE sts_cd <> 'X'")
    try:
        tables = src.list_tables()
        view = next((t for t in tables if t.qualified == "vtest.active_orders"), None)
        assert view is not None, "views must be enumerated, not just base tables"
        assert [col.name for col in view.columns] == ["ord_id", "tot_amt"]
        assert src.query(f"SELECT count(*) FROM {src.qualify('vtest', 'active_orders')}") == [(189,)]
        # pg_catalog and pg_toast are large and would otherwise dominate a scan.
        assert not any(t.schema == "pg_catalog" or t.schema.startswith("pg_") for t in tables)
        assert not any(t.schema == "information_schema" for t in tables)
    finally:
        with psycopg.connect(src.con.info.dsn, autocommit=True) as c:
            c.execute("DROP SCHEMA vtest CASCADE")


# --- the source protocol is a two-method contract --------------------------
# Not Postgres-specific, but it needs a real server, so it lives here.
#
# This is the open-door guard for a generic connector -- JDBC/ODBC via a
# bridge, SQLAlchemy, or any engine we have not met. Such a source can offer
# generic metadata and generic SQL and nothing else: no vendor catalog, no
# planner statistics, no dialect-specific row cap. If the pipeline ever starts
# REQUIRING a capability beyond those two methods, that option quietly closes,
# and the person who closed it will not notice. This test notices.
#
# Keep it failing loudly rather than relaxing it: the fix is to probe the new
# capability with getattr/hasattr and degrade, the way row_cap_clause,
# list_comments, qualify and quote_ident already do.

class MinimalSource:
    """A source implementing ONLY the two required methods, and nothing else.

    Deliberately has no `qualify`, `row_cap_clause`, `list_comments`,
    `quote_ident` or `close`. Do not add any -- that is the point of it.
    """

    def __init__(self, dsn: str):
        import psycopg

        self._con = psycopg.connect(dsn, autocommit=True)

    def list_tables(self):
        from semlayer.source import ColumnMeta, TableMeta

        rows = self._con.execute(
            "SELECT table_schema, table_name, column_name, data_type "
            "FROM information_schema.columns WHERE table_schema = 'mart' "
            "ORDER BY table_schema, table_name, ordinal_position"
        ).fetchall()
        grouped: dict = {}
        for schema, table, col, dtype in rows:
            grouped.setdefault((schema, table), []).append(ColumnMeta(col, dtype))
        return [TableMeta(s, t, tuple(c)) for (s, t), c in grouped.items()]

    def query(self, sql: str):
        return list(self._con.execute(sql).fetchall())


def _shape(layer):
    """Everything inference claims, except what only a catalog can supply."""
    return {
        "tables": sorted(
            (t["name"], t.get("table_type"), tuple(t.get("primary_key") or ()))
            for t in layer["tables"]
        ),
        "relationships": sorted(
            (r["from"]["table"], tuple(r["from"]["columns"]),
             r["to"]["table"], tuple(r["to"]["columns"]), r.get("cardinality"))
            for r in layer["relationships"]
        ),
        "hierarchies": sorted(
            (h["kind"], h["dimension_table"], tuple(lv["column"] for lv in h["levels"]))
            for h in layer.get("hierarchies", [])
        ),
        "metrics": sorted(m["name"] for m in layer.get("metrics", [])),
    }


def test_minimal_two_method_source_completes_full_inference(pg_uri):
    """list_tables + query must be the entire requirement."""
    src = MinimalSource(pg_uri)
    for absent in ("qualify", "row_cap_clause", "list_comments", "quote_ident"):
        assert not hasattr(src, absent), f"MinimalSource must not implement {absent}"
    doc, report = infer_with_report(src, llm=None)
    sl = doc["semantic_layer"] if "semantic_layer" in doc else doc
    assert report["counts"]["tables"] == 3
    assert _shape(sl)["relationships"], "a generic source must still find joins"


def test_generic_source_loses_only_catalog_descriptions(pg_uri, layer):
    """Quantifies what a generic connector would actually cost us.

    Measured, not assumed: everything structural -- table types, the composite
    primary key, both foreign keys, the three-level hierarchy, the metrics --
    is identical. The only loss is the native COMMENT text, and a JDBC bridge
    might even supply that through DatabaseMetaData REMARKS.
    """
    doc, _ = infer_with_report(MinimalSource(pg_uri), llm=None)
    generic = doc["semantic_layer"] if "semantic_layer" in doc else doc

    assert _shape(generic) == _shape(layer)

    full_descriptions = {t["name"]: t.get("description") for t in layer["tables"]}
    generic_descriptions = {t["name"]: t.get("description") for t in generic["tables"]}
    assert full_descriptions["ord_hdr"] == "one row per order header"
    assert generic_descriptions["ord_hdr"] is None
    assert not any(
        p["signal"] == "catalog_prior"
        for t in generic["tables"] for p in t.get("provenance", [])
    )


def test_reads_declared_constraints_from_pg_constraint(src):
    """Rung 1 on Postgres: ordinal arrays, so no DDL text to parse.

    `pg_constraint` pairs child and parent columns by position via `conkey` /
    `confkey`, which is what makes a composite foreign key come out in
    declaration order rather than alphabetised.
    """
    d = src.list_constraints()
    assert d, "the mart declares keys; an empty result means the query is wrong"
    assert d.primary_keys[("mart", "cust_dim")] == ("cust_id",)
    assert d.primary_keys[("mart", "ord_hdr")] == ("ord_id",)
    assert d.primary_keys[("mart", "ord_ln")] == ("ord_id", "ln_num")

    edges = {(f.child_table, f.child_columns, f.parent_table, f.parent_columns)
             for f in d.foreign_keys}
    assert edges == {
        ("ord_hdr", ("cust_id",), "cust_dim", ("cust_id",)),
        ("ord_ln", ("ord_id",), "ord_hdr", ("ord_id",)),
    }
    assert all(f.enforced for f in d.foreign_keys), "these are validated"


def test_declared_constraints_readable_at_minimal_grant(reader_dsn):
    """The grant script must be sufficient for rung 1 too.

    pg_constraint is world-readable, but the join reaches pg_attribute and
    pg_class for names, so this is worth asserting rather than assuming -- the
    same way pg_stats turned out to need no extra grant.
    """
    src = PostgresSource.from_dsn(reader_dsn)
    try:
        d = src.list_constraints()
    finally:
        src.close()
    assert d.primary_keys[("mart", "ord_ln")] == ("ord_id", "ln_num")
    assert len(d.foreign_keys) == 2


# --- rung 0: the engine's own statistics ----------------------------------

def test_reads_engine_statistics_without_touching_a_table(src):
    """pg_stats needs no extra grant and no scan."""
    stats = src.list_column_statistics()
    assert stats, "the mart has been ANALYZEd; empty means the query is wrong"
    cs = stats[("mart", "ord_ln", "ord_id")]
    assert cs.analyzed_at, "staleness must be knowable, not assumed"


def test_negative_n_distinct_is_converted_from_a_ratio(src):
    """Postgres stores n_distinct as a NEGATIVE number when it means a ratio.

    -1.0 is "distinct in every row", i.e. a key. Read as a count it becomes 1,
    which makes every key in the warehouse look single-valued -- the single
    easiest way to get rung 0 completely wrong.
    """
    stats = src.list_column_statistics()
    cs = stats[("mart", "ord_hdr", "ord_id")]
    rows = src.query("SELECT count(*) FROM mart.ord_hdr")[0][0]
    assert cs.distinct_is_ratio, "a unique column is stored as a ratio"
    assert cs.n_distinct == rows, f"expected {rows} distinct, got {cs.n_distinct}"


def test_null_fraction_is_accurate_enough_to_use(src):
    """Measured at <=0.002 absolute error over 0%, 14% and 99% null columns."""
    stats = src.list_column_statistics()
    cs = stats[("mart", "ord_hdr", "sts_cd")]
    exact = src.query(
        "SELECT (count(*) - count(sts_cd))::float / count(*) FROM mart.ord_hdr")[0][0]
    assert cs.null_frac is not None
    assert abs(cs.null_frac - exact) < 0.01


def test_engine_statistics_readable_at_minimal_grant(reader_dsn):
    """pg_stats filters to tables the caller can SELECT, so no extra grant."""
    s = PostgresSource.from_dsn(reader_dsn)
    try:
        stats = s.list_column_statistics()
    finally:
        s.close()
    assert ("mart", "ord_ln", "ord_id") in stats
