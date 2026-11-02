"""Day 2: the layer built yesterday is found, and the agent says which one.

The first build of this module cached a layer on disk and then started
empty, which is the same wall as day 1 had: the work was done and the
server did not know. These tests are about the three answers that fixes --
what is already here, load exactly one of them without being asked, and ask
when there is a choice -- plus the vocabulary a stranger's agent needs
(`supported`, `route`) and the swap the infer job will need next.
"""

from __future__ import annotations

import json
import threading

import pytest

from semlayer import session as sess


@pytest.fixture(autouse=True)
def _nowhere(nowhere):
    """Every test here runs on an empty machine: see conftest's `nowhere`."""
    return nowhere


# --- identifying a document without parsing it ----------------------------

def test_scan_reads_identity_and_counts_tables(tmp_path, write_layer):
    info = sess.scan_layer(write_layer(tmp_path / "layer.yaml", name="mart", tables=7))
    assert info["name"] == "mart"
    assert info["warehouse"] == "duckdb:/tmp/t.duckdb"
    assert info["tables"] == 7
    assert info["engine"] == "0.6.0b1"


def test_the_scanned_count_agrees_with_the_parsed_one():
    """The count is read by indentation rather than by parsing, which is the
    only reason `status` can list twenty candidates and still answer
    instantly. It has to agree with the truth on a real document."""
    from pathlib import Path
    gold = Path(__file__).resolve().parents[1] / "fixtures" / "golds" / "messy_mart.yaml"
    parsed = len(sess.load_document(gold)["semantic_layer"]["tables"])
    assert sess.scan_layer(gold)["tables"] == parsed


def test_a_file_that_is_not_a_layer_is_not_a_candidate(tmp_path):
    (tmp_path / "ci.yaml").write_text("jobs:\n  build:\n    runs-on: ubuntu\n")
    assert sess.scan_layer(tmp_path / "ci.yaml") is None


def test_a_document_with_no_warehouse_is_still_listed(tmp_path):
    """Hand-authored golds have no `warehouse` key at all. A layer we cannot
    attribute to a connection is still a layer the user can load."""
    path = tmp_path / "layer.yaml"
    path.write_text("semantic_layer:\n  name: gold\n  tables:\n  - name: t\n")
    info = sess.scan_layer(path)
    assert info["warehouse"] is None
    assert info["tables"] == 1


def test_an_undated_document_falls_back_to_its_file_time(tmp_path, write_layer):
    info = sess.scan_layer(write_layer(tmp_path / "layer.yaml"))
    assert info["inferred_at"].startswith("20")


# --- what is already on this machine --------------------------------------

def test_a_layer_in_the_project_is_found(_nowhere, write_layer):
    write_layer(_nowhere / "cwd" / "layer.yaml")
    found = sess.visible_layers()
    assert [x["source"] for x in found] == ["repo"]
    assert found[0]["connection"] == "duckdb:/tmp/t.duckdb"


def test_a_layer_in_the_cache_is_found(_nowhere, cache_layer):
    cache_layer(_nowhere / "home")
    found = sess.visible_layers()
    assert [x["source"] for x in found] == ["cache"]


def test_the_repo_copy_wins_over_the_cache_copy(_nowhere, write_layer, cache_layer):
    """The repo is where a team's review decisions live, so it is the copy of
    record; the cache is the cold-start fallback."""
    write_layer(_nowhere / "cwd" / "layer.yaml", name="in-repo")
    cache_layer(_nowhere / "home", name="in-cache")
    found = sess.visible_layers()
    assert [x["name"] for x in found] == ["in-repo"]


def test_two_warehouses_are_both_listed(_nowhere, write_layer, cache_layer):
    write_layer(_nowhere / "cwd" / "layer.yaml", name="one", warehouse="duckdb:/tmp/a.duckdb")
    cache_layer(_nowhere / "home", name="two", warehouse="duckdb:/tmp/b.duckdb")
    assert {x["name"] for x in sess.visible_layers()} == {"one", "two"}


