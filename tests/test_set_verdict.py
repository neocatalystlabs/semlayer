"""`set:<value>` replaces the engine's claim with the reviewer's, in spec vocabulary only."""

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
            {"name": "dim_campaign", "source": "shop.dim_campaign", "primary_key": ["sk_campaign"],
             "columns": [
                 {"name": "sk_campaign", "sql_type": "INTEGER", "semantic_type": "quantity",
                  "entity_role": "measure", "confidence": 0.15, "lifecycle": "inferred",
                  "aggregations": {"allowed": ["sum"], "default": "sum"},
                  "provenance": [{"signal": "statistic", "detail": "integer fallback"}]},
                 {"name": "owner_email", "sql_type": "VARCHAR", "semantic_type": "code",
                  "entity_role": "dimension", "confidence": 0.6, "lifecycle": "inferred",
                  "provenance": [{"signal": "naming", "detail": "name rule -> code"}]}]},
            {"name": "fct_orders_hist", "source": "shop.fct_orders_hist", "primary_key": ["id"],
             "table_type": "snapshot_scd2", "confidence": 0.35, "lifecycle": "inferred",
             "provenance": [{"signal": "naming",
                             "detail": "table_type: snapshot naming/flag (no validity columns)"}],
             "columns": [{"name": "id", "sql_type": "INTEGER", "semantic_type": "identifier",
                          "entity_role": "primary_key"},
                         {"name": "amount", "sql_type": "DECIMAL", "semantic_type": "monetary_value",
                          "entity_role": "measure",
                          "aggregations": {"allowed": ["sum"], "default": "sum"}}]},
            {"name": "agg_orders_monthly", "source": "shop.agg_orders_monthly",
             "table_type": "aggregate",
             "columns": [{"name": "amount", "sql_type": "DECIMAL", "semantic_type": "monetary_value",
                          "entity_role": "measure",
                          "aggregations": {"allowed": ["sum"], "default": "sum"}}]},
        ],
        "metrics": [{"name": "total_amount", "type": "simple", "measure": "agg_orders_monthly.amount",
                     "agg": "sum", "lifecycle": "inferred", "confidence": 0.7, "provenance": []}],
    }}


def _q(asked, pred):
    return next(q for q in asked if pred(q))


def test_set_replaces_a_column_claim_and_drops_confidence_and_aggregations():
    d = _doc()
    asked, _ = review.group(review.collect(d), d)
    q = _q(asked, lambda q: "count or quantity" in q.what)
    assert review.check_set(q, "identifier/primary_key", d) is None
    assert "types:" in review.check_set(q, "identifier", d)
    assert "types:" in review.check_set(q, "widget/primary_key", d)
    review.set_author("raj")
    try:
        assert review.answer(d, q, "set:identifier/primary_key") == 1
    finally:
        review.set_author(None)
    c = d["semantic_layer"]["tables"][0]["columns"][0]
    assert (c["semantic_type"], c["entity_role"], c["lifecycle"]) == ("identifier", "primary_key",
                                                                        "reviewed")
    assert "confidence" not in c and "aggregations" not in c
    assert c["provenance"][-1]["detail"] == (
        "set: holds a count or quantity; summed (type quantity) -> identifies a row; "
        "the table's key (type identifier)")
    assert validate_document(d).ok
    # pii types set the flag; the question has left the queue
    q2 = _q(review.group(review.collect(d), d)[0], lambda q: "coded value" in q.what)
    review.answer(d, q2, "set:pii_email/dimension")
    assert d["semantic_layer"]["tables"][0]["columns"][1]["pii"] is True
    assert not review.group(review.collect(d), d)[0] or all(
        "quantity" not in q.what and "coded" not in q.what for q in review.group(review.collect(d), d)[0])


def test_set_replaces_a_table_type_and_a_metrics_base_and_is_refused_elsewhere():
    d = _doc()
    asked, _ = review.group(review.collect(d), d)
    tq = _q(asked, lambda q: q.noun == "table")
    assert "one of:" in review.check_set(tq, "snapshot", d)
    review.answer(d, tq, "set:fact")
    t = next(t for t in d["semantic_layer"]["tables"] if t["name"] == "fct_orders_hist")
    assert t["table_type"] == "fact" and "confidence" not in t
    assert t["provenance"][-1]["detail"].startswith("set: holds a dated version")
    mq = _q(asked, lambda q: q.kind == "metric_plausibility")
    assert "no such table" in review.check_set(mq, "base=nowhere", d)
    assert "has no column" in review.check_set(mq, "base=dim_campaign", d)
    assert review.check_set(mq, "base=fct_orders_hist", d) is None
    review.answer(d, mq, "set:base=fct_orders_hist")
    m = d["semantic_layer"]["metrics"][0]
    assert m["measure"] == "fct_orders_hist.amount" and m["lifecycle"] == "reviewed"
    assert "confidence" not in m
    assert validate_document(d).ok
    rows = review.decisions(d)
    assert {r.verdict for r in rows} == {"set"} and len(rows) == 2
    # a yes-or-no question offers no set
    fake = review.Question("q-x", "seam_edge", "x", "", "", None, tq.items, "", "")
    assert review.set_shape(fake) is None and "not available" in review.check_set(fake, "fact", d)


def test_cli_answers_with_set_and_refuses_a_value_outside_the_vocabulary(tmp_path):
    p = tmp_path / "shop.yaml"
    p.write_text(yaml.safe_dump(_doc(), sort_keys=False))
    run = lambda *a: subprocess.run([sys.executable, "-m", "semlayer.cli", "review", str(p), *a],  # noqa: E731
                                    capture_output=True, text=True, cwd=OSS)
    j = json.loads(run("--list", "--json").stdout)
    col = next(q for q in j["questions"] if "count or quantity" in q["claim"])
    assert col["set_accepts"] == "<semantic_type>/<entity_role>"
    assert "set <semantic_type>/<entity_role> -> replaces the claim" in run("--list").stdout
    before = p.read_text()
    bad = run("--answer", f"{col['key']}=set:identifier", "--by", "raj")
    assert bad.returncode != 0 and "types:" in (bad.stderr + bad.stdout) and p.read_text() == before
    ok = run("--answer", f"{col['key']}=set:identifier/primary_key", "--by", "raj")
    assert ok.returncode == 0, ok.stderr
    assert f"set question {col['n']}" in ok.stdout
    dec = run("--decisions").stdout
    assert "set       dim_campaign.sk_campaign" in dec and "1 set." in dec
