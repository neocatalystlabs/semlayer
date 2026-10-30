"""The maintainer loop, replayed: one drift event, end to end, reproducibly.

The maintainer design records that the loop -- detect, changeset, targeted
re-inference, merge -- was run once on messy_mart (drop a column, add a
column, create a table) and that the run existed in a commit message and
nowhere reproducible. This is that replay, the first deliverable of the
decay study. Every step uses a shipped primitive; the only thing new here
is that they are chained and the outcome is asserted.
"""

import sys
from pathlib import Path

import duckdb
import pytest
import yaml

OSS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OSS / "fixtures"))

from semlayer import drift, review  # noqa: E402
from semlayer.llm.provider import AnthropicProvider, CassetteMiss  # noqa: E402
from semlayer.merge import merge_layer, render_report  # noqa: E402
from semlayer.pipeline import InferOptions, infer  # noqa: E402
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
    suite = yaml.safe_load((OSS / "fixtures" / "cqs" / "messy_mart.yaml").read_text())
    yield con, src, doc, suite["cq_suite"]["questions"]
    con.close()


def _col(doc, table, column):
    t = next(t for t in doc["semantic_layer"]["tables"] if t["name"] == table)
    return next(c for c in t["columns"] if c["name"] == column)


def test_one_drift_event_replays_end_to_end(world):
    con, src, doc, suite = world
    items = review.collect(doc)

    # A human has been here: one verdict on the table about to change, so the
    # replay proves the merge keeps it. With the LLM tier every ord_hdr column
    # is above the review threshold, so the verdict is written the way
    # review.apply writes one rather than drawn from the queue.
    reviewed = _col(doc, "ord_hdr", "chnl_cd")
    reviewed["lifecycle"] = "reviewed"
    reviewed["provenance"].append({"signal": "human", "date": "2026-10-30", "by": "raj",
                                   "detail": f"accepted: {review.describe_column(reviewed['semantic_type'], reviewed['entity_role'])}"})
    reviewed_before = (reviewed["semantic_type"], reviewed["entity_role"])

    # 1. the event: drop a column, add a column, create a table
    before = drift.snapshot(src)
    con.execute("ALTER TABLE ord_hdr DROP COLUMN tot_amt")
    con.execute("ALTER TABLE ord_hdr ADD COLUMN gift_flg BOOLEAN")
    con.execute("CREATE TABLE loyalty_pts (cust_id VARCHAR, pts INTEGER)")
    con.execute("INSERT INTO loyalty_pts SELECT cust_id, 10 FROM cust_mstr")

    # 2. detect -> changeset
    events = drift.diff_snapshots(before, drift.snapshot(src))
    assert sorted(e.kind for e in events) == ["column_added", "column_dropped", "table_added"]
    cs = drift.apply_drift(doc, events)
    cs.broken_cqs = drift.cq_regression(con, suite, events, doc)
    assert _col(doc, "ord_hdr", "tot_amt")["lifecycle"] == "orphaned"
    assert cs.broken_cqs, "dropping the revenue column must break competency questions"
    assert set(cs.needs_inference) >= {"loyalty_pts", "ord_hdr.gift_flg"}
    assert drift.render_changeset(cs)

    # 3. targeted re-inference: only the tables the changeset names
    retable = sorted({n.split(".", 1)[0] for n in cs.needs_inference})
    assert retable == ["loyalty_pts", "ord_hdr"]
    partial = infer(src, llm=None, options=InferOptions(only=retable))
    assert {t["name"] for t in partial["semantic_layer"]["tables"]} == set(retable)

    # 4. merge into the layer we have
    report = merge_layer(doc, partial, scope=set(retable))
    text = render_report(report)
    assert "loyalty_pts" in report.tables_added
    assert any(c.endswith(".gift_flg") for c in report.columns_added), report.columns_added
    # the human's verdict survived re-inference of its own table
    reviewed_after = _col(doc, "ord_hdr", "chnl_cd")
    assert reviewed_after["lifecycle"] == "reviewed"
    assert (reviewed_after["semantic_type"], reviewed_after["entity_role"]) == reviewed_before
    assert any(p["signal"] == "human" for p in reviewed_after["provenance"])
    # the dropped column is still there, orphaned, not resurrected and not deleted
    assert _col(doc, "ord_hdr", "tot_amt")["lifecycle"] == "orphaned"
    # the merge is small: the one hand run reported one line on reviewed content
    assert report.touched <= 40, (report.touched, text)
    assert validate_document(doc).ok, validate_document(doc).errors[:5]

    # 5. what the next run would ask: the new table's columns are in the queue
    after = review.collect(doc)
    assert any(i.table == "loyalty_pts" for i in after)
    assert {i.key for i in after} <= {i.key for i in items} | {i.key for i in after
                                                              if i.table in retable}
