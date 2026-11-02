# SPDX-License-Identifier: FSL-1.1-ALv2
"""Source abstraction: MetadataProvider + QueryExecutor.

The split is deliberate: a catalog-native metadata provider (Iceberg
REST Catalog, Glue) can later pair with any executor (customer engine or
embedded DuckDB) without refactoring. Warehouse connectors implement both.

**A source is required to implement exactly two methods**: `list_tables()` and
`query(sql)`. Everything else here is optional and is probed with
getattr/hasattr so that a source lacking it degrades rather than fails --
`row_cap_clause`, `list_comments`, `qualify`, `quote_ident`. Measured on a real
server: a source with only the two required methods produces output identical
to the full Postgres connector apart from catalog descriptions.

Keep it that way. The two-method floor is what holds open the option of a
generic connector (JDBC/ODBC via a bridge, SQLAlchemy, an engine we have not
met) for a user who is blocked at onboarding and has no patience for an
architecture argument. If you need a new capability, probe for it and degrade;
do NOT make it required. `tests/test_postgres.py::MinimalSource` fails if you
do. The trigger to build one is a warehouse whose catalog already carries the
statistics, where a full scan would re-derive what it could have read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ColumnMeta:
    """A single column's name and warehouse-reported SQL type."""

    name: str
    sql_type: str


@dataclass(frozen=True)
class TableMeta:
    """A table's schema, name, and columns as reported by a metadata provider."""

    schema: str
    name: str
    columns: tuple[ColumnMeta, ...]

    @property
    def qualified(self) -> str:
        """Schema-qualified name, e.g. 'analytics.orders'."""
        return f"{self.schema}.{self.name}"


@runtime_checkable
class MetadataProvider(Protocol):
    """Read-only catalog access: enumerate tables and columns, no data."""

    def list_tables(self) -> list[TableMeta]:
        """Return all tables visible to the connection, outside internal schemas."""
        ...


# Row caps are the one construct with NO portable spelling across our targets.
# `FETCH FIRST n ROWS ONLY` is ANSI and works on Oracle, Postgres, Snowflake and
# DuckDB; BigQuery rejects it and wants `LIMIT n`. Everything else we push down
# is written to be portable by construction -- see tests/test_portability.py,
# which fails if a dialect-specific form creeps back in.
#
# sqlglot is NOT used to transpile at runtime even though we depend on it for
# the linter: it passes `FILTER (WHERE ...)` through verbatim to Oracle, MySQL
# and T-SQL, where it is a syntax error. A transpiler that silently emits broken
# SQL for one construct is worse than writing portable SQL by hand.
def _strip_idents(csv: str) -> list[str]:
    """Split a constraint's column list, dropping quotes and whitespace."""
    return [c.strip().strip('"') for c in csv.split(",") if c.strip()]


def row_cap(source, n: int) -> str:
    """The source dialect's trailing row-limit clause, e.g. ' LIMIT 10'."""
    return getattr(source, "row_cap_clause", lambda k: f" LIMIT {k}")(n)


def qcol(source, name: str) -> str:
    """Quote one identifier the way THIS source quotes identifiers.

    Use this for every column or alias that goes into generated SQL. The
    alternative -- an f-string with literal double quotes -- is what PROFILE
    avoided and what ENRICH, LINK and probe did not, and on BigQuery it is
    not a quoting style but a different kind of expression: double quotes
    there delimit a STRING, so `sum("amount")` asks BigQuery to add up the
    text "amount" and it answers

        No matching signature for aggregate function SUM
          Argument 1: Unable to coerce type STRING to expected type FLOAT64

    which is why `semlayer infer` could profile a BigQuery project and never
    finish enriching one. DuckDB and Postgres hid it because double quotes
    genuinely are their identifier quote; Snowflake would have failed
    differently, since its quoted identifiers are case-SENSITIVE while
    unquoted names fold to upper case, so `"order_total"` does not match a
    stored ORDER_TOTAL.

    The fallback doubles any embedded quote, which the f-strings it replaces
    did not: a column actually named `we"ird` produced a syntax error that
    blamed the SQL rather than the name.
    """
    quoter = getattr(source, "quote_ident", None)
    if quoter is not None:
        return quoter(name)
    return '"' + name.replace('"', '""') + '"'


