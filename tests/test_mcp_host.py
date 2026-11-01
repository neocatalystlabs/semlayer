"""Which MCP client launched the server, and how it reaches the event.

Why this is tested in-process rather than end to end: driving the real
server over stdio means a child process, and a child does not inherit
conftest's `urlopen` guard -- it would POST to production telemetry. That
hole is the subject of HANDOFF 2026-10-31; a test must not widen it.

What is covered here is the hook location, which is the part that can
silently stop working: FastMCP binds its handlers during construction, so
the note has to live in an override, and it has to fire on `list_tools`
rather than only on a tool call -- the sessions this field exists to
identify disconnect after a p50 of 0 seconds.
"""

from __future__ import annotations

import anyio
import pytest
import yaml

import mcp.server.lowlevel.server as lowlevel
from mcp.server.lowlevel.server import RequestContext

from semlayer import mcp_server, telemetry
from semlayer import telemetry_schema as ts


@pytest.fixture
def doc():
    """A hand-authored gold layer: no warehouse, no LLM, no cassette."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    return yaml.safe_load((root / "fixtures" / "golds" / "fan_trap.yaml").read_text())


@pytest.fixture(autouse=True)
def _clean_pending():
    """`telemetry._pending` is process-scoped and never reset by design."""
    telemetry._pending.clear()
    yield
    telemetry._pending.clear()


class _Client:
    def __init__(self, name):
        self.clientInfo = type("Info", (), {"name": name})()


class _Session:
    def __init__(self, name):
        self.client_params = _Client(name)


def _in_a_request(name, fn, *a):
    """Run `fn` as the SDK would: inside a request context for one client."""
    ctx = RequestContext(request_id=1, meta=None, session=_Session(name),
                         lifespan_context=None)
    token = lowlevel.request_ctx.set(ctx)
    try:
        return anyio.run(fn, *a)
    finally:
        lowlevel.request_ctx.reset(token)


def test_listing_tools_notes_the_client(doc):
    """The hook that matters: it fires before any tool is called."""
    srv = mcp_server.build_server(doc)
    tools = _in_a_request("Claude Code", srv.list_tools)
    assert tools, "the override must still return the tool list"
    assert telemetry._pending["host"] == "Claude Code"
    assert ts.serialize(telemetry._pending)["host"] == "claude-code"


def test_calling_a_tool_notes_the_client(doc):
    """For a client that cached the tool list and calls straight in."""
    srv = mcp_server.build_server(doc)
    _in_a_request("cursor-vscode", srv.call_tool, "get_domains", {})
    assert ts.serialize(telemetry._pending)["host"] == "cursor"


def test_a_tool_still_returns_its_answer(doc):
    """The override wraps; it must not swallow or reshape the result."""
    srv = mcp_server.build_server(doc)
    out = _in_a_request("mcp", srv.call_tool, "get_domains", {})
    assert out


def test_no_request_context_is_silent(doc):
    """`build_server` is used in-process by other tests, where there is no
    request at all. A telemetry note may never break a tool call."""
    srv = mcp_server.build_server(doc)
    tools = anyio.run(srv.list_tools)          # no context set
    assert tools
    assert "host" not in telemetry._pending


def test_a_client_without_clientinfo_is_silent(doc):
    """A session that never completed initialize carries no client_params."""
    srv = mcp_server.build_server(doc)
    ctx = RequestContext(request_id=1, meta=None, session=object(),
                         lifespan_context=None)
    token = lowlevel.request_ctx.set(ctx)
    try:
        assert anyio.run(srv.list_tools)
    finally:
        lowlevel.request_ctx.reset(token)
    assert "host" not in telemetry._pending
