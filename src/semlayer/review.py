# SPDX-License-Identifier: FSL-1.1-ALv2
"""Review queue: collect inferred claims needing human judgment, apply verdicts.

Item kinds:
- low_confidence: semantic-type/role claims under the review threshold
- llm_guess_enum: guessed decodes (metric-filter-blocked until promoted)
- conflict: recorded signal disagreements
- fk_candidate: review-queued FK candidates (from Link's corroboration policy)
- discovered_filter: a business rule recovered by aggregate reconciliation
- aggregate_mapping: a heuristic aggregate mapping; accepting it is what earns
  verified routing (SPEC.md 2.7) -- data proves today's rows, a human knows
  whether the job still applies the rule
- metric_plausibility: a metric proposed on a table that is not an active fact
- seam_edge: a cross-document relationship in a hub below the threshold
- same_entity: two members' tables found to hold one entity, with no
  canonical copy decided (neither certified, same key set)

Metric-tier items are queued by CLAIM KIND, never by a confidence threshold:
metric confidences are rule constants, not measured rates (docs/metrics.md).

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
    # non-table targets: "metrics.<name>" or "aggregate_tables.<table>".
    # `table` still carries the owning fact, so display and grouping work.
    ref: str | None = None

    @property
    def key(self) -> str:
        """Stable dedup key: kind + ref, or kind + table[.column]."""
        if self.ref is not None:
            return f"{self.kind}:{self.ref}"
        loc = f"{self.table}.{self.column}" if self.column else self.table
        return f"{self.kind}:{loc}"

    @property
    def location(self) -> str:
        """What to print as the item's address."""
        if self.ref is not None:
            return self.ref
        return f"{self.table}.{self.column}" if self.column else self.table


def collect(doc: dict) -> list[ReviewItem]:
    """Scan the semantic layer for claims and conflicts awaiting human review."""
    items: list[ReviewItem] = []
    for t in _modelled(doc["semantic_layer"]):      # inventory-only: _inventory_items
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
    items += _metric_items(doc["semantic_layer"])
    items += _inventory_items(doc["semantic_layer"])
    items += _seam_items(doc["semantic_layer"])
    return items


def _owner(table: str) -> str:
    return table.split("/", 1)[0] if "/" in table else ""


def _seam_items(sl: dict) -> list[ReviewItem]:
    """Inferred relationships that cross documents in a set, below the review threshold."""
    out = []
    for r in sl.get("relationships", []) or []:
        a, b = r["from"]["table"], r["to"]["table"]
        if not a or _owner(a) == _owner(b) or not _owner(a):
            continue
        inferred = r.get("lifecycle", "inferred") == "inferred"
        if r.get("same_entity"):
            undecided = not r.get("canonical") or (r.get("confidence") or 1.0) < REVIEW_THRESHOLD
            if inferred and undecided:
                ov = r.get("key_overlap") or {}
                out.append(ReviewItem(
                    "same_entity", a, None,
                    f"same entity as {b}; canonical: {r.get('canonical') or 'undecided'}",
                    f"keys of {a} in {b}: {ov.get('from_in_to', '?')}, reverse "
                    f"{ov.get('to_in_from', '?')}; confidence {r.get('confidence')}",
                    ref=f"seam.{r.get('name', '')}"))
            continue
        if inferred and (r.get("confidence") or 1.0) < REVIEW_THRESHOLD:
            out.append(ReviewItem(
                "seam_edge", a, ",".join(r["from"]["columns"]),
                f"-> {b}.{','.join(r['to']['columns'])} ({r.get('cardinality')})",
                f"confidence {r.get('confidence')}", ref=f"seam.{r.get('name', '')}"))
    return out


def for_hub(items: list[ReviewItem], hub_name: str) -> list[ReviewItem]:
    """A hub's queue: its own tables and the seam. Members review their own files."""
    return [i for i in items
            if i.kind in ("seam_edge", "same_entity") or _owner(i.table) == hub_name]


def _modelled(sl: dict) -> list[dict]:
    return [t for t in sl["tables"] if t.get("modeling") != "inventory_only"]


def _inventory_items(sl: dict) -> list[ReviewItem]:
    """One line per tier for the tables set aside as inventory-only.

    Not one per column: these tables were set aside on purpose
    (semlayer.tier), and the question for a reviewer is "is this tier right",
    not four thousand column types. On a 2,000-table estate the per-column
    form was 76% of the queue.
    """
    by_tier: dict[str, list[str]] = {}
    for t in sl["tables"]:
        if t.get("modeling") == "inventory_only":
            by_tier.setdefault((t.get("tier") or {}).get("value", "unknown"), []).append(t["name"])
    return [
        ReviewItem("inventory_only", names[0], None,
                   f"{len(names)} tables in the {tier} tier are listed but not modelled "
                   f"(no relationships, descriptions or metrics)",
                   "e.g. " + ", ".join(names[:5]) + (" ..." if len(names) > 5 else ""),
                   ref=f"tier.{tier}")
        for tier, names in sorted(by_tier.items())
    ]


