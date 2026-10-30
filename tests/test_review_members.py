"""On a hub, --members asks each question once across the set and writes to the member files."""

import json
import subprocess
import sys
from pathlib import Path

import yaml

OSS = Path(__file__).resolve().parent.parent


def _member(name, table):
    return {"semantic_layer": {
        "spec_version": "0.6.0", "name": name, "warehouse": "snowflake:ANALYTICS",
        "tables": [{"name": table, "source": f"{name}.{table}", "primary_key": ["id"],
                    "columns": [
                        {"name": "id", "sql_type": "INTEGER", "semantic_type": "identifier",
                         "entity_role": "primary_key"},
                        {"name": "region_name", "sql_type": "VARCHAR", "semantic_type": "name",
                         "entity_role": "dimension", "confidence": 0.65, "lifecycle": "inferred",
                         "provenance": [{"signal": "naming", "detail": "name rule -> name"}]},
                    ]}],
    }}


def _estate(tmp_path):
    (tmp_path / "finance.yaml").write_text(yaml.safe_dump(_member("finance", "dim_cost_center")))
    (tmp_path / "sales.yaml").write_text(yaml.safe_dump(_member("sales", "dim_territory")))
    hub = {"semantic_layer": {"spec_version": "0.6.0", "name": "acme",
                              "members": [{"name": "finance", "path": "finance.yaml"},
                                          {"name": "sales", "path": "sales.yaml"}]}}
    p = tmp_path / "acme.yaml"
    p.write_text(yaml.safe_dump(hub))
    return p


def _run(*args):
    return subprocess.run([sys.executable, "-m", "semlayer.cli", "review", *args],
                          capture_output=True, text=True, cwd=OSS)


def test_without_members_a_hub_asks_only_its_own_questions(tmp_path):
    hub = _estate(tmp_path)
    r = _run(str(hub), "--list")
    assert r.returncode == 0, r.stderr
    assert "review queue empty" in r.stdout


def test_with_members_one_question_covers_both_files_and_a_verdict_reaches_each(tmp_path):
    hub = _estate(tmp_path)
    j = json.loads(_run(str(hub), "--members", "--list", "--json").stdout)
    assert len(j["questions"]) == 1
    q = j["questions"][0]
    assert [o["where"] for o in q["objects"]] == ["finance/dim_cost_center.region_name",
                                                  "sales/dim_territory.region_name"]
    r = _run(str(hub), "--members", "--answer", f"{q['key']}=accept", "--by", "raj")
    assert r.returncode == 0, r.stderr
    assert "2 of 2 column(s)" in r.stdout
    assert "finance.yaml" in r.stdout and "sales.yaml" in r.stdout
    for m in ("finance", "sales"):
        doc = yaml.safe_load((tmp_path / f"{m}.yaml").read_text())
        col = doc["semantic_layer"]["tables"][0]["columns"][1]
        assert col["lifecycle"] == "reviewed"
        assert col["provenance"][-1]["detail"].startswith("accepted: holds the display name")
    # the hub file itself gained no tables
    assert "tables" not in yaml.safe_load(hub.read_text())["semantic_layer"]
    # and the decisions listing across the set names the member in each address
    d = _run(str(hub), "--members", "--decisions").stdout
    assert "finance/dim_cost_center.region_name" in d and "sales/dim_territory.region_name" in d
    assert "2 decisions: 2 accepted" in d
    # a second listing finds nothing left to ask
    assert "review queue empty" in _run(str(hub), "--members", "--list").stdout


def test_members_flag_is_refused_on_a_plain_document(tmp_path):
    p = tmp_path / "finance.yaml"
    p.write_text(yaml.safe_dump(_member("finance", "dim_cost_center")))
    r = _run(str(p), "--members", "--list")
    assert r.returncode != 0 and "not a hub" in (r.stderr + r.stdout)
