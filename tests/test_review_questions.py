"""The queue is questions: identical claim + identical reason = one question; agents answer by key."""

import json
import subprocess
import sys
from pathlib import Path

import yaml

from semlayer import review
from semlayer.validate import validate_document

OSS = Path(__file__).resolve().parent.parent


def _col(name, stype, role, conf, reason, sql="VARCHAR"):
    return {"name": name, "sql_type": sql, "semantic_type": stype, "entity_role": role,
            "confidence": conf, "lifecycle": "inferred",
            "provenance": [{"signal": "naming", "detail": reason}]}


def _doc():
    def table(name, cols, **extra):
        return {"name": name, "source": f"shop.{name}", "primary_key": [cols[0]["name"]],
                "columns": cols, **extra}
    pk = {"name": "id", "sql_type": "INTEGER", "semantic_type": "identifier",
          "entity_role": "primary_key"}
    return {"semantic_layer": {
        "spec_version": "0.6.0", "name": "shop", "warehouse": "duckdb:shop",
        "tables": [
            table("dim_account", [pk, _col("account_name", "name", "dimension", 0.65,
                                          "name rule -> name"),
                                  _col("src_system", "unknown", "dimension", 0.05, "no rule matched")]),
            table("dim_store", [pk, _col("store_name", "name", "dimension", 0.65, "name rule -> name"),
                                _col("src_system", "unknown", "dimension", 0.05, "no rule matched")]),
            table("dim_channel", [pk, _col("channel_name", "code", "dimension", 0.6,
                                          "name rule -> code")]),
            table("fct_orders_hist", [pk], table_type="snapshot_scd2", confidence=0.35,
                  lifecycle="inferred",
                  provenance=[{"signal": "naming",
                               "detail": "table_type: snapshot naming/flag (no validity columns)"}]),
            table("int__orders_hist", [pk], table_type="snapshot_scd2", confidence=0.35,
                  lifecycle="inferred",
                  provenance=[{"signal": "naming",
                               "detail": "table_type: snapshot naming/flag (no validity columns)"}]),
        ],
    }}


def test_identical_claim_and_reason_make_one_question_and_untyped_is_not_asked():
    d = _doc()
    asked, not_asked = review.group(review.collect(d), d)
    by_kind = {(q.kind, q.what): q for q in asked}
    names = by_kind[("low_confidence", "holds the display name of the row; grouped by (type name)")]
    assert names.objects == ["dim_account.account_name", "dim_store.store_name"]
    assert names.reason == "the column name ends in _name" and names.confidence == 0.65
    code = by_kind[("low_confidence", "holds a short coded value; grouped by (type code)")]
    assert code.objects == ["dim_channel.channel_name"]      # a different claim: its own question
    snaps = next(q for q in asked if q.noun == "table")
    assert len(snaps.items) == 2 and snaps.distinct_names() == ["orders_hist"]
    assert len(asked) == 3
    untyped = next(q for q in not_asked if q.kind == "untyped")
    assert not untyped.decidable and len(untyped.items) == 2
    assert "src_system (2 tables)" in untyped.reason
    assert [q.n for q in asked + not_asked] == [1, 2, 3, 4]


def test_a_question_key_is_stable_across_runs_and_documents():
    a, _ = review.group(review.collect(_doc()), _doc())
    b, _ = review.group(review.collect(_doc()), _doc())
    assert [q.key for q in a] == [q.key for q in b]
    assert all(q.key.startswith("q-") and len(q.key) == 10 for q in a)


def test_answering_a_question_reaches_every_object_except_the_excluded_ones():
    d = _doc()
    asked, _ = review.group(review.collect(d), d)
    names = next(q for q in asked if q.noun == "column" and "display name" in q.what)
    n = review.answer(d, names, "accept", exclude={"dim_store.store_name"})
    assert n == 1
    acc = d["semantic_layer"]["tables"][0]["columns"][1]
    skipped = d["semantic_layer"]["tables"][1]["columns"][1]
    assert acc["lifecycle"] == "reviewed" and skipped.get("lifecycle") == "inferred"
    assert acc["provenance"][-1]["detail"].startswith("accepted: holds the display name")
    assert validate_document(d).ok
    # a verdict on a not-asked question changes nothing
    _, not_asked = review.group(review.collect(d), d)
    assert review.answer(d, not_asked[0], "reject") == 0


def test_an_agent_verdict_is_signed_agent_and_a_note_is_kept_on_the_document():
    d = _doc()
    asked, not_asked = review.group(review.collect(d), d)
    review.set_author("maintainer/claude-opus-5-5", "agent")
    try:
        snaps = next(q for q in asked if q.noun == "table")
        review.answer(d, snaps, "accept")
        review.note(d, not_asked[0], "no rule names these; the dbt project has no description")
    finally:
        review.set_author(None)
    t = next(t for t in d["semantic_layer"]["tables"] if t["name"] == "fct_orders_hist")
    p = t["provenance"][-1]
    assert p["signal"] == "agent" and p["by"] == "maintainer/claude-opus-5-5"
    note = d["semantic_layer"]["review_notes"][0]
    assert note["signal"] == "agent" and note["question"] == not_asked[0].key
    assert validate_document(d).ok
    rows = review.decisions(d)
    assert {r.signal for r in rows} == {"agent"} and len(rows) == 2
    # the note shows up on its question next time
    _, na = review.group(review.collect(d), d)
    assert na[0].notes and na[0].notes[0]["note"].startswith("no rule names")