def test_a_layer_at_the_repo_root_is_found_from_a_subdirectory(_nowhere, write_layer):
    """Claude Code and Cursor start the server in the project, which is often
    not the directory the layer file sits in."""
    root = _nowhere / "cwd"
    (root / ".git").mkdir()
    write_layer(root / "layer.yaml")
    sub = root / "models" / "marts"
    sub.mkdir(parents=True)
    import os
    os.chdir(sub)
    assert len(sess.visible_layers()) == 1


def test_the_candidate_list_is_capped(_nowhere, write_layer):
    """A directory full of YAML must not make `status` slow. The known names
    are looked at first, so the cap can never cost us `layer.yaml`."""
    cwd = _nowhere / "cwd"
    for i in range(sess._MAX_LAYER_CANDIDATES + 10):
        write_layer(cwd / f"z{i:03d}.yaml", warehouse=f"duckdb:/tmp/{i}.duckdb")
    write_layer(cwd / "layer.yaml", name="the-real-one")
    found = sess.visible_layers()
    assert len(found) <= sess._MAX_LAYER_CANDIDATES
    assert "the-real-one" in {x["name"] for x in found}


def test_the_one_used_last_is_marked(_nowhere, write_layer, cache_layer):
    write_layer(_nowhere / "cwd" / "layer.yaml", warehouse="duckdb:/tmp/a.duckdb")
    cache_layer(_nowhere / "home", warehouse="duckdb:/tmp/b.duckdb")
    s = sess.Session()
    s.load_layer("duckdb:/tmp/b.duckdb")
    marked = [x for x in sess.visible_layers() if x.get("last_used")]
    assert len(marked) == 1
    assert marked[0]["connection"] == "duckdb:/tmp/b.duckdb"


# --- loading one without being asked, or asking --------------------------

def test_exactly_one_layer_is_loaded_on_start(_nowhere, write_layer):
    write_layer(_nowhere / "cwd" / "layer.yaml", name="yesterday")
    s = sess.Session()
    assert s.autoload() is True
    assert s.state == "ready"
    assert s.autoloaded is True
    assert s.connection == "duckdb:/tmp/t.duckdb"


def test_the_agent_is_told_to_name_the_warehouse_it_found(_nowhere, write_layer):
    write_layer(_nowhere / "cwd" / "layer.yaml")
    s = sess.Session()
    s.autoload()
    nxt = s.status()["next"]
    assert "duckdb:/tmp/t.duckdb" in nxt
    # A layer we chose on their behalf is a layer they have not agreed to.
    assert "before you answer" in nxt
    assert "wrong one" in nxt


def test_two_layers_are_a_question_not_a_default(_nowhere, write_layer, cache_layer):
    write_layer(_nowhere / "cwd" / "layer.yaml", warehouse="duckdb:/tmp/a.duckdb")
    cache_layer(_nowhere / "home", warehouse="duckdb:/tmp/b.duckdb")
    s = sess.Session()
    assert s.autoload() is False
    assert s.state == "no_layer"
    nxt = s.status()["next"]
    assert "load_layer" in nxt
    assert "which one they mean" in nxt
    # Named in words, not by ref: a cached layer's ref is a path with a hash
    # in it, which is right for the tool call and wrong to read to a user.
    assert "duckdb:/tmp/a.duckdb" in nxt
    assert "cached" in nxt


def test_a_named_connection_is_not_overridden_by_a_found_layer(_nowhere, write_layer):
    """Someone who passed `--connection` has said what the session is about."""
    write_layer(_nowhere / "cwd" / "layer.yaml")
    s = sess.Session("postgres")
    assert s.autoload() is False
    assert s.state == "no_layer"


def test_an_unreadable_found_layer_does_not_stop_the_server(_nowhere):
    (_nowhere / "cwd" / "layer.yaml").write_text("semantic_layer:\n  tables: [\n")
    s = sess.Session()
    assert s.autoload() is False
    assert s.status()["state"] == "no_layer"


