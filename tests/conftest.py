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
