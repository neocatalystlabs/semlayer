"""Building a layer from inside a chat: the wait, the job, and the secret.

Three things here are promises rather than features.

The WAIT: a small warehouse is discovered, inferred and answered in one
turn, because the tool call holds open long enough for a DuckDB file to
finish. A big one cannot, so the call returns and `status` carries the news
-- MCP has no way to wake a model, and a test pins that nothing pretends to.

The JOB: a document is swapped by replacing a reference, so a tool call in
flight never reads a half-built layer.

The SECRET: a credential pasted into a chat is accepted for the session and
goes nowhere else. The tests for that are written as attempts to find it in
the status payload, the cache key, the layer and the telemetry event.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from semlayer import session as sess

# Four tables rather than thirty-six: these tests run REAL inferences, and
# most of them are about the job rather than the layer. `messy_mart` is used
# where the size is the point. Both come through conftest's `built_fixture`,
# which skips rather than errors when `fixtures/build/` is empty.
SMALL = "fan_trap"
BIG = "messy_mart"


@pytest.fixture(autouse=True)
def _nowhere(nowhere, monkeypatch):
    """An empty machine, and no API key: these runs must cost nothing."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return nowhere


@pytest.fixture(autouse=True)
def _no_stragglers():
    """Join every inference thread before the next test starts.

    Not tidiness. A job deliberately left running by one test records its
    telemetry event whenever it finishes, which lands in whatever the NEXT
    test has monkeypatched `telemetry.record` to -- and that test then sees
    two events and fails. It did: the via=mcp test passed alone and failed
    in the file, which is the same shape of cross-test leak as the `.env`
    one in test_session.py.
    """
    import threading
    yield
    for t in threading.enumerate():
        if t.name == "semlayer-infer":
            t.join(timeout=60)


def _place(where: Path, fixture: Path) -> str:
    import shutil
    shutil.copy(fixture, where / "cwd" / "warehouse.duckdb")
    return "duckdb:warehouse.duckdb"


@pytest.fixture
def warehouse(_nowhere, built_fixture):
    """A real, small DuckDB warehouse in the session's working directory."""
    return _place(_nowhere, built_fixture(SMALL))


@pytest.fixture
def big_warehouse(_nowhere, built_fixture):
    """The 36-table fixture, for the tests where the count is the claim."""
    return _place(_nowhere, built_fixture(BIG))


# --- the wait -------------------------------------------------------------

def test_a_small_warehouse_finishes_inside_the_call(big_warehouse):
    """The case worth optimising: one turn, no polling."""
    s = sess.Session()
    out = s.start_infer(big_warehouse, question="how many customers?",
                        wait_seconds=60, use_llm=False)
    assert out["state"] == "ready", out.get("error")
    assert out["job"]["state"] == "done"
    assert out["layer"]["tables"] == 36
    assert s.doc is not None


def test_the_layer_is_cached_where_the_next_session_will_find_it(big_warehouse):
    s = sess.Session()
    s.start_infer(big_warehouse, wait_seconds=60, use_llm=False)
    cached = sess.layer_cache_path(big_warehouse)
    assert cached.is_file()
    assert sess.scan_layer(cached)["tables"] == 36
    # and a brand-new session finds it with no argument at all
    again = sess.Session()
    assert again.autoload() is True
    assert again.state == "ready"


def test_the_question_is_remembered_and_then_surfaced(warehouse):
    """The run outlives the turn that asked for it, so the server reminds
    the agent what it was for rather than hoping the agent remembers."""
    s = sess.Session()
    out = s.start_infer(warehouse, question="revenue by segment?",
                        wait_seconds=60, use_llm=False)
    assert out["pending_question"] == "revenue by segment?"
    assert "revenue by segment?" in out["next"]
    assert "context_for_question" in out["next"]


