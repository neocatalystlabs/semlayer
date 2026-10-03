"""`--no-sample-egress` is a privacy promise and a measured no-op. Both are tested.

The promise: no value read out of a warehouse cell reaches the model. The
measurement (2026-10-03, 7 fixtures / 759 columns, `score_types`): corpus-wide
typing is identical with and without sample egress (0.809 both ways) and role is
marginally better without it (0.842 vs 0.838) — net free. Per fixture it trades:
messy_mart loses a point (0.904 -> 0.894), collision_heavy gains 1.6, and they
cancel. So this test pins the *bound* on the trade, not parity: withholding cell
values must never cost more than a point or so on any one fixture.
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
    load_dotenv(OSS.parent / ".env")  # enables cassette RECORDING in dev; CI replays
except ImportError:
    pass

from semlayer.describe import run as drun  # noqa: E402
from semlayer.link import link_source  # noqa: E402
from semlayer.llm.provider import AnthropicProvider, CassetteMiss  # noqa: E402
from semlayer.pipeline import infer  # noqa: E402
from semlayer.profile.run import profile_with_stats  # noqa: E402
from semlayer.scoring import score_types  # noqa: E402
from semlayer.source import DuckDBSource  # noqa: E402

# Measured 2026-10-03: the worst per-fixture movement is messy_mart at -0.010
# (corpus-wide the delta is 0.000). This bounds the trade with a little room;
# a larger regression means withholding samples started genuinely costing us.
EGRESS_PARITY_TOLERANCE = 0.02


def _build(fixture):
    import importlib
    mod = importlib.import_module(f"generators.{fixture}")
    con = duckdb.connect(":memory:")
    mod.build(con)
    return con


def test_no_sample_egress_sends_no_warehouse_cell_values():
    """Intercept every Describe payload and prove no read cell value is in it.

    `_evidence` has an `enum_decodes` branch that is *not* gated on the flag. It
    is currently harmless because Describe runs before Enrich, so the only
    decodes present are the model's own guesses from column names — values it
    supplied, not values we read. This test fails if that ever stops being true
    (e.g. if Describe is reordered after Enrich), because then real decode
    values would start leaving in a mode that promises they do not.
    """
    con = _build("messy_mart")
    src = DuckDBSource(con)
    payloads = []
    orig = drun._evidence

    def spy(t, stats, neighbors, no_samples, context=None):
        ev = orig(t, stats, neighbors, no_samples, context=context)
        payloads.append((t, ev))
        return ev

    drun._evidence = spy
    try:
        llm = AnthropicProvider()
        doc, stats = profile_with_stats(src, no_sample_values=True, llm=llm)
        link_source(src, doc, stats, llm=llm)
        drun.describe_source(doc, stats, llm, no_samples=True)
    except CassetteMiss as e:
        pytest.skip(str(e))
    finally:
        drun._evidence = orig
        con.close()

    assert payloads, "no Describe payloads intercepted — the spy did not take"
    import json
    guessed = 0
    for t, ev in payloads:
        j = json.loads(ev)
        src_cols = {c["name"]: c for c in t["columns"]}
        for c in j["columns"]:
            assert "sample_values" not in c, f"{j['table']}.{c['name']} leaked sample_values"
            assert "top_values" not in c, f"{j['table']}.{c['name']} leaked top_values"
            assert "range" not in c, f"{j['table']}.{c['name']} leaked range"
            if "enum_decodes" in c:
                # permitted only when the model itself invented the decode
                origin = src_cols[c["name"]].get("enum_values") or []
                assert origin and all(e.get("decode_source") == "llm_guess" for e in origin), (
                    f"{j['table']}.{c['name']} echoed decodes read from the warehouse"
                )
                guessed += 1
    # documents what the permitted case looks like today, so a change is visible
    assert guessed <= 8, f"unexpectedly many echoed guess-decodes ({guessed})"


def test_no_sample_egress_costs_at_most_a_point():
    """Withholding cell values may trade a little, but must not cost real accuracy.

    messy_mart is the fixture where sample values genuinely help (ambiguous
    names), so it is the worst case in the corpus and the right one to bound.
    """
    con = _build("messy_mart")
    gold = yaml.safe_load((OSS / "fixtures/golds/messy_mart.yaml").read_text())
    try:
        llm = AnthropicProvider()
        default = infer(DuckDBSource(con), llm=llm, no_sample_egress=False)
        llm2 = AnthropicProvider()
        noegress = infer(DuckDBSource(con), llm=llm2, no_sample_egress=True)
    except CassetteMiss as e:
        pytest.skip(str(e))
    finally:
        con.close()

    d, n = score_types(default, gold), score_types(noegress, gold)
    assert n.type_accuracy >= d.type_accuracy - EGRESS_PARITY_TOLERANCE, (
        f"no-sample-egress cost typing accuracy: "
        f"{d.type_accuracy:.3f} -> {n.type_accuracy:.3f}"
    )
    assert n.role_accuracy >= d.role_accuracy - EGRESS_PARITY_TOLERANCE, (
        f"no-sample-egress cost role accuracy: "
        f"{d.role_accuracy:.3f} -> {n.role_accuracy:.3f}"
    )
