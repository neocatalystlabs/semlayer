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


def test_the_session_tools_are_registered():
    srv = mcp_server.build_server(Session())
    names = {t.name for t in anyio.run(srv.list_tools)}
    assert {"status", "discover_connections", "load_layer"} <= names


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
    assert "built" in out["next"]
    assert "semlayer infer" not in out["next"]


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


# --- the instructions: what the agent is told before it calls anything ----

def test_the_handshake_forbids_asking_for_a_secret():
    """Pinned by the spec. This string is the only product knowledge the
    model gets before it starts guessing, and the one rule we cannot enforce
    in code is the one it must carry: never ask for a password in chat."""
    text = mcp_server._INSTRUCTIONS
    assert "Never ask the user for a password or a key in chat." in text


def test_the_handshake_says_a_layer_has_to_be_built():
    text = mcp_server._INSTRUCTIONS
    assert "BUILT" in text
    assert "status" in text


def test_the_handshake_names_the_postgres_family():
    text = mcp_server._INSTRUCTIONS.lower()
    assert "redshift" in text
    assert "supabase" in text
    assert "postgres connection" in text


# Slice 1 pinned the opposite of this: it asserted that the handshake did
# NOT say "how to start inference", because that build could not. It can
# now, and `test_the_handshake_now_names_inference_and_the_answer_order`
# below is the replacement -- kept as a note because the deleted assertion
# is the clearest record of what changed between the two slices.


# --- a warehouse the user named that was not in the list -----------------

def test_discover_connections_routes_a_warehouse_we_cannot_open():
    srv = mcp_server.build_server(Session())
    out = call(srv, "discover_connections", {"requested": "databricks"})
    assert out["supported"] is False
    assert out["engine"] == "databricks"
    assert "dbt" in out["route"]


def test_discover_connections_corrects_redshift_rather_than_refusing_it():
    srv = mcp_server.build_server(Session())
    out = call(srv, "discover_connections", {"requested": "Amazon Redshift"})
    assert out["supported"] is True
    assert out["kind"] == "postgres"


def test_discover_connections_with_no_argument_still_lists(_nowhere):
    (_nowhere / "cwd" / "warehouse.duckdb").write_text("")
    srv = mcp_server.build_server(Session())
    out = call(srv, "discover_connections")
    assert [c["uri"] for c in out] == ["duckdb:warehouse.duckdb"]


def test_the_status_payload_carries_the_supported_vocabulary():
    srv = mcp_server.build_server(Session())
    out = call(srv, "status")
    postgres = next(k for k in out["supported"] if k["kind"] == "postgres")
    assert "redshift" in postgres["aliases"]
    assert out["unsupported"]
    assert out["env_file"]["path"].endswith(".env")


# --- loading one of the layers already here ------------------------------

def test_load_layer_loads_and_answers_with_the_new_state(_nowhere, write_layer):
    write_layer(_nowhere / "cwd" / "layer.yaml", name="yesterday")
    srv = mcp_server.build_server(Session())
    assert call(srv, "status")["state"] == "no_layer"
    out = call(srv, "load_layer", {"ref": "layer.yaml"})
    assert out["state"] == "ready"
    assert out["layer"]["name"] == "yesterday"
    # and the server keeps it: the next tool call queries the document
    assert call(srv, "get_tables")[0]["name"] == "t0"


def test_load_layer_on_a_missing_file_answers_instead_of_erroring(_nowhere):
    """The rule is that an agent never gets a bare error. The SDK turns a
    raised exception into a protocol error with nowhere to go, so this tool
    answers with the status object and an `error` field on it."""
    srv = mcp_server.build_server(Session())
    out = call(srv, "load_layer", {"ref": "nope.yaml"})
    assert out["state"] == "no_layer"
    assert "nope.yaml" in out["error"]
    assert out["next"]


# --- day 2 through the command --------------------------------------------

def test_no_argument_loads_the_one_layer_already_here(served, _nowhere, write_layer):
    """What day 2 is: a new chat, possibly a new host, and the person who
    inferred yesterday says nothing today."""
    write_layer(_nowhere / "cwd" / "layer.yaml", name="yesterday")
    from semlayer.cli import main
    assert main(["mcp"]) == 0
    assert served[0].state == "ready"
    assert served[0].autoloaded is True
    assert served[0].connection == "duckdb:/tmp/t.duckdb"