def test_load_layer_takes_a_ref_a_connection_or_a_path(_nowhere, write_layer):
    path = write_layer(_nowhere / "cwd" / "layer.yaml", name="pick-me")
    for ref in (str(path), "duckdb:/tmp/t.duckdb", "layer.yaml"):
        s = sess.Session()
        out = s.load_layer(ref)
        assert out["state"] == "ready", ref
        assert out["layer"]["name"] == "pick-me"
        assert out["autoloaded"] is False


def test_load_layer_sets_the_connection_it_loaded(_nowhere, write_layer):
    write_layer(_nowhere / "cwd" / "layer.yaml")
    s = sess.Session()
    assert s.load_layer("layer.yaml")["connection"] == "duckdb:/tmp/t.duckdb"


def test_load_layer_on_a_path_that_is_not_there_answers_with_status(_nowhere):
    """`load` raises, because the CLI's caller typed a path and wants it.
    `load_layer`'s caller is an agent repeating what a person said, and a
    raise reaches it as a bare protocol error with nowhere to go."""
    out = sess.Session().load_layer("no-such-layer.yaml")
    assert out["state"] == "no_layer"
    assert "no-such-layer.yaml" in out["error"]
    assert "layers" in out


def test_load_raises_where_load_layer_does_not(_nowhere):
    from semlayer.errors import SpecError
    with pytest.raises(SpecError):
        sess.Session().load("no-such-layer.yaml")


# --- the vocabulary a stranger's agent needs ------------------------------

def test_every_kind_says_how_to_address_it():
    kinds = {k["kind"]: k for k in sess.supported()}
    assert set(kinds) == {"duckdb", "postgres", "bigquery", "snowflake"}
    for kind in kinds.values():
        assert kind["uri_forms"]
        assert isinstance(kind["driver_installed"], bool)


def test_redshift_is_named_as_the_postgres_connection():
    """The spec pins this one: four vendors speak the Postgres protocol, and
    an agent that does not know asks the user to wait for a connector we
    already have."""
    postgres = next(k for k in sess.supported() if k["kind"] == "postgres")
    assert "redshift" in postgres["aliases"]
    assert "supabase" in postgres["aliases"]
    assert "neon" in postgres["aliases"]


def test_a_missing_driver_is_reported_with_the_fix(monkeypatch):
    monkeypatch.setattr(sess, "_driver_present", lambda module: False)
    for kind in sess.supported():
        assert kind["driver_installed"] is False
        assert "semlayer[warehouses]" in kind["note"]


@pytest.mark.parametrize(("said", "kind"), [
    ("redshift", "postgres"),
    ("Amazon Redshift", "postgres"),
    ("supabase", "postgres"),
    ("neon", "postgres"),
    ("bigquery", "bigquery"),
    ("motherduck", "duckdb"),
])
def test_a_warehouse_we_open_under_another_name(said, kind):
    out = sess.route_for(said)
    assert out["supported"] is True
    assert out["kind"] == kind
    assert out["uri_forms"]


@pytest.mark.parametrize(("said", "engine"), [
    ("databricks", "databricks"),
    ("our unity catalog tables", "databricks"),
    ("trino", "trino"),
    ("presto", "trino"),
    ("athena", "athena"),
    ("iceberg on s3", "iceberg"),
    ("sql server", "mssql"),
    ("clickhouse", "clickhouse"),
    ("oracle", "oracle"),
    ("spark", "spark"),
])
def test_a_warehouse_we_cannot_open_gets_a_route(said, engine):
    out = sess.route_for(said)
    assert out["supported"] is False
    assert out["engine"] == engine
    assert out["route"]
    assert "ask whether to proceed" in out["next"]


def test_iceberg_is_routed_through_duckdb_not_only_dbt():
    """The one route that is not "write a dbt project": DuckDB can attach an
    Iceberg REST catalog, so the data is reachable."""
    assert "DuckDB" in sess.route_for("iceberg")["route"]


