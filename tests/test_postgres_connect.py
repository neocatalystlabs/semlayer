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


def _from_dsn(**kw):
    from semlayer.connectors.postgres import PostgresSource
    return PostgresSource.from_dsn("postgresql://u@h/db", **kw)


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
