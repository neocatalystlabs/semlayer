"""Describe stage evals: coverage, judge floor (Sonnet, cassette-replayed),
and table-type floors on the decisive fixtures (hard classes report-only
per feasibility F4)."""

import json
import random
import re
import sys
from pathlib import Path

import duckdb
import pytest
import yaml

OSS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OSS / "fixtures"))

try:
    from dotenv import load_dotenv
    load_dotenv(OSS.parent / ".env")  # enables cassette RECORDING in dev; CI replays
except ImportError:
    pass

from semlayer.describe import describe_source  # noqa: E402
from semlayer.link import link_source  # noqa: E402
from semlayer.llm.provider import AnthropicProvider, CassetteMiss  # noqa: E402
from semlayer.profile.run import profile_with_stats  # noqa: E402
from semlayer.source import DuckDBSource  # noqa: E402
from semlayer.validate import validate_document  # noqa: E402

JUDGE_SYS = """You are a strict reviewer of auto-generated data warehouse documentation.
For each item, given the EVIDENCE (schema + stats) and a DESCRIPTION, judge:
- correct: no factual claim contradicts the evidence (unverifiable-but-plausible is OK; wrong numbers/meanings are not)
- useful: it adds meaning beyond restating the column/table name
Respond ONLY JSON: [{"id": n, "correct": bool, "useful": bool, "issue": "<=12 words or ''"}]"""

TABLE_TYPE_FLOORS = {"fan_trap": 0.99, "obt": 0.99, "messy_mart": 0.85}


def _pipeline(fixture, use_declared=False):
    import importlib
    mod = importlib.import_module(f"generators.{fixture}")
    con = duckdb.connect(":memory:")
    mod.build(con)
    llm = AnthropicProvider()
    try:
        doc, stats = profile_with_stats(DuckDBSource(con), llm=llm)
        link_source(DuckDBSource(con), doc, stats, llm=llm, use_declared=use_declared)
        describe_source(doc, stats, llm)
    except CassetteMiss as e:
        pytest.skip(str(e))
    finally:
        con.close()
    doc.pop("_link_audit", None)
    return doc, stats


def test_a_declared_key_is_not_a_human_written_description():
    """The discriminator itself, with no model and no cassettes involved.

    The integration test below replays recordings, so a regression that
    changes a prompt shows there as a SKIP -- which passes CI. This one
    cannot skip: it is the gate that fails if `catalog_prior` ever again
    means "a human wrote this description" regardless of what it describes.
    """
    from semlayer.describe.run import _apply_parsed, _from_catalog
    from semlayer.profile.run import _COMMENT_TABLE_DESC

    keyed = {"name": "ord", "columns": [],
             "provenance": [{"signal": "catalog_prior",
                             "detail": "PRIMARY KEY (ord_id) declared in the warehouse "
                                       "catalog"}]}
    assert not _from_catalog(keyed), "a declared key says nothing about the description"
    _apply_parsed(keyed, {"table_description": "one row per order"}, 0)
    assert keyed["description"] == "one row per order"

    commented = {"name": "cust", "columns": [],
                 "description": "what a person wrote",
                 "provenance": [{"signal": "catalog_prior", "detail": _COMMENT_TABLE_DESC}]}
    assert _from_catalog(commented)
    _apply_parsed(commented, {"table_description": "LLM guess"}, 0)
    assert commented["description"] == "what a person wrote", "priors are never overwritten"


def test_declared_keys_do_not_suppress_descriptions():
    """A declared key is provenance for the KEY, never for the description.

    Every other LLM-on test here runs `use_declared=False`, which was chosen
    to avoid a paid re-record (docs/calibration.md) -- so the declared-key
    path and the describe path were never exercised together, and `infer`
    with the LLM on crashed with KeyError: 'description' on the first table
    of any warehouse that declares primary keys. fan_trap declares them, and
    its prompts are unchanged by the declarations, so this replays.
    """
    doc, _ = _pipeline("fan_trap", use_declared=True)
    tables = doc["semantic_layer"]["tables"]
    declared = [t for t in tables
                if any(pv.get("signal") == "catalog_prior"
                       and "PRIMARY KEY" in (pv.get("detail") or "")
                       for pv in (t.get("provenance") or []))]
    assert declared, "fixture no longer declares primary keys; this test is moot"
    assert all(t.get("description") for t in declared), \
        "a declared primary key suppressed the table description"
    undescribed = [(t["name"], c["name"]) for t in tables
                   for c in t["columns"] if not c.get("description")]
    assert not undescribed, f"undescribed columns: {undescribed[:5]}"


def test_describe_full_coverage_and_conformance():
    doc, _ = _pipeline("messy_mart")
    assert validate_document(doc).ok
    tables = doc["semantic_layer"]["tables"]
    assert all(t.get("description") for t in tables), "every table described"
    missing = [(t["name"], c["name"]) for t in tables
               for c in t["columns"] if not c.get("description")]
    assert not missing, f"undescribed columns: {missing[:5]}"


