"""M4 part 2: review workflow + MCP server query functions + end-to-end CLI."""

import subprocess
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

from semlayer import mcp_server, review  # noqa: E402
from semlayer.llm.provider import AnthropicProvider, CassetteMiss  # noqa: E402
from semlayer.pipeline import infer  # noqa: E402
from semlayer.source import DuckDBSource  # noqa: E402
from semlayer.validate import validate_document  # noqa: E402


@pytest.fixture(scope="module")
def doc():
    import importlib
    mod = importlib.import_module("generators.messy_mart")
    con = duckdb.connect(":memory:")
    mod.build(con)
    try:
        d = infer(DuckDBSource(con), llm=AnthropicProvider())
    except CassetteMiss as e:
        pytest.skip(str(e))
    finally:
        con.close()
    return d


# ------------------------------------------------------------------ review

def test_review_queue_collects_expected_kinds(doc):
    items = review.collect(doc)
    kinds = {i.kind for i in items}
    print(f"\n[review] {len(items)} items, kinds={sorted(kinds)}")
    assert items, "messy_mart must produce review items"
    assert "low_confidence" in kinds


def test_review_accept_is_sticky(doc):
    import copy
    d = copy.deepcopy(doc)
    items = review.collect(d)
    it = next(i for i in items if i.kind == "low_confidence" and i.column)
    review.apply(d, it, "accept")
    t = next(x for x in d["semantic_layer"]["tables"] if x["name"] == it.table)
    c = next(x for x in t["columns"] if x["name"] == it.column)
    assert c["lifecycle"] == "reviewed"
    assert any(p["signal"] == "human" for p in c["provenance"])
    assert validate_document(d).ok
    # accepted items leave the queue
    assert it.key not in {i.key for i in review.collect(d)}


def test_review_queues_metric_tier_claims(doc):
    """Metrics and aggregate mappings reach the queue, by claim kind not confidence."""
    kinds = {i.kind for i in review.collect(doc)}
    assert "discovered_filter" in kinds, "the reconciliation-discovered rule must be reviewable"
    assert "aggregate_mapping" in kinds, "a heuristic aggregate must ask for a verdict"


def test_accepting_aggregate_mapping_records_the_verdict(doc):
    """Accept promotes lifecycle only — SPEC 2.7 still reserves verified routing."""
    import copy
    d = copy.deepcopy(doc)
    it = next(i for i in review.collect(d) if i.kind == "aggregate_mapping")
    agg = next(a for a in d["semantic_layer"]["aggregate_tables"]
               if f"aggregate_tables.{a['table']}" == it.ref)
    review.apply(d, it, "accept")
    assert agg["lifecycle"] == "reviewed"
    assert agg["mapping_source"] == "heuristic", "promotion is a spec question, not settled here"
    assert agg["routing"]["status"] == "advisory"
    assert any(p["signal"] == "human" for p in agg["provenance"])
    assert validate_document(d).ok, validate_document(d).errors
    assert it.key not in {i.key for i in review.collect(d)}


def test_rejecting_aggregate_mapping_stops_routing_to_it(doc):
    import copy
    d = copy.deepcopy(doc)
    it = next(i for i in review.collect(d) if i.kind == "aggregate_mapping")
    table = it.ref.split(".", 1)[1]
    review.apply(d, it, "reject")
    agg = next(a for a in d["semantic_layer"]["aggregate_tables"] if a["table"] == table)
    assert agg["lifecycle"] == "deprecated"
    assert agg["routing"]["status"] == "advisory"
    routes = d["semantic_layer"]["repo_knowledge"]["routing"]
    assert not any(table in (r.get("use") or []) for r in routes)
    assert validate_document(d).ok, validate_document(d).errors


def test_rejecting_discovered_filter_drops_it_everywhere(doc):
    """The metric filter and the table rule are one claim; one verdict settles both."""
    import copy
    d = copy.deepcopy(doc)
    it = next(i for i in review.collect(d) if i.kind == "discovered_filter")
    m = next(x for x in d["semantic_layer"]["metrics"]
             if f"metrics.{x['name']}" == it.ref)
    expr, base = m["filter"], it.table
    review.apply(d, it, "reject")
    assert "filter" not in m
    t = next(x for x in d["semantic_layer"]["tables"] if x["name"] == base)
    rfs = (t.get("knowledge") or {}).get("required_filters", [])
    assert not any(f["expr"] == expr for f in rfs)
    assert validate_document(d).ok, validate_document(d).errors


def test_discovered_filter_verdict_is_idempotent(doc):
    """A second verdict on a rule already removed must not crash or re-remove."""
    import copy
    d = copy.deepcopy(doc)
    it = next(i for i in review.collect(d) if i.kind == "discovered_filter")
    review.apply(d, it, "reject")
    review.apply(d, it, "reject")   # the filter is gone; nothing left to move
    m = next(x for x in d["semantic_layer"]["metrics"] if f"metrics.{x['name']}" == it.ref)
    assert "filter" not in m
    assert validate_document(d).ok, validate_document(d).errors


def test_accepting_discovered_filter_keeps_it_and_promotes(doc):
    import copy
    d = copy.deepcopy(doc)
    it = next(i for i in review.collect(d) if i.kind == "discovered_filter")
    m = next(x for x in d["semantic_layer"]["metrics"] if f"metrics.{x['name']}" == it.ref)
    expr = m["filter"]
    review.apply(d, it, "accept")
    assert m["filter"] == expr
    assert m["lifecycle"] == "reviewed"
    assert validate_document(d).ok, validate_document(d).errors


