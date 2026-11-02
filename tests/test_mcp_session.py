"""The MCP server with no layer: what a fresh install actually talks to.

The server is the front door -- it is what people add to Claude Desktop,
Claude Code or Cursor first, and for most installs the only thing they ever
run. Until now it demanded a layer path, and every tool indexed straight
into the document, so a server started without one raised on the first
call. An agent handed an error there can only retry or give up.

So the questions here are about the no-layer state: does `status` answer,
does every other tool fall back to it rather than raising, and does the
answer tell an agent what to do next. Driven in-process rather than over
stdio for the reason `test_mcp_host.py` gives: a child process does not
inherit conftest's urlopen guard.
"""

from __future__ import annotations

import json

import anyio
import pytest
import yaml

from semlayer import mcp_server
from semlayer.session import Session


@pytest.fixture(autouse=True)
def _nowhere(nowhere):
    """Every test here runs on an empty machine: see conftest's `nowhere`."""
    return nowhere


@pytest.fixture
def gold():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    return yaml.safe_load((root / "fixtures" / "golds" / "fan_trap.yaml").read_text())


def call(srv, tool, arguments=None) -> dict | list:
    """Call a tool the way the SDK does, and parse its JSON answer.

    `arguments` is a dict rather than keywords because `table_detail` takes a
    parameter called `name`, and a helper cannot have both.
    """
    content, _ = anyio.run(srv.call_tool, tool, arguments or {})
    return json.loads(content[0].text)


def test_the_server_starts_with_no_layer_at_all():
    """The whole point: `semlayer mcp` with no argument must come up."""
    srv = mcp_server.build_server(Session())
    assert call(srv, "status")["state"] == "no_layer"


def test_both_session_tools_are_registered():
    srv = mcp_server.build_server(Session())
    names = {t.name for t in anyio.run(srv.list_tools)}
    assert {"status", "discover_connections"} <= names


@pytest.mark.parametrize(("tool", "arguments"), [
    ("semantic_search", {"query": "revenue"}),
    ("get_domains", {}),
    ("get_tables", {}),
    ("table_detail", {"name": "orders"}),
    ("get_metrics", {}),
    ("route_intent", {"intent": "revenue by month"}),
    ("check_sql", {"sql": "select 1"}),
    ("compile_metric", {"name": "revenue"}),
])
def test_every_tool_answers_with_status_when_there_is_no_layer(tool, arguments):
    """An agent must never get a bare error. Each of these used to raise."""
    srv = mcp_server.build_server(Session())
    out = call(srv, tool, arguments)
    assert out["state"] == "no_layer"
    assert "semlayer infer" in out["next"]


def test_the_fallback_names_what_it_can_see(_nowhere):
    (_nowhere / "cwd" / "warehouse.duckdb").write_text("")
    srv = mcp_server.build_server(Session())
    out = call(srv, "get_tables")
    assert [c["uri"] for c in out["connections"]] == ["duckdb:warehouse.duckdb"]
    assert "duckdb:warehouse.duckdb" in out["next"]


def test_discover_connections_opens_nothing(_nowhere):
    (_nowhere / "cwd" / "not-a-database.duckdb").write_text("junk")
    srv = mcp_server.build_server(Session())
    out = call(srv, "discover_connections")
    assert out[0]["uri"] == "duckdb:not-a-database.duckdb"
    assert out[0]["supported"] is True


def test_a_layer_loaded_from_a_path_serves_as_before(tmp_path, gold):
    """`semlayer mcp <path>` is unchanged, and the tools answer from the
    document rather than falling back."""
    path = tmp_path / "layer.yaml"
    path.write_text(yaml.safe_dump(gold))
    s = Session()
    s.load(path)
    srv = mcp_server.build_server(s)
    assert call(srv, "status")["state"] == "ready"
    assert {t["name"] for t in call(srv, "get_tables")} == {
        t["name"] for t in gold["semantic_layer"]["tables"]}
    assert call(srv, "table_detail", {"name": "orders"})["name"] == "orders"
    assert call(srv, "semantic_search", {"query": "order revenue"})


def test_a_bare_document_still_builds_a_working_server(gold):
    """Every in-process caller passes a document and no connection."""
    srv = mcp_server.build_server(gold)
    assert call(srv, "status")["state"] == "ready"
    assert call(srv, "get_domains")


