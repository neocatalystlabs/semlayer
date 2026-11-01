"""What an MCP session tells telemetry: which client, and which tools.

Named for `host` because that field came first. Kept that way deliberately:
renaming it would mean the public tree holds a file the monorepo no longer
has until the rename merges, and `check_public_tree.py` is right to call
that out -- it cannot tell a rename in flight from a file someone added
straight to the public repo. A filename is not worth blunting that.

Why this is tested in-process rather than end to end: driving the real
server over stdio means a child process, and a child does not inherit
conftest's `urlopen` guard -- `SEMLAYER_HOME` does not propagate through
the SDK's default environment allowlist, so the child would use the real
spool and post to the real endpoint. A test must not widen that.

What is covered here is the hook location, which is the part that can
silently stop working: FastMCP binds its handlers during construction, so
the note has to live in an override, and it has to fire on `list_tools`
rather than only on a tool call -- the sessions these fields exist to
identify disconnect almost at once, well before any tool is reached.

The two fields need different hooks, which is why both are tested.
`host` has to be noted on `list_tools`, because a session that lists and
leaves is still a client worth counting. `tools` can only come from
`call_tool`, because listing the tools says nothing about using them --
and "listed, called nothing" is precisely the reading the field exists to
produce.
"""

from __future__ import annotations

import json

import anyio
import mcp.server.lowlevel.server as lowlevel
import pytest
import yaml
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
    """Both of these are process-scoped and never reset by design: one CLI
    process serves one session and writes one event at exit. Tests run many
    sessions in one process, so they have to undo what the design relies on
    not undoing."""
    telemetry._pending.clear()
    mcp_server._TOOLS_SEEN.clear()
    yield
    telemetry._pending.clear()
    mcp_server._TOOLS_SEEN.clear()


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


# --- tools: whether the session used the server, and for what --------------

def test_calling_a_tool_notes_its_name(doc):
    srv = mcp_server.build_server(doc)
    _in_a_request("cursor", srv.call_tool, "get_domains", {})
    assert ts.serialize(telemetry._pending)["tools"] == ["get_domains"]


def test_several_calls_accumulate_into_one_set(doc):
    """One event is written per process, so the field has to carry the whole
    session rather than the last call. `telemetry.note` replaces a value, so
    the accumulation has to happen before it."""
    srv = mcp_server.build_server(doc)
    _in_a_request("cursor", srv.call_tool, "get_domains", {})
    _in_a_request("cursor", srv.call_tool, "semantic_search", {"query": "orders"})
    _in_a_request("cursor", srv.call_tool, "get_domains", {})
    assert ts.serialize(telemetry._pending)["tools"] == ["get_domains",
                                                         "semantic_search"]


def test_listing_tools_is_not_using_them(doc):
    """The distinction the field exists for: a client that lists and leaves
    has a `host` and no `tools`, which is what a probe looks like."""
    srv = mcp_server.build_server(doc)
    _in_a_request("sdk", srv.list_tools)
    out = ts.serialize(telemetry._pending)
    assert out["host"] == "sdk"
    assert "tools" not in out


def test_tool_arguments_are_never_noted(doc):
    """`check_sql` takes SQL and `semantic_search` takes the user's question.
    Neither may appear anywhere in the event."""
    srv = mcp_server.build_server(doc)
    _in_a_request("cursor", srv.call_tool, "check_sql",
                  {"sql": "select * from secret_payroll"})
    out = ts.serialize(telemetry._pending)
    assert out["tools"] == ["check_sql"]
    assert "payroll" not in json.dumps(out)


def test_an_unknown_tool_name_cannot_grow_the_set_without_bound():
    """The set is keyed on what a client asks for, and a client is not ours
    to trust. Past the cap we stop collecting; the names are still classified
    by the serializer, not here.

    Calls `_note_tool` directly: a real client cannot reach 52 distinct tool
    names through `call_tool`, because the SDK rejects a name the server
    never registered -- which is exactly why the cap is defence against the
    hook being reused, not against today's dispatch path.
    """
    for i in range(mcp_server._TOOLS_CAP + 20):
        mcp_server._note_tool(f"made_up_{i}")
    assert len(mcp_server._TOOLS_SEEN) == mcp_server._TOOLS_CAP
    assert ts.serialize(telemetry._pending)["tools"] == ["other"]


def test_a_fruitless_tool_call_still_records_that_it_was_called(doc):
    """The note happens before dispatch deliberately: a client that asked for
    a table that is not there still used the server, and a session that only
    ever comes up empty is a different story from one that never tried.

    `table_detail` on an unknown name does not raise -- it answers with the
    unknown-object payload -- so what this pins down is the ordering, not
    exception handling.
    """
    srv = mcp_server.build_server(doc)
    out = _in_a_request("cursor", srv.call_tool, "table_detail",
                        {"name": "no_such_table_at_all"})
    assert out
    assert ts.serialize(telemetry._pending)["tools"] == ["table_detail"]