def test_a_warehouse_nobody_has_heard_of_still_gets_an_answer():
    out = sess.route_for("Vertica")
    assert out["supported"] is False
    assert out["engine"] is None
    assert "dbt" in out["route"]


def test_what_the_user_said_is_capped_before_it_is_echoed():
    """It comes back to the agent, so it is bounded -- an agent that pastes a
    connection string in here must not get a connection string back."""
    out = sess.route_for("x" * 500)
    assert len(out["requested"]) <= 64


# --- the status payload ---------------------------------------------------

def test_the_payload_carries_the_credentials_file_and_its_keys(_nowhere):
    env = _nowhere / "home" / ".semlayer" / ".env"
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text("PG_READER_DSN=postgresql://reader:hunter2@h:5432/db\n")
    out = sess.Session().status()
    assert out["env_file"]["exists"] is True
    assert out["env_file"]["keys"] == ["PG_READER_DSN"]
    assert "hunter2" not in json.dumps(out)


def test_a_line_added_to_the_file_is_seen_without_a_restart(_nowhere):
    """The loop the whole design is for: the agent prescribes a line, the
    user adds it, the user says "done", the agent calls status again."""
    env = _nowhere / "home" / ".semlayer" / ".env"
    env.parent.mkdir(parents=True, exist_ok=True)
    s = sess.Session()
    assert [c["kind"] for c in s.status()["connections"]] == []
    env.write_text("PG_READER_DSN=postgresql://reader@h:5432/db\n")
    assert [c["kind"] for c in s.status()["connections"]] == ["postgres"]
    env.write_text("# changed my mind\n")
    assert [c["kind"] for c in s.status()["connections"]] == []


def test_the_pending_question_is_reserved_and_empty(_nowhere):
    """Reserved for the infer tool, so that slice fills a field in rather
    than changing the shape of this payload."""
    assert sess.Session().status()["pending_question"] is None


# --- the swap the infer job will need -------------------------------------

def test_a_tool_call_never_sees_a_half_loaded_layer(_nowhere, write_layer):
    """`load` builds the document and then replaces the reference, which is
    what lets the infer job swap a layer in under a live conversation. A
    reader must see the old document or the new one, never a mutation in
    progress."""
    small = write_layer(_nowhere / "cwd" / "small.yaml", tables=1)
    big = write_layer(_nowhere / "cwd" / "big.yaml", tables=9)
    s = sess.Session()
    s.load(small)
    counts, stop = [], threading.Event()

    def read():
        while not stop.is_set():
            doc = s.doc                      # one read, as `_answer` does
            counts.append(len(doc["semantic_layer"]["tables"]))

    reader = threading.Thread(target=read)
    reader.start()
    try:
        for _ in range(40):
            s.load(big)
            s.load(small)
    finally:
        stop.set()
        reader.join(timeout=5)
    assert set(counts) <= {1, 9}, sorted(set(counts))
    assert len(counts) > 1


# --- where a variable was seen -------------------------------------------

def test_a_connection_from_the_file_says_the_file(_nowhere):
    """Not cosmetic: a variable exported in a shell is invisible to a server
    an MCP client launched, so "it is already set" would be wrong in the one
    case that matters, and the fix differs."""
    env = _nowhere / "home" / ".semlayer" / ".env"
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text("PG_READER_DSN=postgresql://r@h:5432/db\n")
    row = next(c for c in sess.discover() if c["kind"] == "postgres")
    assert row["source"].endswith(".env: PG_READER_DSN")


def test_a_connection_from_the_shell_says_the_environment(_nowhere, monkeypatch):
    monkeypatch.setenv("PG_READER_DSN", "postgresql://r@h:5432/db")
    row = next(c for c in sess.discover() if c["kind"] == "postgres")
    assert row["source"] == "environment: PG_READER_DSN"


