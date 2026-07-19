"""Profile stage vs gold fixtures — the first real eval numbers (Layer 2).

Floors here are the HEURISTIC-TIER baseline, set from measured runs (see
targets note below); the LLM tier (M3) must beat them. Raising a floor is a
reviewed diff; lowering one is a regression.
"""

import sys
from pathlib import Path

import duckdb
import pytest
import yaml

OSS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OSS / "fixtures"))

from semlayer.profile import profile_source  # noqa: E402
from semlayer.scoring import score_types  # noqa: E402
from semlayer.source import DuckDBSource  # noqa: E402
from semlayer.validate import validate_document  # noqa: E402

# fixture -> (type_accuracy floor, role_accuracy floor, pk_recall floor)
# Measured heuristic baseline 2026-07-18: fan_trap 0.929/1.0/1.0,
# messy_mart 0.813/0.722/0.690. Floors sit just below measured.
FLOORS = {
    "fan_trap": (0.90, 0.95, 0.99),
    "messy_mart": (0.76, 0.68, 0.65),
}


def _profile(fixture: str):
    import importlib
    mod = importlib.import_module(f"generators.{fixture}")
    con = duckdb.connect(":memory:")
    mod.build(con)
    draft = profile_source(DuckDBSource(con))
    con.close()
    gold = yaml.safe_load((OSS / "fixtures" / "golds" / f"{fixture}.yaml").read_text())
    return draft, gold


@pytest.mark.parametrize("fixture", list(FLOORS))
def test_profile_output_is_spec_conformant(fixture):
    draft, _ = _profile(fixture)
    result = validate_document(draft)
    assert result.ok, "\n".join(result.errors[:10])


@pytest.mark.parametrize("fixture", list(FLOORS))
def test_profile_scores_meet_floor(fixture):
    draft, gold = _profile(fixture)
    s = score_types(draft, gold)
    ta_floor, ra_floor, pkr_floor = FLOORS[fixture]
    report = (
        f"\n[{fixture}] columns={s.n_columns} type_acc={s.type_accuracy:.3f} "
        f"macro_f1={s.type_macro_f1:.3f} role_acc={s.role_accuracy:.3f} "
        f"pk_p={s.pk_precision:.3f} pk_r={s.pk_recall:.3f}"
    )
    print(report)
    if s.mismatches:
        print(f"  worst mismatches: {s.mismatches[:8]}")
    assert s.n_columns > 0, "no columns joined between draft and gold"
    assert s.type_accuracy >= ta_floor, f"type accuracy regression: {report}"
    assert s.role_accuracy >= ra_floor, f"role accuracy regression: {report}"
    assert s.pk_recall >= pkr_floor, f"pk recall regression: {report}"


# --- LLM escalation tier (cassette-replayed in CI; live only when recording) ---

FLOORS_LLM = {
    # measured 2026-07-18 on Haiku tier: fan_trap 1.000/1.000, messy_mart 0.904/0.778
    "fan_trap": (0.95, 0.95),
    "messy_mart": (0.85, 0.72),
}


@pytest.mark.parametrize("fixture", list(FLOORS_LLM))
def test_profile_with_llm_escalation_meets_floor(fixture):
    from semlayer.llm.provider import AnthropicProvider, CassetteMiss

    import importlib
    mod = importlib.import_module(f"generators.{fixture}")
    con = duckdb.connect(":memory:")
    mod.build(con)
    llm = AnthropicProvider()
    try:
        draft = profile_source(DuckDBSource(con), llm=llm)
    except CassetteMiss as e:
        pytest.skip(f"cassette missing and no API key: {e}")
    finally:
        con.close()
    assert validate_document(draft).ok
    gold = yaml.safe_load((OSS / "fixtures" / "golds" / f"{fixture}.yaml").read_text())
    s = score_types(draft, gold)
    ta, ra = FLOORS_LLM[fixture]
    print(f"\n[{fixture}+llm] type_acc={s.type_accuracy:.3f} role_acc={s.role_accuracy:.3f} ({llm.spend_summary})")
    assert s.type_accuracy >= ta
    assert s.role_accuracy >= ra


def test_no_egress_delta_within_tolerance():
    """--no-sample-egress must cost only a small, MEASURED accuracy delta
    (measured 2026-07-18: 0.904 -> 0.894 with LLM tier on messy_mart)."""
    from semlayer.llm.provider import AnthropicProvider, CassetteMiss

    import importlib
    mod = importlib.import_module("generators.messy_mart")
    con = duckdb.connect(":memory:")
    mod.build(con)
    try:
        draft = profile_source(DuckDBSource(con), no_sample_values=True, llm=AnthropicProvider())
    except CassetteMiss as e:
        pytest.skip(str(e))
    finally:
        con.close()
    gold = yaml.safe_load((OSS / "fixtures" / "golds" / "messy_mart.yaml").read_text())
    s = score_types(draft, gold)
    print(f"\n[messy_mart no-egress+llm] type_acc={s.type_accuracy:.3f}")
    assert s.type_accuracy >= 0.85  # within 0.06 of the default-mode floor
