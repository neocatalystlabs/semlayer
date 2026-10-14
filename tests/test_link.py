"""Link stage evals (Layer 2 + Layer 4): FK discovery vs golds, and the
high-confidence error rate against collision_heavy's seeded traps.

Floors from measured 2026-07-18 run (cassette-replayed): all three fixtures
at P/R/F1 = 1.000, trap_errors = 0. Floors sit just below; the trap-error
assertion is HARD ZERO — one auto-included trap is a build failure, because
a confidently-wrong FK is the product's cardinal sin (adversarial B2).
"""

import sys
from pathlib import Path

import os

import duckdb
import pytest
import yaml

OSS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OSS / "fixtures"))

from semlayer.link import link_source  # noqa: E402
from semlayer.llm.provider import AnthropicProvider, CassetteMiss  # noqa: E402
from semlayer.profile.run import profile_with_stats  # noqa: E402
from semlayer.source import DuckDBSource  # noqa: E402
from semlayer.validate import validate_document  # noqa: E402

FK_FLOORS = {  # fixture -> (precision, recall); measured 2026-07-18
    "fan_trap": (0.99, 0.99),        # measured 1.000/1.000
    "messy_mart": (0.95, 0.95),      # measured 1.000/1.000 (36 FKs)
    "collision_heavy": (0.95, 0.95), # measured 1.000/1.000
    "tpcds_clean": (0.97, 0.90),     # measured 1.000/0.971 (104 FKs)
    "self_ref": (0.99, 0.99),        # measured 1.000/1.000 (recursive-credited)
}


def _gold_fks(gold):
    return {(t["name"], c["name"], *c["foreign_key"]["references"].split("."))
            for t in gold["semantic_layer"]["tables"]
            for c in t.get("columns", []) if c.get("foreign_key")}


def _run_link(fixture):
    os.environ.setdefault("TPCDS_SF", "0.01")
    import importlib
    mod = importlib.import_module(f"generators.{fixture}")
    con = duckdb.connect(":memory:")
    mod.build(con)
    llm = AnthropicProvider()
    try:
        doc, stats = profile_with_stats(DuckDBSource(con), llm=llm)
        link_source(DuckDBSource(con), doc, stats, llm=llm, use_declared=False)
    except CassetteMiss as e:
        pytest.skip(f"cassette missing and no API key: {e}")
    finally:
        con.close()
    return doc, mod


@pytest.mark.parametrize("fixture", list(FK_FLOORS))
def test_fk_discovery_meets_floor(fixture):
    doc, _ = _run_link(fixture)
    audit = doc.pop("_link_audit")
    assert validate_document(doc).ok
    accepted = {tuple(k) for k, _, _ in audit["accepted"]}
    # same-table gold FKs are satisfied by recursive-hierarchy detection
    rec = {(h["dimension_table"], h["recursive"]["parent_column"],
            h["dimension_table"], h["recursive"]["child_column"])
           for h in doc["semantic_layer"].get("hierarchies", [])
           if h.get("kind") == "recursive"}
    truth = _gold_fks(yaml.safe_load((OSS / "fixtures" / "golds" / f"{fixture}.yaml").read_text()))
    accepted = accepted | (rec & truth)
    tp = len(accepted & truth)
    p = tp / len(accepted) if accepted else 0.0
    r = tp / len(truth) if truth else 1.0
    print(f"\n[{fixture}:link] FK P={p:.3f} R={r:.3f} accepted={len(accepted)} "
          f"review={len(audit['review'])} killed={len(audit['killed'])}")
    pf, rf = FK_FLOORS[fixture]
    assert p >= pf, f"FK precision regression: {sorted(accepted - truth)[:5]}"
    assert r >= rf, f"FK recall regression: {sorted(truth - accepted)[:5]}"


def test_zero_trap_errors_hard_gate():
    """No seeded trap may EVER be auto-included. This is the
    high-confidence-error-rate gate at its required value: zero."""
    doc, mod = _run_link("collision_heavy")
    audit = doc.pop("_link_audit")
    accepted = {tuple(k) for k, _, _ in audit["accepted"]}
    trap_hits = [t for t in mod.TRAPS
                 if tuple(t[0].split(".") + t[1].split(".")) in accepted]
    assert not trap_hits, f"HIGH-CONFIDENCE ERRORS — seeded traps auto-included: {trap_hits}"


# ------------------------------------------- statistics-first key discovery

