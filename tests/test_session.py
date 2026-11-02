"""The session: which layer, which connection, and what it tells an agent.

The MCP server is the front door -- it is what people install first and
often the only thing they run -- so the questions here are the ones a server
with NO layer has to answer: what do you have, what can you see, what do I
do next.

Nothing in this file opens a warehouse. Discovery is stubbed at the
filesystem and the environment, which is also the claim being tested: a
`status` call on a laptop with credentials lying around must not connect to
anything.
"""

from __future__ import annotations

import json

import pytest
import yaml

from semlayer import session as sess


@pytest.fixture(autouse=True)
def _nowhere(nowhere):
    """Every test here runs on an empty machine: see conftest's `nowhere`."""
    return nowhere


@pytest.fixture
def gold():
    """A hand-authored gold layer: no warehouse, no LLM, no cassette."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    return yaml.safe_load((root / "fixtures" / "golds" / "fan_trap.yaml").read_text())


# --- the cache key: one warehouse, one entry, no secret in the name --------

def test_a_password_in_the_url_does_not_change_the_key():
    """The property the key exists for. A user who exports the password in
    one shell and not in another is looking at one warehouse."""
    with_pw = sess.cache_key("postgresql://u:s3cr3t@db.internal:5432/sales")
    without = sess.cache_key("postgresql://u@db.internal:5432/sales")
    assert with_pw == without


def test_no_secret_reaches_the_key_or_the_path():
    key = sess.cache_key("snowflake://u:s3cr3t@acct/db?role=r&password=s3cr3t")
    assert "s3cr3t" not in key
    assert "s3cr3t" not in str(sess.layer_cache_path(
        "snowflake://u:s3cr3t@acct/db?role=r&password=s3cr3t"))


def test_two_warehouses_do_not_share_an_entry():
    assert sess.cache_key("postgresql://u@h/a") != sess.cache_key("postgresql://u@h/b")
    assert sess.cache_key("snowflake:prod") != sess.cache_key("snowflake:staging")


def test_query_order_and_fragment_do_not_split_a_warehouse():
    a = sess.cache_key("snowflake://u@acct/db?role=r&warehouse=w")
    b = sess.cache_key("snowflake://u@acct/db?warehouse=w&role=r")
    assert a == b


def test_a_relative_duckdb_path_is_one_warehouse_per_file(tmp_path, monkeypatch):
    """`duckdb:warehouse.duckdb` names different files from different
    directories; a key that could not tell them apart would serve one
    warehouse's layer for another's."""
    here = sess.cache_key("duckdb:warehouse.duckdb")
    monkeypatch.chdir(tmp_path)
    there = sess.cache_key("duckdb:warehouse.duckdb")
    assert here != there
    assert sess.cache_key(f"duckdb:{tmp_path}/warehouse.duckdb") == there


def test_the_key_is_a_filename():
    for uri in ("duckdb:/var/data/w.duckdb", "postgresql://u@h:5432/db",
                "snowflake:prod", "bigquery:my-project"):
        key = sess.cache_key(uri)
        assert "/" not in key and ":" not in key and " " not in key
        assert sess.layer_cache_path(uri).parent.name == "layers"


def test_the_cache_lives_under_semlayer_home(_nowhere):
    path = sess.layer_cache_path("snowflake:prod")
    assert path.parent.parent == _nowhere / "home" / ".semlayer"


# --- what the session holds -----------------------------------------------

def test_an_empty_session_is_no_layer():
    s = sess.Session()
    assert s.state == "no_layer"
    assert s.doc is None and s.layer_path is None and s.connection is None


def test_a_document_alone_is_a_ready_session(gold):
    """`build_server(doc)` is how every in-process caller builds a server."""
    s = sess.Session.of(gold)
    assert s.state == "ready"
    assert sess.Session.of(s) is s


def test_an_explicit_path_loads_and_is_recorded(tmp_path, gold):
    path = tmp_path / "layer.yaml"
    path.write_text(yaml.safe_dump(gold))
    s = sess.Session()
    s.load(path)
    assert s.state == "ready"
    assert s.layer_path == path


def test_an_explicit_path_that_is_not_there_still_raises(tmp_path):
    """Today's behaviour, kept: someone who named a file wants that file, and
    starting empty instead would be a mystery rather than an error."""
    from semlayer.errors import SemlayerError
    with pytest.raises(SemlayerError):
        sess.Session().load(tmp_path / "nope.yaml")


def test_selecting_a_connection_with_no_cached_layer_starts_empty():
    s = sess.Session()
    assert s.select("snowflake:prod") is False
    assert s.connection == "snowflake:prod"
    assert s.state == "no_layer"


def test_selecting_a_connection_loads_its_cached_layer(gold):
    path = sess.layer_cache_path("duckdb:/w.duckdb")
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(gold))
    s = sess.Session()
    assert s.select("duckdb:/w.duckdb") is True
    assert s.state == "ready" and s.layer_path == path


def test_a_cached_layer_we_cannot_read_is_not_fatal():
    """A cache entry is ours, not the user's. Half-written, or written by a
    version whose output this one cannot read, means start empty and say so --
    not refuse to start the server."""
    path = sess.layer_cache_path("snowflake:prod")
    path.parent.mkdir(parents=True)
    path.write_text("this: is not a semantic layer\n")
    s = sess.Session()
    assert s.select("snowflake:prod") is False
    assert s.state == "no_layer"


# --- status: the object every tool falls back to ---------------------------

def test_status_with_nothing_says_so_and_says_what_to_do():
    out = sess.Session().status()
    assert out["state"] == "no_layer"
    assert out["connection"] is None
    assert out["layer"] is None
    assert out["connections"] == []
    assert "semlayer infer" in out["next"]


def test_status_is_json_serialisable():
    """It is returned to an agent through an MCP tool, so it has to survive
    `json.dumps` without a default= rescue."""
    json.dumps(sess.Session().status())


def test_status_describes_a_loaded_layer(tmp_path, gold):
    path = tmp_path / "layer.yaml"
    path.write_text(yaml.safe_dump(gold))
    s = sess.Session()
    s.load(path)
    out = s.status()
    assert out["state"] == "ready"
    layer = out["layer"]
    assert layer["path"] == str(path)
    assert layer["tables"] == len(gold["semantic_layer"]["tables"])
    assert layer["columns"] == sum(len(t["columns"])
                                   for t in gold["semantic_layer"]["tables"])
    assert layer["inferred_at"], "an agent has to be able to say how old this is"
    assert isinstance(layer["undecided_review_items"], int)


def test_status_reports_engine_and_model_when_the_document_carries_them(tmp_path, gold):
    gold["semantic_layer"]["generated_by"] = {
        "engine": "0.6.0", "model": "claude-x/escalation",
        "generated_at": "2026-11-01T10:00:00Z"}
    path = tmp_path / "layer.yaml"
    path.write_text(yaml.safe_dump(gold))
    s = sess.Session()
    s.load(path)
    layer = s.status()["layer"]
    assert layer["engine"] == "0.6.0"
    assert layer["model"] == "claude-x/escalation"
    assert layer["inferred_at"] == "2026-11-01T10:00:00Z"


def test_a_ready_session_is_told_to_search_not_to_infer(tmp_path, gold):
    s = sess.Session.of(gold)
    assert "semantic_search" in s.status()["next"]


# --- discovery, source 1: DuckDB files in the working directory -----------

def test_a_duckdb_file_in_the_working_directory_is_found(_nowhere):
    (_nowhere / "cwd" / "warehouse.duckdb").write_text("")
    found = sess.discover()
    assert [c["uri"] for c in found] == ["duckdb:warehouse.duckdb"]
    assert found[0]["kind"] == "duckdb"
    assert found[0]["supported"] is True
    assert found[0]["source"] == "working directory"


def test_a_dot_db_file_is_offered_with_the_sqlite_caveat(_nowhere):
    (_nowhere / "cwd" / "app.db").write_text("")
    note = sess.discover()[0]["note"]
    assert "SQLite" in note, "we cannot tell without opening it, and we do not open it"


def test_discovery_is_not_recursive(_nowhere):
    """A directory listing is cheap; walking someone's home directory is not."""
    nested = _nowhere / "cwd" / "data"
    nested.mkdir()
    (nested / "deep.duckdb").write_text("")
    assert sess.discover() == []