def test_a_run_that_does_not_finish_returns_inferring(warehouse, monkeypatch):
    """No pretending: the call comes back, the job keeps going, and `next`
    says that nothing will wake the agent when it ends."""
    real = sess.Session._infer_once

    def slow(self, job, use_llm):
        time.sleep(1.5)
        return real(self, job, use_llm)

    monkeypatch.setattr(sess.Session, "_infer_once", slow)
    s = sess.Session()
    out = s.start_infer(warehouse, wait_seconds=0.2, use_llm=False)
    assert out["state"] == "inferring"
    assert out["job"]["state"] == "running"
    assert "NOTHING WILL WAKE YOU" in out["next"]
    assert s.job is not None and s.job.thread is not None
    s.job.thread.join(timeout=60)
    assert s.state == "ready"


def test_the_wait_is_capped(warehouse, monkeypatch):
    """A tool call may not be held open indefinitely by its own argument."""
    waits = []
    real_join = sess.threading.Thread.join

    def spy(self, timeout=None):
        waits.append(timeout)
        return real_join(self, timeout=0.01)

    monkeypatch.setattr(sess.threading.Thread, "join", spy)
    sess.Session().start_infer(warehouse, wait_seconds=9999, use_llm=False)
    assert waits and waits[0] == sess.INFER_WAIT_CAP_S


def test_status_does_not_block_while_a_run_is_going(warehouse, monkeypatch):
    def slow(self, job, use_llm):
        time.sleep(2)
        return {"semantic_layer": {"name": "x", "tables": []}}

    monkeypatch.setattr(sess.Session, "_infer_once", slow)
    s = sess.Session()
    s.start_infer(warehouse, wait_seconds=0.1, use_llm=False)
    t0 = time.time()
    out = s.status()
    assert time.time() - t0 < 1.0
    assert out["state"] == "inferring"


# --- the job --------------------------------------------------------------

def test_a_second_run_is_refused_while_one_is_going(warehouse, monkeypatch):
    def slow(self, job, use_llm):
        time.sleep(1.5)
        return {"semantic_layer": {"name": "x", "tables": []}}

    monkeypatch.setattr(sess.Session, "_infer_once", slow)
    s = sess.Session()
    s.start_infer(warehouse, wait_seconds=0.1, use_llm=False)
    out = s.start_infer(warehouse, wait_seconds=0.1, use_llm=False)
    assert "already running" in out["error"]


def test_cancelling_stops_the_run_at_a_stage_boundary(warehouse):
    s = sess.Session()
    s.start_infer(warehouse, wait_seconds=0.01, use_llm=False)
    out = s.cancel_infer()
    assert "cancelling" in out or out.get("error")
    s.job.thread.join(timeout=60)
    assert s.job.state in ("cancelled", "done")


def test_cancelling_nothing_says_so(warehouse):
    assert sess.Session().cancel_infer()["error"] == "no inference is running"


def test_a_failed_run_is_reported_not_raised(_nowhere):
    s = sess.Session()
    out = s.start_infer("duckdb:no-such-file.duckdb", wait_seconds=60,
                        use_llm=False)
    assert out["state"] == "no_layer"
    assert out["job"]["state"] == "failed"
    assert out["error"]


def test_the_stages_are_reported_as_they_finish(warehouse):
    s = sess.Session()
    out = s.start_infer(warehouse, wait_seconds=60, use_llm=False)
    assert set(out["job"]["stages_done"]) >= {"profile", "link", "enrich"}


def test_no_key_means_no_llm_and_the_job_says_so(warehouse):
    s = sess.Session()
    out = s.start_infer(warehouse, wait_seconds=60)      # use_llm defaults true
    assert out["job"]["model"] == "none/heuristic"
    assert out["state"] == "ready"


# --- the secret -----------------------------------------------------------

DSN = "postgresql://reader:hunter2@db.internal:5432/analytics"


