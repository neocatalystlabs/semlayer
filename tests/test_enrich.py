"""Enrich stage evals: metric coverage, business-rule discovery, deprecation,
aggregates, and the dbt exporter round-trip (M4)."""

import sys
from pathlib import Path

import duckdb
import pytest
import yaml

OSS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OSS / "fixtures"))

try:
    from dotenv import load_dotenv
    load_dotenv(OSS.parent / ".env")
except ImportError:
    pass

from semlayer.describe import describe_source  # noqa: E402
from semlayer.enrich import enrich_source  # noqa: E402
from semlayer.export.dbt import export_dbt  # noqa: E402
from semlayer.link import link_source  # noqa: E402
from semlayer.llm.provider import AnthropicProvider, CassetteMiss  # noqa: E402
from semlayer.profile.run import profile_with_stats  # noqa: E402
from semlayer.source import DuckDBSource  # noqa: E402
from semlayer.validate import validate_document  # noqa: E402


@pytest.fixture(scope="module")
def enriched():
    import importlib
    mod = importlib.import_module("generators.messy_mart")
    con = duckdb.connect(":memory:")
    mod.build(con)
    llm = AnthropicProvider()
    try:
        doc, stats = profile_with_stats(DuckDBSource(con), llm=llm)
        link_source(DuckDBSource(con), doc, stats, llm=llm)
        describe_source(doc, stats, llm)
        enrich_source(DuckDBSource(con), doc, stats)
    except CassetteMiss as e:
        pytest.skip(str(e))
    finally:
        con.close()
    doc.pop("_link_audit", None)
    return doc


def test_enriched_doc_is_spec_conformant(enriched):
    r = validate_document(enriched)
    assert r.ok, "\n".join(r.errors[:5])


def test_metric_coverage_vs_gold(enriched):
    """All measurable gold metrics covered (measured 4/4 incl. the filtered
    completed-revenue variant via dictionary-joined decodes)."""
    gold = yaml.safe_load((OSS / "fixtures" / "golds" / "messy_mart.yaml").read_text())
    gm = {(m.get("measure"), m.get("agg", "sum"))
          for m in gold["semantic_layer"].get("metrics", []) if m.get("measure")}
    dm = {(m.get("measure"), m.get("agg", "sum"))
          for m in enriched["semantic_layer"].get("metrics", [])}
    covered = len(gm & dm)
    print(f"\n[enrich] metric coverage {covered}/{len(gm)}")
    assert covered == len(gm)
    # the completed-status filtered variant must exist (gold: completed_order_revenue)
    assert any(m.get("filter") and "= 'C'" in m["filter"]
               for m in enriched["semantic_layer"]["metrics"])


def test_business_rule_discovered(enriched):
    """The reconciliation verifier must rediscover the cancelled-exclusion rule,
    scoped to MONETARY metrics (not event counts) + a usage note."""
    ord_hdr = next(t for t in enriched["semantic_layer"]["tables"] if t["name"] == "ord_hdr")
    notes = " ".join(ord_hdr.get("knowledge", {}).get("usage_notes", []))
    assert "sts_cd <> 'X'" in notes
    metrics = enriched["semantic_layer"]["metrics"]
    rev = next(m for m in metrics if m["measure"] == "ord_hdr.tot_amt" and m["agg"] == "sum"
               and "completed" not in m["name"])
    assert rev.get("filter") == "sts_cd <> 'X'"
    counts = [m for m in metrics if m.get("agg") == "count" and m["measure"].startswith("ord_hdr.")]
    assert all(not m.get("filter") for m in counts), "counts must NOT inherit the revenue rule"


def test_aggregates_detected_and_advisory(enriched):
    aggs = enriched["semantic_layer"].get("aggregate_tables", [])
    by_table = {a["table"]: a for a in aggs}
    assert {"dly_sls_agg", "mth_cust_agg"} <= set(by_table)
    for a in aggs:
        assert a["mapping_source"] == "heuristic"
        assert a["routing"]["status"] == "advisory", "heuristic mappings must never verify routing"


def test_deprecation_detected(enriched):
    dep = {t["name"]: t.get("deprecation", {}).get("replacement")
           for t in enriched["semantic_layer"]["tables"] if t.get("lifecycle") == "deprecated"}
    assert dep.get("ord_hdr_legacy") == "ord_hdr"
    assert dep.get("cust_mstr_legacy") == "cust_mstr"


def test_enum_decodes_upgraded_from_dictionary(enriched):
    ord_hdr = next(t for t in enriched["semantic_layer"]["tables"] if t["name"] == "ord_hdr")
    sts = next(c for c in ord_hdr["columns"] if c["name"] == "sts_cd")
    assert sts.get("enum_values"), "sts_cd should carry decodes"
    assert all(e["decode_source"] == "dictionary_join" for e in sts["enum_values"])


def test_routing_avoids_deprecated(enriched):
    routing = enriched["semantic_layer"].get("repo_knowledge", {}).get("routing", [])
    ord_route = next(r for r in routing if "ord hdr" in r["intent"])
    assert any(a["table"] == "ord_hdr_legacy" for a in ord_route.get("avoid", []))


def test_dbt_export_round_trip(enriched):
    exported, losses = export_dbt(enriched)
    # YAML-serializable and structurally sane
    text = yaml.safe_dump(exported, sort_keys=False)
    back = yaml.safe_load(text)
    sms = {m["name"]: m for m in back["semantic_models"]}
    active = [t for t in enriched["semantic_layer"]["tables"]
              if t.get("lifecycle") not in ("deprecated", "orphaned")]
    assert set(sms) == {t["name"] for t in active}
    # measures survive with mapped agg names
    ord_hdr = sms["ord_hdr"]
    assert any(m["name"] == "tot_amt" and m["agg"] == "sum" for m in ord_hdr["measures"])
    assert any(e["type"] == "primary" for e in ord_hdr["entities"])
    # metrics exported with filters intact
    mnames = {m["name"]: m for m in back["metrics"]}
    assert "total_tot_amt_completed" in mnames
    assert "sts_cd" in mnames["total_tot_amt_completed"]["filter"]
    # losses are REPORTED, never silent
    assert any("hierarchies" in l or "repo knowledge" in l for l in losses)
    assert any("confidence" in l for l in losses)
    # deprecated tables excluded AND reported
    assert any("ord_hdr_legacy" in l for l in losses)