def test_a_directory_of_databases_is_summarised_rather_than_listed(_nowhere):
    for i in range(sess._MAX_DUCKDB_FILES + 5):
        (_nowhere / "cwd" / f"w{i:02d}.duckdb").write_text("")
    found = sess.discover()
    assert len(found) == sess._MAX_DUCKDB_FILES + 1
    assert found[-1]["supported"] is False
    assert "name the file you want" in found[-1]["note"]


def test_discovery_never_opens_a_connection(_nowhere, monkeypatch):
    """The claim that makes it safe to run on every `status` call. A file that
    is not a database at all must cost nothing -- if anything opened it, this
    would raise rather than list it."""
    (_nowhere / "cwd" / "not-a-database.duckdb").write_text("junk")
    import semlayer.pipeline as pipeline
    monkeypatch.setattr(pipeline, "open_source_from_uri",
                        lambda *a, **k: pytest.fail("discovery opened a connection"))
    assert sess.discover()[0]["uri"] == "duckdb:not-a-database.duckdb"


# --- discovery, source 2: a Postgres DSN in the environment ---------------

def test_a_postgres_dsn_in_the_environment_is_found(monkeypatch):
    monkeypatch.setenv("PG_READER_DSN", "postgresql://u:pw@db.internal/sales")
    found = sess.discover()
    assert [c["uri"] for c in found] == ["postgres"]
    assert found[0]["source"] == "environment: PG_READER_DSN"
    assert found[0]["supported"] is True


