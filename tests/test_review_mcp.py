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
    assert "columns" in detail and "usage_notes" in detail
    assert any("sts_cd <> 'X'" in n for n in detail["usage_notes"])


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
