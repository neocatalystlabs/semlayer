"""The forms a person can hand `semlayer infer` for Snowflake.

Why this file exists: in the ten days after the telemetry sender went live,
exactly one install outside the team tried a real warehouse — Snowflake, 14
runs, 14 `connection` errors, zero successes. The only Snowflake code path
that produces that error class is the "unsupported source uri" refusal, so
what they pasted was a connection name or a URL, and the CLI accepted neither:
only the bare word `snowflake` plus environment variables. Every test here is
one spelling that must not be refused again.

The driver is stubbed throughout, so these run anywhere — including CI, which
has no Snowflake account and no `[warehouses]` extra.
"""

from __future__ import annotations

import sys
import types

import pytest

from semlayer.errors import SourceError


class _FakeDriverError(Exception):
    """Stands in for snowflake.connector.errors.Error."""


class _FakeCursor:
    """Answers `select current_role()` and nothing else.

    `_effective_role` asks the session what it is running as, so the stub has
    to be able to answer -- otherwise every test would exercise the
    could-not-ask fallback rather than the path being tested.
    """

    def __init__(self, current_role):
        self._current_role = current_role
        self.executed: list[str] = []
        self._row = None

    def execute(self, sql):
        self.executed.append(sql)
        if "current_role" in sql.lower():
            self._row = (self._current_role,)
        return self

    def fetchone(self):
        return self._row

    def fetchall(self):
        return [self._row] if self._row else []

    def close(self):
        pass


def _stub_driver(monkeypatch, captured: dict, raises: Exception | None = None,
                 current_role: str = "WHATEVER_ROLE"):
    mod = types.ModuleType("snowflake")
    conn = types.ModuleType("snowflake.connector")
    errors = types.ModuleType("snowflake.connector.errors")
    errors.Error = _FakeDriverError

    def connect(**kw):
        captured.update(kw)
        if raises is not None:
            raise raises
        return types.SimpleNamespace(
            cursor=lambda: _FakeCursor(current_role), close=lambda: None)

    conn.connect = connect
    conn.errors = errors
    mod.connector = conn
    for name, m in (("snowflake", mod), ("snowflake.connector", conn),
                    ("snowflake.connector.errors", errors)):
        monkeypatch.setitem(sys.modules, name, m)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """No Snowflake setting leaks in from the developer's shell."""
    for k in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_DATABASE", "SNOWFLAKE_WAREHOUSE",
              "SNOWFLAKE_ROLE", "SNOWFLAKE_HOME", "SNOWFLAKE_AUTHENTICATOR",
              "SNOWFLAKE_PRIVATE_KEY_PATH", "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE",
              "SNOWFLAKE_READER_USER", "SNOWFLAKE_READER_ROLE",
              "SNOWFLAKE_READER_PASSWORD", "SNOWFLAKE_READER_AUTHENTICATOR",
              "SNOWFLAKE_READER_PRIVATE_KEY_PATH"):
        monkeypatch.delenv(k, raising=False)


def _open(uri):
    from semlayer.pipeline import open_source_from_uri
    return open_source_from_uri(uri)


# --- the URL form ---------------------------------------------------------

def test_a_url_carries_account_user_database_role_and_warehouse(monkeypatch):
    captured: dict = {}
    _stub_driver(monkeypatch, captured, current_role="reader_role")
    monkeypatch.setenv("SNOWFLAKE_READER_PASSWORD", "pw")
    src = _open("snowflake://me@my-org-acct/analytics?role=reader_role&warehouse=wh_xs")
    assert captured["account"] == "my-org-acct"
    assert captured["user"] == "me"
    assert captured["database"] == "analytics"
    assert captured["role"] == "reader_role"
    assert captured["warehouse"] == "wh_xs"
    assert src.database == "analytics"
    assert src.session_role == "reader_role", "the run report records the role in effect"


def test_a_pasted_hostname_is_accepted_as_an_account(monkeypatch):
    """What the Snowflake console shows is a hostname, not an identifier."""
    captured: dict = {}
    _stub_driver(monkeypatch, captured)
    monkeypatch.setenv("SNOWFLAKE_READER_PASSWORD", "pw")
    _open("snowflake://me@abc12345.us-east-1.snowflakecomputing.com/db?role=r")
    assert captured["account"] == "abc12345.us-east-1"


def test_a_url_password_is_used_and_called_out(monkeypatch, caplog):
    captured: dict = {}
    _stub_driver(monkeypatch, captured)
    with caplog.at_level("WARNING"):
        _open("snowflake://me:secret@acct/db?role=r")
    assert captured["password"] == "secret"
    assert "shell history" in caplog.text, "a password in a URL must be said out loud"


