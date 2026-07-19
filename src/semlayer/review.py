"""Review queue: collect inferred claims needing human judgment, apply verdicts.

Item kinds:
- low_confidence: semantic-type/role claims under the review threshold
- llm_guess_enum: guessed decodes (metric-filter-blocked until promoted)
- conflict: recorded signal disagreements
- fk_candidate: review-queued FK candidates (from Link's corroboration policy)

Verdicts are STICKY (SPEC.md §3.3): accept -> lifecycle reviewed;
reject removes the claim, never the column.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

REVIEW_THRESHOLD = 0.7
_FKQ_RE = re.compile(r"REVIEW-QUEUED candidate -> (\S+) \(ratio ([\d.]+), name score ([\d.]+)")


@dataclass
class ReviewItem:
    """One inferred claim (or conflict) awaiting a human accept/reject verdict."""

    kind: str
    table: str
    column: str | None
    claim: str
    evidence: str

    @property
    def key(self) -> str:
        """Stable dedup key: kind + table[.column]."""
        loc = f"{self.table}.{self.column}" if self.column else self.table
        return f"{self.kind}:{loc}"


def collect(doc: dict) -> list[ReviewItem]:
    """Scan the semantic layer for claims and conflicts awaiting human review."""
    items: list[ReviewItem] = []
    for t in doc["semantic_layer"]["tables"]:
        if t.get("lifecycle") == "inferred" and (t.get("confidence") or 1.0) < REVIEW_THRESHOLD:
            items.append(ReviewItem("low_confidence", t["name"], None,
                                    f"table_type={t.get('table_type')}", _prov(t)))
        for con in t.get("conflicts", []) or []:
            items.append(ReviewItem("conflict", t["name"], None, con.get("detail", ""), ""))
        for c in t["columns"]:
            if c.get("lifecycle") == "inferred" and (c.get("confidence") or 1.0) < REVIEW_THRESHOLD:
                claim = f"semantic_type={c.get('semantic_type')}, role={c.get('entity_role')}"
                items.append(ReviewItem("low_confidence", t["name"], c["name"], claim, _prov(c)))
            for con in c.get("conflicts", []) or []:
                items.append(
                    ReviewItem("conflict", t["name"], c["name"], con.get("detail", ""), ""))
            if any(e.get("decode_source") == "llm_guess" for e in c.get("enum_values") or []):
                decs = {e["value"]: e["meaning"] for e in c["enum_values"]}
                items.append(ReviewItem("llm_guess_enum", t["name"], c["name"],
                                        f"guessed decodes {decs}", ""))
            for p in c.get("provenance", []) or []:
                m = _FKQ_RE.search(p.get("detail", ""))
                if m:
                    items.append(ReviewItem("fk_candidate", t["name"], c["name"],
                                            f"foreign key -> {m.group(1)}",
                                            f"inclusion {m.group(2)}, name score {m.group(3)}"))
    return items


def _apply_low_confidence(target: dict, item: ReviewItem, verdict: str) -> None:
    """Accept promotes lifecycle; reject also resets the claimed type to unknown."""
    if verdict != "accept":
        if item.column is not None:
            target["semantic_type"] = "unknown"
            target["entity_role"] = "dimension"
        else:
            target["table_type"] = "unknown"
    target["lifecycle"] = "reviewed"
    target.setdefault("provenance", []).append({"signal": "human", "detail": f"review: {verdict}"})


def _apply_llm_guess_enum(target: dict, verdict: str) -> None:
    """Accept promotes guessed decodes to human-sourced; reject drops them."""
    if verdict == "accept":
        for e in target.get("enum_values", []):
            if e.get("decode_source") == "llm_guess":
                e["decode_source"] = "human"
    else:
        target.pop("enum_values", None)
    detail = f"enum decodes: {verdict}"
    target.setdefault("provenance", []).append({"signal": "human", "detail": detail})


def _apply_conflict(target: dict, verdict: str) -> None:
    """Resolving a conflict clears it; accept also promotes lifecycle."""
    target.pop("conflicts", None)
    detail = f"conflict resolved: {verdict}"
    target.setdefault("provenance", []).append({"signal": "human", "detail": detail})
    if verdict == "accept":
        target["lifecycle"] = "reviewed"


def _apply_fk_candidate(target: dict, item: ReviewItem, verdict: str) -> None:
    """Accept materializes the FK; either verdict clears the REVIEW-QUEUED marker."""
    m = re.search(r"-> (\S+)", item.claim)
    if verdict == "accept" and m and item.column:
        target["entity_role"] = "foreign_key"
        target["foreign_key"] = {"references": m.group(1), "relationship": "many_to_one"}
        target["lifecycle"] = "reviewed"
    target["provenance"] = [p for p in target.get("provenance", [])
                            if "REVIEW-QUEUED" not in p.get("detail", "")]
    detail = f"fk candidate: {verdict}"
    target.setdefault("provenance", []).append({"signal": "human", "detail": detail})


_APPLIERS = {
    "low_confidence": lambda target, item, verdict: _apply_low_confidence(target, item, verdict),
    "llm_guess_enum": lambda target, item, verdict: _apply_llm_guess_enum(target, verdict),
    "conflict": lambda target, item, verdict: _apply_conflict(target, verdict),
    "fk_candidate": lambda target, item, verdict: _apply_fk_candidate(target, item, verdict),
}


def apply(doc: dict, item: ReviewItem, verdict: str) -> None:
    """verdict: accept | reject. Mutates doc; sticky per SPEC."""
    t = next(x for x in doc["semantic_layer"]["tables"] if x["name"] == item.table)
    target = t if item.column is None else next(c for c in t["columns"] if c["name"] == item.column)
    applier = _APPLIERS.get(item.kind)
    if applier is not None:
        applier(target, item, verdict)


def _prov(obj) -> str:
    ps = obj.get("provenance") or []
    return "; ".join(p.get("detail", "")[:60] for p in ps[-2:])
