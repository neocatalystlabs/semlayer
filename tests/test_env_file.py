"""The `.env` file: what it accepts, what it refuses, and what it never keeps.

The point of this file in the product is that an MCP server cannot ask
anyone to export a variable -- so the tests that matter are the ones about
the loop it enables (add a line, call status, it works; remove the line, it
stops) and the ones about what must never happen (a value reaching an
event, a `PATH` line taking effect, a secret outliving the call).
"""

from __future__ import annotations

import os

import pytest

from semlayer import env_file


# --- parsing: the shapes people actually write ----------------------------

def test_the_ordinary_line():
    assert env_file.parse("PG_READER_DSN=postgresql://r@h:5432/db") == {
        "PG_READER_DSN": "postgresql://r@h:5432/db"}


def test_export_blank_lines_and_comments():
    text = """
    # semlayer credentials

    export SNOWFLAKE_ACCOUNT=ORG-ACCT
      GCP_PROJECT=my-project
    # SNOWFLAKE_READER_USER=commented_out
    """
    assert env_file.parse(text) == {"SNOWFLAKE_ACCOUNT": "ORG-ACCT",
                                    "GCP_PROJECT": "my-project"}


@pytest.mark.parametrize(("line", "value"), [
    ("A='keep #this'", "keep #this"),
    ('A="keep #this"', "keep #this"),
    ("A=drop # this", "drop"),
    ("A=pa#ssword", "pa#ssword"),
    ("A=  spaced  ", "spaced"),
    ("A=", ""),
])
def test_quotes_and_the_inline_comment(line, value):
    """A `#` inside a password is the realistic case, so only ` #` comments.

    Which means an unquoted value cannot hold ` # ` -- documented in the
    module, and the reason the quoted forms exist.
    """
    assert env_file.parse(line) == {"A": value}


def test_escapes_inside_double_quotes_only():
    assert env_file.parse('A="one\\ntwo"') == {"A": "one\ntwo"}
    assert env_file.parse("A='one\\ntwo'") == {"A": "one\\ntwo"}


@pytest.mark.parametrize("line", [
    "not a key=value", "9LEADING=1", "NO_EQUALS_SIGN", "=orphan", "-X=1",
])
def test_lines_that_are_not_assignments_are_skipped(line):
    """Skipped, never raised: this file is hand-edited mid-conversation, and
    a typo three lines down must not take `status` with it."""
    assert env_file.parse(f"GOOD=1\n{line}\nALSO_GOOD=2") == {
        "GOOD": "1", "ALSO_GOOD": "2"}


def test_a_lowercase_key_is_kept_so_the_agent_can_see_the_mistake():
    """`pg_reader_dsn` is a legal variable name and a useless one -- the
    connectors read the upper-case spelling. Dropping it silently would hide
    the typo; keeping it puts the name in `status.env_file.keys`, where an
    agent can see what the user actually wrote and tell them."""
    assert env_file.parse("pg_reader_dsn=x") == {"pg_reader_dsn": "x"}


@pytest.mark.parametrize("key", ["PATH", "LD_PRELOAD", "HOME", "SEMLAYER_HOME",
                                 "PYTHONPATH", "BASH_ENV"])
def test_keys_that_shape_the_process_are_refused(key):
    """A credentials file is trusted with credentials, not with this process.

    `SEMLAYER_HOME` is in the list because the file lives inside it: a line
    that moved the home directory would relocate the directory it was read
    from.
    """
    assert env_file.parse(f"{key}=/tmp/anything") == {}
    assert key not in env_file.parse(f"{key}=/tmp/x\nPG_READER_DSN=y")


def test_an_enormous_value_is_dropped():
    assert env_file.parse("A=" + "x" * (env_file.MAX_VALUE + 1)) == {}


# --- reading: a missing or hopeless file is normal ------------------------

def test_a_missing_file_is_not_an_error(tmp_path):
    assert env_file.read(tmp_path / "nope.env") == {}


def test_a_directory_is_not_an_error(tmp_path):
    assert env_file.read(tmp_path) == {}


def test_a_file_too_big_to_be_credentials_is_not_read(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=1\n" + "# padding\n" * env_file.MAX_BYTES)
    assert env_file.read(path) == {}


def test_binary_rubbish_does_not_raise(tmp_path):
    path = tmp_path / ".env"
    path.write_bytes(b"\xff\xfe\x00A=1\n")
    env_file.read(path)              # the assertion is that it returns


# --- the environment it produces -----------------------------------------

def test_the_file_wins_over_the_inherited_environment(tmp_path, monkeypatch):
    """Because the inherited value is the one that needs a client restart to
    change, and the file is the one the user can fix while we are talking."""
    monkeypatch.setenv("PG_READER_DSN", "postgresql://stale@old:5432/db")
    (tmp_path / ".env").write_text("PG_READER_DSN=postgresql://fresh@new:5432/db")
    env = env_file.environment(tmp_path / ".env")
    assert env["PG_READER_DSN"] == "postgresql://fresh@new:5432/db"


def test_the_real_environment_is_still_there(tmp_path, monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "FROM-CONFIG")
    (tmp_path / ".env").write_text("GCP_PROJECT=p")
    env = env_file.environment(tmp_path / ".env")
    assert env["SNOWFLAKE_ACCOUNT"] == "FROM-CONFIG"
    assert env["GCP_PROJECT"] == "p"


def test_a_removed_line_stops_working_on_the_next_call(tmp_path):
    """The whole reason the file is re-read rather than cached."""
    path = tmp_path / ".env"
    path.write_text("PG_READER_DSN=postgresql://r@h:5432/db")
    assert "PG_READER_DSN" in env_file.environment(path)
    path.write_text("# removed\n")
    assert "PG_READER_DSN" not in env_file.environment(path)


def test_reading_the_file_does_not_touch_the_process_environment(tmp_path):
    (tmp_path / ".env").write_text("SEMLAYER_TEST_ONLY=1")
    env_file.environment(tmp_path / ".env")
    assert "SEMLAYER_TEST_ONLY" not in os.environ


# --- applying to os.environ: the one case that must be temporary ---------

def test_applied_sets_and_then_removes(monkeypatch):
    monkeypatch.delenv("SEMLAYER_TEST_ONLY", raising=False)
    with env_file.applied({"SEMLAYER_TEST_ONLY": "yes"}):
        assert os.environ["SEMLAYER_TEST_ONLY"] == "yes"
    assert "SEMLAYER_TEST_ONLY" not in os.environ


def test_applied_restores_a_previous_value(monkeypatch):
    monkeypatch.setenv("SEMLAYER_TEST_ONLY", "before")
    with env_file.applied({"SEMLAYER_TEST_ONLY": "during"}):
        assert os.environ["SEMLAYER_TEST_ONLY"] == "during"
    assert os.environ["SEMLAYER_TEST_ONLY"] == "before"


def test_applied_restores_after_a_failure(monkeypatch):
    monkeypatch.setenv("SEMLAYER_TEST_ONLY", "before")
    with pytest.raises(RuntimeError), env_file.applied({"SEMLAYER_TEST_ONLY": "during"}):
        raise RuntimeError("the connection failed")
    assert os.environ["SEMLAYER_TEST_ONLY"] == "before"


def test_applied_refuses_the_denied_keys(monkeypatch):
    before = os.environ["PATH"]
    with env_file.applied({"PATH": "/nowhere"}):
        assert os.environ["PATH"] == before
    assert os.environ["PATH"] == before