def test_the_fallback_variable_the_connector_honours_is_honoured_here_too(monkeypatch):
    """`PG_DSN` is read by `PostgresSource.from_env`, so a machine configured
    that way is configured, and reporting nothing for it would be wrong."""
    monkeypatch.setenv("PG_DSN", "postgresql://u@h/db")
    assert sess.discover()[0]["source"] == "environment: PG_DSN"


def test_a_dsn_value_never_appears_in_discovery_output(monkeypatch):
    """The value is a hostname, a database name and usually a password. The
    whole point of reporting the variable NAME is that none of that travels."""
    monkeypatch.setenv("PG_READER_DSN", "postgresql://u:s3cr3t@db.internal/sales")
    out = json.dumps(sess.discover())
    for leak in ("s3cr3t", "db.internal", "sales"):
        assert leak not in out


def test_status_names_the_connections_it_found_in_what_to_do_next(monkeypatch):
    monkeypatch.setenv("PG_READER_DSN", "postgresql://u:pw@h/db")
    out = sess.Session().status()
    assert "postgres" in out["next"]
    assert "pw" not in json.dumps(out), "a DSN's value may never be reported"


# --- discovery, source 3: a BigQuery project or credential ----------------

def test_a_gcp_project_is_found_and_named(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT", "acme-analytics")
    found = sess.discover()
    assert [c["uri"] for c in found] == ["bigquery:acme-analytics"]
    assert "acme-analytics" in found[0]["note"]
    assert found[0]["kind"] == "bigquery"


def test_application_default_credentials_alone_are_enough_to_offer_bigquery(
        _nowhere, monkeypatch):
    """The `gcloud auth application-default login` case: no project set, so
    the URI is the bare word and the note says where the project will come
    from."""
    gcloud = _nowhere / "gcloud"
    gcloud.mkdir()
    (gcloud / "application_default_credentials.json").write_text("{}")
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(gcloud))
    found = sess.discover()
    assert [c["uri"] for c in found] == ["bigquery"]
    assert "quota project" in found[0]["note"]


