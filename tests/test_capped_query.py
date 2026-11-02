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


def test_the_cap_is_small_and_the_clock_is_generous():
    """The rows go into a model's context, so the cap is modest. The clock is
    ten minutes: a real analytical query takes minutes, so the 30 seconds
    this started at refused legitimate work, while no limit at all holds the
    session's one connection for as long as the warehouse will run it."""
    assert RUN_SQL_ROWS <= 500
    assert RUN_SQL_TIMEOUT_S == 600


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


def test_nothing_is_armed_when_the_clock_is_turned_off(duck):
    """`timeout_s=None` has to arm NOTHING. A watchdog left running would
    interrupt the connection -- not the statement -- and so could land on a
    query it was never asked about."""
    import threading
    before = {t.name for t in threading.enumerate()}
    duck.query_capped("select 1 as one", timeout_s=None)
    assert {t.name for t in threading.enumerate()} == before


def test_the_default_clock_is_actually_applied_on_postgres(postgres):
    """Read back from inside the capped call: the setting is live only for
    the duration of the statement, so `show` run through `query_capped` is
    exactly how to see what it set."""
    _cols, rows, _trunc = postgres.query_capped("show statement_timeout")
    assert rows[0][0] == "10min"


def test_turning_the_clock_off_leaves_the_users_setting_alone(postgres):
    """With None, the user's own server or role configuration is what
    applies, so the session setting must not be touched."""
    # through the raw connection: the connector's `query` always fetches,
    # and a SET returns no rows.
    postgres.con.execute("SET statement_timeout = 7000")
    try:
        postgres.query_capped("select 1 as one", timeout_s=None)
        assert postgres.query("show statement_timeout")[0][0] == "7s"
    finally:
        postgres.con.execute("SET statement_timeout = DEFAULT")
