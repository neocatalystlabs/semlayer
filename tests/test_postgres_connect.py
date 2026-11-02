"""How the Postgres connector fails, and what it puts in SQL.

Postgres had half of the signature the Snowflake work fixed. There was never a
URI wall -- `postgresql://` DSNs have always been accepted inline, which is how
most people hold a Postgres credential -- but `psycopg.connect` and the
`SET ROLE` were both bare. A refused login, an unreachable host or a role that
does not exist reached the user as a raw traceback and reached telemetry as
`internal`: the class that is supposed to mean a defect in semlayer. A
connection problem filed as a crash is a connection problem nobody goes
looking for.

`tests/test_postgres.py` is the real integration test, against a real server
via pgserver, and it proves inference and the read-only grant. It cannot
easily prove how a *failure* reads, because making a real server refuse in
each distinct way is slow and fiddly. So the driver is stubbed here, and these
run anywhere -- including the many developer venvs that have no psycopg.
"""

from __future__ import annotations

import sys
import types

import pytest

from semlayer.errors import SourceError


class _FakeDriverError(Exception):
    """Stands in for psycopg.Error."""


class _FakeConnection:
    def __init__(self, role_answer="dsn_user", on_execute=None):
        self.executed: list = []
        self.closed = False
        self._role_answer = role_answer
        self._on_execute = on_execute

    def execute(self, sql):
        self.executed.append(sql)
        if self._on_execute is not None:
            self._on_execute(sql)
        return types.SimpleNamespace(fetchone=lambda: (self._role_answer,))

    def close(self):
        self.closed = True


def _stub_psycopg(monkeypatch, *, connect_raises=None, connection=None):
    """A psycopg stub with a real-enough `sql` module.

    `sql.SQL`/`sql.Identifier` are modelled rather than mocked away, because
    the thing being tested is that the role goes through composition instead
    of an f-string.
    """
    mod = types.ModuleType("psycopg")
    sql_mod = types.ModuleType("psycopg.sql")
    mod.Error = _FakeDriverError

    class _Composed:
        def __init__(self, template, parts):
            self.template = template
            self.parts = parts

        def __str__(self):
            return self.template.format(*(str(p) for p in self.parts))

    class SQL:
        def __init__(self, template):
            self.template = template

        def format(self, *parts):
            return _Composed(self.template, parts)

    class Identifier:
        def __init__(self, name):
            self.name = name

        def __str__(self):
            # What libpq-side quoting does: wrap in double quotes and double
            # any double quote inside.
            return '"' + self.name.replace('"', '""') + '"'

    sql_mod.SQL = SQL
    sql_mod.Identifier = Identifier
    mod.sql = sql_mod

    con = connection if connection is not None else _FakeConnection()

    def connect(dsn, autocommit=False):
        if connect_raises is not None:
            raise connect_raises
        con.dsn = dsn
        con.autocommit = autocommit
        return con

    mod.connect = connect
    monkeypatch.setitem(sys.modules, "psycopg", mod)
    monkeypatch.setitem(sys.modules, "psycopg.sql", sql_mod)
    return con


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """No PG_* role or DSN variable may reach these tests from the shell.

    Added with the PG_ROLE fix below: once `from_dsn` reads the environment,
    a developer with PG_ROLE exported would get different results from CI,
    and every assertion here about "no role was asked for" would be quietly
    testing their shell instead of the code.
    """
    for k in ("PG_ROLE", "PG_READER_ROLE", "PG_ADMIN_ROLE",
              "PG_DSN", "PG_READER_DSN", "PG_ADMIN_DSN", "PGDATABASE"):
        monkeypatch.delenv(k, raising=False)


def _from_dsn(**kw):
    from semlayer.connectors.postgres import PostgresSource
    return PostgresSource.from_dsn("postgresql://u@h/db", **kw)


# --- the role a DSN run adopts ---------------------------------------------

def test_a_dsn_run_honours_pg_role(monkeypatch):
    """It did not, and nothing said so.

    `pipeline.py` dispatches `postgresql://...` to `from_dsn(uri)` with no
    role, while the bare `postgres` URI went through `from_env`, which read
    the variable. So the documented PG_ROLE worked on one path and was
    silently ignored on the other -- the one most people use, since a DSN is
    how a Postgres credential is normally held. Nothing failed; the run just
    profiled as the DSN's user while the person believed it was the reader
    role, which is the worst shape a config bug can take.
    """
    con = _stub_psycopg(monkeypatch)
    monkeypatch.setenv("PG_ROLE", "layer_reader")
    _from_dsn()
    assert str(con.executed[0]) == 'SET ROLE "layer_reader"'


def test_the_persona_specific_variable_wins_over_the_generic_one(monkeypatch):
    con = _stub_psycopg(monkeypatch)
    monkeypatch.setenv("PG_ROLE", "generic")
    monkeypatch.setenv("PG_READER_ROLE", "specific")
    _from_dsn()
    assert str(con.executed[0]) == 'SET ROLE "specific"'


def test_an_explicit_argument_beats_the_environment(monkeypatch):
    """Same precedence snowflake._asked_role uses: what the caller passed
    wins, because they said it later and more deliberately."""
    con = _stub_psycopg(monkeypatch)
    monkeypatch.setenv("PG_ROLE", "from_env")
    _from_dsn(role="from_arg")
    assert str(con.executed[0]) == 'SET ROLE "from_arg"'


def test_no_role_anywhere_still_issues_no_set_role(monkeypatch):
    """Running as yourself stays the default -- the fix must not start
    asserting a role nobody asked for."""
    con = _stub_psycopg(monkeypatch)
    _from_dsn()
    assert not any("SET ROLE" in str(x) for x in con.executed)