def qtable(source, schema: str, table: str) -> str:
    """Qualify one table the way THIS source qualifies tables.

    The same job `qcol` does, for the other half of a FROM clause. Most
    callers already reach for `source.qualify`; this exists so a caller does
    not have to carry its own fallback lambda, and so the fallback is written
    once with the quote-doubling the inline ones omit.

    SnowflakeSource defines no `qualify`, so the fallback is live rather than
    decorative: it quotes the names exactly as `list_tables` reported them,
    which is what Snowflake needs, because a quoted identifier there is
    case-sensitive and unquoted names are stored folded to upper case.
    """
    qualifier = getattr(source, "qualify", None)
    if qualifier is not None:
        return qualifier(schema, table)
    return f"{qcol(source, schema)}.{qcol(source, table)}"


@runtime_checkable
class CatalogProvider(Protocol):
    """Native catalog descriptions: the cheapest prior knowledge there is.

    If a warehouse has column comments, a human has already written down what
    we are otherwise inferring, and a human's sentence is better evidence than
    any naming heuristic. We ignored them entirely until 2026-10-12 while
    nine separate naming vocabularies were failing on schemas we did not
    write.

    Optional: a source without comments returns {} and nothing downstream
    changes.
    """

    def list_comments(self) -> dict[tuple[str, str, str | None], str]:
        """{(schema, table, column or None): comment}. None column = the table's own."""
        ...


@dataclass(frozen=True)
class DeclaredFK:
    """A foreign key the warehouse itself declares.

    `enforced` is False wherever the engine records the constraint but never
    checks it -- Snowflake and BigQuery never enforce, and Oracle allows
    DISABLE/NOVALIDATE. An unenforced constraint is still a human writing down
    which tables join, which is better evidence than any naming heuristic, but
    it is a CLAIM rather than a guarantee and the provenance has to say so.
    """

    child_schema: str
    child_table: str
    child_columns: tuple[str, ...]
    parent_schema: str
    parent_table: str
    parent_columns: tuple[str, ...]
    enforced: bool = False


@dataclass(frozen=True)
class DeclaredConstraints:
    """Declared keys read from the catalog: rung 1 of the read-before-scan ladder.

    Free -- one catalog query per source, no table touched. It also REMOVES
    work: a declared primary key skips composite-key probing (up to 45
    DISTINCT-over-a-tuple scans per table), and a declared foreign key skips an
    inclusion check.
    """

    primary_keys: dict[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)
    foreign_keys: tuple[DeclaredFK, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.primary_keys or self.foreign_keys)


@dataclass(frozen=True)
class Partition:
    """One physical partition of a table, and a token for "has it changed?".

    `change_token` is opaque and compared only for EQUALITY. It must never be
    compared as an ordering: Postgres' `n_tup_*` counters are cumulative since
    the last stats reset, so `pg_stat_reset()` makes them go backwards. Any
    difference -- including a decrease -- means re-profile.

    What the token must catch is the BACKFILL: a correction rewriting an old
    partition in place. Verified on PostgreSQL 16 -- an UPDATE of 10,000 rows
    moved `n_tup_upd` by 10,000 and left the row count completely unchanged, so
    a token built from row counts would silently keep a stale sketch forever.
    `est_rows` is therefore a convenience, never the change signal.
    """

    schema: str
    name: str
    parent_schema: str
    parent_table: str
    bound: str = ""            # human-readable range/list bound, for provenance
    change_token: str = ""
    est_rows: int = -1         # from the catalog, no scan; -1 if unknown

    @property
    def qualified(self) -> str:
        """Schema-qualified partition name, e.g. 'w.evt_2024_01'."""
        return f"{self.schema}.{self.name}"