_RECONCILED_RE = re.compile(r"business rule from aggregate reconciliation: (.+)")


def _base_table(m: dict) -> str:
    return (m.get("measure") or m.get("numerator") or ".").split(".", 1)[0]


def _metric_items(sl: dict) -> list[ReviewItem]:
    """Metric- and aggregate-tier review items.

    Three claims a human settles in seconds and no statistic can: is this
    discovered business rule real, does this summary table still mean what the
    arithmetic says, and is this proposed metric a metric at all.
    """
    items: list[ReviewItem] = []
    types = {t["name"]: t for t in sl["tables"]}
    for m in sl.get("metrics", []) or []:
        if m.get("lifecycle") != "inferred":
            continue
        base = _base_table(m)
        rule = next((mt.group(1) for p in m.get("provenance", []) or []
                     if (mt := _RECONCILED_RE.search(p.get("detail", "")))), None)
        if rule is not None:
            items.append(ReviewItem(
                "discovered_filter", base, None,
                f"{m['name']} applies {rule} — a rule nobody declared",
                _full_prov(m), ref=f"metrics.{m['name']}"))
            continue
        t = types.get(base)
        if t is not None and (t.get("table_type") != "fact"
                              or t.get("lifecycle") in ("deprecated", "orphaned")):
            items.append(ReviewItem(
                "metric_plausibility", base, None,
                f"{m['name']} = {m.get('agg', 'ratio')} over {base} "
                f"(table_type={t.get('table_type')})",
                _prov(m), ref=f"metrics.{m['name']}"))
    for a in sl.get("aggregate_tables", []) or []:
        if a.get("mapping_source") != "heuristic" or a.get("lifecycle") != "inferred":
            continue
        src = "; ".join(str(mm.get("source")) for mm in a.get("measure_mappings", []))
        items.append(ReviewItem(
            "aggregate_mapping", a["aggregates"], None,
            f"{a['table']} is {src} grouped by {', '.join(a.get('grain', []))}",
            _full_prov(a), ref=f"aggregate_tables.{a['table']}"))
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


def _human(obj: dict, detail: str) -> None:
    obj.setdefault("provenance", []).append({"signal": "human", "detail": detail})


def _apply_discovered_filter(sl: dict, metric: dict, verdict: str) -> None:
    """Accept promotes the rule; reject drops it from the metric AND the table.

    A rejected rule cannot be left on the table while the metric stops using
    it -- the required_filter is the same claim, and SPEC 2.2 would keep
    applying it to every other consumer.
    """
    expr = metric.get("filter")
    if expr is None:
        # the rule was already removed (a second verdict, or a hand edit);
        # the lifecycle verdict still stands, there is just nothing to move
        metric["lifecycle"] = "reviewed" if verdict == "accept" else metric.get(
            "lifecycle", "inferred")
        _human(metric, f"discovered filter {verdict}: no filter on the metric")
        return
    base = _base_table(metric)
    if verdict == "accept":
        metric["lifecycle"] = "reviewed"
        _human(metric, f"discovered filter confirmed: {expr}")
        _promote_required_filter(sl, base, expr)
        return
    metric.pop("filter", None)
    _human(metric, f"discovered filter rejected: {expr}")
    _drop_required_filter(sl, base, expr)


def _promote_required_filter(sl: dict, base: str, expr: str) -> None:
    for t in sl["tables"]:
        if t["name"] != base:
            continue
        for f in (t.get("knowledge") or {}).get("required_filters", []):
            if f["expr"] == expr:
                _human(t, f"required_filter confirmed: {expr}")


def _drop_required_filter(sl: dict, base: str, expr: str) -> None:
    """Remove the rejected rule, and flag any table that inherited it."""
    for t in sl["tables"]:
        k = t.get("knowledge") or {}
        rfs = k.get("required_filters") or []
        if t["name"] == base:
            kept = [f for f in rfs if f["expr"] != expr]
            if len(kept) != len(rfs):
                k["required_filters"] = kept
                _human(t, f"required_filter rejected in review: {expr}")
            continue
        for f in rfs:
            if f"inherited from {base}" in (f.get("reason") or ""):
                t.setdefault("conflicts", []).append({
                    "between": ["statistic", "human"],
                    "detail": (f"inherited rule {f['expr']} survives, but its parent rule "
                               f"on {base} was rejected in review"),
                })