def test_a_key_file_is_reported_by_variable_name_and_not_by_path(
        _nowhere, monkeypatch):
    """Knowing a key file exists is the useful part. Knowing where it lives is
    not ours to pass on, and the key itself never is."""
    key = _nowhere / "secret-sa-key.json"
    key.write_text("{}")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS_READER", str(key))
    out = json.dumps(sess.discover())
    assert "GOOGLE_APPLICATION_CREDENTIALS_READER" in out
    assert "secret-sa-key" not in out


def test_no_bigquery_signal_means_no_bigquery_row():
    assert [c for c in sess.discover() if c["kind"] == "bigquery"] == []


# --- discovery, source 4: dbt's profiles.yml ------------------------------

def _profiles(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(body))


def test_a_dbt_postgres_target_becomes_a_dsn(_nowhere):
    _profiles(_nowhere / "home" / ".dbt" / "profiles.yml", {
        "jaffle": {"target": "dev", "outputs": {
            "dev": {"type": "postgres", "host": "db.internal", "port": 5439,
                    "user": "analyst", "password": "s3cr3t", "dbname": "sales",
                    "schema": "public"}}}})
    found = sess.discover()
    assert [c["uri"] for c in found] == ["postgresql://analyst@db.internal:5439/sales"]
    assert found[0]["supported"] is True
    assert found[0]["source"].startswith("dbt: ~/")
    assert "default" in found[0]["note"]
    assert "s3cr3t" not in json.dumps(found), "a profile's password never travels"


def test_a_dbt_snowflake_target_carries_role_and_warehouse(_nowhere):
    _profiles(_nowhere / "home" / ".dbt" / "profiles.yml", {
        "acme": {"target": "prod", "outputs": {
            "prod": {"type": "snowflake", "account": "ab12345.us-east-1",
                     "user": "svc", "role": "reader", "database": "ANALYTICS",
                     "warehouse": "WH", "private_key_path": "/keys/k.p8"}}}})
    found = sess.discover()
    assert found[0]["uri"] == ("snowflake://svc@ab12345.us-east-1/ANALYTICS"
                               "?role=reader&warehouse=WH")
    assert "/keys/k.p8" not in json.dumps(found)


def test_a_dbt_bigquery_target_becomes_the_project_form(_nowhere):
    _profiles(_nowhere / "cwd" / "profiles.yml", {
        "acme": {"target": "dev", "outputs": {
            "dev": {"type": "bigquery", "method": "oauth",
                    "project": "acme-analytics", "dataset": "dbt"}}}})
    assert sess.discover()[0]["uri"] == "bigquery:acme-analytics"


def test_a_templated_profile_is_reported_as_unresolvable_not_dropped(_nowhere):
    """`{{ env_var('...') }}` is what a checked-in profile actually looks like.
    Rendering it means implementing dbt's Jinja context; saying so lets the
    agent ask the user instead of silently seeing no warehouse."""
    (_nowhere / "home" / ".dbt").mkdir(parents=True)
    (_nowhere / "home" / ".dbt" / "profiles.yml").write_text(
        "acme:\n  target: prod\n  outputs:\n    prod:\n"
        "      type: snowflake\n      account: \"{{ env_var('SF_ACCOUNT') }}\"\n"
        "      database: ANALYTICS\n")
    found = sess.discover()
    assert found[0]["uri"] == "dbt:acme.prod"
    assert found[0]["supported"] is False
    assert "templates" in found[0]["note"]


def test_an_adapter_we_do_not_support_says_so(_nowhere):
    """The answer to "I have dbt, why can you not see my warehouse"."""
    _profiles(_nowhere / "home" / ".dbt" / "profiles.yml", {
        "acme": {"target": "dev", "outputs": {
            "dev": {"type": "databricks", "host": "h", "http_path": "p"}}}})
    found = sess.discover()
    assert found[0] == {"uri": "dbt:acme.dev", "kind": "databricks",
                        "source": found[0]["source"], "supported": False,
                        "note": "dbt profile 'acme', target 'dev' (the profile's "
                                "default): semlayer has no connector for dbt's "
                                "'databricks' adapter"}