@dataclass(frozen=True)
class ColumnStatistic:
    """A statistic the engine already computed, read without touching the table.

    Rung 0 of the read-before-scan ladder: a mature warehouse has mostly
    already computed what we profile. These are ESTIMATES from the engine's own
    sampling, and they go stale -- `analyzed_at` is the whole reason this type
    carries more than numbers. Trusting a stale figure silently is exactly the
    failure mode we sell against.

    `n_distinct` is normalised to a COUNT here. Postgres stores it as a
    negative number when it means a ratio of the row count: -1.0 is "distinct
    in every row", i.e. a key. Reading that as a count makes every key in the
    warehouse look like a single-valued column.
    """

    n_distinct: int | None = None
    null_frac: float | None = None
    most_common: tuple[str, ...] = ()
    analyzed_at: str | None = None        # ISO; None = never analyzed
    distinct_is_ratio: bool = False       # the raw value was a ratio


@runtime_checkable
class StatisticsProvider(Protocol):
    """The engine's own gathered statistics. Free, and possibly stale."""

    def list_column_statistics(self) -> dict[tuple[str, str, str], ColumnStatistic]:
        """(schema, table, column) -> what the engine already knows."""
        ...


def source_identity(source) -> str:
    """A stable, credential-free name for this warehouse, for state keys.

    It matters for correctness, not tidiness: two warehouses sharing a state
    file would otherwise share cached statistics, and one would silently be
    profiled with the other's numbers. A source that cannot identify itself
    falls back to its class name, which is weak -- so point separate state
    files at separate warehouses.
    """
    getter = getattr(source, "source_id", None)
    if callable(getter):
        try:
            got = getter()
            if got:
                return str(got)
        except Exception:
            pass
    return type(source).__name__


def warehouse_uri(source) -> str:
    """The warehouse a document was inferred from, as the document records it.

    Credential-free and portable: consumers compare two documents' values to
    decide whether their tables can meet in one SQL statement, so it must be
    the same string on every machine that points at the same warehouse. A
    DuckDB file is named by its basename, not its path, for that reason.
    Distinct from `source_identity`, which keys state files and may be as
    specific as it likes.
    """
    kind = type(source).__name__
    if kind == "DuckDBSource":
        import os
        ident = source_identity(source).removeprefix("duckdb:")
        if not ident or ident in (kind, "memory"):
            return "duckdb:memory"
        return "duckdb:" + os.path.basename(ident.split(";")[0])
    if kind == "SnowflakeSource":
        return f"snowflake:{getattr(source, 'database', '')}"
    if kind == "BigQuerySource":
        return f"bigquery:{getattr(source, 'project', '')}"
    ident = source_identity(source)
    return ident if ident != kind else kind.lower()


@runtime_checkable
class SketchProvider(Protocol):
    """Mergeable distinct-count sketches, so partitions can be combined.

    This is not a speed optimisation, it is a correctness requirement of
    partition-wise profiling. Merging counts alone bounds a distinct count at
    `max(d_i) <= D <= sum(d_i)`, which reads a genuine 60,000-row key spread
    over six partitions as `[10,000 .. 60,000]` -- so without sketches,
    partition-wise profiling cannot detect primary keys at all.

    A sketch still never PROVES uniqueness: an estimate of 3,001,357 +/- 95,000
    cannot establish "distinct in exactly 3,000,000 rows". Its job is to narrow
    the bounds enough to say which columns deserve an exact check.

    Sketches are PRODUCER-BOUND. Snowflake `HLL_ACCUMULATE`, BigQuery
    `HLL_COUNT.INIT` and Apache DataSketches blobs are not mutually readable, so
    a cross-producer union would be silent garbage rather than an error. Hence
    `sketch_producer`, which is stored alongside every sketch.
    """

    def sketch_producer(self) -> str:
        """Stable name of the sketch implementation, or "" if unavailable."""
        ...

    def build_distinct_sketch(self, qualified: str, column: str) -> str | None:
        """A serialised sketch of that column's distinct values, or None."""
        ...

    def estimate_sketch_union(self, sketches: list[str]) -> tuple[int, int, int] | None:
        """Union serialised sketches -> (estimate, lower, upper), or None."""
        ...