def test_a_pasted_password_is_not_in_the_status_payload(_nowhere):
    s = sess.Session()
    s.select(DSN)
    blob = json.dumps(s.status(), default=str)
    assert "hunter2" not in blob
    assert "reader:***@db.internal" in blob


def test_a_pasted_password_is_not_in_the_cache_key(_nowhere):
    assert "hunter2" not in sess.cache_key(DSN)
    assert sess.cache_key(DSN) == sess.cache_key(
        "postgresql://reader@db.internal:5432/analytics")


def test_the_full_string_is_still_what_gets_opened(_nowhere):
    s = sess.Session()
    s.select(DSN)
    assert s.target() == DSN, "the secret has to reach the driver"
    assert s.connection != DSN, "and nothing else"


@pytest.mark.parametrize(("raw", "shown"), [
    ("postgresql://u:p@h:5432/db", "postgresql://u:***@h:5432/db"),
    ("snowflake://u:p@acct/db?role=R", "snowflake://u:***@acct/db?role=R"),
    ("postgresql://h/db?password=p", "postgresql://h/db?password=***"),
    ("postgresql://u@h/db", "postgresql://u@h/db"),
    ("duckdb:warehouse.duckdb", "duckdb:warehouse.duckdb"),
])
def test_redaction_keeps_everything_but_the_secret(raw, shown):
    """A redaction the user cannot recognise as their own connection is one
    they will paste again."""
    assert sess.redact(raw) == shown


def test_the_reply_to_a_pasted_secret_says_three_things(_nowhere):
    reply = sess.pasted_credential_reply(DSN)
    assert "accepted" in reply["credential"]
    assert "restart" in reply["gone_when"]
    assert reply["durable_instead"] == "postgresql://reader:***@db.internal:5432/analytics"
    assert ".env" in reply["next"]
    assert "hunter2" not in json.dumps(reply)


def test_a_pasted_secret_is_not_in_the_job_record(_nowhere):
    s = sess.Session()
    out = s.start_infer(DSN, wait_seconds=0.1, use_llm=False)
    assert "hunter2" not in json.dumps(out, default=str)
    if s.job is not None and s.job.thread is not None:
        s.job.thread.join(timeout=30)
    assert "hunter2" not in json.dumps(s.status(), default=str)


def test_a_pasted_secret_is_not_in_the_telemetry_event(_nowhere, monkeypatch):
    rows = []
    monkeypatch.setattr("semlayer.telemetry.record",
                        lambda event, **props: rows.append({"event": event, **props}))
    s = sess.Session()
    s.start_infer(DSN, wait_seconds=30, use_llm=False)
    if s.job is not None and s.job.thread is not None:
        s.job.thread.join(timeout=30)
    assert rows, "the run has to be recorded"
    blob = json.dumps(rows, default=str)
    assert "hunter2" not in blob
    assert "db.internal" not in blob
    assert "analytics" not in blob


# --- telemetry ------------------------------------------------------------

def test_an_agent_started_run_is_recorded_as_via_mcp(big_warehouse, monkeypatch):
    """The measurement the whole phase exists for: an inference that happened
    without anyone opening a terminal."""
    rows = []
    monkeypatch.setattr("semlayer.telemetry.record",
                        lambda event, **props: rows.append({"event": event, **props}))
    s = sess.Session()
    s.start_infer(big_warehouse, wait_seconds=60, use_llm=False)
    assert len(rows) == 1
    row = rows[0]
    assert row["event"] == "cli", "same event as a CLI infer, so they compare"
    assert row["command"] == "infer"
    assert row["via"] == "mcp"
    assert row["connector"] == "duckdb"
    assert row["outcome"] == "ok"
    assert row["tables"] == 36
    assert row["llm"] == "none"
    assert set(row["stages"]) >= {"profile", "link", "enrich"}


