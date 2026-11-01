"""What may leave the machine, asserted rather than promised.

The serializer exists so that "we never send names or data" is a property
of one function instead of everyone's future discipline. These tests are
what makes that true: each one is a thing a caller might do by accident,
and the assertion that it cannot get out.
"""

from __future__ import annotations

import importlib
import json

import pytest

from semlayer import telemetry_schema as ts

# --- the whitelist ----------------------------------------------------

def test_undeclared_fields_are_dropped():
    out = ts.serialize({
        "event": "infer",
        "warehouse_name": "acme_prod",            # a name
        "table_list": ["customers", "orders"],    # names
        "uri": "snowflake://acct/db",             # a credentialled URL
        "sql": "select * from customers",         # a query
    })
    assert out == {"event": "infer"}


def test_reserved_fields_are_not_caller_settable():
    """schema, t and anon are set by record(), so they are not in FIELDS."""
    assert not (ts.RESERVED & set(ts.FIELDS))
    assert ts.serialize({"schema": 99, "t": 0, "anon": "x" * 32}) == {}


# --- bucketing --------------------------------------------------------

@pytest.mark.parametrize("n,want", [
    (0, "0"), (1, "1_5"), (5, "1_5"), (6, "6_20"), (20, "6_20"),
    (21, "21_50"), (47, "21_50"), (50, "21_50"), (51, "51_200"),
    (200, "51_200"), (201, "201_1000"), (999, "201_1000"),
    (1000, "1000_plus"), (50_000, "1000_plus"),
])
def test_table_buckets_at_their_boundaries(n, want):
    assert ts.serialize({"tables": n})["tables"] == want


@pytest.mark.parametrize("n,want", [
    (0, "0"), (1, "1_50"), (611, "501_2000"), (10_000, "10000_plus"),
])
def test_column_buckets(n, want):
    assert ts.serialize({"columns": n})["columns"] == want


def test_an_exact_count_can_never_be_sent():
    """The whole point of bucketing here rather than at call sites."""
    for n in (1, 47, 611, 99_999):
        for field in ("tables", "columns", "schemas"):
            got = ts.serialize({field: n})[field]
            assert got != n and isinstance(got, str)


def test_a_negative_or_boolean_count_is_dropped():
    assert ts.serialize({"tables": -1}) == {}
    assert ts.serialize({"tables": True}) == {}      # bool is an int in Python


# --- enums ------------------------------------------------------------

def test_unknown_enum_becomes_other_where_other_exists():
    assert ts.serialize({"llm": "mistral"})["llm"] == "other"
    assert ts.serialize({"os": "plan9"})["os"] == "other"


def test_unknown_enum_is_dropped_where_other_does_not_exist():
    """Dropping is the safe default: an unanticipated value is exactly the
    kind of thing that turns out to be a hostname."""
    assert "connector" not in ts.serialize({"connector": "acme-warehouse-prod"})
    assert "outcome" not in ts.serialize({"outcome": "weird"})


def test_flags_keep_only_known_names():
    out = ts.serialize({"flags": ["no_llm", "--password=hunter2", "json"]})
    assert out["flags"] == ["json", "no_llm"]


# --- objects ----------------------------------------------------------

def test_stage_times_keep_ints_and_drop_everything_else():
    out = ts.serialize({"stages": {"profile": 12, "link": "34", "describe": 4.5}})
    assert out["stages"] == {"profile": 12}


def test_stage_names_are_data_not_an_enum():
    """A renamed or new stage must not need a schema bump."""
    out = ts.serialize({"stages": {"brand_new_stage": 7}})
    assert out["stages"] == {"brand_new_stage": 7}


def test_probe_drops_long_strings():
    out = ts.serialize({"probe": {"grade": "B", "ratio": 0.4,
                                  "note": "x" * 200}})
    assert out["probe"] == {"grade": "B", "ratio": 0.4}


# --- record() ---------------------------------------------------------

@pytest.fixture
def tele(tmp_path, monkeypatch):
    monkeypatch.setenv("SEMLAYER_HOME", str(tmp_path))
    monkeypatch.delenv("SEMLAYER_TELEMETRY", raising=False)
    from semlayer import telemetry
    return importlib.reload(telemetry)


def _row(tmp_path):
    return json.loads((tmp_path / "telemetry.jsonl").read_text().splitlines()[0])


def test_record_stamps_schema_time_and_anon(tele, tmp_path):
    tele.record("cli", command="validate")
    row = _row(tmp_path)
    assert row["schema"] == ts.SCHEMA
    assert len(row["anon"]) == 32
    assert isinstance(row["t"], int)


def test_record_honours_a_supplied_start_time(tele, tmp_path):
    """t is when the command STARTED, though the write happens at the end."""
    tele.record("cli", t=1760600000, command="validate")
    assert _row(tmp_path)["t"] == 1760600000


def test_a_caller_cannot_override_machine_facts(tele, tmp_path):
    tele.record("cli", os="MyCustomOS", version="../../etc/passwd",
                python="3.12.1-custom-build")
    row = _row(tmp_path)
    assert row["os"] in ts.OSES and row["os"] != "MyCustomOS"
    assert row["version"] != "../../etc/passwd"
    assert row["python"].count(".") == 1          # major.minor only