@runtime_checkable
class PartitionProvider(Protocol):
    """Physical partitions, so re-inference can touch only what changed.

    Optional. A source without it is profiled whole, exactly as before.
    """

    def list_partitions(self) -> dict[tuple[str, str], tuple[Partition, ...]]:
        """(schema, table) -> its partitions. Absent key = not partitioned."""
        ...


@runtime_checkable
class ConstraintProvider(Protocol):
    """Declared primary and foreign keys, where the catalog records them.

    Optional, like CatalogProvider: a source without it returns nothing and
    discovery proceeds exactly as before.
    """

    def list_constraints(self) -> DeclaredConstraints:
        """Declared PKs and FKs for every table visible to the connection."""
        ...


@runtime_checkable
class QueryExecutor(Protocol):
    """Read-only SQL execution against the warehouse, for profiling/sampling."""

    def query(self, sql: str) -> list[tuple]:
        """Run `sql` and return the result rows."""
        ...


class DuckDBSource:
    """MetadataProvider + QueryExecutor over a DuckDB connection.

    The fixture connector — proves the interface split (M1 exit criterion)
    and is the CI test bed for every pipeline stage.
    """

    INTERNAL_SCHEMAS = ("information_schema", "pg_catalog")
    INTERNAL_CATALOGS = ("system", "temp")

    def row_cap_clause(self, n: int) -> str:
        """DuckDB takes either; LIMIT is the form the fixture SQL is read in."""
        return f" LIMIT {n}"

    def source_id(self) -> str:
        """The database file, which is what distinguishes one DuckDB from another."""
        try:
            rows = self.con.execute(
                "SELECT path FROM duckdb_databases() WHERE NOT internal "
                "AND path IS NOT NULL ORDER BY 1").fetchall()
        except Exception:
            return "duckdb"
        return "duckdb:" + ",".join(r[0] for r in rows) if rows else "duckdb:memory"

    def __init__(self, con):
        self.con = con
        self._sketches: bool | None = None
        # (schema, table) -> catalog, for tables living in ATTACHed catalogs
        # (Iceberg REST catalogs, secondary .duckdb files). Same-named tables
        # across catalogs collide by design in v1 — the pipeline keys tables
        # by name; attach one external catalog at a time.
        self._catalog_of: dict[tuple[str, str], str] = {}
        self._current_catalog: str = con.execute("SELECT current_catalog()").fetchone()[0]

    def list_tables(self) -> list[TableMeta]:
        """Enumerate tables + columns via information_schema, skipping internal schemas.

        Spans ATTACHed catalogs too (that is how DuckDB surfaces Iceberg REST
        catalogs / Glue / S3 Tables), recording each table's catalog so
        `qualify` can produce fully-resolvable names.
        """
        rows = self.con.execute(
            """
            SELECT table_catalog, table_schema, table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema NOT IN ('information_schema', 'pg_catalog')
              AND table_catalog NOT IN ('system', 'temp')
            ORDER BY table_catalog, table_schema, table_name, ordinal_position
            """
        ).fetchall()
        tables: dict[tuple[str, str], list[ColumnMeta]] = {}
        for catalog, schema, table, col, dtype in rows:
            if catalog != self._current_catalog:
                self._catalog_of[(schema, table)] = catalog
            tables.setdefault((schema, table), []).append(ColumnMeta(col, dtype))
        return [
            TableMeta(schema, table, tuple(cols))
            for (schema, table), cols in tables.items()
        ]

    def list_comments(self) -> dict[tuple[str, str, str | None], str]:
        """Native COMMENT ON text, from duckdb_tables()/duckdb_columns().

        Not in information_schema -- DuckDB exposes comments only through its
        own metadata functions, which is why a generic information_schema
        query finds nothing.
        """
        out: dict[tuple[str, str, str | None], str] = {}
        for sql, has_col in (
            ("SELECT schema_name, table_name, NULL, comment FROM duckdb_tables()", False),
            ("SELECT schema_name, table_name, column_name, comment FROM duckdb_columns()", True),
        ):
            try:
                rows = self.con.execute(sql).fetchall()
            except Exception:   # older DuckDB without comment support
                continue
            for schema, table, col, comment in rows:
                if comment and schema not in self.INTERNAL_SCHEMAS:
                    out[(schema, table, col if has_col else None)] = comment
        return out

    # `FOREIGN KEY (a, b) REFERENCES sch.tbl(x, y)` -- DuckDB exposes the child
    # columns structurally but the TARGET only inside constraint_text, so it
    # has to be parsed. The format is stable; a line that does not match is
    # skipped rather than guessed at.
    _FK_RE = re.compile(
        r"FOREIGN KEY\s*\(([^)]*)\)\s*REFERENCES\s+([^(]+?)\s*\(([^)]*)\)",
        re.IGNORECASE)

    # lg_config_k = 12 gives ~2 KB per sketch and ~1.6% standard error, which
    # is ample for "might this be a key?" -- the exact check decides.
    _SKETCH_K = 12

    def _has_datasketches(self) -> bool:
        """Is the datasketches extension loaded? Cached, since it cannot change."""
        if self._sketches is None:
            try:
                self.con.execute("LOAD datasketches")
                self.con.execute("SELECT datasketch_hll_estimate(datasketch_hll(12, 1))")
                self._sketches = True
            except Exception:
                # DuckDB's BUILT-IN approx_count_distinct is also HLL but exposes
                # no sketch, so there is nothing to merge; and it measured 24.9%
                # error at 1,000 distinct, so it is not a substitute.
                self._sketches = False
        return self._sketches

    def sketch_producer(self) -> str:
        """Apache DataSketches via the DuckDB community extension, or ""."""
        return f"datasketches-hll-k{self._SKETCH_K}" if self._has_datasketches() else ""

    def build_distinct_sketch(self, qualified: str, column: str) -> str | None:
        """Serialise an HLL sketch of one column to base64."""
        if not self._has_datasketches():
            return None
        q = '"' + column.replace('"', '""') + '"'
        row = self.con.execute(
            f"SELECT to_base64(datasketch_hll({self._SKETCH_K}, {q})::BLOB) "
            f"FROM {qualified} WHERE {q} IS NOT NULL").fetchone()
        return row[0] if row and row[0] else None

    def estimate_sketch_union(self, sketches: list[str]) -> tuple[int, int, int] | None:
        """Union base64 sketches and return (estimate, lower, upper) at 95%."""
        if not sketches or not self._has_datasketches():
            return None
        # Passed as literals rather than a temp table so this stays read-only.
        vals = ", ".join("(?)" for _ in sketches)
        try:
            row = self.con.execute(
                f"WITH s(b) AS (VALUES {vals}), "
                f"d AS (SELECT from_base64(b)::sketch_hll AS sk FROM s), "
                f"u AS (SELECT datasketch_hll_union({self._SKETCH_K}, sk) AS k FROM d) "
                f"SELECT datasketch_hll_estimate(k), datasketch_hll_lower_bound(k, 2), "
                f"       datasketch_hll_upper_bound(k, 2) FROM u", sketches).fetchone()
        except Exception:
            # These bytes came out of OUR state file, so malformed means corrupt
            # state -- which the cache rule says must degrade to a re-profile,
            # never abort inference.
            return None
        if not row or row[0] is None:
            return None
        return round(row[0]), int(row[1]), round(row[2])

    def list_constraints(self) -> DeclaredConstraints:
        """Declared PKs and FKs from duckdb_constraints()."""
        pks: dict[tuple[str, str], tuple[str, ...]] = {}
        fks: list[DeclaredFK] = []
        try:
            rows = self.con.execute(
                "SELECT schema_name, table_name, constraint_type, "
                "constraint_column_names, constraint_text FROM duckdb_constraints()"
            ).fetchall()
        except Exception:      # older DuckDB without the table function
            return DeclaredConstraints()
        for schema, table, ctype, cols, text in rows:
            if schema in self.INTERNAL_SCHEMAS:
                continue
            if ctype == "PRIMARY KEY" and cols:
                pks[(schema, table)] = tuple(cols)
            elif ctype == "FOREIGN KEY":
                m = self._FK_RE.search(text or "")
                if not m:
                    continue
                child_cols = tuple(_strip_idents(m.group(1)))
                target = m.group(2).strip().strip('"')
                parent_cols = tuple(_strip_idents(m.group(3)))
                pschema, _, ptable = target.rpartition(".")
                fks.append(DeclaredFK(
                    schema, table, child_cols,
                    (pschema or schema).strip('"'), ptable.strip('"'), parent_cols,
                    enforced=True))     # DuckDB does enforce
        return DeclaredConstraints(pks, tuple(fks))

    def qualify(self, schema: str, table: str) -> str:
        """Catalog-qualified name for attached-catalog tables; schema.table otherwise."""
        def q(ident: str) -> str:
            return '"' + ident.replace('"', '""') + '"'
        catalog = self._catalog_of.get((schema, table))
        if catalog is not None:
            return f"{q(catalog)}.{q(schema)}.{q(table)}"
        return f"{q(schema)}.{q(table)}"

    def query(self, sql: str) -> list[tuple]:
        """Execute `sql` on the underlying DuckDB connection and fetch all rows."""
        return self.con.execute(sql).fetchall()

    def for_worker(self) -> WorkerExecutor:
        """An executor one thread may use while others use the connection.

        DuckDB forbids two threads sharing one connection object; `cursor()`
        opens a second connection to the same database. Everything but
        `query` -- qualification, quoting, the attached-catalog map -- is
        delegated back to this source, so a query built through the worker
        is byte-identical to one built through the source.
        """
        return WorkerExecutor(self, self.con.cursor())


