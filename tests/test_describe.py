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


def _pipeline(fixture):
    import importlib
    mod = importlib.import_module(f"generators.{fixture}")
    con = duckdb.connect(":memory:")
    mod.build(con)
    llm = AnthropicProvider()
    try:
        doc, stats = profile_with_stats(DuckDBSource(con), llm=llm)
        link_source(DuckDBSource(con), doc, stats, llm=llm)
        describe_source(doc, stats, llm)
    except CassetteMiss as e:
        pytest.skip(str(e))
    finally:
        con.close()
    doc.pop("_link_audit", None)
    return doc, stats


def test_describe_full_coverage_and_conformance():
    doc, _ = _pipeline("messy_mart")
    assert validate_document(doc).ok
    tables = doc["semantic_layer"]["tables"]
    assert all(t.get("description") for t in tables), "every table described"
    missing = [(t["name"], c["name"]) for t in tables
               for c in t["columns"] if not c.get("description")]
    assert not missing, f"undescribed columns: {missing[:5]}"


def test_description_judge_floor():
    """Sonnet-judged correct+useful >= 0.80 (M3 target; measured 0.889)."""
    doc, stats = _pipeline("messy_mart")
    rng = random.Random(42)  # MUST match the recording run for cassette hits
    sampled = rng.sample(doc["semantic_layer"]["tables"], 12)
    judge = AnthropicProvider(model="claude-sonnet-5")
    items, results = [], []
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
    try:
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
