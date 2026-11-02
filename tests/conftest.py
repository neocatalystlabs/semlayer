"""Test-wide safety nets.

The important one: no test may reach the telemetry endpoint. Three test
files invoke `main()`, which now flushes the spool at the end of every
command -- so without this the suite would POST to production on every run,
polluting real usage data with CI noise and making the tests depend on a
network.

This patches `urllib.request.urlopen` rather than `telemetry._post`,
because several telemetry tests reload the module and a patch on a module
attribute would not survive that. urlopen is the narrowest thing that
cannot be reloaded out from under us.
"""

from __future__ import annotations

import os
import urllib.request
from pathlib import Path

import pytest


class TelemetryEscapedInTests(AssertionError):
    """Raised if a test tries to send. Loud on purpose."""


@pytest.fixture(autouse=True)
def _isolate_semlayer_home(tmp_path, monkeypatch):
    """No test may touch the developer's real ~/.semlayer.

    Tests that invoke main() write a spool, an anon id and a first-run
    notice marker. Without this they land in the real home directory, which
    pollutes a developer's own telemetry state and -- worse -- makes tests
    order-dependent: one that asserts on stderr passed only because an
    EARLIER test had already created the notice marker, and failed the
    moment pytest-xdist changed the order.

    Tests that want their own home still set it; this runs first and theirs
    wins.
    """
    monkeypatch.setenv("SEMLAYER_HOME", str(tmp_path / "semlayer-home"))


@pytest.fixture(autouse=True)
def _no_network_telemetry(monkeypatch, request):
    """Fail any attempt to POST telemetry from a test.

    The sender swallows every exception, so in normal tests this simply
    means nothing is sent and the spool is kept -- which is also what a
    user with no network sees, and therefore a realistic default.
    """
    if "allow_telemetry_network" in request.keywords:
        return

    real = urllib.request.urlopen

    def guard(req, *a, **kw):
        url = getattr(req, "full_url", req)
        if isinstance(url, str) and "telemetry.semlayer.dev" in url:
            raise TelemetryEscapedInTests(
                "a test tried to send telemetry to production")
        return real(req, *a, **kw)

    monkeypatch.setattr(urllib.request, "urlopen", guard)


_LAYER_DOC = """semantic_layer:
  spec_version: 0.6.0
  name: {name}
  warehouse: {warehouse}
  generated_by:
    engine: 0.6.0b1
    model: none/heuristic
  tables:
{tables}"""

_LAYER_TABLE = """  - name: t{n}
    source: main.t{n}
    columns:
    - name: id
      sql_type: INTEGER
"""


@pytest.fixture
def built_fixture():
    """The path to a built DuckDB fixture, or a SKIP if it is not built.

    `fixtures/build/` is gitignored and CI builds it before the suite, so a
    test that needs a real warehouse must skip rather than error in a fresh
    clone -- the convention `test_benchmark_replays.py` already follows.
    """
    def get(name: str) -> Path:
        path = Path(__file__).resolve().parents[1] / "fixtures" / "build" / f"{name}.duckdb"
        if not path.is_file():
            pytest.skip(f"fixtures not built: python fixtures/build.py {name}")
        return path
    return get


@pytest.fixture
def write_layer():
    """Write a small but real layer document, for the session tests.

    Here rather than in one test module and imported by the other: CI runs
    `pytest`, which does not put the repository root on `sys.path`, so
    `from tests.test_session_day2 import write` passes under
    `python -m pytest` locally and fails in CI with ModuleNotFoundError.
    It did, on #84's first run.
    """
    def write(path, *, name="tiny", warehouse="duckdb:/tmp/t.duckdb", tables=1):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "".join(_LAYER_TABLE.format(n=i) for i in range(tables))
        path.write_text(_LAYER_DOC.format(name=name, warehouse=warehouse,
                                          tables=body))
        return path
    return write


@pytest.fixture
def cache_layer(write_layer):
    """The same document, in the cache, keyed the way the engine keys it."""
    def cache(home, **kw):
        from semlayer.session import cache_key
        connection = kw.get("warehouse", "duckdb:/tmp/t.duckdb")
        return write_layer(Path(home) / ".semlayer" / "layers"
                           / f"{cache_key(connection)}.yaml", **kw)
    return cache


@pytest.fixture
def nowhere(tmp_path, monkeypatch):
    """A machine with no warehouse configured: empty cwd, empty home, no env.

    For the session and MCP tests, where the subject is discovery -- which
    reads the working directory, the environment, and files under $HOME, so
    all three have to be moved for a test to be about what it stubs.

    The environment is cleared by PREFIX rather than by a list of names, which
    matters: `tests/test_review_mcp.py` loads the developer's own `.env` into
    the process with `load_dotenv`, so by the time these tests run in a full
    suite, every SNOWFLAKE_* and GCP_* variable on this laptop is set. Two
    tests passed alone and failed in the batch before this was prefix-based.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SEMLAYER_HOME", str(tmp_path / "home" / ".semlayer"))
    (tmp_path / "home").mkdir()
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    for var in list(os.environ):
        if var.startswith(("PG_", "SNOWFLAKE_", "GCP_", "DBT_", "CLOUDSDK_",
                           "GOOGLE_APPLICATION_CREDENTIALS")):
            monkeypatch.delenv(var, raising=False)
    return tmp_path
