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
    import importlib

    from semlayer.llm.provider import AnthropicProvider, CassetteMiss
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
    import importlib

    from semlayer.llm.provider import AnthropicProvider, CassetteMiss
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


# ------------------------------------------------- PascalCase name folding

def test_camel_case_keys_are_identifiers_not_measures():
    """A PascalCase surrogate key must type as an identifier.

    Found by acquiring AdventureWorksDW (a real Microsoft warehouse): every
    naming rule here matches `_`-delimited tokens, so `DateKey` lowercased to
    `datekey`, matched nothing, and fell through to the numeric fallback as a
    measure. That single miss cost all 28 primary keys and all 41 foreign keys
    on that warehouse, and made `DimDate` look like a fact table.
    """
    from semlayer.profile.typing_rules import normalise_name

    assert normalise_name("DateKey") == "date_key"
    assert normalise_name("CustomerAlternateKey") == "customer_alternate_key"
    assert normalise_name("ProductSKUName") == "product_sku_name"


def test_name_folding_leaves_snake_case_alone():
    """The fixture corpus is snake_case and its scores must not move."""
    from semlayer.profile.typing_rules import normalise_name

    for name in ("cust_id", "sts_cd", "tot_amt", "ord_hdr_id", "npi", "id"):
        assert normalise_name(name) == name


def test_small_int_domain_is_a_grouped_quantity_not_a_code():
    """An integer with few distinct values is a quantity you GROUP BY.

    The fallback used to return `code`, which was wrong on the type: in the
    population that reaches it -- numerics no name rule matched -- gold says
    `quantity` 81 times and `code` never, because every numeric code in the
    corpus is name-signalled and caught upstream. But returning
    `quantity`/`measure` would be wrong on the role instead: for 44 of 57 such
    columns in tpcds_clean gold says `dimension`, since `d_moy` and
    `s_floor_space` are grouped by, not summed. Both have to hold.
    """
    from semlayer.profile.stats import ColumnStats
    from semlayer.profile.typing_rules import classify

    t = classify(ColumnStats(name="floor_space", sql_type="BIGINT", row_count=12,
                             n_null=0, n_distinct=6, is_unique=False))
    assert (t.semantic_type, t.entity_role) == ("quantity", "dimension")


def test_bare_code_suffix_is_a_code_whatever_the_stem():
    """`zone_cd` is a code even though `zone` is not in the stem vocabulary.

    The code name rule matches a vocabulary (typ, cat, class, seg, chnl, rsn),
    so a `_cd` suffix on an unknown stem fell through to the numeric fallback
    and was typed as a quantity. Specific rules must still win first.
    """
    from semlayer.profile.stats import ColumnStats
    from semlayer.profile.typing_rules import classify

    def t(name, sql_type="INTEGER", n_distinct=8):
        return classify(ColumnStats(name=name, sql_type=sql_type, row_count=500,
                                    n_null=0, n_distinct=n_distinct, is_unique=False))

    assert t("zone_cd").semantic_type == "code"
    assert t("priority_cd").semantic_type == "code"
    assert t("pay_grade").semantic_type == "code"
    # the abbreviation the generic rule shadowed until it was added
    assert t("cntry_cd", "VARCHAR").semantic_type == "geo_country"
    assert t("country_cd", "VARCHAR").semantic_type == "geo_country"


def test_generic_code_rule_still_escalates_after_the_enum_bonus():
    """A `_cd` column must stay under the escalation cutoff, bonus included.

    _rule_name_table adds +0.15 to any code with <= 30 distinct values. At a
    base of 0.55 that landed on exactly 0.70, which is not BELOW
    ESCALATE_BELOW, so these columns stopped reaching the LLM -- and since
    _role_for maps every code to `dimension`, three columns that are actually
    primary or foreign keys regressed. The type signal is reliable; the role
    one is not, so the LLM must still be asked.
    """
    from semlayer.profile.run import ESCALATE_BELOW
    from semlayer.profile.stats import ColumnStats
    from semlayer.profile.typing_rules import classify

    t = classify(ColumnStats(name="dept_cd", sql_type="VARCHAR", row_count=500,
                             n_null=0, n_distinct=8, is_unique=False))
    assert t.semantic_type == "code"
    assert t.confidence < ESCALATE_BELOW, (
        f"confidence {t.confidence} must stay under {ESCALATE_BELOW} so the role "
        "still goes to the LLM")


def test_profiling_can_be_scoped_to_a_table_subset():
    """`only` profiles just the named tables, and complains about names it cannot find.

    This is the targeted half of incremental re-inference: `drift.py` already
    computes which tables changed and what depends on them, and this is what
    lets that answer be acted on instead of re-profiling the warehouse. On a
    multi-terabyte warehouse that difference is the whole game.

    The filter is applied AFTER list_tables() so no connector changes --
    metadata enumeration is one information_schema query while per-column
    profiling is the expensive part.
    """
    import importlib

    import pytest

    from semlayer.errors import SemlayerError
    mod = importlib.import_module("generators.messy_mart")
    con = duckdb.connect(":memory:")
    mod.build(con)
    try:
        doc = profile_source(DuckDBSource(con), only={"ord_hdr", "ord_ln"})
        assert {t["name"] for t in doc["semantic_layer"]["tables"]} == {"ord_hdr", "ord_ln"}
        # a typo must not quietly profile fewer tables than asked for
        with pytest.raises(SemlayerError, match="not in this warehouse"):
            profile_source(DuckDBSource(con), only={"ord_hdr", "no_such_table"})
    finally:
        con.close()


def test_native_comments_become_priors_and_survive_describe():
    """The warehouse's own COMMENT text is prior knowledge, not raw material.

    We ignored native comments entirely until 2026-10-12, while nine separate
    naming vocabularies were failing on schemas we did not author. If a
    warehouse has column comments, a human has already written down what we
    are inferring, and their sentence is better evidence than any heuristic.

    DuckDB exposes comments only via duckdb_tables()/duckdb_columns(), NOT
    information_schema, which is why a generic query finds nothing.
    """
    from semlayer.describe.run import _apply_parsed, _from_catalog

    con = duckdb.connect(":memory:")
    con.sql("CREATE TABLE orders (order_id INTEGER, sts_cd VARCHAR)")
    con.sql("INSERT INTO orders VALUES (1, 'A'), (2, 'X'), (3, 'A')")
    con.sql("COMMENT ON TABLE orders IS 'one row per order header'")
    con.sql("COMMENT ON COLUMN orders.sts_cd IS 'X means cancelled'")
    try:
        doc = profile_source(DuckDBSource(con))
    finally:
        con.close()

    t = doc["semantic_layer"]["tables"][0]
    assert t["description"] == "one row per order header"
    assert _from_catalog(t)
    sts = next(c for c in t["columns"] if c["name"] == "sts_cd")
    assert sts["description"] == "X means cancelled"
    assert _from_catalog(sts)
    # a column WITHOUT a comment is untouched and stays describable
    assert not _from_catalog(next(c for c in t["columns"] if c["name"] == "order_id"))

    # Describe must not replace a human's sentence with a generated one
    _apply_parsed(t, {"table_description": "LLM guess",
                      "columns": [{"name": "sts_cd", "description": "LLM guess"}]}, 0)
    assert t["description"] == "one row per order header"
    assert sts["description"] == "X means cancelled"