def test_from_env_and_from_dsn_resolve_the_role_identically(monkeypatch):
    """The real defect was that two entry points each did their own lookup.

    from_env now delegates to from_dsn rather than duplicating it, so this
    test is about the structure holding, not about either answer.
    """
    from semlayer.connectors.postgres import PostgresSource

    con_a = _stub_psycopg(monkeypatch)
    monkeypatch.setenv("PG_ROLE", "same_role")
    monkeypatch.setenv("PG_DSN", "postgresql://u@h/db")
    PostgresSource.from_env(persona="reader")
    via_env = [str(x) for x in con_a.executed if "SET ROLE" in str(x)]

    con_b = _stub_psycopg(monkeypatch)
    PostgresSource.from_dsn("postgresql://u@h/db")
    via_dsn = [str(x) for x in con_b.executed if "SET ROLE" in str(x)]

    assert via_env == via_dsn == ['SET ROLE "same_role"']


# --- failures that are not our bug ----------------------------------------

def test_a_refused_connection_is_a_source_error(monkeypatch):
    """Was a raw `psycopg.OperationalError`, recorded as `internal`."""
    _stub_psycopg(monkeypatch,
                  connect_raises=_FakeDriverError('password authentication '
                                                  'failed for user "u"'))
    with pytest.raises(SourceError) as e:
        _from_dsn()
    assert "Postgres refused the connection" in str(e.value)
    assert 'password authentication failed' in e.value.hint


def test_a_missing_driver_says_pip_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg", None)
    with pytest.raises(SourceError) as e:
        _from_dsn()
    assert "driver is not installed" in str(e.value)
    assert "semlayer[warehouses]" in e.value.hint


def test_a_role_that_does_not_exist_names_it_and_releases_the_connection(monkeypatch):
    """Two things at once: the message is actionable, and we do not leak a
    live connection on the way out."""
    def boom(sql):
        raise _FakeDriverError('role "ghost" does not exist')

    con = _FakeConnection(on_execute=boom)
    _stub_psycopg(monkeypatch, connection=con)
    with pytest.raises(SourceError) as e:
        _from_dsn(role="ghost")
    assert "ghost" in str(e.value)
    assert 'role "ghost" does not exist' in e.value.hint
    assert "PG_ROLE is optional" in e.value.hint
    assert con.closed, "a failed SET ROLE must not leave the connection open"


# --- what reaches the SQL -------------------------------------------------

def test_the_role_is_composed_as_an_identifier_not_interpolated(monkeypatch):
    """`SET ROLE "{role}"` put an environment variable straight into SQL.

    Not a privilege boundary -- it is the caller's own environment -- but a
    role name containing a quote broke out of the identifier and failed with
    a syntax error that blamed the SQL rather than the name.
    """
    con = _stub_psycopg(monkeypatch)
    _from_dsn(role='we"ird')
    statement = str(con.executed[0])
    assert statement == 'SET ROLE "we""ird"'


def test_the_connection_is_autocommit(monkeypatch):
    """Without it psycopg holds a transaction for the whole scan, which pins
    the oldest xmin and blocks vacuum on a production replica."""
    con = _stub_psycopg(monkeypatch)
    _from_dsn()
    assert con.autocommit is True


# --- what the run report records ------------------------------------------

def test_the_report_records_the_role_the_session_is_running_as(monkeypatch):
    """`session_role` was the caller's own input, or the literal string
    "unknown" when they passed nothing. A minimal-grant claim that quotes
    your input back at you has checked nothing."""
    _stub_psycopg(monkeypatch, connection=_FakeConnection(role_answer="reader_role"))
    assert _from_dsn().session_role == "reader_role"


def test_running_as_the_dsn_user_is_recorded_rather_than_unknown(monkeypatch):
    """No PG_ROLE: the run is still accountable, it just names the user you
    already had."""
    _stub_psycopg(monkeypatch, connection=_FakeConnection(role_answer="analyst"))
    src = _from_dsn()
    assert src.session_role == "analyst"


def test_a_divergence_between_asked_and_effective_is_said_out_loud(monkeypatch, caplog):
    _stub_psycopg(monkeypatch, connection=_FakeConnection(role_answer="something_else"))
    with caplog.at_level("WARNING"):
        src = _from_dsn(role="asked_for")
    assert src.session_role == "something_else"
    assert "asked_for" in caplog.text and "something_else" in caplog.text


def test_a_session_that_cannot_be_asked_falls_back_rather_than_failing(monkeypatch, caplog):
    """A failed metadata query is a poor reason to abandon a connection that
    otherwise works."""
    class _Mute(_FakeConnection):
        def execute(self, sql):
            if "current_role" in str(sql).lower():
                raise _FakeDriverError("nope")
            return super().execute(sql)

    _stub_psycopg(monkeypatch, connection=_Mute())
    with caplog.at_level("WARNING"):
        src = _from_dsn(role="asked_for")
    assert src.session_role == "asked_for"
    assert "current_role" in caplog.text


def test_real_psycopg_quotes_the_identifier_the_way_this_file_assumes():
    """Guards the stub above against being a model of what I believed.

    The composition test uses a hand-written `Identifier` that doubles inner
    quotes. If psycopg did it differently, that test would pass while the
    product was broken. Skips where the driver is absent, which is most
    developer venvs; it runs in CI, where `.[dev]` installs psycopg.
    """
    sql = pytest.importorskip("psycopg.sql")
    composed = sql.SQL("SET ROLE {}").format(sql.Identifier('we"ird'))
    assert composed.as_string(None) == 'SET ROLE "we""ird"'
