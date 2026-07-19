"""Generic gold-CQ runner (test plan Layer 5, gold-anchor half).

For every CQ suite in fixtures/cqs/:
- validates against cq-suite.schema.json
- builds its fixture in :memory:
- executes expected_sql (must succeed, non-null for scalars)
- where wrong_sql exists, asserts its result differs materially (canary holds)
"""

import json
import sys
from pathlib import Path

import duckdb
import jsonschema
import pytest
import yaml

OSS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OSS / "fixtures"))

CQ_FILES = sorted((OSS / "fixtures" / "cqs").glob("*.yaml"))
CQ_SCHEMA = json.loads((OSS / "spec" / "cq-suite.schema.json").read_text())

_fixture_cache: dict[str, duckdb.DuckDBPyConnection] = {}


def _connect(fixture: str) -> duckdb.DuckDBPyConnection:
    if fixture not in _fixture_cache:
        import importlib
        mod = importlib.import_module(f"generators.{fixture}")
        con = duckdb.connect(":memory:")
        mod.build(con)
        _fixture_cache[fixture] = con
    return _fixture_cache[fixture]


def _suites():
    for f in CQ_FILES:
        yield f, yaml.safe_load(f.read_text())


@pytest.mark.parametrize("path", CQ_FILES, ids=[f.stem for f in CQ_FILES])
def test_suite_schema_valid(path):
    doc = yaml.safe_load(path.read_text())
    jsonschema.Draft202012Validator(CQ_SCHEMA).validate(doc)
    suite = doc["cq_suite"]
    ids = [q["id"] for q in suite["questions"]]
    assert len(ids) == len(set(ids)), "duplicate CQ ids"
    for q in suite["questions"]:
        if q["expected_kind"] in ("scalar", "rows"):
            assert "expected_sql" in q, f"{q['id']}: scalar/rows CQ requires expected_sql"


def _all_executable_cqs():
    out = []
    for path, doc in _suites():
        suite = doc["cq_suite"]
        for q in suite["questions"]:
            if "expected_sql" in q and "duckdb" in q["expected_sql"]:
                out.append(pytest.param(suite["fixture"], q, id=q["id"]))
    return out


@pytest.mark.parametrize("fixture,q", _all_executable_cqs())
def test_expected_sql_executes(fixture, q):
    con = _connect(fixture)
    rows = con.execute(q["expected_sql"]["duckdb"]).fetchall()
    assert rows, f"{q['id']}: expected_sql returned no rows"
    if q["expected_kind"] == "scalar":
        assert len(rows) == 1 and rows[0][0] is not None, f"{q['id']}: scalar CQ must return one non-null value"


def _all_canary_cqs():
    out = []
    for path, doc in _suites():
        suite = doc["cq_suite"]
        for q in suite["questions"]:
            if "wrong_sql" in q and "duckdb" in q.get("wrong_sql", {}):
                out.append(pytest.param(suite["fixture"], q, id=f"canary-{q['id']}"))
    return out


@pytest.mark.parametrize("fixture,q", _all_canary_cqs())
def test_canary_wrong_sql_diverges(fixture, q):
    """The known-wrong formulation must produce a materially different answer."""
    con = _connect(fixture)
    tol = q.get("tolerance", 0.01)
    expected = con.execute(q["expected_sql"]["duckdb"]).fetchall()
    wrong = con.execute(q["wrong_sql"]["duckdb"]).fetchall()
    if q["expected_kind"] == "scalar":
        e, w = float(expected[0][0]), float(wrong[0][0])
        assert abs(e - w) > max(tol * 10, abs(e) * 0.01), (
            f"{q['id']}: wrong_sql did not diverge (expected={e}, wrong={w}) — canary is dead"
        )
    else:
        assert expected != wrong, f"{q['id']}: wrong_sql produced identical rows — canary is dead"
