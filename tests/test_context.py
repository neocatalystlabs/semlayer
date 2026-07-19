"""Knowledge-doc priors (v0.2): intake, matching, and priors-never-truth.

The load-bearing assertions: a doc cracks a statistics-proof column (with
`docs` provenance), and a doc that contradicts observed data produces a
conflict — never a silent override.
"""

import sys
from pathlib import Path

import duckdb
import pytest

OSS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OSS / "fixtures"))

try:
    from dotenv import load_dotenv

    load_dotenv(OSS.parent / ".env")  # enables cassette RECORDING in dev; CI replays
except ImportError:
    pass

from semlayer.context import (  # noqa: E402
    MAX_CHARS_PER_TABLE,
    apply_doc_decodes,
    load_context,
    parse_dictionary,
    relevant_excerpts,
)
from semlayer.errors import SpecError  # noqa: E402
from semlayer.llm.provider import AnthropicProvider, CassetteMiss  # noqa: E402
from semlayer.pipeline import infer  # noqa: E402
from semlayer.source import DuckDBSource  # noqa: E402
from semlayer.validate import validate_document  # noqa: E402

CTX_DIR = OSS / "fixtures" / "context"


# ---------- intake ----------


def test_load_directory_walks_and_filters(tmp_path):
    (tmp_path / "notes.md").write_text("# Orders\nord_hdr holds order headers.")
    (tmp_path / "readme.txt").write_text("plain text about cust_mstr")
    (tmp_path / "ignore.bin").write_bytes(b"\x00\x01")
    sub = tmp_path / ".git"
    sub.mkdir()
    (sub / "config.md").write_text("# not context")
    bundle = load_context([str(tmp_path)])
    sources = {c.source for c in bundle.chunks}
    assert any(s.startswith("notes.md") for s in sources)
    assert any(s.startswith("readme.txt") for s in sources)
    assert not any("config" in s for s in sources)


def test_load_glob_and_missing_path(tmp_path):
    (tmp_path / "a.md").write_text("# A\nbody")
    bundle = load_context([str(tmp_path / "*.md")])
    assert bundle.chunks
    with pytest.raises(SpecError):
        load_context([str(tmp_path / "nope" / "*.md")])


def test_html_stripped(tmp_path):
    (tmp_path / "wiki.html").write_text(
        "<html><head><style>x{}</style></head>"
        "<body><h1>Codes</h1><p>sts_cd: X = Cancelled</p>"
        "<script>alert(1)</script></body></html>"
    )
    bundle = load_context([str(tmp_path / "wiki.html")])
    text = " ".join(c.text for c in bundle.chunks)
    assert "sts_cd" in text and "alert" not in text and "x{}" not in text


def test_csv_dictionary_detected_and_fallback(tmp_path):
    (tmp_path / "dict.csv").write_text(
        "Table,Column,Description\nord_hdr,sts_cd,Order status code\n"
    )
    (tmp_path / "notes.csv").write_text("a,b\n1,2\n")  # not a dictionary shape
    bundle = load_context([str(tmp_path / "dict.csv"), str(tmp_path / "notes.csv")])
    assert len(bundle.dictionary) == 1
    e = bundle.dictionary[0]
    assert (e.table, e.column) == ("ord_hdr", "sts_cd")
    assert bundle.chunks  # notes.csv fell back to prose


def test_dictionary_without_table_column():
    entries = parse_dictionary("d.csv", "field,definition\nsts_cd,Status code\n")
    assert entries and entries[0].table is None and entries[0].column == "sts_cd"


# ---------- matching ----------


def test_relevant_excerpts_bounded_and_ranked(tmp_path):
    (tmp_path / "big.md").write_text(
        "# ord_hdr deep dive\n"
        + ("ord_hdr sts_cd details. " * 500)
        + "\n# unrelated\nnothing here about tables\n"
    )
    bundle = load_context([str(tmp_path / "big.md")])
    ex = relevant_excerpts(bundle.chunks, "ord_hdr", ["sts_cd", "tot_amt"])
    assert ex
    assert sum(len(e["text"]) for e in ex) <= MAX_CHARS_PER_TABLE
    assert all("unrelated" not in e["source"] for e in ex)


# ---------- priors, never truth (unit) ----------


def _doc_with_decodes(source: str, meaning: str = "Cancelled"):
    return {
        "semantic_layer": {
            "tables": [
                {
                    "name": "ord_hdr",
                    "columns": [
                        {
                            "name": "sts_cd",
                            "enum_values": [
                                {"value": "C", "meaning": "Completed", "decode_source": source},
                                {"value": "X", "meaning": meaning, "decode_source": source},
                            ],
                        }
                    ],
                }
            ]
        }
    }


def test_doc_decode_contradiction_is_conflict_not_override(tmp_path):
    (tmp_path / "wiki.md").write_text("# codes\nsts_cd: C = Completed, X = Refunded\n")
    bundle = load_context([str(tmp_path / "wiki.md")])
    doc = _doc_with_decodes("dictionary_join")
    apply_doc_decodes(doc, bundle.chunks)
    c = doc["semantic_layer"]["tables"][0]["columns"][0]
    assert c["enum_values"][1]["meaning"] == "Cancelled"  # data wins
    assert any(
        "Refunded" in cf["detail"] and "docs" in cf["between"] for cf in c.get("conflicts", [])
    )
    # the agreeing claim (C=Completed) corroborates instead
    assert any(
        p["signal"] == "docs" and "corroborated" in p["detail"] for p in c.get("provenance", [])
    )


