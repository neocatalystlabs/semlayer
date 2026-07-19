"""M7a: the scripted mutation suite (test plan Layer 7).

Apply real DDL/DML mutations to a fixture, assert the whole loop: detection,
orphaning (never deletion), conflicts on protected content, blast radius,
CQ regression firing.
"""

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

from semlayer import drift  # noqa: E402
from semlayer.llm.provider import AnthropicProvider, CassetteMiss  # noqa: E402
from semlayer.pipeline import infer  # noqa: E402
from semlayer.source import DuckDBSource  # noqa: E402
from semlayer.validate import validate_document  # noqa: E402


@pytest.fixture()
def world():
    import importlib
    mod = importlib.import_module("generators.messy_mart")
    con = duckdb.connect(":memory:")
    mod.build(con)
    src = DuckDBSource(con)
    try:
        doc = infer(src, llm=AnthropicProvider())
    except CassetteMiss as e:
        pytest.skip(str(e))
    suite = yaml.safe_load((OSS / "fixtures" / "cqs" / "messy_mart.yaml").read_text())["cq_suite"]["questions"]
    return con, src, doc, suite


def test_column_drop_orphans_and_breaks_cqs(world):
    con, src, doc, suite = world
    before = drift.snapshot(src)
    con.execute("ALTER TABLE ord_hdr DROP COLUMN tot_amt")
    events = drift.diff_snapshots(before, drift.snapshot(src))
    assert [e.kind for e in events] == ["column_dropped"]
    cs = drift.apply_drift(doc, events)
    cs.broken_cqs = drift.cq_regression(con, suite, events, doc)
    # orphaned, not deleted
    ord_hdr = next(t for t in doc["semantic_layer"]["tables"] if t["name"] == "ord_hdr")
    col = next(c for c in ord_hdr["columns"] if c["name"] == "tot_amt")
    assert col["lifecycle"] == "orphaned"
    # metrics on the column orphan too
    assert any(o.startswith("metrics.") for o in cs.orphaned)
    # blast radius includes aggregates built on the fact
    assert any("aggregate_tables." in a for a in cs.affected)
    # THE ALARM: revenue CQs break loudly
    assert cs.broken_cqs, "dropping the revenue column must break CQs"
    assert any("SQL broken" in b for b in cs.broken_cqs)
    assert validate_document(doc).ok


def test_table_drop_orphans_whole_table(world):
    con, src, doc, suite = world
    before = drift.snapshot(src)
    con.execute("DROP TABLE cust_addr")
    events = drift.diff_snapshots(before, drift.snapshot(src))
    cs = drift.apply_drift(doc, events)
    t = next(t for t in doc["semantic_layer"]["tables"] if t["name"] == "cust_addr")
    assert t["lifecycle"] == "orphaned"
    assert "cust_addr" in cs.orphaned
    assert validate_document(doc).ok


def test_new_table_and_column_queue_inference(world):
    con, src, doc, _ = world
    before = drift.snapshot(src)
    con.execute("CREATE TABLE loyalty_pts (cust_id VARCHAR, pts INTEGER)")
    con.execute("ALTER TABLE ord_hdr ADD COLUMN gift_flg BOOLEAN")
    events = drift.diff_snapshots(before, drift.snapshot(src))
    kinds = sorted(e.kind for e in events)
    assert kinds == ["column_added", "table_added"]
    cs = drift.apply_drift(doc, events)
    assert "loyalty_pts" in cs.needs_inference
    assert "ord_hdr.gift_flg" in cs.needs_inference
    assert not cs.orphaned


def test_retype_under_certified_records_conflict(world):
    con, src, doc, _ = world
    ord_hdr = next(t for t in doc["semantic_layer"]["tables"] if t["name"] == "ord_hdr")
    col = next(c for c in ord_hdr["columns"] if c["name"] == "src_sys_id")
    col["lifecycle"] = "certified"  # simulate a human-certified column
    before = drift.snapshot(src)
    con.execute("ALTER TABLE ord_hdr ALTER COLUMN src_sys_id TYPE VARCHAR")
    events = drift.diff_snapshots(before, drift.snapshot(src))
    retypes = [e for e in events if e.kind == "column_retyped"]
    if not retypes:
        pytest.skip("engine reports same logical type")
    drift.apply_drift(doc, events)
    assert any("type changed" in c["detail"] for c in col.get("conflicts", [])), \
        "certified content must get a conflict record, never silent rewrite"


def test_enum_value_added_detected(world):
    con, src, doc, _ = world
    con.execute("INSERT INTO sts_cd_dim VALUES ('R', 'Refunded')")
    con.execute("UPDATE ord_hdr SET sts_cd='R' WHERE ord_id <= 5")
    events = drift.semantic_drift(src, doc)
    hits = [e for e in events if e.kind == "enum_value_added" and e.column == "sts_cd"]
    assert hits, "unmodeled enum value must be detected"
    cs = drift.apply_drift(doc, events)
    ord_hdr = next(t for t in doc["semantic_layer"]["tables"] if t["name"] == "ord_hdr")
    col = next(c for c in ord_hdr["columns"] if c["name"] == "sts_cd")
    assert any("'R'" in c["detail"] for c in col.get("conflicts", []))


def test_no_change_no_events(world):
    con, src, doc, _ = world
    before = drift.snapshot(src)
    events = drift.diff_snapshots(before, drift.snapshot(src))
    assert events == []
    assert drift.apply_drift(doc, events).empty


def test_changeset_renders(world):
    con, src, doc, suite = world
    before = drift.snapshot(src)
    con.execute("ALTER TABLE ord_hdr DROP COLUMN tot_amt")
    events = drift.diff_snapshots(before, drift.snapshot(src))
    cs = drift.apply_drift(doc, events)
    cs.broken_cqs = drift.cq_regression(con, suite, events, doc)
    md = drift.render_changeset(cs)
    assert "column_dropped" in md and "Orphaned" in md and "BROKEN COMPETENCY" in md