def test_drift_stops_an_aggregate_claiming_it_reconciles(doc):
    """The mapping stands; the stale measurement behind it does not."""
    import copy

    from semlayer import drift as drift_mod
    d = copy.deepcopy(doc)
    agg = next(a for a in d["semantic_layer"]["aggregate_tables"]
               if a.get("consistency", {}).get("status") == "consistent")
    ev = drift_mod.DriftEvent(kind="column_dropped", table=agg["aggregates"],
                              column=_mapped_column(agg), detail="")
    cs = drift_mod.apply_drift(d, [ev])
    assert agg["consistency"]["status"] == "unverified"
    assert agg["routing"]["status"] == "advisory"
    assert agg["measure_mappings"], "the mapping itself is never rewritten"
    assert f"aggregate_tables.{agg['table']}" in cs.demoted
    assert validate_document(d).ok, validate_document(d).errors


def _mapped_column(agg: dict) -> str:
    """The base column named inside the aggregate's measure mapping."""
    import re
    src = str(agg["measure_mappings"][0]["source"])
    return re.search(r"SUM\((\w+)\)", src).group(1)


def test_review_reject_removes_claim_not_column(doc):
    import copy
    d = copy.deepcopy(doc)
    items = review.collect(d)
    it = next(i for i in items if i.kind == "low_confidence" and i.column)
    review.apply(d, it, "reject")
    t = next(x for x in d["semantic_layer"]["tables"] if x["name"] == it.table)
    c = next(x for x in t["columns"] if x["name"] == it.column)
    assert c["semantic_type"] == "unknown" and c["lifecycle"] == "reviewed"
    assert validate_document(d).ok


# --------------------------------------------------------------------- mcp

def test_mcp_progressive_disclosure_sizes(doc):
    """Summaries must be small; only table_detail is big."""
    import json
    tables = mcp_server.list_tables(doc)
    assert len(json.dumps(tables)) < 12000, "table list must stay summary-sized"
    detail = mcp_server.get_table(doc, "ord_hdr")
    assert "columns" in detail and "required_filters" in detail
    assert any("sts_cd <> 'X'" in f["expr"] for f in detail["required_filters"])


def test_mcp_deprecated_marked_unusable(doc):
    tables = {t["name"]: t for t in mcp_server.list_tables(doc)}
    legacy = tables["ord_hdr_legacy"]
    assert legacy.get("UNUSABLE") is True
    assert legacy.get("use_instead") == "ord_hdr"


def test_mcp_search_finds_revenue_paths(doc):
    hits = mcp_server.search(doc, "order revenue total")
    kinds = {h["kind"] for h in hits}
    assert "metric" in kinds or "column" in kinds
    names = " ".join(h["name"] for h in hits)
    assert "tot_amt" in names or "total" in names


def test_mcp_routing_prefers_intent_match(doc):
    routes = mcp_server.routing(doc, "order analysis")
    assert routes and "ord" in routes[0]["intent"]
    assert any(a["table"] == "ord_hdr_legacy" for a in routes[0].get("avoid", []))


def test_mcp_server_builds_with_tools(doc):
    srv = mcp_server.build_server(doc)
    import anyio
    tools = anyio.run(srv.list_tools)
    names = {t.name for t in tools}
    assert {"semantic_search", "get_domains", "get_tables", "table_detail",
            "get_metrics", "route_intent"} <= names


# ------------------------------------------------------------ CLI end-to-end

def test_cli_infer_dry_run_end_to_end(tmp_path):
    """`semlayer infer` in deterministic mode against a fixture db file."""
    import importlib
    mod = importlib.import_module("generators.fan_trap")
    db = tmp_path / "ft.duckdb"
    con = duckdb.connect(str(db))
    mod.build(con)
    con.close()
    out = tmp_path / "layer.yaml"
    r = subprocess.run(
        [sys.executable, "-m", "semlayer.cli", "infer", f"duckdb:{db}",
         "-o", str(out), "--no-llm"],
        capture_output=True, text=True, cwd=OSS,
    )
    assert r.returncode == 0, r.stderr[-500:]
    produced = yaml.safe_load(out.read_text())
    assert validate_document(produced).ok
    assert len(produced["semantic_layer"]["tables"]) == 4


def test_attached_catalog_bridge(tmp_path):
    """Tables in an ATTACHed catalog (the DuckDB<->Iceberg-REST bridge shape)
    enumerate, qualify, and infer end-to-end (deterministic tier)."""
    import importlib

    mod = importlib.import_module("generators.fan_trap")
    db = tmp_path / "ext.duckdb"
    con = duckdb.connect(str(db))
    mod.build(con)
    con.close()

    host = duckdb.connect()
    host.execute(f"ATTACH '{db}' AS ice (READ_ONLY)")
    src = DuckDBSource(host)
    assert {t.name for t in src.list_tables()} == {"customers", "orders", "payments", "shipments"}
    assert src.qualify("main", "orders") == '"ice"."main"."orders"'
    doc = infer(src, llm=None)
    assert validate_document(doc).ok
    assert len(doc["semantic_layer"]["tables"]) == 4
    host.close()