def test_the_recorded_event_survives_the_whitelist(big_warehouse, monkeypatch):
    """Recorded fields are useless if the serializer drops them."""
    from semlayer import telemetry_schema as ts
    rows = []
    monkeypatch.setattr("semlayer.telemetry.record",
                        lambda event, **props: rows.append(props))
    sess.Session().start_infer(big_warehouse, wait_seconds=60, use_llm=False)
    out = ts.serialize(rows[0])
    assert out["via"] == "mcp"
    assert out["command"] == "infer"
    assert out["tables"] == "21_50"      # bucketed, never the exact count


def test_a_failed_run_records_its_class_not_its_message(_nowhere, monkeypatch):
    rows = []
    monkeypatch.setattr("semlayer.telemetry.record",
                        lambda event, **props: rows.append(props))
    s = sess.Session()
    s.start_infer("duckdb:/nope/missing.duckdb", wait_seconds=60, use_llm=False)
    assert rows[0]["outcome"] == "error"
    assert rows[0]["error_class"]
    assert "missing.duckdb" not in json.dumps(rows[0])


# --- the credential survives being handed back ---------------------------

def test_the_redacted_form_coming_back_does_not_wipe_the_credential(_nowhere):
    """The flow that broke it: `status` shows the agent the REDACTED
    connection, so that is the string the agent passes to the next call. The
    first version treated "no secret in this string" as "forget the secret",
    and the driver was then handed a literal `***`.
    """
    s = sess.Session()
    s.select(DSN)
    assert s.target() == DSN
    s.start_infer(s.connection, wait_seconds=0.1, use_llm=False)
    assert s.target() == DSN, "the held credential was dropped"
    assert "***" not in (s.target() or "")


def test_a_new_secret_for_the_same_warehouse_replaces_the_old_one(_nowhere):
    """A rotated password, or the user pasting it again. Only ever replace a
    credential with another credential."""
    s = sess.Session()
    s.select(DSN)
    rotated = DSN.replace("hunter2", "correcthorse")
    s.start_infer(rotated, wait_seconds=0.1, use_llm=False)
    assert s.target() == rotated
    assert "hunter2" not in json.dumps(s.status(), default=str)


def test_switching_warehouse_does_drop_the_old_credential(_nowhere):
    s = sess.Session()
    s.select(DSN)
    s.select("duckdb:other.duckdb")
    assert s.target() == "duckdb:other.duckdb"
    assert s._credential is None


# --- one connection, one user at a time ----------------------------------

def test_run_sql_is_refused_while_an_inference_is_running(warehouse, monkeypatch):
    """DuckDB forbids two threads on one connection object -- its own
    docstring says so -- and on every engine the profiling queries and the
    agent's query would interleave on one handle. So the query is refused
    rather than the connection shared."""
    import time as _t

    real = sess.Session._infer_once

    def slow(self, job, use_llm):
        _t.sleep(2)
        return real(self, job, use_llm)

    monkeypatch.setattr(sess.Session, "_infer_once", slow)
    s = sess.Session()
    # a layer from before, so `run_sql` would otherwise go ahead
    s.start_infer(warehouse, wait_seconds=60, use_llm=False)
    doc = s.doc
    s.start_infer(warehouse, wait_seconds=0.2, use_llm=False)
    assert s.state == "inferring"

    out = s.run_query(doc, "select 1 as one")
    assert out["ran"] is False
    assert "busy" in out["error"]
    assert "run_sql again" in out["next"]
    assert "rows" not in out

    s.job.thread.join(timeout=60)
    after = s.run_query(doc, "select 1 as one")
    assert after["ran"] is True, "and allowed again once the run is done"


def test_a_query_does_not_block_the_turn_it_was_asked_in(warehouse, monkeypatch):
    """Refused, not queued: a query is seconds and the agent can ask again,
    while a run is minutes. Waiting would hold the tool call open for the
    length of an inference."""
    import time as _t

    def slow(self, job, use_llm):
        _t.sleep(2)
        return {"semantic_layer": {"name": "x", "tables": []}}

    monkeypatch.setattr(sess.Session, "_infer_once", slow)
    s = sess.Session()
    s.start_infer(warehouse, wait_seconds=0.1, use_llm=False)
    t0 = _t.time()
    s.run_query({"semantic_layer": {"name": "x", "tables": []}}, "select 1")
    assert _t.time() - t0 < 1.0


