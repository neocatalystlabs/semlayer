# SPDX-License-Identifier: FSL-1.1-ALv2
"""Table-type classification (fact/dimension/aggregate/... ) — heuristic tier.

Signals: naming conventions, FK fan-out/fan-in from the validated join graph,
measure density, SCD/snapshot column patterns. Hard classes stay low-confidence
and review-queued (feasibility F4: per-class honesty over a blended number).
"""

from __future__ import annotations

import re

from semlayer.profile.typing_rules import normalise_name

_STAGING_RE = re.compile(r"(^|_)(stg|staging|raw|tmp|temp)($|_)")
_OPS_RE = re.compile(r"(^|_)(etl|log|audit|job|run|batch)($|_)")
_AGG_RE = re.compile(r"(^|_)(agg|summary|rollup|dly|daily|mth|monthly|wkly|weekly)($|_)")
_DIM_RE = re.compile(r"(^|_)(dim|ref|mstr|master|lookup|xref)($|_)")
_SNAP_RE = re.compile(r"(^|_)(snpsht|snapshot|snap|hist|history)($|_)")


def classify_table(t: dict, fk_out: int, fk_in: int) -> tuple[str, float, str]:
    """Classify a table's role using a first-match-wins ordered rule list.

    Order encodes priority, not just readability: naming conventions (staging,
    ops) are checked before structural signals (FK fan-out/in, measure
    density) because a naming hit is a stronger, more direct signal than an
    inferred graph shape. Returns (table_type, confidence, detail).

    Confidence is the measured rate at which each rule picks the gold
    table_type across the fixture corpus, shrunk toward 0.5 with a pseudo-count
    of 2. Rules that fired 3 times or fewer are left at their original value
    and are flagged as uncalibrated in docs/calibration.md. This number says
    only how sure we are of the LABEL -- a high score on a staging table means
    "confidently staging", not "good table". Canonical-ness lives in lifecycle
    and repo_knowledge.routing, not here.
    """
    # Folded, not just lowercased: every rule below matches `_`-delimited
    # tokens, so `DimCustomer` lowercased to `dimcustomer` and the 0.95
    # dimension rule never fired on a PascalCase warehouse. On
    # AdventureWorksDW that left every Dim* table to the graph heuristics.
    name = normalise_name(t["name"])
    cols = t["columns"]
    col_names = {c["name"].lower() for c in cols}
    n_measures = sum(1 for c in cols if c.get("entity_role") == "measure")
    n_cols = len(cols)
    has_validity = bool(
        {"eff_start_dt", "eff_end_dt"} & col_names
        or {"valid_from", "valid_to"} & col_names
        or any(n.endswith("_start_dt") for n in col_names)
    )
    has_snap = bool(_SNAP_RE.search(name)) or any(
        n in col_names for n in ("snap_dt", "snapshot_dt", "is_current", "is_curr")
    )
    # entity-shape: tiny table, a key plus name/descriptive columns
    has_name_col = any(n.endswith(("_name", "_nm")) for n in col_names)
    # The tier (semlayer.tier) already read the name prefix: an `int__` table is
    # staging there at 0.9 while the rules below, finding no graph signal,
    # called it operational at 0.1. The tier settles the fallthrough only --
    # a table the graph can type keeps the graph's answer.
    tier = t.get("tier") or {}
    staging_tier = tier.get("value") == "staging"
    tier_conf = round(min(float(tier.get("confidence") or 0.5), 0.85), 2)

    rules: list[tuple[bool, str, float, str]] = [
        (bool(_STAGING_RE.search(name)), "staging", 0.85, "staging naming"),
        (bool(_OPS_RE.search(name) and fk_in == 0), "operational", 0.7,
         "ops naming, nothing references it"),
        (has_validity, "snapshot_scd2", 0.8, "validity-window columns"),
        (has_snap, "snapshot_scd2", 0.35, "snapshot naming/flag (no validity columns)"),
        (bool(_AGG_RE.search(name) and n_measures >= 1), "aggregate", 0.75,
         "aggregate naming + measures"),
        (bool(_DIM_RE.search(name)), "dimension", 0.95, "dimension naming"),
        (n_cols > 50, "denormalized", 0.6, f"{n_cols} columns"),
        # Graph signals. Fan-in beats measure count: a dimension with a couple
        # of numeric attributes is still a dimension. Requiring n_measures == 0
        # caught only 39 of 99 gold dimensions and sent the rest to the
        # measure-heavy fact rule -- `customer` is referenced by ELEVEN tables
        # and was called a fact because it has 2 measures. Measured over the
        # corpus: `fk_in > fk_out` catches 74 of 99 dimensions and only 2
        # facts, where the old pure rule caught 39.
        (fk_out >= 2 and n_measures >= 1 and fk_in == 0, "fact", 0.95,
         f"{fk_out} FKs out + {n_measures} measures, nothing references it"),
        (fk_in >= 1 and n_measures == 0 and fk_out == 0, "dimension", 0.95,
         f"referenced by {fk_in} tables, no measures, no FKs out"),
        # Guarded against the parent-fact shape: a fact CAN be referenced by
        # other facts -- fan_trap's `orders` has payments and shipments
        # pointing at it, so fan-in alone calls it a dimension and breaks the
        # fixture built to test exactly that. A dimension is DESCRIPTIVE, so it
        # is wide; a four-column table carrying a measure is a transaction
        # record. The guard costs 22 of the 74 dimensions this rule would
        # otherwise catch, which is the price of not regressing fan_trap.
        (fk_in > fk_out and not (n_measures >= 1 and n_cols <= 5), "dimension", 0.9,
         f"referenced by {fk_in} tables, references {fk_out}: fan-in dominates"),
        (fk_out >= 2 and n_measures >= 1, "fact", 0.7,
         f"{fk_out} FKs out + {n_measures} measures"),
        (fk_out >= 1 and n_measures >= 1 and fk_in == 0, "fact", 0.65,
         f"{fk_out} FK out + {n_measures} measures, nothing references it"),
        (fk_in >= 1 and n_measures == 0, "dimension", 0.5,
         f"referenced by {fk_in} tables, no measures"),
        (n_cols <= 4 and n_measures == 0 and has_name_col, "dimension", 0.65,
         "key + name entity shape"),
        (n_measures >= 2, "fact", 0.1, "measure-heavy, few FKs resolved"),
        (staging_tier and fk_in == 0 and fk_out == 0 and n_measures == 0, "staging", tier_conf,
         "staging tier by name prefix, nothing joins to it"),
        (fk_in == 0 and fk_out == 0 and n_measures == 0, "operational", 0.1,
         "isolated, no measures"),
    ]
    for matched, ttype, conf, detail in rules:
        if matched:
            return ttype, conf, detail
    if staging_tier:
        return "staging", tier_conf, "staging tier by name prefix, no other signal"
    return "unknown", 0.05, "no decisive signal"