def test_the_file_still_wins_when_both_are_set(_nowhere, monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "FROM-SHELL")
    env = _nowhere / "home" / ".semlayer" / ".env"
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text("SNOWFLAKE_ACCOUNT=FROM-FILE\n")
    row = next(c for c in sess.discover() if c["kind"] == "snowflake")
    assert row["source"].endswith(".env: SNOWFLAKE_ACCOUNT")
    assert "FROM-FILE" not in json.dumps(row)
    assert "FROM-SHELL" not in json.dumps(row)


def test_a_plain_dict_is_still_a_valid_environment():
    """`discover(env)` takes a mapping, and a test should not have to build
    the subclass to use it."""
    rows = sess.discover({"PG_READER_DSN": "postgresql://r@h/db"})
    assert next(c["source"] for c in rows if c["kind"] == "postgres") == \
        "environment: PG_READER_DSN"


# --- a document that does not announce itself on line one ----------------

def _golds():
    from pathlib import Path
    return sorted((Path(__file__).resolve().parents[1] / "fixtures" / "golds")
                  .glob("*.yaml"))


@pytest.mark.parametrize("path", _golds(), ids=lambda p: p.stem)
def test_every_hand_authored_gold_scans_exactly(path):
    """Two defects the walkthrough found, both invisible to a test written
    against this engine's own output:

      * `scan_layer` demanded `semantic_layer:` on line ONE, and every gold
        opens with a comment header.
      * it counted table entries at a fixed two-space indent, and every gold
        indents list items by four. Both are legal YAML.

    So the count is checked against the parsed truth, on files this engine
    did not write.
    """
    import yaml
    info = sess.scan_layer(path)
    assert info is not None, path
    truth = yaml.safe_load(path.read_text())["semantic_layer"].get("tables") or []
    assert info["tables"] == len(truth)
    assert info["name"]


def test_a_leading_document_marker_is_skipped(tmp_path):
    path = tmp_path / "layer.yaml"
    path.write_text("---\n# mine\n\nsemantic_layer:\n  name: x\n  tables:\n  - name: t\n")
    assert sess.scan_layer(path)["name"] == "x"


def test_a_long_preamble_is_not_read_forever(tmp_path):
    path = tmp_path / "notalayer.yaml"
    path.write_text("# comment\n" * (sess._MAX_PREAMBLE_LINES + 5) + "semantic_layer:\n")
    assert sess.scan_layer(path) is None


# --- what a note may tell the user to do ---------------------------------

def test_no_note_tells_the_user_to_export_a_variable(_nowhere, monkeypatch):
    """Every note is read out by the agent, and a server its client launched
    never sees a shell -- so "export it" prescribes the one thing that cannot
    work. The file is the answer everywhere."""
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "ORG-ACCT")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/key.json")
    monkeypatch.setenv("PG_READER_DSN", "postgresql://r@h:5432/db")
    import re
    # An imperative: "export it", "export SNOWFLAKE_DATABASE". The word
    # itself is allowed, because the hint explains that a shell export is
    # invisible -- which is the opposite of prescribing one.
    prescribes = re.compile(r"\bexport (it|[A-Z][A-Z_]+)")
    notes = [c["note"] or "" for c in sess.discover()]
    assert notes
    for note in notes:
        assert not prescribes.search(note), note


def test_a_missing_snowflake_variable_names_the_file(_nowhere, monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "ORG-ACCT")
    note = next(c["note"] for c in sess.discover() if c["kind"] == "snowflake")
    assert ".env" in note
    assert "SNOWFLAKE_DATABASE=<database>" in note
    assert "SNOWFLAKE_READER_USER=<user>" in note
    assert "SNOWFLAKE_READER_PRIVATE_KEY_PATH=<path to the .p8 key>" in note
    assert "SEMLAYER_TEST" in note          # the fallback it would otherwise use
    # One hint, not one per missing thing: the whole note is read out loud.
    assert note.count("call status again") == 1


def test_the_bigquery_project_hint_names_the_file(_nowhere, monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/key.json")
    note = next(c["note"] for c in sess.discover() if c["kind"] == "bigquery")
    assert "GCP_PROJECT=<project>" in note
    assert ".env" in note