def test_a_broken_profiles_file_costs_only_itself(_nowhere, monkeypatch):
    (_nowhere / "home" / ".dbt").mkdir(parents=True)
    (_nowhere / "home" / ".dbt" / "profiles.yml").write_text("{{{ not yaml")
    monkeypatch.setenv("PG_READER_DSN", "postgresql://u@h/db")
    assert [c["uri"] for c in sess.discover()] == ["postgres"]


def test_several_targets_in_one_profile_are_several_connections(_nowhere):
    _profiles(_nowhere / "home" / ".dbt" / "profiles.yml", {
        "acme": {"target": "dev", "outputs": {
            "dev": {"type": "postgres", "host": "localhost", "dbname": "dev",
                    "user": "me"},
            "prod": {"type": "postgres", "host": "db.internal", "dbname": "prod",
                     "user": "svc"}}}})
    assert len(sess.discover()) == 2


def test_the_profiles_dir_variable_is_honoured(_nowhere, monkeypatch):
    other = _nowhere / "elsewhere"
    _profiles(other / "profiles.yml", {
        "acme": {"target": "dev", "outputs": {
            "dev": {"type": "bigquery", "project": "p"}}}})
    monkeypatch.setenv("DBT_PROFILES_DIR", str(other))
    assert sess.discover()[0]["uri"] == "bigquery:p"


# --- discovery, source 5: a Snowflake account in the environment ----------

def test_a_configured_snowflake_account_is_offered(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "ab12345.us-east-1")
    monkeypatch.setenv("SNOWFLAKE_READER_USER", "svc")
    monkeypatch.setenv("SNOWFLAKE_READER_PASSWORD", "s3cr3t")
    monkeypatch.setenv("SNOWFLAKE_DATABASE", "ANALYTICS")
    found = sess.discover()
    assert [c["uri"] for c in found] == ["snowflake"]
    assert found[0]["supported"] is True
    out = json.dumps(found)
    assert "s3cr3t" not in out
    assert "ab12345" not in out, "an account identifier is the organisation's name"


def test_a_missing_variable_is_named_rather_than_left_to_a_later_refusal(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "ab12345")
    found = sess.discover()
    assert found[0]["supported"] is False
    assert "SNOWFLAKE_READER_USER" in found[0]["note"]
    assert "SNOWFLAKE_READER_PASSWORD" in found[0]["note"]


def test_the_test_database_default_is_called_out(monkeypatch):
    """`from_env` falls back to SEMLAYER_TEST when SNOWFLAKE_DATABASE is unset,
    which silently infers the wrong database."""
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "ab12345")
    monkeypatch.setenv("SNOWFLAKE_READER_USER", "svc")
    monkeypatch.setenv("SNOWFLAKE_READER_PRIVATE_KEY_PATH", "/keys/k.p8")
    found = sess.discover()
    assert "SNOWFLAKE_DATABASE is unset" in found[0]["note"]
    assert found[0]["supported"] is True


def test_no_snowflake_account_means_no_snowflake_row():
    assert [c for c in sess.discover() if c["kind"] == "snowflake"] == []


# --- discovery, source 6: ~/.snowflake/connections.toml -------------------

def _connections_toml(_nowhere, text):
    d = _nowhere / "home" / ".snowflake"
    d.mkdir(parents=True, exist_ok=True)
    (d / "connections.toml").write_text(text)


