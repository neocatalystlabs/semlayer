"""Verdicts are written in words with a date and author, and read back by --decisions."""

import copy
import json
import subprocess
import sys
from pathlib import Path

import yaml

from semlayer import review
from semlayer.validate import validate_document

OSS = Path(__file__).resolve().parent.parent


def _doc():
    return {"semantic_layer": {
        "spec_version": "0.6.0", "name": "shop", "warehouse": "duckdb:shop",
        "tables": [
            {"name": "fct_orders_hist", "source": "shop.fct_orders_hist",
             "primary_key": ["order_id"], "table_type": "snapshot_scd2",
             "confidence": 0.35, "lifecycle": "inferred",
             "provenance": [{"signal": "naming",
                             "detail": "table_type: snapshot naming/flag (no validity columns)"}],
             "columns": [
                 {"name": "order_id", "sql_type": "INTEGER", "semantic_type": "identifier",
                  "entity_role": "primary_key"},
                 {"name": "channel_name", "sql_type": "VARCHAR", "semantic_type": "code",
                  "entity_role": "dimension", "confidence": 0.6, "lifecycle": "inferred",
                  "provenance": [{"signal": "naming", "detail": "name rule -> code"}]},
                 {"name": "legacy_flag", "sql_type": "VARCHAR", "semantic_type": "flag",
                  "entity_role": "dimension", "lifecycle": "reviewed",
                  "provenance": [{"signal": "human", "detail": "review: accept"}]},
             ]},
        ],
    }}


def test_a_verdict_is_written_in_words_with_date_and_author():
    d = _doc()
    review.set_author("raj")
    try:
        items = review.collect(d)
        col = next(i for i in items if i.column == "channel_name")
        tbl = next(i for i in items if i.column is None and i.table == "fct_orders_hist")
        review.apply(d, col, "reject")
        review.apply(d, tbl, "accept")
    finally:
        review.set_author(None)
    t = d["semantic_layer"]["tables"][0]
    c = next(c for c in t["columns"] if c["name"] == "channel_name")
    human = next(p for p in c["provenance"] if p["signal"] == "human")
    assert human["by"] == "raj" and len(human["date"]) == 10
    assert human["detail"] == "rejected: holds a short coded value; grouped by (type code); now untyped"
    assert c["semantic_type"] == "unknown"
    th = next(p for p in t["provenance"] if p["signal"] == "human")
    assert th["detail"].startswith("accepted: holds a dated version of each record per row")
    assert validate_document(d).ok


def test_decisions_reads_verdicts_back_newest_first_and_names_legacy_lines():
    d = _doc()
    items = review.collect(d)
    review.apply(d, next(i for i in items if i.column == "channel_name"), "reject")
    rows = review.decisions(d)
    assert [r.where for r in rows] == ["fct_orders_hist.channel_name", "fct_orders_hist.legacy_flag"]
    new, old = rows
    assert new.verdict == "rejected" and new.date and new.what.startswith("holds a short coded value")
    assert old.verdict == "accepted" and old.date is None and "claim not recorded" in old.what


def test_cli_decisions_prints_and_json(tmp_path):
    d = _doc()
    review.apply(d, next(i for i in review.collect(d) if i.column == "channel_name"), "accept")
    p = tmp_path / "shop.yaml"
    p.write_text(yaml.safe_dump(d, sort_keys=False))
    r = subprocess.run([sys.executable, "-m", "semlayer.cli", "review", str(p), "--decisions"],
                       capture_output=True, text=True, cwd=OSS)
    assert r.returncode == 0, r.stderr
    assert "accepted  fct_orders_hist.channel_name" in r.stdout
    assert "(no author recorded)" in r.stdout  # the legacy line
    assert "2 decisions: 2 accepted, 0 rejected.  Undecided items in the queue: 1" in r.stdout
    j = subprocess.run([sys.executable, "-m", "semlayer.cli", "review", str(p), "--decisions",
                        "--json"], capture_output=True, text=True, cwd=OSS)
    rows = json.loads(j.stdout)
    assert {r["verdict"] for r in rows} == {"accepted"}


def test_rejecting_an_untyped_column_says_there_was_nothing_to_reject():
    d = _doc()
    c = d["semantic_layer"]["tables"][0]["columns"][1]
    c["semantic_type"] = "unknown"
    c["confidence"] = 0.05
    it = next(i for i in review.collect(d) if i.column == "channel_name")
    review.apply(d, it, "reject")
    assert c["provenance"][-1]["detail"] == (
        "rejected: no type could be inferred for this column; still untyped")


def test_accept_and_reject_do_not_disturb_other_appliers():
    """Every applier writes an `accepted:`/`rejected:` line (no legacy text left)."""
    d = _doc()
    sl = d["semantic_layer"]
    sl["metrics"] = [{"name": "orders", "type": "simple", "measure": "fct_orders_hist.order_id",
                      "agg": "count", "lifecycle": "inferred", "provenance": []}]
    items = review.collect(copy.deepcopy(d))
    mp = next(i for i in items if i.kind == "metric_plausibility")
    review.apply(d, mp, "reject")
    m = sl["metrics"][0]
    assert m["lifecycle"] == "deprecated"
    assert m["provenance"][-1]["detail"] == (
        "rejected: is a metric over fct_orders_hist; metric deprecated")
    assert all(r.verdict in ("accepted", "rejected") for r in review.decisions(d))


def test_an_etl_bookkeeping_claim_is_a_claim_not_an_absence():
    d = _doc()
    c = d["semantic_layer"]["tables"][0]["columns"][1]
    c.update(semantic_type="unknown", entity_role="metadata", confidence=0.6)
    it = next(i for i in review.collect(d) if i.column == "channel_name")
    review.apply(d, it, "accept")
    assert c["provenance"][-1]["detail"] == (
        "accepted: is an ETL bookkeeping column, not for analysis (type unknown)")
