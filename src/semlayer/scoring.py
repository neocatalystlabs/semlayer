"""Score a draft (inferred) semantic layer against a gold layer.

Joins objects on table.column names. Reports semantic-type accuracy and
macro-F1 (per-type breakdown), entity-role accuracy, and PK precision/recall.
All eval gates in targets.yaml read from this output shape.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class Score:
    """Aggregate accuracy/F1/PK metrics from one draft-vs-gold comparison."""

    n_columns: int = 0
    type_accuracy: float = 0.0
    type_macro_f1: float = 0.0
    per_type: dict = field(default_factory=dict)
    role_accuracy: float = 0.0
    pk_precision: float = 0.0
    pk_recall: float = 0.0
    mismatches: list = field(default_factory=list)


def _columns(doc: dict) -> dict[str, dict]:
    out = {}
    for t in doc["semantic_layer"]["tables"]:
        for c in t.get("columns", []):
            out[f"{t['name']}.{c['name']}"] = c
    return out


def _pks(doc: dict) -> set[str]:
    out = set()
    for t in doc["semantic_layer"]["tables"]:
        for col in t.get("primary_key", []) or []:
            out.add(f"{t['name']}.{col}")
    return out


# Types the rule tier can't distinguish without richer context; scored as
# acceptable matches (reported separately so the LLM tier's lift is visible).
_ACCEPTABLE = {
    ("code", "enum"), ("enum", "code"),
    ("code", "status_code"), ("status_code", "code"),
    ("enum", "status_code"), ("status_code", "enum"),
    ("name", "pii_name"), ("pii_name", "name"),
    ("timestamp_event", "date"), ("date", "timestamp_event"),
    ("quantity", "identifier"),  # int fallback on undeclared fk cols — penalized in role, not type
}


def score_types(draft: dict, gold: dict, lenient: bool = True) -> Score:
    """Join draft and gold columns by table.column and compute type/role/PK metrics."""
    d_cols, g_cols = _columns(draft), _columns(gold)
    joined = [(k, d_cols[k], g_cols[k]) for k in g_cols if k in d_cols]

    s = Score(n_columns=len(joined))
    if not joined:
        return s

    tp: dict[str, int] = defaultdict(int)
    fp: dict[str, int] = defaultdict(int)
    fn: dict[str, int] = defaultdict(int)
    correct = role_correct = 0

    for key, d, g in joined:
        dt, gt = d.get("semantic_type", "unknown"), g.get("semantic_type", "unknown")
        ok = dt == gt or (lenient and (dt, gt) in _ACCEPTABLE)
        if ok:
            correct += 1
            tp[gt] += 1
        else:
            fp[dt] += 1
            fn[gt] += 1
            s.mismatches.append((key, dt, gt))
        dr, gr = d.get("entity_role"), g.get("entity_role")
        # unique_key/natural_key vs primary_key confusions are role-adjacent
        adjacent = {
            frozenset({"primary_key", "unique_key"}),
            frozenset({"unique_key", "natural_key"}),
        }
        if dr == gr or (dr and gr and frozenset({dr, gr}) in adjacent):
            role_correct += 1

    s.type_accuracy = correct / len(joined)
    s.role_accuracy = role_correct / len(joined)

    f1s = []
    for typ in set(list(tp) + list(fp) + list(fn)):
        p = tp[typ] / (tp[typ] + fp[typ]) if tp[typ] + fp[typ] else 0.0
        r = tp[typ] / (tp[typ] + fn[typ]) if tp[typ] + fn[typ] else 0.0
        f1 = 2 * p * r / (p + r) if p + r else 0.0
        support = tp[typ] + fn[typ]
        s.per_type[typ] = {"precision": p, "recall": r, "f1": f1, "support": support}
        if support > 0:
            f1s.append(f1)
    s.type_macro_f1 = sum(f1s) / len(f1s) if f1s else 0.0

    d_pk, g_pk = _pks(draft), _pks(gold)
    if d_pk:
        s.pk_precision = len(d_pk & g_pk) / len(d_pk)
    if g_pk:
        s.pk_recall = len(d_pk & g_pk) / len(g_pk)
    return s