def test_a_url_schema_segment_reaches_the_driver(monkeypatch):
    captured: dict = {}
    _stub_driver(monkeypatch, captured)
    monkeypatch.setenv("SNOWFLAKE_READER_PASSWORD", "pw")
    _open("snowflake://me@acct/db/finance?role=r")
    assert captured["database"] == "db" and captured["schema"] == "finance"


def test_a_url_without_a_role_runs_as_the_user(monkeypatch):
    """This used to be refused outright.

    The refusal was protecting a minimal-grant claim, but the claim was never
    checked -- the report recorded the role we had been handed. Now no role
    is sent, the session keeps whatever default it has, and the report says
    what that turned out to be.
    """
    captured: dict = {}
    _stub_driver(monkeypatch, captured, current_role="ANALYST")
    monkeypatch.setenv("SNOWFLAKE_READER_PASSWORD", "pw")
    src = _open("snowflake://me@acct/db")
    assert "role" not in captured, (
        "passing role=None makes the driver send a null role rather than "
        "leaving the session default alone")
    assert src.session_role == "ANALYST"


def test_a_url_missing_a_part_names_that_part(monkeypatch):
    _stub_driver(monkeypatch, {})
    monkeypatch.setenv("SNOWFLAKE_READER_PASSWORD", "pw")
    with pytest.raises(SourceError, match="no database"):
        _open("snowflake://me@acct?role=r")


# --- the named-connection form -------------------------------------------

def _write_connections(tmp_path, monkeypatch, body):
    monkeypatch.setenv("SNOWFLAKE_HOME", str(tmp_path))
    (tmp_path / "connections.toml").write_text(body)


def test_a_named_connection_is_opened_through_the_driver(monkeypatch, tmp_path):
    captured: dict = {}
    _stub_driver(monkeypatch, captured, current_role="reader_role")
    _write_connections(tmp_path, monkeypatch, """
[work]
account = "my-org-acct"
user = "me"
role = "reader_role"
database = "analytics"
""")
    src = _open("snowflake:work")
    assert captured["connection_name"] == "work"
    assert captured["role"] == "reader_role", "the role we record must be the role sent"
    assert captured["database"] == "analytics"
    assert src.session_role == "reader_role"


def test_the_slashed_spelling_is_the_same_named_connection(monkeypatch, tmp_path):
    captured: dict = {}
    _stub_driver(monkeypatch, captured, current_role="r")
    _write_connections(tmp_path, monkeypatch, """
[work]
role = "r"
database = "db"
""")
    _open("snowflake://work")
    assert captured["connection_name"] == "work"


def test_a_named_connection_without_a_role_runs_as_the_user(monkeypatch, tmp_path):
    """A `snow` user whose connection carries no role is not turned away."""
    captured: dict = {}
    _stub_driver(monkeypatch, captured, current_role="ANALYST")
    _write_connections(tmp_path, monkeypatch, '[work]\ndatabase = "db"\n')
    src = _open("snowflake:work")
    assert "role" not in captured
    assert src.session_role == "ANALYST"


def test_an_unknown_connection_name_lists_the_known_ones(monkeypatch, tmp_path):
    _stub_driver(monkeypatch, {})
    _write_connections(tmp_path, monkeypatch, '[work]\nrole = "r"\ndatabase = "db"\n')
    with pytest.raises(SourceError) as e:
        _open("snowflake:home")
    assert "'home'" in str(e.value) and "work" in e.value.hint


def test_a_missing_connections_file_points_at_the_other_two_forms(monkeypatch, tmp_path):
    """The failure a stranger hits must never be "expected one of: ..."."""
    _stub_driver(monkeypatch, {})
    monkeypatch.setenv("SNOWFLAKE_HOME", str(tmp_path / "absent"))
    with pytest.raises(SourceError) as e:
        _open("snowflake:work")
    assert "connections.toml" in str(e.value)
    assert "snowflake://" in e.value.hint and "init snowflake" in e.value.hint


# --- the environment form, and what the driver says ----------------------

def test_a_missing_account_is_the_users_mistake_not_ours(monkeypatch):
    """`os.environ[...]` raised KeyError, which telemetry filed as `internal`."""
    _stub_driver(monkeypatch, {})
    monkeypatch.setenv("SNOWFLAKE_READER_ROLE", "r")
    monkeypatch.setenv("SNOWFLAKE_READER_PASSWORD", "pw")
    with pytest.raises(SourceError, match="SNOWFLAKE_ACCOUNT"):
        _open("snowflake")


