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
        link_source(DuckDBSource(con), doc, stats, llm=llm)
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