def test_a_caller_cannot_leak_a_name_through_record(tele, tmp_path):
    tele.record("infer", command="infer", tables=47,
                warehouse_name="acme_prod", sql="select 1")
    row = _row(tmp_path)
    assert "warehouse_name" not in row and "sql" not in row
    assert row["tables"] == "21_50"


# --- note(): how handlers contribute without being threaded a telemetry
#     object through every signature ---------------------------------------

def test_note_fields_reach_the_event(tele, tmp_path):
    tele.note(connector="snowflake", tables=47)
    tele.record("cli", command="infer")
    row = _row(tmp_path)
    assert row["connector"] == "snowflake"
    assert row["tables"] == "21_50"          # still bucketed on the way out


def test_an_explicit_argument_beats_a_noted_one(tele, tmp_path):
    tele.note(outcome="ok")
    tele.record("cli", command="infer", outcome="error")
    assert _row(tmp_path)["outcome"] == "error"


def test_note_is_not_a_second_route_past_the_whitelist(tele, tmp_path):
    """The convenience must not become a hole."""
    tele.note(warehouse_name="acme_prod", sql="select 1", tables=99999)
    tele.record("cli", command="infer")
    row = _row(tmp_path)
    assert "warehouse_name" not in row and "sql" not in row
    assert row["tables"] == "1000_plus"


def test_machine_facts_still_win_over_note(tele, tmp_path):
    tele.note(os="MyCustomOS", version="9.9.9")
    tele.record("cli", command="infer")
    row = _row(tmp_path)
    assert row["os"] != "MyCustomOS" and row["version"] != "9.9.9"


# --- probe counts must not bypass the bucketing --------------------------

def test_probe_counts_are_bucketed_like_the_top_level_ones():
    """They were exact, which made the top-level bucketing pointless: the
    same numbers were going out inside a nested object."""
    out = ts.serialize({"probe": {"tables": 36, "columns": 198, "schemas": 1,
                                  "grade": "weak", "declared_pk_ratio": 0.4}})
    p = out["probe"]
    assert p["tables"] == "21_50"
    assert p["columns"] == "51_200"
    assert p["schemas"] == "1"
    assert p["declared_pk_ratio"] == 0.4      # ratios stay exact
    assert p["grade"] == "weak"


def test_no_exact_count_survives_anywhere_in_an_event():
    """Belt and braces: walk the whole structure looking for the raw numbers."""
    out = ts.serialize({"tables": 36, "columns": 198,
                        "probe": {"tables": 36, "columns": 198}})
    flat = json.dumps(out)
    assert "36" not in flat and "198" not in flat


# --- host: which MCP client launched the server ------------------------
#
# Unlike every other enum field, the input here is arbitrary vendor text,
# so `_host` classifies rather than passes through. These tests are the
# reason that distinction can be relied on.

@pytest.mark.parametrize(("raw", "want"), [
    ("claude-ai",             "claude-desktop"),
    ("Claude Desktop",        "claude-desktop"),
    ("claude-code",           "claude-code"),
    ("Claude Code",           "claude-code"),
    ("cursor-vscode",         "cursor"),        # Cursor says both; cursor wins
    ("Cursor",                "cursor"),
    ("Visual Studio Code",    "vscode"),
    ("vscode-mcp",            "vscode"),        # vscode before the sdk rule
    ("mcp-inspector",         "inspector"),     # inspector before the sdk rule
    ("mcp",                   "sdk"),           # the bare SDK default
    ("mcp-python-sdk",        "sdk"),
    ("some-agent-framework",  "other"),
])
def test_host_classifies_known_clients(raw, want):
    assert ts.serialize({"host": raw}) == {"host": want}


def test_an_absent_or_blank_client_name_is_not_guessed_at():
    """A client that sends no clientInfo is absent from the event, not "other"."""
    assert ts.serialize({"host": ""}) == {}
    assert ts.serialize({"host": "   "}) == {}
    assert ts.serialize({"host": None}) == {}
    assert ts.serialize({"host": 7}) == {}


def test_the_raw_client_name_can_never_reach_an_event():
    """The point of classifying instead of passing through.

    A client may call itself anything -- including something that names a
    company, a host or a person -- and `_host` has no path that returns its
    input, so no part of the name survives serialization.
    """
    out = ts.serialize({"host": "acme-prod-warehouse-bot/3.2 (sre@acme.example)"})
    assert out == {"host": "other"}
    assert "acme" not in json.dumps(out)


def test_every_host_label_is_declared():
    """A rule that returns a label absent from HOSTS would be unreadable to
    the receiver's column map."""
    labels = {label for _, label in ts._HOST_RULES} | {"other"}
    assert labels <= ts.HOSTS


def test_host_arrived_with_a_schema_bump():
    """Adding a field without bumping SCHEMA is the mistake the docstring
    at the top of telemetry_schema warns about: the receiver would park the
    new column in `misc` and prune it after 30 days."""
    assert "host" in ts.FIELDS
    assert ts.SCHEMA >= 2