def _apply_metric_plausibility(sl: dict, metric: dict, verdict: str) -> None:
    """Accept promotes; reject deprecates the metric (never deletes it)."""
    if verdict == "accept":
        metric["lifecycle"] = "reviewed"
        _human(metric, "metric confirmed in review")
        return
    metric["lifecycle"] = "deprecated"
    metric["deprecation"] = {"reason": "rejected in review: not a meaningful metric"}
    _human(metric, "metric rejected in review")


def _apply_aggregate_mapping(sl: dict, agg: dict, verdict: str) -> None:
    """Accept records the verdict; reject stops routing to the table.

    Accept does NOT promote `routing.status` to `verified`: SPEC 2.7 reserves
    that for `mapping_source: lineage`, and whether a human verdict should also
    earn it is an open spec question. A reviewed lifecycle still outranks an
    inferred one under 2.6, so the verdict is not inert.
    """
    if verdict == "accept":
        agg["lifecycle"] = "reviewed"
        _human(agg, "mapping confirmed in review")
        return
    agg["lifecycle"] = "deprecated"
    agg["deprecation"] = {"reason": "rejected in review: mapping does not hold"}
    agg.setdefault("routing", {})["status"] = "advisory"
    agg.setdefault("consistency", {})["status"] = "divergent"
    _human(agg, "mapping rejected in review")
    _drop_from_routing(sl, agg["table"])


def _drop_from_routing(sl: dict, table: str) -> None:
    for r in (sl.get("repo_knowledge") or {}).get("routing", []):
        if table in (r.get("use") or []):
            r["use"] = [u for u in r["use"] if u != table]


def _apply_seam(sl: dict, rel: dict, verdict: str) -> None:
    """Accept: reviewed, kept across `stitch --refresh`; reject: deprecated, also kept.

    A rejected edge stays in the hub so the same inference does not come
    back on the next refresh; consumers do not traverse it.
    """
    if verdict == "accept":
        rel["lifecycle"] = "reviewed"
        _human(rel, "seam edge accepted in review")
    else:
        rel["lifecycle"] = "deprecated"
        _human(rel, "seam edge rejected in review: these tables are not related this way")


def _resolve_ref(sl: dict, ref: str) -> dict | None:
    kind, _, name = ref.partition(".")
    if kind == "seam":
        return next((r for r in sl.get("relationships", []) if r.get("name") == name), None)
    if kind == "metrics":
        return next((m for m in sl.get("metrics", []) if m["name"] == name), None)
    if kind == "aggregate_tables":
        return next((a for a in sl.get("aggregate_tables", []) if a["table"] == name), None)
    return None


_REF_APPLIERS = {
    "seam_edge": _apply_seam,
    "same_entity": _apply_seam,
    "discovered_filter": _apply_discovered_filter,
    "metric_plausibility": _apply_metric_plausibility,
    "aggregate_mapping": _apply_aggregate_mapping,
}


def apply(doc: dict, item: ReviewItem, verdict: str) -> None:
    """verdict: accept | reject. Mutates doc; sticky per SPEC."""
    sl = doc["semantic_layer"]
    if item.ref is not None:
        target = _resolve_ref(sl, item.ref)
        applier = _REF_APPLIERS.get(item.kind)
        if target is not None and applier is not None:
            applier(sl, target, verdict)
        return
    t = next(x for x in sl["tables"] if x["name"] == item.table)
    target = t if item.column is None else next(c for c in t["columns"] if c["name"] == item.column)
    applier = _APPLIERS.get(item.kind)
    if applier is not None:
        applier(target, item, verdict)


def _prov(obj) -> str:
    ps = obj.get("provenance") or []
    return "; ".join(p.get("detail", "")[:60] for p in ps[-2:])


def _full_prov(obj) -> str:
    """Untruncated provenance: for metric-tier items the evidence IS the argument.

    The per-group coverage counts are what the human is being asked to weigh,
    so clipping them to 60 characters would hide the reason to say yes.
    """
    return "; ".join(p.get("detail", "") for p in (obj.get("provenance") or []))


def apply_to_hub(hub: dict, item: ReviewItem, verdict: str) -> bool:
    """Apply a verdict from a hub's queue to the hub's OWN document, not the resolved set.

    The resolved set is a reading surface; saving it would write every
    member's tables into the hub file. Seam items live in the hub's
    relationships under the same names. An item on one of the hub's own
    tables is applied with the `hub/` prefix removed. An item that belongs
    to a member is not the hub's to settle: returns False, nothing changes.
    """
    hub_name = hub["semantic_layer"]["name"]
    if item.ref is not None and item.ref.startswith("seam."):
        apply(hub, item, verdict)
        return True
    if _owner(item.table) != hub_name:
        return False
    bare = ReviewItem(item.kind, item.table.split("/", 1)[1], item.column, item.claim,
                      item.evidence, ref=item.ref)
    apply(hub, bare, verdict)
    return True
