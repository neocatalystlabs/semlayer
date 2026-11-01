# SPDX-License-Identifier: FSL-1.1-ALv2
"""Postgres connector: MetadataProvider + QueryExecutor + CatalogProvider.

The first connector added after the SQL portability audit, and the first
non-cloud-warehouse one -- so it is also the proof that the source protocol
generalises. It needed no changes to the protocol: `list_tables`, `query`,
`qualify`, the optional `list_comments`, and `row_cap_clause`.

Also covers Amazon Redshift, which speaks the Postgres wire protocol, though
Redshift's catalog differs in places and is untested here.

`psycopg` (v3) is used rather than `psycopg2`: v3 is the maintained line, and
`psycopg[binary]` ships libpq so there is no system package to install --
the same property that makes python-oracledb's thin mode attractive.
"""

from __future__ import annotations

import logging
import os

from semlayer.source import (
    ColumnMeta,
    ColumnStatistic,
    DeclaredConstraints,
    DeclaredFK,
    Partition,
    TableMeta,
)

# `pg_` is a reserved schema prefix, so excluding the whole prefix is both safe
# and necessary: besides pg_catalog and pg_toast there are per-session pg_temp_N
# and pg_toast_temp_N schemas whose names are not known in advance.
SKIP_SCHEMAS_SQL = "n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg\\_%'"
# information_schema spells the same predicate with a bare column name.
SKIP_SCHEMAS_ISO = (
    "c.table_schema <> 'information_schema' AND c.table_schema NOT LIKE 'pg\\_%'"
)

logger = logging.getLogger(__name__)


def _effective_role(con, asked: str | None) -> str:
    """The role the session is actually running as, from Postgres itself.

    The same reasoning as the Snowflake connector: `session_role` reached the
    run report as whatever the caller passed in, or the literal string
    "unknown" when they passed nothing -- and a minimal-grant claim that
    quotes the caller's own input back at them has checked nothing.

    Guarded, and never fatal. We are already connected; a failed metadata
    query is a poor reason to abandon a working connection, so the honest
    answer is what we asked for, or "unknown".
    """
    try:
        row = con.execute("SELECT current_role").fetchone()
        effective = (row[0] if row else "") or ""
    except Exception as e:
        logger.warning("could not read current_role: %s", e)
        return asked or "unknown"
    effective = str(effective).strip()
    if not effective:
        return asked or "unknown"
    if asked and effective != asked:
        logger.warning(
            "asked for role %s but the session is running as %s; the run "
            "report records %s", asked, effective, effective)
    return effective


