"""`semlayer maintain`: detect, plan, and with --apply re-infer and merge; dry run by default."""

import json
import subprocess
import sys
from pathlib import Path

import duckdb
import yaml

OSS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OSS / "fixtures"))

from semlayer.pipeline import infer  # noqa: E402
from semlayer.source import DuckDBSource  # noqa: E402

CQS = OSS / "fixtures" / "cqs" / "messy_mart.yaml"


def _world(tmp_path):
    import importlib
    db = tmp_path / "mart.duckdb"
    con = duckdb.connect(str(db))
    importlib.import_module("generators.messy_mart").build(con)
    doc = infer(DuckDBSource(con), llm=None)
    con.close()
    layer = tmp_path / "mart.yaml"
    layer.write_text(yaml.safe_dump(doc, sort_keys=False))
    return db, layer


def _run(*args):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "ANTHROPIC_API_KEY": ""}
    return subprocess.run([sys.executable, "-m", "semlayer.cli", "maintain", *args],
                          capture_output=True, text=True, cwd=OSS, env=env)


def test_no_drift_is_exit_zero_and_says_so(tmp_path):
    db, layer = _world(tmp_path)
    r = _run(str(layer), f"duckdb:{db}")
    assert r.returncode == 0, r.stderr
    assert "nothing to do" in r.stdout


def test_dry_run_plans_and_writes_nothing_then_apply_does_it(tmp_path):
    db, layer = _world(tmp_path)
    con = duckdb.connect(str(db))
    con.execute("ALTER TABLE ord_hdr DROP COLUMN tot_amt")
    con.execute("ALTER TABLE ord_hdr ADD COLUMN gift_flg BOOLEAN")
    con.execute("CREATE TABLE loyalty_pts (cust_id VARCHAR, pts INTEGER)")
    con.execute("INSERT INTO loyalty_pts SELECT cust_id, 10 FROM cust_mstr")
    con.close()
    before = layer.read_text()

    r = _run(str(layer), f"duckdb:{db}", "--cqs", str(CQS))
    assert r.returncode == 1, r.stderr                       # drift found, nothing written
    assert "3 change(s)" in r.stdout and "dry run: nothing written" in r.stdout
    assert "re-infer 2 table(s) without an LLM: loyalty_pts, ord_hdr" in r.stdout
    assert "competency question(s) flagged" in r.stdout
    assert layer.read_text() == before

    j = json.loads(_run(str(layer), f"duckdb:{db}", "--json").stdout)
    assert j["applied"] is False and j["plan"] == {"reinfer": ["loyalty_pts", "ord_hdr"],
                                                   "with_llm": False}
    assert {e["kind"] for e in j["events"]} == {"column_dropped", "column_added", "table_added"}
    assert "ord_hdr.tot_amt" in j["changeset"]["orphaned"]

    r = _run(str(layer), f"duckdb:{db}", "--apply", "--json")
    assert r.returncode == 0, r.stderr
    j = json.loads(r.stdout)
    assert j["applied"] is True and j["valid"] is True
    assert "loyalty_pts" in j["merge"]["tables_added"]
    assert "ord_hdr.gift_flg" in j["merge"]["columns_added"]
    assert j["review_queue"]["new"] >= 1
    doc = yaml.safe_load(layer.read_text())["semantic_layer"]
    names = {t["name"] for t in doc["tables"]}
    assert "loyalty_pts" in names
    tot = next(c for t in doc["tables"] if t["name"] == "ord_hdr" for c in t["columns"]
               if c["name"] == "tot_amt")
    assert tot["lifecycle"] == "orphaned"

    # settled: a second run finds nothing
    r = _run(str(layer), f"duckdb:{db}")
    assert r.returncode == 0 and "nothing to do" in r.stdout


def test_json_out_writes_the_record_while_the_report_prints(tmp_path):
    db, layer = _world(tmp_path)
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE loyalty_pts (cust_id VARCHAR, pts INTEGER)")
    con.close()
    out = tmp_path / "maintain.json"
    r = _run(str(layer), f"duckdb:{db}", "--json-out", str(out))
    assert r.returncode == 1 and "dry run" in r.stdout
    j = json.loads(out.read_text())
    assert j["plan"]["reinfer"] == ["loyalty_pts"] and j["applied"] is False


def test_the_workflow_template_is_what_it_says(tmp_path):
    """The scheduled template parses, runs maintain, and builds the PR body from the record."""
    import re
    wf = yaml.safe_load((OSS / "examples" / "maintain" / "semlayer-maintain.yml").read_text())
    assert "schedule" in wf[True] if True in wf else "schedule" in wf["on"]   # PyYAML reads `on:` as True
    assert wf["permissions"] == {"contents": "write", "pull-requests": "write"}
    steps = {s.get("name", ""): s for s in wf["jobs"]["maintain"]["steps"]}
    run = steps["Maintain the layer"]["run"]
    assert "semlayer maintain" in run and "--apply" in run and "--json-out maintain.json" in run
    pr = steps["Open or update the pull request"]["run"]
    assert "gh pr create" in pr and "gh pr edit" in pr and "--force-with-lease" in pr
    # the body-building python, extracted and run against a real record
    import textwrap
    body_py = textwrap.dedent(re.search(r"python - <<'PY'\n(.*?)\nPY\n", pr, re.S).group(1))
    db, layer = _world(tmp_path)
    con = duckdb.connect(str(db))
    con.execute("ALTER TABLE ord_hdr DROP COLUMN tot_amt")
    con.close()
    _run(str(layer), f"duckdb:{db}", "--apply", "--cqs", str(CQS), "--json-out",
         str(tmp_path / "maintain.json"))
    import os
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(body_py, {})
    finally:
        os.chdir(cwd)
    text = buf.getvalue()
    assert "Engine: without an LLM" in text and "Review queue:" in text
    assert "Competency questions flagged:" in text
