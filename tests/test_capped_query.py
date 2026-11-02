"""The read path an agent's own SQL goes down: columns, a cap, and a clock.

Real engines only. The cap and the timeout are the two things a mock would
agree with while the real driver ignored them -- and the timeout is the one
that matters, because the alternative to enforcing it is a chat turn that
never comes back.

DuckDB runs here always. Postgres runs wherever `pgserver` is installed,
which is CI too. Snowflake and BigQuery are covered by the live tiers in
`test_snowflake_live.py` and `test_bigquery_live.py`.
"""

from __future__ import annotations

import time

import pytest

from semlayer.source import RUN_SQL_ROWS, RUN_SQL_TIMEOUT_S, AgentQueryExecutor


@pytest.fixture
def duck():
    import duckdb

    from semlayer.source import DuckDBSource
    con = duckdb.connect(":memory:")
    con.execute("create table t as select range as id, range * 2 as doubled "
                "from range(500)")
    try:
        yield DuckDBSource(con)
    finally:
        con.close()


def test_the_row_cap_is_small_and_there_is_no_clock():
    """The rows go into a model's context, so the cap is modest. There is no
    time limit at all: a real analytical query takes minutes, and the
    warehouse being billed is the user's, with their own limits on it."""
    assert RUN_SQL_ROWS <= 500
    assert RUN_SQL_TIMEOUT_S is None


def test_duckdb_returns_named_columns(duck):
    columns, rows, truncated = duck.query_capped("select id, doubled from t "
                                                 "order by id", limit=3)
    assert columns == ["id", "doubled"]
    assert rows == [(0, 0), (1, 2), (2, 4)]
    assert truncated is True


def test_duckdb_says_when_it_did_not_truncate(duck):
    columns, rows, truncated = duck.query_capped("select count(*) as n from t")
    assert columns == ["n"]
    assert rows == [(500,)]
    assert truncated is False


def test_duckdb_enforces_the_timeout(duck):
    """DuckDB has no statement timeout, so this is a watchdog that
    interrupts -- and it has to actually fire."""
    t0 = time.time()
    with pytest.raises(Exception):
        duck.query_capped("select count(*) from range(100000000000) a, "
                          "range(1000) b", timeout_s=1)
    assert time.time() - t0 < 10


def test_duckdb_is_usable_after_a_timeout(duck):
    """The session keeps this connection, so an interrupt must not poison it."""
    with pytest.raises(Exception):
        duck.query_capped("select count(*) from range(100000000000) a, "
                          "range(1000) b", timeout_s=1)
    assert duck.query_capped("select 1 as one")[1] == [(1,)]


def test_duckdb_satisfies_the_protocol(duck):
    assert isinstance(duck, AgentQueryExecutor)


# --- Postgres, against a real server ------------------------------------

@pytest.fixture(scope="module")
def postgres():
    pytest.importorskip("pgserver")
    pytest.importorskip("psycopg")
    import tempfile

    import pgserver
    import psycopg

    from semlayer.pipeline import open_source_from_uri
    srv = pgserver.get_server(tempfile.mkdtemp(prefix="pgcapped-"))
    try:
        uri = srv.get_uri()
        with psycopg.connect(uri, autocommit=True) as c:
            c.execute("create table t as select g as id, g * 2 as doubled "
                      "from generate_series(1, 500) g")
        yield open_source_from_uri(uri)
    finally:
        srv.cleanup()


def test_postgres_returns_named_columns(postgres):
    columns, rows, truncated = postgres.query_capped(
        "select id, doubled from t order by id", limit=3)
    assert columns == ["id", "doubled"]
    assert rows == [(1, 2), (2, 4), (3, 6)]
    assert truncated is True


def test_postgres_enforces_the_timeout_server_side(postgres):
    """`statement_timeout` is PostgreSQL's own, so a query that hangs on the
    server is cancelled there rather than leaving us on a socket."""
    t0 = time.time()
    with pytest.raises(Exception):
        postgres.query_capped("select pg_sleep(10)", timeout_s=1)
    assert time.time() - t0 < 5


def test_postgres_is_usable_after_a_cancellation(postgres):
    with pytest.raises(Exception):
        postgres.query_capped("select pg_sleep(10)", timeout_s=1)
    assert postgres.query_capped("select 1 as one")[1] == [(1,)]


def test_postgres_resets_the_timeout_it_set(postgres):
    """The connection outlives the query, so a 1-second limit left behind
    would break the next inference on the same session.

    Read through plain `query`, not `query_capped`: the capped path sets the
    timeout for the duration of its own statement, so asking it what the
    setting is tells you only what it set. The first version of this test
    did exactly that and reported the cap back to itself.
    """
    with pytest.raises(Exception):
        postgres.query_capped("select pg_sleep(10)", timeout_s=1)
    assert postgres.query("show statement_timeout")[0][0] in ("0", "0ms")


def test_nothing_is_armed_by_default_on_duckdb(duck):
    """A watchdog that fired by default would interrupt whatever this
    connection is running, including a query it was not asked about."""
    import threading
    before = {t.name for t in threading.enumerate()}
    duck.query_capped("select 1 as one")
    assert {t.name for t in threading.enumerate()} == before


def test_postgres_does_not_touch_the_setting_by_default(postgres):
    """With no timeout asked for, the user's own server configuration is
    what applies -- so the session setting must be left alone."""
    # through the raw connection: the connector's `query` always fetches,
    # and a SET returns no rows.
    postgres.con.execute("SET statement_timeout = 7000")
    try:
        postgres.query_capped("select 1 as one")
        assert postgres.query("show statement_timeout")[0][0] == "7s"
    finally:
        postgres.con.execute("SET statement_timeout = DEFAULT")