class PostgresSource:
    """MetadataProvider + QueryExecutor + CatalogProvider over one database."""

    def __init__(self, con, database: str = ""):
        self.con = con
        self.database = database
        self.session_role: str = "unknown"

    @classmethod
    def from_env(cls, persona: str = "reader") -> PostgresSource:
        """Connect from PG* environment variables or a single PG_DSN.

        `persona` selects the credential set, matching the other connectors.
        Postgres has no session-role parameter on connect the way Snowflake
        does -- a role is adopted with SET ROLE -- so if PG_ROLE is set we
        issue it explicitly and record it, for the same reason A4 exists: a
        minimal-grant claim is about the privileges actually in effect.
        """
        prefix = "PG_READER" if persona == "reader" else "PG_ADMIN"
        dsn = os.environ.get(f"{prefix}_DSN") or os.environ.get("PG_DSN")
        if not dsn:
            from semlayer.errors import SourceError

            raise SourceError(
                f"no Postgres connection string: export {prefix}_DSN (or PG_DSN)",
                hint="e.g. postgresql://user:pass@host:5432/dbname",
            )
        return cls.from_dsn(dsn, role=os.environ.get(f"{prefix}_ROLE") or os.environ.get("PG_ROLE"))

    @classmethod
    def from_dsn(cls, dsn: str, role: str | None = None) -> PostgresSource:
        """Connect to an explicit connection string.

        `autocommit` matters: without it psycopg opens a transaction on the
        first statement and holds it for the whole scan, which pins the
        oldest-xmin and blocks vacuum on a production replica for as long as
        inference runs. We only read, so there is nothing to commit.

        Every failure here is translated. `psycopg.connect` and the `SET ROLE`
        were both bare, so a refused login, an unreachable host, a malformed
        DSN or a nonexistent role reached the user as a raw traceback and
        reached telemetry as `internal` -- the class that is supposed to mean
        a defect in semlayer. None of those are our bug, and a connection
        problem that files itself as a crash is a connection problem nobody
        goes looking for.
        """
        from semlayer.errors import SourceError
        try:
            import psycopg
        except ImportError as e:
            raise SourceError(
                "the Postgres driver is not installed",
                hint="pip install 'semlayer[warehouses]'",
            ) from e

        try:
            con = psycopg.connect(dsn, autocommit=True)
        except psycopg.Error as e:
            # The driver's message names the host, the database and what it
            # objected to; that is what the person needs, so it becomes the
            # hint rather than being swallowed.
            raise SourceError("Postgres refused the connection",
                              hint=str(e).strip()) from e
        src = cls(con, os.environ.get("PGDATABASE", ""))
        if role:
            # Composed, not interpolated. `SET ROLE "{role}"` put an
            # environment variable straight into SQL -- not a privilege
            # boundary, since it is the caller's own environment, but a role
            # name containing a quote broke out of the identifier and failed
            # with a syntax error that blamed the SQL rather than the name.
            from psycopg import sql
            try:
                con.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
            except psycopg.Error as e:
                con.close()
                raise SourceError(
                    f"could not assume Postgres role {role!r}",
                    hint=f"{str(e).strip()} -- the role must exist and your "
                         "user must be a member of it; PG_ROLE is optional, "
                         "and without it the run uses the DSN's own user",
                ) from e
        # Asked for or inherited, the report gets what the session is really
        # running as -- see `_effective_role`.
        src.session_role = _effective_role(con, role)
        return src

    def row_cap_clause(self, n: int) -> str:
        """Postgres takes LIMIT; it also accepts FETCH FIRST, but LIMIT is canonical."""
        return f" LIMIT {n}"

    def query(self, sql: str) -> list[tuple]:
        """Run read-only SQL and return rows."""
        return list(self.con.execute(sql).fetchall())

    def list_tables(self) -> list[TableMeta]:
        """Enumerate tables and columns via information_schema.

        Views are included: a warehouse's presentation layer is often views,
        and they profile and describe exactly like tables.
        """
        rows = self.query(
            f"""
            SELECT c.table_schema, c.table_name, c.column_name, c.data_type
            FROM information_schema.columns c
            JOIN information_schema.tables t
              ON t.table_schema = c.table_schema AND t.table_name = c.table_name
            WHERE {SKIP_SCHEMAS_ISO}
              AND t.table_type IN ('BASE TABLE', 'VIEW')
            ORDER BY c.table_schema, c.table_name, c.ordinal_position
            """
        )
        tables: dict[tuple[str, str], list[ColumnMeta]] = {}
        for schema, table, col, dtype in rows:
            tables.setdefault((schema, table), []).append(ColumnMeta(col, dtype))
        return [TableMeta(s, t, tuple(cols)) for (s, t), cols in tables.items()]

    def list_comments(self) -> dict[tuple[str, str, str | None], str]:
        """Native COMMENT text via obj_description/col_description.

        Postgres keeps comments in pg_description rather than
        information_schema, so the standard accessor functions are the route.
        `col_description` takes an attribute number, which is why this joins
        pg_attribute rather than information_schema.columns.
        """
        out: dict[tuple[str, str, str | None], str] = {}
        for schema, table, comment in self.query(
            f"""
            SELECT n.nspname, c.relname, obj_description(c.oid, 'pg_class')
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'v', 'm', 'p')
              AND {SKIP_SCHEMAS_SQL}
              AND obj_description(c.oid, 'pg_class') IS NOT NULL
            """
        ):
            out[(schema, table, None)] = comment
        for schema, table, col, comment in self.query(
            f"""
            SELECT n.nspname, c.relname, a.attname, col_description(c.oid, a.attnum)
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
            WHERE c.relkind IN ('r', 'v', 'm', 'p')
              AND {SKIP_SCHEMAS_SQL}
              AND col_description(c.oid, a.attnum) IS NOT NULL
            """
        ):
            out[(schema, table, col)] = comment
        return out

    def list_constraints(self) -> DeclaredConstraints:
        """Declared PKs and FKs from pg_constraint.

        `pg_constraint` carries the column lists as ordinal arrays (`conkey`,
        `confkey`), so unlike DuckDB there is no DDL text to parse: the child
        and parent columns are paired by position, which is what makes a
        composite foreign key come out right rather than alphabetised.

        `convalidated` is reported as `enforced`. Postgres does enforce, but a
        constraint added `NOT VALID` is trusted for new rows only, and that
        distinction belongs in the provenance rather than being flattened.
        """
        skip = SKIP_SCHEMAS_SQL.replace("n.nspname", "ns.nspname")
        rows = self.query(f"""
            SELECT ns.nspname, cl.relname, con.contype,
                   array_agg(att.attname ORDER BY k.ord),
                   coalesce(fns.nspname, ''), coalesce(fcl.relname, ''),
                   array_agg(coalesce(fatt.attname, '') ORDER BY k.ord),
                   con.convalidated
            FROM pg_constraint con
            JOIN pg_class cl ON cl.oid = con.conrelid
            JOIN pg_namespace ns ON ns.oid = cl.relnamespace
            CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
            JOIN pg_attribute att
              ON att.attrelid = con.conrelid AND att.attnum = k.attnum
            LEFT JOIN pg_class fcl ON fcl.oid = con.confrelid
            LEFT JOIN pg_namespace fns ON fns.oid = fcl.relnamespace
            LEFT JOIN pg_attribute fatt
              ON fatt.attrelid = con.confrelid AND fatt.attnum = con.confkey[k.ord]
            WHERE con.contype IN ('p', 'f') AND {skip}
            GROUP BY ns.nspname, cl.relname, con.contype, fns.nspname, fcl.relname,
                     con.convalidated, con.oid
        """)
        pks: dict[tuple[str, str], tuple[str, ...]] = {}
        fks: list[DeclaredFK] = []
        for schema, table, ctype, cols, pschema, ptable, pcols, validated in rows:
            if ctype == "p":
                pks[(schema, table)] = tuple(cols)
            elif ctype == "f" and ptable:
                fks.append(DeclaredFK(schema, table, tuple(cols),
                                      pschema, ptable, tuple(pcols),
                                      enforced=bool(validated)))
        return DeclaredConstraints(pks, tuple(fks))

    def list_column_statistics(self) -> dict[tuple[str, str, str], ColumnStatistic]:
        """Rung 0: `pg_stats`, plus when the table was last analyzed.

        Two verified facts shape this:

        - `pg_stats` needs NO extra grant -- it filters its rows to tables the
          caller can already SELECT, so the minimal-grant reader sees
          everything it is entitled to and `pg_read_all_stats` is unnecessary.
        - **`n_distinct` is negative when it is a ratio** of the row count:
          -1.0 means distinct in every row, i.e. a key; -0.3 means 0.3x the
          rows. Reading it as a count makes every key look single-valued, so it
          is converted here against `reltuples` and flagged.

        `last_analyze`/`last_autoanalyze` come from `pg_stat_user_tables` so a
        caller can judge staleness rather than trust the numbers blindly.
        """
        skip = SKIP_SCHEMAS_SQL.replace("n.nspname", "s.schemaname")
        rows = self.query(f"""
            SELECT s.schemaname, s.tablename, s.attname, s.n_distinct, s.null_frac,
                   s.most_common_vals::TEXT,
                   greatest(coalesce(st.last_analyze, '-infinity'::timestamptz),
                            coalesce(st.last_autoanalyze, '-infinity'::timestamptz)),
                   cl.reltuples::BIGINT
            FROM pg_stats s
            JOIN pg_class cl ON cl.relname = s.tablename
            JOIN pg_namespace nsp
              ON nsp.oid = cl.relnamespace AND nsp.nspname = s.schemaname
            LEFT JOIN pg_stat_user_tables st ON st.relid = cl.oid
            WHERE {skip}
        """)
        out: dict[tuple[str, str, str], ColumnStatistic] = {}
        for schema, table, col, nd, null_frac, mcv, analyzed, reltuples in rows:
            is_ratio = nd is not None and nd < 0
            if nd is None:
                count = None
            elif is_ratio:
                # a ratio of the row count, so -1.0 over 1,000 rows is 1,000
                count = round(abs(float(nd)) * max(int(reltuples or 0), 0))
            else:
                count = int(nd)
            vals: tuple[str, ...] = ()
            if mcv:
                vals = tuple(v for v in mcv.strip("{}").split(",") if v)
            stamp = None
            if analyzed is not None and getattr(analyzed, "year", 0) > 1:
                stamp = analyzed.isoformat()
            out[(schema, table, col)] = ColumnStatistic(
                n_distinct=count,
                null_frac=float(null_frac) if null_frac is not None else None,
                most_common=vals, analyzed_at=stamp, distinct_is_ratio=is_ratio)
        return out

    def list_partitions(self) -> dict[tuple[str, str], tuple[Partition, ...]]:
        """Declarative partitions, with a change token, without scanning anything.

        Three verified facts shape this (PostgreSQL 16):

        - `pg_inherits` gives parent -> child, and
          `pg_get_expr(relpartbound, oid)` the human-readable bound.
        - `pg_class.reltuples` gives a row estimate for free; it is an ESTIMATE
          and is only ever used for sizing, never as the change signal.
        - the change token has to come from `pg_stat_user_tables.n_tup_*`. An
          UPDATE of 10,000 rows in one partition moved `n_tup_upd` by 10,000
          and left the row count unchanged, so a count-based token misses
          every in-place backfill.

        `pg_stat_force_next_flush()` is called first because the counters are
        reported asynchronously: reading without it returns pre-UPDATE values
        and a backfill looks like no change at all. It exists in PostgreSQL 15+,
        so a failure is tolerated rather than fatal -- the token is then merely
        staler, and the comparison is still sound.
        """
        try:
            self.con.execute("SELECT pg_stat_force_next_flush()")
            self.con.execute("SELECT pg_stat_clear_snapshot()")
        except Exception:         # PostgreSQL < 15, or not permitted
            pass
        skip = SKIP_SCHEMAS_SQL.replace("n.nspname", "pn.nspname")
        rows = self.query(f"""
            SELECT pn.nspname, p.relname, cn.nspname, ch.relname,
                   coalesce(pg_get_expr(ch.relpartbound, ch.oid), ''),
                   ch.reltuples::BIGINT,
                   coalesce(st.n_tup_ins, 0), coalesce(st.n_tup_upd, 0),
                   coalesce(st.n_tup_del, 0)
            FROM pg_inherits i
            JOIN pg_class p ON p.oid = i.inhparent
            JOIN pg_namespace pn ON pn.oid = p.relnamespace
            JOIN pg_class ch ON ch.oid = i.inhrelid
            JOIN pg_namespace cn ON cn.oid = ch.relnamespace
            LEFT JOIN pg_stat_user_tables st ON st.relid = ch.oid
            WHERE {skip} AND p.relkind = 'p'
            ORDER BY pn.nspname, p.relname, ch.relname
        """)
        out: dict[tuple[str, str], list[Partition]] = {}
        for pschema, ptable, schema, name, bound, est, ins, upd, dele in rows:
            out.setdefault((pschema, ptable), []).append(Partition(
                schema=schema, name=name, parent_schema=pschema,
                parent_table=ptable, bound=bound,
                change_token=f"{ins}:{upd}:{dele}", est_rows=int(est)))
        return {k: tuple(v) for k, v in out.items()}

    def source_id(self) -> str:
        """host:port/dbname. Never the password -- this lands in a file users commit."""
        i = self.con.info
        return f"postgres:{i.host or 'local'}:{i.port}/{i.dbname}"

    def for_worker(self):
        """Per-worker executor: the shared connection.

        A psycopg connection is thread-safe but serialises its queries, so
        workers gain nothing here until a connection pool exists.
        """
        return self

    def close(self) -> None:
        """Close the connection; safe to call twice."""
        try:
            self.con.close()
        except Exception:
            pass

    def qualify(self, schema: str, table: str) -> str:
        """Quoted schema.table; Postgres folds unquoted identifiers to lower case."""
        def q(ident: str) -> str:
            return '"' + ident.replace('"', '""') + '"'
        return f"{q(schema)}.{q(table)}"