def test_a_refused_login_becomes_a_source_error_carrying_the_drivers_words(monkeypatch):
    captured: dict = {}
    _stub_driver(monkeypatch, captured,
                 raises=_FakeDriverError("250001: Could not connect to Snowflake "
                                         "backend after 0 attempt(s)"))
    monkeypatch.setenv("SNOWFLAKE_READER_PASSWORD", "pw")
    with pytest.raises(SourceError) as e:
        _open("snowflake://me@acct/db?role=r")
    assert "refused the connection" in str(e.value)
    assert "250001" in e.value.hint, "the driver's message is what the person needs"


def test_a_truly_unsupported_uri_still_lists_the_forms():
    with pytest.raises(SourceError) as e:
        _open("redshift://cluster/db")
    assert "snowflake://" in e.value.hint and "duckdb" in e.value.hint


# --- the role in effect, not the role asked for ---------------------------

def test_the_report_records_the_role_the_session_is_actually_running_as(monkeypatch):
    """The whole point of asking CURRENT_ROLE().

    Snowflake decides what a session runs as; a secondary role or an
    ownership rule can leave it running as something other than what was
    requested. The run report has to name what ran, because that is what the
    minimal-grant claim is about.
    """
    captured: dict = {}
    _stub_driver(monkeypatch, captured, current_role="SOMETHING_ELSE")
    monkeypatch.setenv("SNOWFLAKE_READER_PASSWORD", "pw")
    src = _open("snowflake://me@acct/db?role=asked_for")
    assert captured["role"] == "asked_for", "we still ask for what we were told to"
    assert src.session_role == "SOMETHING_ELSE", "but we record what we got"


def test_a_role_that_is_not_the_one_asked_for_is_said_out_loud(monkeypatch, caplog):
    """Warned, not raised: the run is valid, the difference just must not be
    silent."""
    _stub_driver(monkeypatch, {}, current_role="SOMETHING_ELSE")
    monkeypatch.setenv("SNOWFLAKE_READER_PASSWORD", "pw")
    with caplog.at_level("WARNING"):
        _open("snowflake://me@acct/db?role=asked_for")
    assert "asked_for" in caplog.text and "SOMETHING_ELSE" in caplog.text


def test_agreement_is_not_announced(monkeypatch, caplog):
    """A warning on every ordinary run is a warning nobody reads."""
    _stub_driver(monkeypatch, {}, current_role="reader_role")
    monkeypatch.setenv("SNOWFLAKE_READER_PASSWORD", "pw")
    with caplog.at_level("WARNING"):
        _open("snowflake://me@acct/db?role=reader_role")
    assert "running as" not in caplog.text


@pytest.mark.parametrize("answer", ["", "   ", "NULL", None])
def test_no_role_in_effect_at_all_is_still_refused(monkeypatch, answer):
    """The one case the old refusal was right about.

    An account with no DEFAULT_ROLE and no role passed has no privileges, so
    nothing downstream works. This is the old message, kept for the situation
    that actually warranted it rather than applied to everyone.
    """
    _stub_driver(monkeypatch, {}, current_role=answer)
    monkeypatch.setenv("SNOWFLAKE_READER_PASSWORD", "pw")
    with pytest.raises(SourceError) as e:
        _open("snowflake://me@acct/db")
    assert "no Snowflake role is in effect" in str(e.value)
    assert "SNOWFLAKE_READER_ROLE" in e.value.hint
    assert "?role=" in e.value.hint and "connections.toml" in e.value.hint


def test_a_session_that_cannot_be_asked_falls_back_rather_than_failing(monkeypatch, caplog):
    """A failed metadata query is a poor reason to abandon a working
    connection. We say we could not tell, and carry on."""
    import types as _types

    captured: dict = {}
    _stub_driver(monkeypatch, captured, current_role="ignored")
    monkeypatch.setenv("SNOWFLAKE_READER_PASSWORD", "pw")

    import snowflake.connector as driver  # the stub installed above

    def broken_connect(**kw):
        captured.update(kw)
        return _types.SimpleNamespace(cursor=lambda: None, close=lambda: None)

    monkeypatch.setattr(driver, "connect", broken_connect)
    with caplog.at_level("WARNING"):
        src = _open("snowflake://me@acct/db?role=asked_for")
    assert src.session_role == "asked_for", "fall back to what we asked for"
    assert "CURRENT_ROLE()" in caplog.text


def test_running_as_yourself_needs_no_role_anywhere(monkeypatch):
    """The environment form, which is what `semlayer init` sets up -- and the
    path a first run takes before anyone has created a role."""
    captured: dict = {}
    _stub_driver(monkeypatch, captured, current_role="ANALYST")
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "my-org-acct")
    monkeypatch.setenv("SNOWFLAKE_READER_USER", "me")
    monkeypatch.setenv("SNOWFLAKE_READER_PASSWORD", "pw")
    src = _open("snowflake")
    assert "role" not in captured
    assert src.session_role == "ANALYST"