def _run(*args):
    return subprocess.run([sys.executable, "-m", "semlayer.cli", "review", *args],
                          capture_output=True, text=True, cwd=OSS)


def test_cli_lists_questions_answers_by_key_and_speaks_json(tmp_path):
    p = tmp_path / "shop.yaml"
    p.write_text(yaml.safe_dump(_doc(), sort_keys=False))
    r = _run(str(p), "--list")
    assert r.returncode == 0, r.stderr
    assert "3 questions" in r.stdout and "Shown, not asked:" in r.stdout
    assert "engine saw: the column name ends in _name; confidence 0.65" in r.stdout
    assert "name rule" not in r.stdout.split("key:")[0]      # no engine strings before the key
    j = json.loads(_run(str(p), "--list", "--json").stdout)
    assert j["undecided_items"] == 7 and len(j["questions"]) == 3
    q = j["questions"][0]
    assert {"n", "key", "kind", "claim", "engine_saw", "engine_saw_raw", "confidence", "objects",
            "on_accept", "on_reject", "distinct_names"} <= set(q)
    assert q["engine_saw_raw"] == "name rule -> name"        # the raw string survives for machines
    one = _run(str(p), "--list", "--question", q["key"]).stdout
    assert "   1. dim_account.account_name" in one and "   2. dim_store.store_name" in one
    a = _run(str(p), "--answer", f"{q['key']}=accept,2=reject", "--exclude", "1=dim_store.store_name",
             "--by", "raj")
    assert a.returncode == 0, a.stderr
    assert "accepted question 1" in a.stdout and "1 of 2 column(s)" in a.stdout
    dec = _run(str(p), "--decisions").stdout
    assert "accepted  dim_account.account_name" in dec and "dim_store" not in dec
    ag = _run(str(p), "--agent", "--by", "bot", "--note", "3=cannot tell without the load job")
    assert ag.returncode == 0, ag.stderr
    assert "noted question 3" in ag.stdout
    assert "note (bot," in _run(str(p), "--list").stdout
    bad = _run(str(p), "--agent", "--answer", "3=accept")
    assert bad.returncode != 0 and "--by" in (bad.stderr + bad.stdout)
    # an exclusion naming an object outside the question is refused, and nothing is written
    before = p.read_text()
    code = next(x for x in j["questions"] if "coded value" in x["claim"])["key"]
    bad = _run(str(p), "--answer", f"{code}=accept", "--exclude",
               f"{code}=dim_account.account_name", "--by", "raj")
    assert bad.returncode != 0 and "not in question" in (bad.stderr + bad.stdout)
    assert p.read_text() == before


def test_infer_warns_before_overwriting_a_file_with_verdicts(tmp_path):
    import argparse
    import contextlib
    import io

    from semlayer.cli import _warn_if_overwriting_verdicts
    p = tmp_path / "layer.yaml"
    p.write_text("semantic_layer:\n  tables: []\n  x:\n  - signal: human\n    detail: 'accepted: y'\n")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        _warn_if_overwriting_verdicts(argparse.Namespace(into=None, out=str(p)))
    assert "1 recorded verdict(s)" in err.getvalue() and "--into" in err.getvalue()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        _warn_if_overwriting_verdicts(argparse.Namespace(into=str(p), out=str(p)))
    assert err.getvalue() == ""


def test_decisions_prints_one_line_for_a_verdict_given_to_many_objects(tmp_path):
    d = _doc()
    for t in d["semantic_layer"]["tables"][:2]:
        for k in range(5):
            t["columns"].append(_col(f"x{k}_name", "name", "dimension", 0.65, "name rule -> name"))
    asked, _ = review.group(review.collect(d), d)
    names = next(q for q in asked if "display name" in q.what)
    assert len(names.items) == 12
    review.set_author("raj")
    try:
        review.answer(d, names, "accept")
    finally:
        review.set_author(None)
    p = tmp_path / "shop.yaml"
    p.write_text(yaml.safe_dump(d, sort_keys=False))
    out = _run(str(p), "--decisions").stdout
    assert "accepted  12 objects: holds the display name of the row; grouped by (type name)" in out
    assert "... and 8 more" in out
    assert len(json.loads(_run(str(p), "--decisions", "--json").stdout)) == 12


def test_a_measured_number_in_the_reason_does_not_split_the_question():
    d = _doc()
    cols = d["semantic_layer"]["tables"][0]["columns"]
    cols.append(_col("n_a", "quantity", "dimension", 0.2,
                     "integer with 5 distinct values: grouped, not summed", sql="INTEGER"))
    cols.append(_col("n_b", "quantity", "dimension", 0.2,
                     "integer with 22 distinct values: grouped, not summed", sql="INTEGER"))
    asked, _ = review.group(review.collect(d), d)
    q = next(q for q in asked if "count or quantity" in q.what)
    assert len(q.items) == 2
    assert q.reason == ("an integer column with only a few distinct values, so grouped rather "
                        "than summed")
    assert q.reason_raw.startswith("integer with 5 distinct values")   # the first object's own
