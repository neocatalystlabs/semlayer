"""Guard the SQL we push down against dialect-specific constructs.

Every connector after the first gets cheaper if the profiling SQL is portable
by construction, and more expensive if each incompatibility is discovered one
error at a time against a real warehouse. Written 2026-10-12, before the
Oracle connector, after an audit found four non-portable constructs -- one of
which had been introduced hours earlier the same day.

sqlglot is deliberately NOT used to transpile at runtime, though we depend on
it for the linter's dialects: it passes `FILTER (WHERE ...)` through verbatim
to Oracle, MySQL and T-SQL, where it is a syntax error. A transpiler that
silently emits broken SQL for one construct is worse than portable SQL.
"""

from __future__ import annotations

import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "semlayer"

# Modules that build SQL sent to the customer's warehouse. Connector modules
# are excluded: their SQL is dialect-specific BY DESIGN.
PUSHDOWN_MODULES = [
    "profile/stats.py", "profile/run.py", "link/run.py", "link/candidates.py",
    "enrich/run.py", "drift.py", "cq/answer.py", "lint.py",
]


def _code_lines(rel: str) -> list[tuple[int, str]]:
    """Lines of a module with whole-line comments dropped."""
    out = []
    for i, line in enumerate((SRC / rel).read_text().splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        out.append((i, line))
    return out


@pytest.mark.parametrize("rel", PUSHDOWN_MODULES)
def test_no_filter_clause_in_pushed_down_sql(rel):
    """`FILTER (WHERE ...)` is ANSI and invalid on Oracle, MySQL and T-SQL.

    Use `sum(CASE WHEN cond THEN 1 ELSE 0 END)`, which is portable everywhere.
    """
    bad = [(i, ln.strip()) for i, ln in _code_lines(rel) if "FILTER (WHERE" in ln]
    assert not bad, f"{rel}: {bad}"


@pytest.mark.parametrize("rel", PUSHDOWN_MODULES)
def test_row_caps_go_through_the_source_dialect(rel):
    """Row caps are the one construct with no portable spelling.

    `FETCH FIRST n ROWS ONLY` is ANSI and works on Oracle, Postgres, Snowflake
    and DuckDB; BigQuery rejects it and wants `LIMIT n`. So the source decides,
    via `semlayer.source.row_cap`.
    """
    bad = [(i, ln.strip()) for i, ln in _code_lines(rel)
           if "LIMIT " in ln and "row_cap" not in ln]
    assert not bad, f"{rel} should use row_cap(): {bad}"


def test_row_cap_defaults_to_limit_and_is_overridable():
    import duckdb

    from semlayer.source import DuckDBSource, row_cap

    class _Oracleish:
        def row_cap_clause(self, n):
            return f" FETCH FIRST {n} ROWS ONLY"

    assert row_cap(object(), 10) == " LIMIT 10"          # default for a bare source
    assert row_cap(DuckDBSource(duckdb.connect(":memory:")), 10) == " LIMIT 10"
    assert row_cap(_Oracleish(), 10) == " FETCH FIRST 10 ROWS ONLY"


@pytest.mark.parametrize("sql", [
    # the canonical shapes profiling and linking push down
    "SELECT count(*) FROM t",
    'SELECT count(*) - count("c"), count(DISTINCT "c") FROM t',
    'SELECT min("c"), max("c"), avg(length("c")) FROM t',
    'SELECT "c", count(*) FROM t WHERE "c" IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1',
    'SELECT sum(CASE WHEN n > 1 THEN 1 ELSE 0 END), count(*) FROM '
    '(SELECT "a", count(DISTINCT "b") n FROM t GROUP BY 1) g',
    'SELECT count(*) FROM (SELECT DISTINCT "a", "b" FROM t) d',
    'SELECT round(sum("amt"), 2) FROM t WHERE "sts" <> \'X\'',
    # The FK inclusion test -- the most-issued query in the system, 24,827 of
    # them on one 116-table warehouse, and absent from this list until bare
    # `EXCEPT` turned out to be a syntax error on BigQuery. Added here as well
    # as to the runtime capture below: the curated list failed by omission, but
    # it would have caught this the day the shape was added.
    'SELECT count(*) FROM (SELECT DISTINCT "c" AS v FROM "s"."t" WHERE "c" IS NOT NULL) d '
    'WHERE NOT EXISTS (SELECT 1 FROM "s"."p" p WHERE p."k" = d.v)',
])
@pytest.mark.parametrize("dialect", ["oracle", "postgres", "snowflake", "bigquery", "duckdb"])
def test_canonical_pushdown_sql_parses_in_every_target_dialect(sql, dialect):
    """Each shape must parse as-is in every dialect we target or plan to."""
    sqlglot = pytest.importorskip("sqlglot")
    sqlglot.parse_one(sql, read=dialect)   # raises on a dialect that refuses it


# --- runtime capture: validate the SQL we ACTUALLY issue -------------------
#
# The curated list above missed the single most-issued query in the system --
# the foreign-key inclusion test, 24,827 of them on one 116-table warehouse --
# and bare `EXCEPT` is a syntax error on BigQuery, so FK discovery silently did
# not run there at all.
#
# A curated list cannot catch what nobody adds to it. An AST walk over the
# f-strings would reach more, but not SQL assembled by ", ".join(...) across
# lines, and much of ours is. Capturing at the source boundary reaches
# everything, because it sees the finished string.
#
# Its one blind spot is code the tests never exercise, so the curated list
# stays as a static backstop rather than being replaced.

_SHAPE_IDENT = __import__("re").compile(r'"[^"]+"')
_SHAPE_STR = __import__("re").compile(r"'[^']*'")
_SHAPE_NUM = __import__("re").compile(r"\b\d+\b")


def _shape(sql: str) -> str:
    """Collapse identifiers, literals and numbers so distinct SHAPES dedupe."""
    s = _SHAPE_IDENT.sub('"c"', sql)
    s = _SHAPE_STR.sub("'x'", s)
    s = _SHAPE_NUM.sub("0", s)
    return " ".join(s.split())


@pytest.mark.parametrize("fixture", ["fan_trap", "messy_mart"])
def test_every_query_we_issue_parses_in_every_target_dialect(fixture):
    """Run the pipeline, capture the real SQL, parse each shape in each dialect."""
    sqlglot = pytest.importorskip("sqlglot")
    import duckdb

    from semlayer.link import link_source
    from semlayer.profile.run import profile_with_stats
    from semlayer.source import DuckDBSource

    db = SRC.parent.parent / "fixtures" / "build" / f"{fixture}.duckdb"
    if not db.exists():
        pytest.skip(f"{db} not built")

    class Capturing(DuckDBSource):
        def __init__(self, con):
            super().__init__(con)
            self.sql: list[str] = []

        def query(self, sql):
            self.sql.append(sql)
            return super().query(sql)

    con = duckdb.connect(str(db), read_only=True)
    try:
        src = Capturing(con)
        doc, stats = profile_with_stats(src, llm=None)
        link_source(src, doc, stats, llm=None, use_declared=False)
    finally:
        con.close()

    shapes = {_shape(q) for q in src.sql}
    assert shapes, "captured nothing -- the spy did not take"
    refused = []
    for sh in sorted(shapes):
        for dialect in ("oracle", "postgres", "snowflake", "bigquery", "duckdb"):
            try:
                sqlglot.parse_one(sh, read=dialect)
            except Exception as e:
                refused.append(f"{dialect}: {type(e).__name__}: {sh[:120]}")
    assert not refused, (
        f"{len(refused)} of {len(shapes)} captured shapes are not portable:\n"
        + "\n".join(refused[:5]))