def test_an_ordinary_query_releases_the_connection(warehouse):
    """The lock must not leak: two queries in a row have to both work."""
    s = sess.Session()
    s.start_infer(warehouse, wait_seconds=60, use_llm=False)
    for _ in range(3):
        assert s.run_query(s.doc, "select 1 as one")["ran"] is True


def test_a_refused_query_still_releases_the_connection(warehouse):
    s = sess.Session()
    s.start_infer(warehouse, wait_seconds=60, use_llm=False)
    assert s.run_query(s.doc, "delete from customers")["ran"] is False
    assert s.run_query(s.doc, "select 1 as one")["ran"] is True


def test_a_run_waiting_for_the_connection_says_so(warehouse):
    """A query may now hold the connection for as long as its own time
    limit, so a run queued behind one must not report itself as `starting`
    for ten minutes."""
    s = sess.Session()
    s.select(warehouse)
    s._in_use.acquire()                      # as a long query would
    try:
        out = s.start_infer(warehouse, wait_seconds=0.3, use_llm=False)
        assert out["state"] == "inferring"
        assert out["job"]["stage"] == "waiting for the warehouse connection"
    finally:
        s._in_use.release()
    s.job.thread.join(timeout=60)
    assert s.job.state == "done", "and it proceeds once the query is done"


# --- what a keyless layer says about itself ------------------------------

def test_a_statistics_only_layer_says_so(warehouse):
    """The behaviour shipped in #85 -- no key, statistics only, still
    `ready`. What was missing is the layer admitting it."""
    s = sess.Session()
    out = s.start_infer(warehouse, wait_seconds=60, use_llm=False)
    assert out["layer"]["built_without_model"] is True
    assert out["layer"]["model"] == "none/heuristic"


def test_a_layer_we_did_not_write_claims_nothing(_nowhere):
    """A hand-authored gold has no `generated_by`, and the messy_mart gold
    has no table descriptions either -- so counting descriptions would
    label somebody's hand-written layer statistics-only. Unknown is a
    third state, not a false."""
    from pathlib import Path
    gold = Path(__file__).resolve().parents[1] / "fixtures" / "golds" / "messy_mart.yaml"
    s = sess.Session()
    s.load(gold)
    assert s.status()["layer"]["built_without_model"] is None


@pytest.mark.parametrize(("doc", "answer"), [
    ({"generated_by": {"engine": "0.6.0b1", "model": "none/heuristic"}}, True),
    ({"generated_by": {"model": "none"}}, True),
    ({"generated_by": {"engine": "0.6.0b1", "model": "haiku-4-5/escalation"}}, False),
    # Not a document this engine wrote: ours always records both halves of
    # `generated_by`, so a missing or empty model is a producer who told us
    # nothing -- not a producer who told us "no model".
    ({"generated_by": {"engine": "someone-else"}}, None),
    ({"generated_by": {"model": None}}, None),
    ({"generated_by": {"model": "   "}}, None),
    ({"name": "hand-written"}, None),
])
def test_the_claim_is_only_made_where_it_is_recorded(doc, answer):
    assert sess.built_without_model({"semantic_layer": doc}) is answer