def test_doc_decode_upgrades_llm_guess(tmp_path):
    (tmp_path / "wiki.md").write_text("# codes\nsts_cd: C = Completed, X = Cancelled\n")
    bundle = load_context([str(tmp_path / "wiki.md")])
    doc = _doc_with_decodes("llm_guess")
    apply_doc_decodes(doc, bundle.chunks)
    c = doc["semantic_layer"]["tables"][0]["columns"][0]
    assert all(e["decode_source"] == "docs" for e in c["enum_values"])


def test_decode_claims_require_column_mention(tmp_path):
    # table-only mention must NOT push claims onto sibling enum columns
    (tmp_path / "wiki.md").write_text("# ord_hdr\nvalues: C = Completed, X = Refunded\n")
    bundle = load_context([str(tmp_path / "wiki.md")])
    doc = _doc_with_decodes("dictionary_join")
    apply_doc_decodes(doc, bundle.chunks)
    c = doc["semantic_layer"]["tables"][0]["columns"][0]
    assert not c.get("conflicts")


# ---------- integration: messy_mart, deterministic (no LLM) ----------


def _messy_con():
    import importlib

    mod = importlib.import_module("generators.messy_mart")
    con = duckdb.connect(":memory:")
    mod.build(con)
    return con


def test_infer_with_context_no_llm():
    bundle = load_context([str(CTX_DIR)])
    con = _messy_con()
    try:
        doc = infer(DuckDBSource(con), context=bundle)
    finally:
        con.close()
    assert validate_document(doc).ok
    tables = {t["name"]: t for t in doc["semantic_layer"]["tables"]}
    # CSV dictionary seeded a description with docs provenance
    dept = next(c for c in tables["dept_dim"]["columns"] if c["name"] == "dept_cd")
    assert dept["description"] == "Two-letter department code assigned by merchandising"
    assert any(p["signal"] == "docs" for p in dept["provenance"])
    # stale wiki claim (X = Refunded) vs discovered decode dim (X = Cancelled)
    sts = next(c for c in tables["ord_hdr"]["columns"] if c["name"] == "sts_cd")
    decodes = {e["value"]: e["meaning"] for e in sts["enum_values"]}
    assert decodes["X"] == "Cancelled"  # data wins
    assert any(
        "Refunded" in cf["detail"] and "docs" in cf["between"] for cf in sts.get("conflicts", [])
    )


# ---------- integration: doc cracks a statistics-proof column (LLM) ----------


def test_doc_cracks_statistics_proof_column():
    """yr_mth (YYYYMM ints) types as unknown from statistics; the wiki page
    that explains it must flip the LLM verdict to date."""
    bundle = load_context([str(CTX_DIR / "messy_mart_wiki.md")])
    llm = AnthropicProvider()
    con = _messy_con()
    try:
        doc = infer(DuckDBSource(con), llm=llm, context=bundle)
    except CassetteMiss as e:
        pytest.skip(str(e))
    finally:
        con.close()
    tables = {t["name"]: t for t in doc["semantic_layer"]["tables"]}
    ym = next(c for c in tables["mth_cust_agg"]["columns"] if c["name"] == "yr_mth")
    assert ym["semantic_type"] == "date"
    assert any(p["signal"] == "llm" for p in ym["provenance"])
    # describe stage consulted the docs and said so
    assert any(p["signal"] == "docs" for p in tables["mth_cust_agg"].get("provenance", []))


def test_doc_promotion_corrects_confident_heuristic():
    """sts_cd_dim.sts_desc types as status_code at 0.85 from the name rule —
    above the escalation threshold, so the LLM never sees it. A doc naming
    the column must promote it into escalation, and the correction must land
    WITH a conflict recorded (doc-prompted overrides are never silent)."""
    bundle = load_context([str(CTX_DIR / "messy_mart_wiki.md")])
    llm = AnthropicProvider()
    con = _messy_con()
    try:
        doc = infer(DuckDBSource(con), llm=llm, context=bundle)
    except CassetteMiss as e:
        pytest.skip(str(e))
    finally:
        con.close()
    tables = {t["name"]: t for t in doc["semantic_layer"]["tables"]}
    sd = next(c for c in tables["sts_cd_dim"]["columns"] if c["name"] == "sts_desc")
    # mechanism assertions: promoted, corrected off the wrong answer, and the
    # override is review-visible. (Exact label — name vs enum — is the
    # LLM's claim; the conflict entry is what routes it to a human.)
    assert sd["semantic_type"] != "status_code"
    assert any(p["signal"] == "docs" and "re-escalated" in p["detail"]
               for p in sd["provenance"])
    assert any("doc-prompted re-escalation" in cf["detail"]
               for cf in sd.get("conflicts", []))
