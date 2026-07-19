"""Link stage runner: profile document -> +FKs, +relationships, +table types.

Decision policy per candidate (the confidently-wrong defense, adversarial B2):
- AUTO-INCLUDE requires statistics (inclusion >= 0.95) AND corroboration:
  strong naming agreement (>= 0.6) AND, when an LLM is present, a plausible
  verdict. Statistics alone NEVER auto-include.
- Statistically-strong candidates with weak naming are the trap shape: they
  go to the LLM; survivors land in the REVIEW QUEUE (lifecycle inferred,
  low confidence), never auto-included.
- LLM-implausible kills the candidate; the kill is recorded (provenance of
  absence matters for the audit sample).
"""

from __future__ import annotations

from semlayer.link.candidates import (
    FKCandidate,
    _inclusion,
    _name_tokens,
    _type_family,
    generate_candidates,
)
from semlayer.link.table_type import classify_table

AUTO_NAME_THRESHOLD = 0.6
REVIEW_CONFIDENCE = 0.45

_REC_HINT = ("mgr", "manager", "parent", "sup", "boss", "report", "referrer", "ref_by")


def _affinity(tn: str, cn: str) -> int:
    """Count name tokens `cn` shares with its own table `tn` (self-affinity)."""
    return len(_name_tokens(cn, tn) & _name_tokens(tn, tn))


def _resolve_directions(candidates: list[FKCandidate], stats_by_table: dict) -> list[FKCandidate]:
    """Flip symmetric unique<->unique candidates so the true entity-table PK is the parent.

    If both A.x->B.y and B.y->A.x are candidates with equal distinct counts,
    naming affinity decides which side is the entity table (the parent).
    """
    fixed = []
    for c in candidates:
        both_unique = (stats_by_table[c.child_table].columns[c.child_column].is_unique
                       and stats_by_table[c.parent_table].columns[c.parent_column].is_unique)
        inverted = (both_unique
                    and c.child_distinct == c.parent_distinct
                    and _affinity(c.child_table, c.child_column)
                    > _affinity(c.parent_table, c.parent_column))
        if inverted:
            c = FKCandidate(c.parent_table, c.parent_column, c.child_table, c.child_column,
                            c.inclusion_ratio, c.parent_distinct, c.child_distinct, c.name_score)
        fixed.append(c)
    return fixed


def _decide_candidates(
    candidates: list[FKCandidate], verdicts: dict, llm
) -> tuple[list[tuple[FKCandidate, dict | None]], list[tuple[FKCandidate, dict | None]],
           list[tuple[FKCandidate, dict | None]]]:
    """Bucket candidates into (accepted, review, killed) per the corroboration rule.

    Direction resolution for symmetric unique<->unique pairs (1:1/xref):
    for each child column, only its single best-scored candidate parent
    survives to the accept/review/kill decision (best_for_child).
    """
    best_for_child: dict[tuple, FKCandidate] = {}
    for c in candidates:
        prev = best_for_child.get((c.child_table, c.child_column))
        if prev is None or (c.name_score, c.inclusion_ratio) > (
            prev.name_score, prev.inclusion_ratio,
        ):
            best_for_child[(c.child_table, c.child_column)] = c

    accepted, review, killed = [], [], []
    for c in best_for_child.values():
        v = verdicts.get(c.key())
        llm_plausible = (v or {}).get("verdict") == "plausible"
        llm_implausible = (v or {}).get("verdict") == "implausible"
        if llm_implausible:
            killed.append((c, v))
            continue
        corroborated = c.name_score >= AUTO_NAME_THRESHOLD
        if corroborated and (llm is None or llm_plausible):
            accepted.append((c, v))
        else:
            review.append((c, v))
    return accepted, review, killed


def _apply_accepted(accepted: list, review: list, cols: dict) -> None:
    """Write foreign_key/confidence/provenance onto accepted columns; provenance-only for review."""
    for c, v in accepted:
        col = cols[(c.child_table, c.child_column)]
        col["entity_role"] = "foreign_key"
        col["foreign_key"] = {
            "references": f"{c.parent_table}.{c.parent_column}",
            "relationship": "many_to_one",
        }
        col["confidence"] = round(min(0.9, 0.6 + 0.2 * c.name_score + (0.1 if v else 0.0)), 2)
        col.setdefault("provenance", []).append({
            "signal": "inclusion_dependency",
            "detail": f"ratio {c.inclusion_ratio}, name score {c.name_score}"
                      + (f", llm: {v['reason']}" if v else ""),
        })
    for c, v in review:
        col = cols[(c.child_table, c.child_column)]
        col.setdefault("provenance", []).append({
            "signal": "inclusion_dependency",
            "detail": f"REVIEW-QUEUED candidate -> {c.parent_table}.{c.parent_column} "
                      f"(ratio {c.inclusion_ratio}, name score {c.name_score}"
                      + (f", llm: {v['reason']}" if v else ", no llm") + ")",
        })


def _emit_relationships(accepted: list, cols: dict, doc: dict) -> tuple[dict, dict]:
    """Build the relationships block from accepted FKs; return fk_out/fk_in fan counts."""
    rels = []
    fk_out: dict[str, int] = {}
    fk_in: dict[str, int] = {}
    for c, _v in accepted:
        rels.append({
            "name": f"{c.child_table}_to_{c.parent_table}",
            "from": {"table": c.child_table, "columns": [c.child_column]},
            "to": {"table": c.parent_table, "columns": [c.parent_column]},
            "cardinality": "many_to_one",
            "join_type": "left",
            "fanout_risk": False,
            "lifecycle": "inferred",
            "confidence": cols[(c.child_table, c.child_column)]["confidence"],
        })
        fk_out[c.child_table] = fk_out.get(c.child_table, 0) + 1
        fk_in[c.parent_table] = fk_in.get(c.parent_table, 0) + 1
    # reverse direction of each accepted many_to_one is a fan-out for the parent side
    for r in rels:
        r["fanout_risk"] = True  # traversing one-to-many from parent side multiplies rows
    if rels:
        doc["semantic_layer"]["relationships"] = rels
    return fk_out, fk_in