def test_a_real_resolved_hub_claims_nothing(tmp_path):
    """Through `load_document`, which is where the bug actually lived: it
    resolves a hub into ONE document carrying the HUB's `generated_by`, so
    a set used to report its members as description-free while one of them
    had been written by a model."""
    def member(name, model, description):
        doc = {"semantic_layer": {
            "spec_version": "0.6.0", "name": name,
            "warehouse": "duckdb:w.duckdb",
            "generated_by": {"engine": "0.6.0b1", "model": model},
            "tables": [{"name": f"{name}_t", "source": f"main.{name}_t",
                        "description": description,
                        "columns": [{"name": "id", "sql_type": "INTEGER"}]}]}}
        sess.save_document(tmp_path / f"{name}.yaml", doc)
        return {"name": name, "path": f"{name}.yaml"}

    members = [member("sales", "haiku-4-5/escalation", "written by a model"),
               member("ops", "none/heuristic", "")]
    sess.save_document(tmp_path / "hub.yaml", {"semantic_layer": {
        "spec_version": "0.6.0", "name": "hub",
        "generated_by": {"engine": "0.6.0b1", "model": "none/heuristic"},
        "members": members}})

    s = sess.Session()
    s.load(tmp_path / "hub.yaml")
    layer = s.status()["layer"]
    assert layer["tables"] == 2, "the hub resolved to its members' tables"
    assert layer["model"] == "none/heuristic", "the hub's own line is unchanged"
    assert layer["built_without_model"] is None


def test_a_set_claims_nothing_about_its_members():
    """A stitched hub records `none/heuristic` because assembling a hub needs
    no model -- which says nothing about the member documents merged into it.
    Those may be full of model-written descriptions, and announcing "no
    descriptions" over a resolved set would be flatly wrong."""
    hub = {"semantic_layer": {
        "members": [{"name": "sales", "path": "sales.yaml"}],
        "generated_by": {"engine": "0.6.0b1", "model": "none/heuristic"},
    }}
    assert sess.built_without_model(hub) is None


def test_the_key_sentence_comes_after_the_first_answer_not_before(warehouse):
    """A stranger has no key, so day 1 has to work keyless: the suggestion
    may never read as a precondition."""
    s = sess.Session()
    out = s.start_infer(warehouse, wait_seconds=60, use_llm=False)
    assert "model key" not in out["next"]
    assert s.answered_once is False

    ran = s.run_query(s.doc, "select 1 as one")
    assert ran["ran"] is True
    assert s.answered_once is True
    assert "statistics only" in ran["next"]
    assert ".env" in ran["next"]
    assert "works without one" in ran["next"]


def test_the_key_sentence_is_said_exactly_once(warehouse):
    s = sess.Session()
    s.start_infer(warehouse, wait_seconds=60, use_llm=False)
    said = [("statistics only" in s.run_query(s.doc, "select 1 as one")["next"])
            for _ in range(3)]
    assert said == [True, False, False], said


def test_a_refused_query_is_not_an_answer(warehouse):
    """The sentence rides the first ANSWER. A rejected query is not one, and
    spending the single mention on it would waste it."""
    s = sess.Session()
    s.start_infer(warehouse, wait_seconds=60, use_llm=False)
    refused = s.run_query(s.doc, "delete from customers")
    assert refused["ran"] is False
    assert s.answered_once is False
    assert "statistics only" not in (refused.get("next") or "")


def test_nothing_is_said_when_a_model_wrote_the_layer(warehouse):
    s = sess.Session()
    s.start_infer(warehouse, wait_seconds=60, use_llm=False)
    s.doc["semantic_layer"]["generated_by"]["model"] = "claude-haiku-4-5"
    out = s.run_query(s.doc, "select 1 as one")
    assert "statistics only" not in out["next"]


def test_with_a_key_already_set_it_says_to_infer_again(warehouse, monkeypatch):
    """Telling someone to add a key they already have is wrong; the layer
    predates the key, and re-inferring is the actual fix."""
    s = sess.Session()
    s.start_infer(warehouse, wait_seconds=60, use_llm=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-a-real-key")
    out = s.run_query(s.doc, "select 1 as one")
    assert "infer` again" in out["next"]
    assert ".env" not in out["next"].split("Say once")[-1]
