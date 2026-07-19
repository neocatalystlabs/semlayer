"""Table-type classification (fact/dimension/aggregate/... ) — heuristic tier.

Signals: naming conventions, FK fan-out/fan-in from the validated join graph,
measure density, SCD/snapshot column patterns. Hard classes stay low-confidence
and review-queued (feasibility F4: per-class honesty over a blended number).
"""

from __future__ import annotations

import re

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
    """
    name = t["name"].lower()
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

    rules: list[tuple[bool, str, float, str]] = [
        (bool(_STAGING_RE.search(name)), "staging", 0.85, "staging naming"),
        (bool(_OPS_RE.search(name) and fk_in == 0), "operational", 0.7,
         "ops naming, nothing references it"),
        (has_validity, "snapshot_scd2", 0.8, "validity-window columns"),
        (has_snap, "snapshot_scd2", 0.6, "snapshot naming/flag (no validity columns)"),
        (bool(_AGG_RE.search(name) and n_measures >= 1), "aggregate", 0.75,
         "aggregate naming + measures"),
        (bool(_DIM_RE.search(name)), "dimension", 0.8, "dimension naming"),
        (n_cols > 50, "denormalized", 0.6, f"{n_cols} columns"),
        # graph signals
        (fk_out >= 2 and n_measures >= 1, "fact", 0.8,
         f"{fk_out} FKs out + {n_measures} measures"),
        (fk_out >= 1 and n_measures >= 1 and fk_in == 0, "fact", 0.65,
         f"{fk_out} FK out + {n_measures} measures, nothing references it"),
        (fk_in >= 1 and n_measures == 0 and fk_out == 0, "dimension", 0.75,
         f"referenced by {fk_in} tables, no measures, no FKs out"),
        (fk_in >= 1 and n_measures == 0, "dimension", 0.6,
         f"referenced by {fk_in} tables, no measures"),
        (n_cols <= 4 and n_measures == 0 and has_name_col, "dimension", 0.6,
         "key + name entity shape"),
        (n_measures >= 2, "fact", 0.55, "measure-heavy, few FKs resolved"),
        (fk_in == 0 and fk_out == 0 and n_measures == 0, "operational", 0.4,
         "isolated, no measures"),
    ]
    for matched, ttype, conf, detail in rules:
        if matched:
            return ttype, conf, detail
    return "unknown", 0.3, "no decisive signal"