def _detect_self_ref_hierarchy(t: dict, stats_by_table: dict, source) -> list[dict]:
    """Detect a column referencing the table's own PK (org-chart style).

    Requires a SEMANTIC hint in the name — numeric ⊆-coincidences against
    the PK are rampant (TPC-DS i_class_id ⊆ i_item_sk), so inclusion alone
    never creates a recursive hierarchy.
    """
    tn = t["name"]
    pk = (t.get("primary_key") or [None])[0]
    if not pk:
        return []
    keyish = [c for c in t["columns"]
              if c["name"] != pk and c["name"].lower().endswith(("_id", "_key", "_sk"))]
    hierarchies = []
    for c in keyish:
        if not any(h in c["name"].lower() for h in _REC_HINT):
            continue
        cs = stats_by_table[tn].columns[c["name"]]
        ps = stats_by_table[tn].columns[pk]
        if cs.is_unique or cs.n_distinct < 2:
            continue
        if _type_family(cs.sql_type) != _type_family(ps.sql_type):
            continue
        if _inclusion(source, stats_by_table, tn, c["name"], tn, pk) >= 0.95:
            hierarchies.append({
                "name": f"{tn}_{c['name']}_hierarchy", "kind": "recursive",
                "dimension_table": tn,
                "recursive": {"parent_column": c["name"], "child_column": pk},
                "lifecycle": "inferred", "confidence": 0.7,
                "provenance": [{"signal": "inclusion_dependency",
                                "detail": f"{c['name']} values ⊆ {pk} (self-reference)"}],
            })
    return hierarchies


def _detect_bom_hierarchy(t: dict) -> list[dict]:
    """Detect parent_X / child_X column pairs (bill-of-materials style)."""
    tn = t["name"]
    by_suffix: dict[str, dict] = {}
    for c in t["columns"]:
        m = c["name"].lower()
        if m.startswith("parent_"):
            by_suffix.setdefault(m[7:], {})["parent"] = c["name"]
        elif m.startswith("child_"):
            by_suffix.setdefault(m[6:], {})["child"] = c["name"]
    hierarchies = []
    for suffix, pair in by_suffix.items():
        if "parent" in pair and "child" in pair:
            hierarchies.append({
                "name": f"{tn}_{suffix}_hierarchy", "kind": "recursive",
                "dimension_table": tn,
                "recursive": {"parent_column": pair["parent"], "child_column": pair["child"]},
                "lifecycle": "inferred", "confidence": 0.75,
                "provenance": [{"signal": "naming",
                                "detail": f"parent_/child_ column pair over '{suffix}'"}],
            })
    return hierarchies


def _detect_recursive_hierarchies(doc: dict, stats_by_table: dict, source) -> list[dict]:
    """Detect self-referential (org-chart) and parent_/child_ (BOM) recursive structures."""
    hierarchies = []
    for t in doc["semantic_layer"]["tables"]:
        hierarchies.extend(_detect_self_ref_hierarchy(t, stats_by_table, source))
        hierarchies.extend(_detect_bom_hierarchy(t))
    return hierarchies


def _classify_tables(doc: dict, fk_out: dict, fk_in: dict) -> None:
    """Assign table_type + confidence/provenance to every table in place."""
    for t in doc["semantic_layer"]["tables"]:
        ttype, _conf, detail = classify_table(t, fk_out.get(t["name"], 0), fk_in.get(t["name"], 0))
        t["table_type"] = ttype
        t.setdefault("provenance", []).append(
            {"signal": "statistic", "detail": f"table_type: {detail}"}
        )


def link_source(source, doc: dict, stats_by_table: dict, llm=None) -> dict:
    """Run the Link stage over a profiled document.

    Mutates and returns doc: adds foreign_key blocks, relationships,
    table_type. Also returns audit info in doc['_link_audit'] (stripped
    before serialization; used by eval).
    """
    candidates = generate_candidates(source, stats_by_table, doc)
    candidates = _resolve_directions(candidates, stats_by_table)

    verdicts = {}
    if llm is not None and candidates:
        from semlayer.link.validate_llm import validate_candidates
        verdicts = validate_candidates(llm, candidates)

    cols = {
        (t["name"], c["name"]): c
        for t in doc["semantic_layer"]["tables"] for c in t["columns"]
    }
    accepted, review, killed = _decide_candidates(candidates, verdicts, llm)
    _apply_accepted(accepted, review, cols)
    fk_out, fk_in = _emit_relationships(accepted, cols, doc)

    hierarchies = _detect_recursive_hierarchies(doc, stats_by_table, source)
    if hierarchies:
        doc["semantic_layer"]["hierarchies"] = hierarchies

    _classify_tables(doc, fk_out, fk_in)

    doc["_link_audit"] = {
        "n_candidates": len(candidates),
        "accepted": [(c.key(), c.inclusion_ratio, c.name_score) for c, _ in accepted],
        "review": [(c.key(), c.inclusion_ratio, c.name_score) for c, _ in review],
        "killed": [(c.key(), (v or {}).get("reason")) for c, v in killed],
    }
    return doc