class WorkerExecutor:
    """A QueryExecutor for one worker thread, backed by its own cursor.

    `query` goes to the private cursor; every other attribute is read from
    the source it was made from. `close()` releases the cursor only -- never
    the source's connection.
    """

    def __init__(self, source, cursor):
        self._source = source
        self._cursor = cursor

    def query(self, sql: str) -> list[tuple]:
        """Execute `sql` on this worker's private cursor and fetch all rows."""
        return self._cursor.execute(sql).fetchall()

    def close(self) -> None:
        """Release the private cursor; the source's connection is untouched."""
        self._cursor.close()

    def __getattr__(self, name: str):
        return getattr(self._source, name)


def document_name(name: str | None, schema: set[str] | frozenset[str] | None, source) -> str:
    """The document's identity, and so its default file name: `<name>.yaml`.

    `--name` wins. Else the schema when the run is scoped to one, the schemas
    joined with `_` when scoped to several, else the warehouse itself: the
    DuckDB file's stem, the Snowflake database, the BigQuery project, the
    Postgres database. The identity is what other documents reference once
    layers are combined, so it is never a generic word like `semantic_layer`
    when something more specific is known.
    """
    if name:
        return name
    if schema:
        return "_".join(sorted(schema))
    label = warehouse_uri(source).split(":", 1)[-1].rstrip("/").rsplit("/", 1)[-1]
    label = re.sub(r"\.(duckdb|ddb|db)$", "", label)
    label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("_")
    if not label or label == "memory":
        return "semantic_layer"
    return label