def test_no_argument_with_two_layers_starts_empty(served, _nowhere, write_layer,
                                                  cache_layer):
    """Two is a question, and the server asks it in `status` rather than
    picking one on the user's behalf."""
    write_layer(_nowhere / "cwd" / "layer.yaml", warehouse="duckdb:/tmp/a.duckdb")
    cache_layer(_nowhere / "home", warehouse="duckdb:/tmp/b.duckdb")
    from semlayer.cli import main
    assert main(["mcp"]) == 0
    assert served[0].state == "no_layer"
    assert served[0].autoloaded is False


def test_a_named_path_is_not_an_autoload(served, _nowhere, write_layer):
    path = write_layer(_nowhere / "cwd" / "layer.yaml")
    from semlayer.cli import main
    assert main(["mcp", str(path)]) == 0
    assert served[0].state == "ready"
    assert served[0].autoloaded is False


# --- the front door: build a layer, then answer with it ------------------

def test_the_four_front_door_tools_are_registered():
    srv = mcp_server.build_server(Session())
    names = {t.name for t in anyio.run(srv.list_tools)}
    assert {"infer", "cancel_infer", "context_for_question", "run_sql"} <= names


def test_infer_and_then_answer_in_one_session(_nowhere, built_fixture):
    """The journey, through the real tool surface: nothing, then a layer,
    then an answer, without a second server and without a terminal."""
    import shutil
    shutil.copy(built_fixture("messy_mart"), _nowhere / "cwd" / "warehouse.duckdb")
    srv = mcp_server.build_server(Session())

    assert call(srv, "status")["state"] == "no_layer"

    built = call(srv, "infer", {"connection": "duckdb:warehouse.duckdb",
                                "question": "how many customers per segment?",
                                "wait_seconds": 60, "use_llm": False})
    assert built["state"] == "ready", built.get("error")
    assert built["pending_question"] == "how many customers per segment?"

    ctx = call(srv, "context_for_question",
               {"question": "how many customers per segment?"})
    assert "cust_mstr" in ctx["tables"]
    assert ctx["context"]
    # asking for the context IS answering it, so the reminder goes
    assert call(srv, "status")["pending_question"] is None

    ran = call(srv, "run_sql", {"sql": "select seg_cd, count(*) as n "
                                       "from cust_mstr group by 1 order by 2 desc"})
    assert ran["ran"] is True
    assert ran["columns"] == ["seg_cd", "n"]
    assert len(ran["rows"]) == 3
    assert "assumptions" in ran


def test_run_sql_refuses_to_write_through_the_tool(_nowhere, built_fixture):
    """The guard is in the module and the tool must not route around it."""
    import shutil
    shutil.copy(built_fixture("messy_mart"), _nowhere / "cwd" / "warehouse.duckdb")
    srv = mcp_server.build_server(Session())
    call(srv, "infer", {"connection": "duckdb:warehouse.duckdb",
                        "wait_seconds": 60, "use_llm": False})
    out = call(srv, "run_sql", {"sql": "drop table cust_mstr"})
    assert out["ran"] is False
    assert "DROP" in out["refused"]
    # still there
    assert call(srv, "run_sql",
                {"sql": "select count(*) as n from cust_mstr"})["ran"] is True


def test_the_answer_tools_fall_back_to_status_with_no_layer():
    """Same contract as every other tool: never a bare error."""
    srv = mcp_server.build_server(Session())
    for tool, args in (("context_for_question", {"question": "revenue?"}),
                       ("run_sql", {"sql": "select 1"})):
        out = call(srv, tool, args)
        assert out["state"] == "no_layer", tool
        assert "built" in out["next"], tool


def test_run_sql_without_a_reachable_connection_says_what_is_wrong(gold):
    """A layer loaded from a FILE names a warehouse this machine may not be
    able to open, and the agent has to be able to tell the user that."""
    srv = mcp_server.build_server(Session.of(gold))
    out = call(srv, "run_sql", {"sql": "select 1"})
    assert out["ran"] is False
    assert "connection" in out["next"]


def test_cancelling_when_nothing_runs_is_an_answer_not_an_error():
    srv = mcp_server.build_server(Session())
    assert call(srv, "cancel_infer")["error"] == "no inference is running"


def test_the_handshake_now_names_inference_and_the_answer_order():
    """Slice 1 could not promise inference and said so. It can now, and the
    reviewed wording is restored in full."""
    text = mcp_server._INSTRUCTIONS
    assert "how to start inference" in text
    assert "context_for_question" in text
    assert "run_sql" in text
    assert "ASK THEM FIRST" in text
    assert "Never ask the user for a password or a key in chat." in text