def test_a_key_shaped_column_is_a_candidate_whatever_it_is_called():
    """Uniqueness and inclusion must be consulted, not gated behind naming.

    Found on CMS's Medicare files: `Rndrng_NPI` is the National Provider
    Identifier -- unique across the summary table, non-unique across the
    detail, 100% inclusion. A textbook foreign key. It ends in `_npi`, matched
    no vocabulary, never became a candidate, and Link returned ZERO
    relationships on a two-table warehouse. The statistics were never
    consulted.
    """
    from semlayer.link.candidates import _key_capable, _shaped_parent

    class _S:
        def __init__(self, uniq, distinct, sql_type):
            self.is_unique, self.n_distinct, self.sql_type = uniq, distinct, sql_type

    class _T:
        def __init__(self, col):
            self.columns = {"c": col}

    assert _shaped_parent({"t": _T(_S(True, 400, "VARCHAR"))}, "t", "c")
    # a one-row table makes EVERY column unique; that is not a key
    assert not _shaped_parent({"t": _T(_S(True, 1, "VARCHAR"))}, "t", "c")
    # a unique DOUBLE is a measure that happens not to repeat
    assert not _shaped_parent({"t": _T(_S(True, 400, "DOUBLE"))}, "t", "c")
    assert _key_capable("INTEGER") and _key_capable("VARCHAR") and _key_capable("DATE")
    assert not _key_capable("DECIMAL(19,4)") and not _key_capable("DOUBLE")


def test_role_playing_survives_but_alternate_keys_are_collapsed():
    """Two paths between the same tables mean two very different things.

    Role-playing: different child columns, SAME parent column -- order date
    and ship date are different questions and both are real. Alternate keys:
    DIFFERENT parent columns -- the surrogate and natural-key paths are one
    relationship, and asserting both gives the compiler two equally-short
    paths so it refuses to group through the ambiguity.
    """
    from semlayer.link.candidates import FKCandidate
    from semlayer.link.run import _drop_alternate_key_paths

    def cand(cc, pc, name_score):
        return FKCandidate("f", cc, "date_dim", pc, 1.0, 10, 100, name_score)

    role = [cand("sold_date_sk", "d_date_sk", 0.9), cand("ship_date_sk", "d_date_sk", 0.9)]
    assert len(_drop_alternate_key_paths(role)) == 2

    alt = [cand("date_key", "date_key", 1.2), cand("ord_dt", "cal_dt", 1.0)]
    kept = _drop_alternate_key_paths(alt)
    assert [c.parent_column for c in kept] == ["date_key"]


def test_dates_and_names_are_not_key_references():
    """A date inside the calendar is a join path, not a foreign key.

    `ord_rtn.rtn_dt` is 100% contained in `date_dim.cal_dt` with an exactly
    matching token, and `etl_log.job_nm -> job_ctrl.job_nm` scores 1.20 on
    naming. Neither is a key, and admitting them took messy_mart's FK
    precision floor from 0.95 to 0.90. Statistics and naming both say yes
    here; only the semantic type says no.
    """
    from semlayer.link.candidates import _NON_REFERENCE_TYPES

    for t in ("date", "timestamp_event", "name", "free_text"):
        assert t in _NON_REFERENCE_TYPES
    for t in ("identifier", "code", "enum", "unknown"):
        assert t not in _NON_REFERENCE_TYPES


def test_composite_primary_keys_are_detected_by_shape_not_by_role():
    """A table keyed on a column PAIR had no primary_key at all.

    PK detection picked the best single unique identifier-named column, so
    every line-item fact in the commonest warehouse benchmark came back with
    nothing: tpcds_clean's store_sales is keyed on
    (ss_item_sk, ss_ticket_number), 180 distinct items and 2,400 distinct
    tickets over 28,810 rows. Gold declares 9 composite PKs; we detected none.

    Candidacy is statistical, NOT role-based. Gating on entity_role excluded
    `ss_ticket_number`, which is typed quantity/measure for ending in
    `_number` while being half of a key that is unique over every row -- and
    gold itself calls that column quantity/primary_key. Gating on a
    name-derived classification while establishing the thing it was guessing
    at is the error that made key discovery name-first.
    """
    from semlayer.link.run import _NEVER_PK_TYPES, _composite_pk_candidates

    class _C:
        def __init__(self, n, sql_type="INTEGER", uniq=False, nulls=0):
            self.n_distinct, self.sql_type, self.is_unique, self.n_null = n, sql_type, uniq, nulls

    class _TS:
        def __init__(self, cols):
            self.columns = cols
            self.row_count = 28810

    cols = {
        "ss_item_sk": _C(180),
        "ss_ticket_number": _C(2400),          # typed quantity/measure, still a key member
        "ss_sales_price": _C(900, "DECIMAL(7,2)"),   # continuous: never a key
        "ss_sold_date_sk": _C(1800, nulls=130),      # nullable: never a key member
        "ss_id": _C(28810, uniq=True),               # unique alone: the whole key
    }
    t = {"name": "store_sales", "columns": [
        {"name": "ss_item_sk", "semantic_type": "identifier", "entity_role": "foreign_key"},
        {"name": "ss_ticket_number", "semantic_type": "quantity", "entity_role": "measure"},
        {"name": "ss_sales_price", "semantic_type": "monetary_value", "entity_role": "measure"},
        {"name": "ss_sold_date_sk", "semantic_type": "identifier", "entity_role": "foreign_key"},
        {"name": "ss_id", "semantic_type": "identifier", "entity_role": "primary_key"},
    ]}
    got = set(_composite_pk_candidates(t, _TS(cols)))
    assert got == {"ss_item_sk", "ss_ticket_number"}, got
    assert "monetary_value" in _NEVER_PK_TYPES