def _judge_items(doc, stats):
    """The judged sample. Byte-stable: the seed and the field order are both
    part of the cassette key, so nothing here may be reordered."""
    rng = random.Random(42)  # MUST match the recording run for cassette hits
    sampled = rng.sample(doc["semantic_layer"]["tables"], 12)
    items = []
    for t in sampled:
        ts = stats[t["name"]]
        ev = [{"name": c["name"], "sql_type": c["sql_type"],
               "n_distinct": ts.columns[c["name"]].n_distinct,
               "samples": [v for v, _ in ts.columns[c["name"]].top_values[:5]]}
              for c in t["columns"]]
        items.append({"kind": "table", "table": t["name"], "evidence_columns": ev,
                      "row_count": ts.row_count, "description": t.get("description", "")})
        for c in rng.sample(t["columns"], min(3, len(t["columns"]))):
            items.append({"kind": "column", "table": t["name"], "column": c["name"],
                          "sql_type": c["sql_type"],
                          "n_distinct": ts.columns[c["name"]].n_distinct,
                          "samples": [v for v, _ in ts.columns[c["name"]].top_values[:5]],
                          "description": c.get("description", "")})
    return items


def _judge_run(judge, items):
    results = []
    for i in range(0, len(items), 12):
        chunk = items[i:i + 12]
        raw = judge.complete(JUDGE_SYS, json.dumps(
            [{**it, "id": j} for j, it in enumerate(chunk)], indent=1, default=str))
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        assert m, "judge returned no JSON"
        for v in json.loads(m.group(0)):
            j = v.get("id")
            if isinstance(j, int) and 0 <= j < len(chunk):
                results.append(v)
    return results


def test_description_judge_floor():
    """Sonnet-judged correct+useful >= 0.80 (M3 target; measured 0.889)."""
    doc, stats = _pipeline("messy_mart")
    judge = AnthropicProvider(model="claude-sonnet-5")
    try:
        results = _judge_run(judge, _judge_items(doc, stats))
    except CassetteMiss as e:
        pytest.skip(str(e))
    ok = sum(1 for v in results if v.get("correct") and v.get("useful"))
    rate = ok / len(results)
    print(f"\n[describe judge] {ok}/{len(results)} = {rate:.3f}")
    assert rate >= 0.80


@pytest.mark.parametrize("fixture", list(TABLE_TYPE_FLOORS))
def test_table_type_meets_floor(fixture):
    doc, _ = _pipeline(fixture)
    gold = yaml.safe_load((OSS / "fixtures" / "golds" / f"{fixture}.yaml").read_text())
    gt = {t["name"]: t.get("table_type", "unknown") for t in gold["semantic_layer"]["tables"]}
    dt = {t["name"]: t["table_type"] for t in doc["semantic_layer"]["tables"]}
    acc = sum(1 for n, g in gt.items() if dt.get(n) == g) / len(gt)
    print(f"\n[{fixture}] table_type acc={acc:.3f}")
    assert acc >= TABLE_TYPE_FLOORS[fixture]


# --- does the judge measure anything? (negative controls) ------------

def _corrupt(items, arm):
    """Break the descriptions in one known way, leaving the evidence intact.

    Each arm attacks one of the judge's two stated axes, so a judge that
    scores them all highly is failing in a diagnosable way rather than
    merely being generous.
    """
    out = [dict(it) for it in items]
    if arm == "generic":
        for it in out:
            it["description"] = ("A table in this data warehouse."
                                 if it["kind"] == "table"
                                 else "A column in this table.")
        return out
    if arm == "name_restatement":
        for it in out:
            it["description"] = (f"The {it['table']} table."
                                 if it["kind"] == "table"
                                 else f"The {it['column']} column.")
        return out
    if arm == "shuffled":
        return _rotate_onto_siblings(out)
    raise ValueError(arm)


def _rotate_onto_siblings(out):
    """Move each description onto a SIBLING column of the same table.

    It stays domain-plausible that way, so only the evidence can expose it --
    which is the point: a judge that passes this is not reading the stats.
    """
    groups: dict[str, list[int]] = {}
    tables: list[int] = []
    for i, it in enumerate(out):
        if it["kind"] == "table":
            tables.append(i)
        else:
            groups.setdefault(it["table"], []).append(i)
    for idxs in [*groups.values(), tables]:
        if len(idxs) < 2:
            continue
        descs = [out[i]["description"] for i in idxs]
        for j, i in enumerate(idxs):
            out[i]["description"] = descs[(j + 1) % len(descs)]
    return out


@pytest.mark.parametrize("arm", ["shuffled", "generic", "name_restatement"])
def test_judge_rejects_broken_descriptions(arm):
    """The floor above is only a quality signal if it collapses on bad input.

    Without this, `test_description_judge_floor` could be scoring 0.867 for
    reasons unrelated to description quality, and Spike A would pre-register
    a metric nobody had checked. Measured 2026-10-21: baseline 0.867 against
    0.000 for all three arms -- `shuffled` breaks `correct` (0.911 -> 0.000,
    so the judge does consult the evidence) while `generic` and
    `name_restatement` break `useful` (0.956 -> 0.000) with `correct` intact
    at 1.000. The two axes discriminate independently.

    These are COARSE corruptions. They establish the instrument is live, not
    that it is sensitive to subtle degradation -- for that, the one data point
    is the FK candidate widening, which moved the baseline 0.867 -> 0.778.
    """
    doc, stats = _pipeline("messy_mart")
    judge = AnthropicProvider(model="claude-sonnet-5")
    try:
        results = _judge_run(judge, _corrupt(_judge_items(doc, stats), arm))
    except CassetteMiss as e:
        pytest.skip(str(e))
    ok = sum(1 for v in results if v.get("correct") and v.get("useful"))
    rate = ok / len(results)
    print(f"\n[judge control: {arm}] {ok}/{len(results)} = {rate:.3f}")
    assert rate <= 0.10, [v for v in results if v.get("correct") and v.get("useful")]