def test_named_snowflake_connections_are_listed(_nowhere):
    _connections_toml(_nowhere, """
[prod]
account = "ab12345.us-east-1"
user = "svc"
role = "reader"
database = "ANALYTICS"

[dev]
account = "ab12345.us-east-1"
user = "me"
database = "DEV"
""")
    found = {c["uri"]: c for c in sess.discover()}
    assert set(found) == {"snowflake:prod", "snowflake:dev"}
    assert found["snowflake:prod"]["source"] == "~/.snowflake/connections.toml"
    assert found["snowflake:prod"]["supported"] is True


def test_role_and_database_are_reported_as_present_or_absent(_nowhere):
    """The run report records both, so an agent should know before the run."""
    _connections_toml(_nowhere, """
[prod]
account = "ab12345"
database = "ANALYTICS"
role = "reader"

[noroles]
account = "ab12345"
database = "ANALYTICS"
""")
    found = {c["uri"]: c for c in sess.discover()}
    assert "carries role, database" in found["snowflake:prod"]["note"]
    assert "no role" in found["snowflake:noroles"]["note"]


def test_a_connection_with_no_database_is_not_offered_as_usable(_nowhere):
    """`from_connection_name` refuses it, so `supported` says so first."""
    _connections_toml(_nowhere, '[prod]\naccount = "ab12345"\nrole = "reader"\n')
    found = sess.discover()[0]
    assert found["supported"] is False
    assert "no database" in found["note"]


def test_a_password_in_connections_toml_never_reaches_the_output(_nowhere):
    """The file holds credentials inline. Section NAMES are the only thing
    this source discloses."""
    _connections_toml(_nowhere, """
[prod]
account = "ab12345.us-east-1"
user = "svc"
password = "s3cr3t"
private_key_path = "/keys/k.p8"
database = "ANALYTICS"
""")
    out = json.dumps(sess.discover())
    assert "snowflake:prod" in out
    for leak in ("s3cr3t", "password", "/keys/k.p8", "ab12345", "svc"):
        assert leak not in out, f"{leak} reached discovery output"


def test_a_malformed_connections_file_costs_only_itself(_nowhere, monkeypatch):
    _connections_toml(_nowhere, "this is not toml [[[")
    monkeypatch.setenv("PG_READER_DSN", "postgresql://u@h/db")
    assert [c["uri"] for c in sess.discover()] == ["postgres"]


def test_snowflake_home_is_honoured(_nowhere, monkeypatch):
    other = _nowhere / "sfhome"
    other.mkdir()
    (other / "connections.toml").write_text('[ci]\naccount = "a"\ndatabase = "D"\n')
    monkeypatch.setenv("SNOWFLAKE_HOME", str(other))
    assert sess.discover()[0]["uri"] == "snowflake:ci"


def test_every_source_is_reported_in_the_documented_order(_nowhere, monkeypatch):
    """The table in the PR: working directory, Postgres, BigQuery, dbt,
    Snowflake environment, Snowflake connections file."""
    (_nowhere / "cwd" / "w.duckdb").write_text("")
    monkeypatch.setenv("PG_READER_DSN", "postgresql://u@h/db")
    monkeypatch.setenv("GCP_PROJECT", "acme")
    _profiles(_nowhere / "home" / ".dbt" / "profiles.yml", {
        "acme": {"target": "dev", "outputs": {
            "dev": {"type": "databricks", "host": "h"}}}})
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "ab12345")
    _connections_toml(_nowhere, '[prod]\naccount = "a"\ndatabase = "D"\n')
    assert [c["uri"] for c in sess.discover()] == [
        "duckdb:w.duckdb", "postgres", "bigquery:acme", "dbt:acme.dev",
        "snowflake", "snowflake:prod"]


def test_what_to_do_next_names_connections_but_does_not_recite_twenty(_nowhere):
    """A fixtures directory with twenty databases in it would otherwise put
    twenty paths in one sentence. The full list is in `connections`."""
    for i in range(12):
        (_nowhere / "cwd" / f"w{i:02d}.duckdb").write_text("")
    out = sess.Session().status()
    assert "duckdb:w00.duckdb" in out["next"]
    assert "and 6 more" in out["next"]
    assert len(out["connections"]) == 12