def test_status_says_which_connection_is_selected():
    s = Session()
    s.select("duckdb:/data/w.duckdb")
    out = call(mcp_server.build_server(s), "status")
    assert out["connection"] == "duckdb:/data/w.duckdb"
    assert out["state"] == "no_layer"


def test_the_instructions_tell_the_client_to_call_status_first():
    """The instructions are the only thing a client reads before it decides
    what to do, and for a no-layer install `status` is the only useful move."""
    srv = mcp_server.build_server(Session())
    assert "status" in srv.instructions.lower()


# --- the CLI command ------------------------------------------------------

@pytest.fixture
def served(monkeypatch):
    """Capture the session `semlayer mcp` would serve, without serving it."""
    sessions = []

    class _Stub:
        def run(self):
            pass

    def fake_build(layer):
        sessions.append(Session.of(layer))
        return _Stub()

    monkeypatch.setattr(mcp_server, "build_server", fake_build)
    return sessions


def test_the_command_takes_no_argument_at_all(served):
    """The change the whole slice exists for: a client can launch the server
    before anything has been inferred."""
    from semlayer.cli import main
    assert main(["mcp"]) == 0
    assert served[0].state == "no_layer"
    assert served[0].connection is None


def test_a_path_still_serves_that_layer(served, tmp_path, gold):
    from semlayer.cli import main
    path = tmp_path / "layer.yaml"
    path.write_text(yaml.safe_dump(gold))
    assert main(["mcp", str(path)]) == 0
    assert served[0].state == "ready"
    assert served[0].layer_path == path


def test_a_missing_path_is_still_an_error(served, tmp_path):
    from semlayer.cli import main
    assert main(["mcp", str(tmp_path / "nope.yaml")]) == 2
    assert served == [], "nothing is served when the named file is not there"


def test_a_connection_with_no_cached_layer_still_starts(served):
    from semlayer.cli import main
    assert main(["mcp", "--connection", "snowflake:prod"]) == 0
    assert served[0].state == "no_layer"
    assert served[0].connection == "snowflake:prod"


def test_a_connection_with_a_cached_layer_serves_it(served, gold):
    from semlayer import session as sess
    from semlayer.cli import main
    path = sess.layer_cache_path("duckdb:/data/w.duckdb")
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(gold))
    assert main(["mcp", "--connection", "duckdb:/data/w.duckdb"]) == 0
    assert served[0].state == "ready"
    assert served[0].layer_path == path


def test_an_explicit_path_beats_a_cached_layer(served, tmp_path, gold, _nowhere):
    """Explicit wins, as it always has: someone who named a file wants it."""
    from semlayer import session as sess
    from semlayer.cli import main
    cached = sess.layer_cache_path("duckdb:/data/w.duckdb")
    cached.parent.mkdir(parents=True)
    cached.write_text(yaml.safe_dump(gold))
    named = tmp_path / "named.yaml"
    named.write_text(yaml.safe_dump(gold))
    assert main(["mcp", str(named), "--connection", "duckdb:/data/w.duckdb"]) == 0
    assert served[0].layer_path == named
    assert served[0].connection == "duckdb:/data/w.duckdb"


def test_the_recorded_event_shape_is_unchanged(served, tmp_path, monkeypatch):
    """argparse runs before the recording block, so an optional argument and
    a new flag must not add a field to the `mcp` event -- a warehouse
    identifier is exactly what `--connection` holds, and it does not travel."""
    import importlib

    monkeypatch.setenv("SEMLAYER_HOME", str(tmp_path / "tele"))
    monkeypatch.delenv("SEMLAYER_TELEMETRY", raising=False)
    from semlayer import telemetry
    importlib.reload(telemetry)
    telemetry._pending.clear()
    try:
        from semlayer.cli import main
        assert main(["mcp", "--connection", "snowflake:prod"]) == 0
        rows = [json.loads(line) for line in
                (tmp_path / "tele" / "telemetry.jsonl").read_text().splitlines()]
    finally:
        telemetry._pending.clear()
        importlib.reload(telemetry)
    assert len(rows) == 1
    assert set(rows[0]) == {"schema", "t", "anon", "event_id", "event", "command",
                            "version", "python", "os", "arch", "ci",
                            "duration_s", "outcome"}
    assert rows[0]["command"] == "mcp"
    assert "snowflake" not in json.dumps(rows[0])
