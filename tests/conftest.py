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

import urllib.request

import pytest


class TelemetryEscapedInTests(AssertionError):
    """Raised if a test tries to send. Loud on purpose."""


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
