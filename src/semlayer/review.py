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

Every verdict is written into the document as a `human` provenance entry
carrying the date, the author when known, and a `detail` of the form
`accepted: <claim in words>` or `rejected: <claim in words>[; <consequence>]`.
`decisions()` reads those entries back, so "what did I decide" is answered
from the layer file itself (SPEC.md producer rule 9).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import re
from dataclasses import dataclass, field

REVIEW_THRESHOLD = 0.7

# The words a reader sees for a claim. The spec vocabulary (`name`, `code`,
# `snapshot_scd2`) stays in the file; nothing printed to a person is a raw
# type name -- "name rule -> name" taught us that.
TYPE_PHRASES = {
    "identifier": "identifies a row",
    "monetary_value": "holds an amount of money",
    "quantity": "holds a count or quantity",
    "rate": "holds a rate",
    "percentage": "holds a percentage",
    "timestamp_event": "records when something happened",
    "timestamp_effective": "records when a value took effect",
    "date": "holds a calendar date",
    "duration": "holds a length of time",
    "status_code": "holds a status code",
    "enum": "holds one of a fixed set of values",
    "flag": "is a yes/no flag",
    "free_text": "holds free text",
    "name": "holds the display name of the row",
    "url": "holds a URL",
    "code": "holds a short coded value",
    "geo_country": "holds a country",
    "geo_region": "holds a region or state",
    "geo_city": "holds a city",
    "geo_postal": "holds a postal code",
    "geo_coordinates": "holds coordinates",
    "pii_email": "holds an email address (personal data)",
    "pii_name": "holds a person's name (personal data)",
    "pii_phone": "holds a phone number (personal data)",
    "pii_address": "holds a postal address (personal data)",
    "pii_national_id": "holds a national id number (personal data)",
    "pii_other": "holds personal data",
    "json_object": "holds a JSON document",
    "unknown": "is untyped",
}
ROLE_PHRASES = {
    "primary_key": "the table's key",
    "unique_key": "unique per row",
    "natural_key": "the business key",
    "foreign_key": "a key into another table",
    "dimension": "grouped by",
    "measure": "summed",
    "metadata": "ETL bookkeeping, not for analysis",
}
TABLE_PHRASES = {
    "fact": "records events or transactions (a fact table)",
    "dimension": "describes one entity per row (a dimension)",
    "denormalized": "is a wide table with its dimensions joined in",
    "aggregate": "is a pre-summarised rollup of a fact table",
    "snapshot_scd2": "holds a dated version of each record per row (a snapshot table)",
    "operational": "is an application table, not shaped for analysis",
    "staging": "is an intermediate ETL step",
    "unknown": "is of unknown kind",
}


def describe_column(semantic_type: str | None, role: str | None) -> str:
    """`code` + `dimension` -> "holds a short coded value; grouped by (type code)"."""
    st, r = semantic_type or "unknown", role or "dimension"
    if st == "unknown" and r == "metadata":
        return "is an ETL bookkeeping column, not for analysis (type unknown)"
    if st == "unknown":
        return f"is untyped; {ROLE_PHRASES.get(r, r)} (type unknown)"
    return f"{TYPE_PHRASES.get(st, st)}; {ROLE_PHRASES.get(r, r)} (type {st})"


def describe_table(table_type: str | None) -> str:
    """`snapshot_scd2` -> "holds a dated version ... (table_type snapshot_scd2)"."""
    tt = table_type or "unknown"
    return f"{TABLE_PHRASES.get(tt, tt)} (table_type {tt})"


_AUTHOR: str | None = None
_SIGNAL = "human"


def set_author(name: str | None, signal: str = "human") -> None:
    """Who signs the verdicts this process writes, and as what.

    `signal` is `human` (the CLI passes git user.name or --by) or `agent`
    (an automated reviewer; `by` then names the agent and model). The file
    always says which, so a decision an agent took is never mistaken for one
    a person took.
    """
    global _AUTHOR, _SIGNAL
    _AUTHOR = name or None
    _SIGNAL = signal


_VERDICT_WORD = {"accept": "accepted", "reject": "rejected", "set": "set"}


# --------------------------------------------------------------- set verdict
#
# Accept says the engine was right; reject says it was wrong and nothing
# replaces the claim. `set:<value>` says what is true. The value is spec
# vocabulary only (a semantic type and role, a table type, a metric's base
# table or filter), never free text: a value in a semantic layer has to be
# something compile, lint and the MCP tools can act on.

def _is_set(verdict: str) -> bool:
    return verdict.startswith("set:")


def _set_value(verdict: str) -> str:
    return verdict[4:].strip()


def vocabulary() -> dict[str, list[str]]:
    """The spec's closed lists a `set` verdict may name, read from the schema."""
    from semlayer.validate import _load_schema
    s = _load_schema()
    d = s["$defs"]
    return {"semantic_type": list(d["semantic_type"]["enum"]),
            "entity_role": list(d["entity_role"]["enum"]),
            "table_type": list(d["table"]["properties"]["table_type"]["enum"])}


def set_shape(q: Question) -> str | None:
    """What `set` accepts on this question, or None where set is not available."""
    if q.kind == "low_confidence":
        return "<semantic_type>/<entity_role>" if q.items[0].column else "<table_type>"
    if q.kind == "metric_plausibility":
        return "base=<table> | filter=<sql expression>"
    return None


def check_set(q: Question, value: str, doc: dict) -> str | None:
    """Why `set:<value>` cannot be applied to this question, or None when it can."""
    shape = set_shape(q)
    if shape is None:
        return f"set is not available on a {q.kind} question; accept or reject it"
    if q.kind == "metric_plausibility":
        return _check_set_metric(q, value, doc)
    if q.items[0].column:
        return _check_set_column(value)
    return _check_set_table(value)


def _check_set_column(value: str) -> str | None:
    voc = vocabulary()
    st, sep, role = value.partition("/")
    if not sep or st not in voc["semantic_type"] or role not in voc["entity_role"]:
        return (f"a column is set as <semantic_type>/<entity_role>; types: "
                f"{', '.join(voc['semantic_type'])}; roles: {', '.join(voc['entity_role'])}")
    return None


def _check_set_table(value: str) -> str | None:
    voc = vocabulary()
    if value not in voc["table_type"]:
        return f"a table is set as one of: {', '.join(voc['table_type'])}"
    return None


def _check_set_metric(q: Question, value: str, doc: dict) -> str | None:
    key, sep, rest = value.partition("=")
    if key == "filter" and sep and rest.strip():
        return None
    if key != "base" or not sep:
        return "a metric is set as base=<table> or filter=<sql expression>"
    sl = doc["semantic_layer"]
    t = next((x for x in sl.get("tables", []) if x["name"] == rest), None)
    if t is None:
        return f"base={rest!r}: no such table in the document"
    cols = {c["name"] for c in t["columns"]}
    for it in q.items:
        metric = _resolve_ref(sl, it.ref or "") or {}
        col = str(metric.get("measure", "")).split(".", 1)[-1]
        if col and col not in cols:
            return f"base={rest}: it has no column {col!r}, which {metric.get('name')} measures"
    return None


def _human(obj: dict, detail: str) -> None:
    entry: dict = {"signal": _SIGNAL, "date": _dt.date.today().isoformat()}
    if _AUTHOR:
        entry["by"] = _AUTHOR
    entry["detail"] = detail
    obj.setdefault("provenance", []).append(entry)


def _verdict(obj: dict, verdict: str, what: str, consequence: str | None = None) -> None:
    detail = f"{_VERDICT_WORD.get(verdict, verdict)}: {what}"
    if consequence:
        detail += f"; {consequence}"
    _human(obj, detail)
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
    # A rollup whose sums reconciled to a fact, per group, has had its metric
    # proven by data; asking a human "is this a metric?" on top of that was
    # 764 of 11,845 items on the 15,000-table estate. An UNRECONCILED rollup
    # is still asked: nothing but a name says the summary means what it says.
    reconciled = {a["table"] for a in sl.get("aggregate_tables", []) or []
                  if a.get("lifecycle") != "deprecated"}
    for m in sl.get("metrics", []) or []:
        if m.get("lifecycle") != "inferred":
            continue
        base = _base_table(m)
        if base in reconciled:
            continue
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
    consequence: str | None
    if item.column is not None:
        if (target.get("semantic_type") or "unknown") == "unknown" \
                and target.get("entity_role", "dimension") == "dimension":
            # there is no claim to accept or reject; say so instead of
            # "is untyped; now untyped" (unknown + metadata IS a claim: ETL bookkeeping)
            what = "no type could be inferred for this column"
            consequence = "still untyped" if verdict != "accept" else "confirmed as untyped"
        else:
            what = describe_column(target.get("semantic_type"), target.get("entity_role"))
            consequence = "now untyped" if verdict != "accept" else None
    else:
        what = describe_table(target.get("table_type"))
        consequence = "now of unknown kind" if verdict != "accept" else None
    if _is_set(verdict):
        _set_claim(target, item, _set_value(verdict), what)
        return
    if verdict != "accept":
        if item.column is not None:
            target["semantic_type"] = "unknown"
            target["entity_role"] = "dimension"
        else:
            target["table_type"] = "unknown"
    target["lifecycle"] = "reviewed"
    _verdict(target, verdict, what, consequence)


def _set_claim(target: dict, item: ReviewItem, value: str, was: str) -> None:
    """Replace the engine's claim with the reviewer's. Human-authored: no confidence."""
    if item.column is not None:
        st, _, role = value.partition("/")
        target["semantic_type"], target["entity_role"] = st, role
        if role == "measure":
            target.setdefault("aggregations", {"allowed": ["sum", "avg", "min", "max", "count"],
                                               "default": "sum"})
        else:
            target.pop("aggregations", None)
        if st.startswith("pii_"):
            target["pii"] = True
        else:
            target.pop("pii", None)
        now = describe_column(st, role)
    else:
        target["table_type"] = value
        now = describe_table(value)
    target.pop("confidence", None)
    target["lifecycle"] = "reviewed"
    _verdict(target, "set", f"{was} -> {now}")


def _apply_llm_guess_enum(target: dict, verdict: str) -> None:
    """Accept promotes guessed decodes to human-sourced; reject drops them."""
    decs = {e["value"]: e["meaning"] for e in target.get("enum_values") or []
            if e.get("decode_source") == "llm_guess"}
    what = f"the coded values mean {decs}"
    if verdict == "accept":
        for e in target.get("enum_values", []):
            if e.get("decode_source") == "llm_guess":
                e["decode_source"] = "human"
        _verdict(target, verdict, what)
    else:
        target.pop("enum_values", None)
        _verdict(target, verdict, what, "guessed meanings removed")


def _apply_conflict(target: dict, item: ReviewItem, verdict: str) -> None:
    """Resolving a conflict clears it; accept also promotes lifecycle."""
    target.pop("conflicts", None)
    what = f"the inferred claim, over the disagreement: {item.claim}"
    if verdict == "accept":
        target["lifecycle"] = "reviewed"
        _verdict(target, verdict, what)
    else:
        _verdict(target, verdict, what, "left as inferred, disagreement cleared")


def _apply_fk_candidate(target: dict, item: ReviewItem, verdict: str) -> None:
    """Accept materializes the FK; either verdict clears the REVIEW-QUEUED marker."""
    m = re.search(r"-> (\S+)", item.claim)
    parent = m.group(1) if m else "?"
    if verdict == "accept" and m and item.column:
        target["entity_role"] = "foreign_key"
        target["foreign_key"] = {"references": m.group(1), "relationship": "many_to_one"}
        target["lifecycle"] = "reviewed"
    target["provenance"] = [p for p in target.get("provenance", [])
                            if "REVIEW-QUEUED" not in p.get("detail", "")]
    _verdict(target, verdict, f"is a key into {parent} (foreign key)",
             None if verdict == "accept" else "no relationship recorded")


_APPLIERS = {
    "low_confidence": lambda target, item, verdict: _apply_low_confidence(target, item, verdict),
    "llm_guess_enum": lambda target, item, verdict: _apply_llm_guess_enum(target, verdict),
    "conflict": lambda target, item, verdict: _apply_conflict(target, item, verdict),
    "fk_candidate": lambda target, item, verdict: _apply_fk_candidate(target, item, verdict),
}


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
        _verdict(metric, verdict, "applies a discovered rule",
                 "the metric no longer carries a filter, nothing to change")
        return
    base = _base_table(metric)
    what = f"applies the rule {expr}, which nobody declared"
    if verdict == "accept":
        metric["lifecycle"] = "reviewed"
        _verdict(metric, verdict, what)
        _promote_required_filter(sl, base, expr)
        return
    metric.pop("filter", None)
    _verdict(metric, verdict, what, "rule removed from the metric and from the table")
    _drop_required_filter(sl, base, expr)


def _promote_required_filter(sl: dict, base: str, expr: str) -> None:
    for t in sl["tables"]:
        if t["name"] != base:
            continue
        for f in (t.get("knowledge") or {}).get("required_filters", []):
            if f["expr"] == expr:
                _verdict(t, "accept", f"every query on this table applies {expr}")


def _drop_required_filter(sl: dict, base: str, expr: str) -> None:
    """Remove the rejected rule, and flag any table that inherited it."""
    for t in sl["tables"]:
        k = t.get("knowledge") or {}
        rfs = k.get("required_filters") or []
        if t["name"] == base:
            kept = [f for f in rfs if f["expr"] != expr]
            if len(kept) != len(rfs):
                k["required_filters"] = kept
                _verdict(t, "reject", f"every query on this table applies {expr}",
                         "rule removed")
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
    what = f"is a metric over {_base_table(metric)}"
    if _is_set(verdict):
        key, _, rest = _set_value(verdict).partition("=")
        if key == "base":
            col = metric.get("measure", "").split(".", 1)[-1]
            metric["measure"] = f"{rest}.{col}"
            now = f"is a metric over {rest}"
        else:
            metric["filter"] = rest
            now = f"{what}, filtered to {rest}"
        metric.pop("confidence", None)
        metric["lifecycle"] = "reviewed"
        _verdict(metric, "set", f"{what} -> {now}")
        return
    if verdict == "accept":
        metric["lifecycle"] = "reviewed"
        _verdict(metric, verdict, what)
        return
    metric["lifecycle"] = "deprecated"
    metric["deprecation"] = {"reason": "rejected in review: not a meaningful metric"}
    _verdict(metric, verdict, what, "metric deprecated")


def _apply_aggregate_mapping(sl: dict, agg: dict, verdict: str) -> None:
    """Accept records the verdict; reject stops routing to the table.

    Accept does NOT promote `routing.status` to `verified`: SPEC 2.7 reserves
    that for `mapping_source: lineage`, and whether a human verdict should also
    earn it is an open spec question. A reviewed lifecycle still outranks an
    inferred one under 2.6, so the verdict is not inert.
    """
    what = f"is a rollup of {agg.get('aggregates')}"
    if verdict == "accept":
        agg["lifecycle"] = "reviewed"
        _verdict(agg, verdict, what)
        return
    agg["lifecycle"] = "deprecated"
    agg["deprecation"] = {"reason": "rejected in review: mapping does not hold"}
    agg.setdefault("routing", {})["status"] = "advisory"
    agg.setdefault("consistency", {})["status"] = "divergent"
    _verdict(agg, verdict, what, "no longer routed to")
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
    a, b = rel["from"]["table"], rel["to"]["table"]
    if rel.get("same_entity"):
        what = f"{a} and {b} are the same entity"
    else:
        what = (f"{a}.{','.join(rel['from']['columns'])} joins "
                f"{b}.{','.join(rel['to']['columns'])}")
    if verdict == "accept":
        rel["lifecycle"] = "reviewed"
        _verdict(rel, verdict, what)
    else:
        rel["lifecycle"] = "deprecated"
        _verdict(rel, verdict, what, "not traversed")


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


@dataclass
class Decision:
    """One human verdict read back from the document."""

    verdict: str          # accepted | rejected | noted
    where: str            # table, table.column, metrics.<name>, aggregate_tables.<t>, seam.<r>
    what: str
    date: str | None = None
    by: str | None = None
    signal: str = "human"  # human | agent


_LEGACY_WHAT = "(claim not recorded: written before verdicts carried their claim)"


def _parse_detail(detail: str) -> tuple[str, str]:
    for v in ("accepted", "rejected", "set"):
        if detail.startswith(v + ": "):
            return v, detail[len(v) + 2:]
    low = detail.lower()
    if "reject" in low:
        return "rejected", f"{detail} {_LEGACY_WHAT}"
    if "accept" in low or "confirmed" in low:
        return "accepted", f"{detail} {_LEGACY_WHAT}"
    return "noted", detail


def _human_entries(obj: dict, where: str) -> list[Decision]:
    out = []
    for p in obj.get("provenance") or []:
        if p.get("signal") not in ("human", "agent"):
            continue
        verdict, what = _parse_detail(p.get("detail", ""))
        out.append(Decision(verdict, where, what, p.get("date"), p.get("by"),
                            p.get("signal", "human")))
    return out


def decisions(doc: dict) -> list[Decision]:
    """Every human verdict in the document, newest first (undated last)."""
    sl = doc["semantic_layer"]
    out: list[Decision] = []
    for t in sl.get("tables", []) or []:
        out += _human_entries(t, t["name"])
        for c in t.get("columns", []) or []:
            out += _human_entries(c, f"{t['name']}.{c['name']}")
    for m in sl.get("metrics", []) or []:
        out += _human_entries(m, f"metrics.{m['name']}")
    for a in sl.get("aggregate_tables", []) or []:
        out += _human_entries(a, f"aggregate_tables.{a['table']}")
    for r in sl.get("relationships", []) or []:
        crosses = _owner(r["from"]["table"]) != _owner(r["to"]["table"])
        out += _human_entries(r, f"{'seam' if crosses else 'relationships'}.{r.get('name', '')}")
    dated = [d for d in out if d.date]
    dated.sort(key=lambda d: (d.signal, d.by or ""))              # one block per author
    dated.sort(key=lambda d: d.date or "", reverse=True)          # newest day first (stable)
    return dated + [d for d in out if not d.date]


# --------------------------------------------------------------- questions
#
# The queue a person or an agent sees is QUESTIONS, not items. Two items are
# in one question only when the engine knows exactly the same things about
# them apart from their names: same kind of claim, same spec fields, same
# reason string the engine wrote, same confidence. Not similar -- identical.
# A grouped question is the per-item questions with the name blanked out;
# nothing the engine could have shown one at a time is lost.

REASON_WORDS = {
    "name rule -> name": "the column name ends in _name",
    "name rule -> code": "the column name contains a word the engine reads as a code",
    "name rule -> status_code": "the column name says status",
    "name rule -> flag": "the column name says flag or is_",
    "integer fallback": "an integer column with many distinct values and no other signal",
    "decimal fallback": "a decimal column with no other signal",
    "audit/etl name": "the column name looks like ETL bookkeeping",
    "audit/etl timestamp name": "the column name looks like an ETL timestamp",
    "no rule matched": "nothing in the name or the statistics matched a rule",
    "id-named but low cardinality": "the name says id but there are only a few distinct values",
    "table_type: snapshot naming/flag (no validity columns)":
        "the table name says _hist or _snapshot but there are no valid_from / valid_to columns",
    "table_type: isolated, no measures":
        "nothing joins to it, it joins to nothing, and it has no measures",
    "table_type: measure-heavy, few FKs resolved":
        "it has several measures but few of its keys resolved to a parent",
    "table_type: no decisive signal": "nothing about the name, keys or measures was decisive",
    "ETL bookkeeping column name": "the column name is one ETL tooling writes",
    "raw payload column name": "the column name says it holds the raw record",
    "staging tier by name prefix, nothing joins to it":
        "the name prefix says staging (int__, stg_), and nothing joins to it",
    "staging tier by name prefix, no other signal":
        "the name prefix says staging (int__, stg_), and nothing else was decisive",
    "integer with N distinct values: grouped, not summed":
        "an integer column with only a few distinct values, so grouped rather than summed",
    "table_type: N FK out + N measures, nothing references it":
        "it has a key out and a measure but nothing references it",
}

_DIGITS_RE = re.compile(r"\d+")


def _norm_reason(raw: str) -> str:
    """`integer with 22 distinct values` and `... 23 ...` are one reason, not two.

    The engine's reason strings carry the number they measured; the question
    is the same whatever the number, so the key and the words see `N`.
    """
    return _DIGITS_RE.sub("N", raw)

_CONSEQUENCE = {
    "column": ("keeps the type, marked reviewed", "clears the type; the column stays, untyped"),
    "table": ("keeps the table kind, marked reviewed", "clears the table kind to unknown"),
    "metric_plausibility": ("keeps the metric, marked reviewed",
                            "deprecates the metric; agents see it as unusable"),
    "discovered_filter": ("the rule becomes a required filter on the table",
                          "the rule is removed from the metric and the table"),
    "aggregate_mapping": ("keeps the mapping, marked reviewed",
                          "deprecates the mapping; the table is no longer routed to"),
    "llm_guess_enum": ("the guessed meanings become human-sourced",
                       "the guessed meanings are removed"),
    "conflict": ("the claim stands, marked reviewed; the disagreement is cleared",
                 "the claim stays inferred; the disagreement is cleared"),
    "fk_candidate": ("the foreign key is recorded", "no relationship is recorded"),
    "seam_edge": ("the join is kept across refreshes", "the join is deprecated and not traversed"),
    "same_entity": ("the two tables are recorded as one entity",
                    "the pair is deprecated; they are not the same entity"),
}


@dataclass
class Question:
    """One decision, over every object the engine made the identical claim about."""

    key: str
    kind: str
    what: str                 # the claim in words, spec vocabulary in brackets
    reason: str               # what the engine saw, in words
    reason_raw: str           # the engine's own string
    confidence: float | None
    items: list[ReviewItem]
    on_accept: str
    on_reject: str
    decidable: bool = True    # False: shown, but a verdict would change nothing
    n: int = 0
    notes: list[dict] = field(default_factory=list)

    @property
    def objects(self) -> list[str]:
        """Every object's address, in queue order."""
        return [i.location for i in self.items]

    @property
    def noun(self) -> str:
        """What one object is called: column, table, metric, join, pair."""
        if self.kind == "low_confidence":
            return "column" if self.items[0].column else "table"
        return {"metric_plausibility": "metric", "discovered_filter": "metric",
                "aggregate_mapping": "aggregate table", "seam_edge": "join",
                "same_entity": "pair", "inventory_only": "tier"}.get(self.kind, "object")

    def distinct_names(self) -> list[str]:
        """What a person scans: the column names (or table stems), not 328 locations."""
        if self.items[0].column:
            return sorted({i.column for i in self.items if i.column})
        return sorted({_stem(i.table) for i in self.items})

    def as_dict(self) -> dict:
        """The question for an agent: every field, every object, nothing truncated."""
        return {
            "n": self.n, "key": self.key, "kind": self.kind, "decidable": self.decidable,
            "claim": self.what, "engine_saw": self.reason, "engine_saw_raw": self.reason_raw,
            "confidence": self.confidence, "count": len(self.items),
            "distinct_names": self.distinct_names(),
            "objects": [{"where": i.location, "table": i.table, "column": i.column,
                         "claim": i.claim, "evidence": i.evidence, "ref": i.ref}
                        for i in self.items],
            "on_accept": self.on_accept, "on_reject": self.on_reject,
            "set_accepts": set_shape(self), "notes": self.notes,
        }


_STAGE_RE = re.compile(r"^(stg_[a-z0-9]+__|int__|dim_|fct_|agg_)", re.I)


def _stem(table: str) -> str:
    return _STAGE_RE.sub("", table.rsplit("/", 1)[-1])


def _target(sl: dict, item: ReviewItem) -> dict | None:
    if item.ref is not None:
        return _resolve_ref(sl, item.ref)
    t = next((x for x in sl["tables"] if x["name"] == item.table), None)
    if t is None or item.column is None:
        return t
    return next((c for c in t["columns"] if c["name"] == item.column), None)


def _reason_of(target: dict | None) -> str:
    ps = (target or {}).get("provenance") or []
    return ps[-1].get("detail", "") if ps else ""


def _key(*parts: str) -> str:
    return "q-" + hashlib.sha1("|".join(parts).encode()).hexdigest()[:8]


def _classify(it: ReviewItem, tg: dict | None, raw: str, tables: dict) -> tuple:
    """(key, claim in words, raw reason, reason in words, confidence, on_accept, on_reject)."""
    tg = tg or {}
    norm = _norm_reason(raw)
    if it.kind == "low_confidence":
        if it.column:
            what = describe_column(tg.get("semantic_type"), tg.get("entity_role"))
            acc, rej = _CONSEQUENCE["column"]
        else:
            what = describe_table(tg.get("table_type"))
            acc, rej = _CONSEQUENCE["table"]
        conf = tg.get("confidence")
        return (_key(it.kind, what, norm, str(conf)), what, raw, REASON_WORDS.get(norm, norm),
                conf, acc, rej)
    if it.kind == "metric_plausibility":
        tt = tables.get(it.table, {}).get("table_type") or "unknown"
        what = f"is a metric over a table that {TABLE_PHRASES.get(tt, tt)} (table_type {tt})"
        words = ("the base table is a pre-summarised rollup, and metrics are normally "
                 "defined on the fact it rolls up")
        acc, rej = _CONSEQUENCE[it.kind]
        return _key(it.kind, tt), what, f"metric on {tt}", words, None, acc, rej
    acc, rej = _CONSEQUENCE.get(it.kind, ("marked reviewed", "claim removed"))
    return (_key(it.kind, it.location, it.claim), it.claim, raw, REASON_WORDS.get(norm, norm),
            None, acc, rej)


def group(items: list[ReviewItem], doc: dict) -> tuple[list[Question], list[Question]]:
    """Fold the queue into questions. Returns (asked, not_asked).

    not_asked holds what is shown but not asked: claims the engine never made
    (semantic_type unknown with no rule matched: nothing to accept or reject)
    and the inventory-only tier lines (a verdict changes nothing; the lever is
    `tier` on the table).
    """
    sl = doc["semantic_layer"]
    tables = {t["name"]: t for t in sl.get("tables") or []}
    asked: dict[str, Question] = {}
    untyped: dict[str, list[ReviewItem]] = {}
    tiers: list[Question] = []
    for it in items:
        tg = _target(sl, it)
        raw = _reason_of(tg)
        if it.kind == "inventory_only":
            tiers.append(Question(_key("tier", it.ref or ""), it.kind, it.claim,
                                  it.evidence, "", None, [it], "nothing changes",
                                  "nothing changes: set `tier` on the tables you want modelled",
                                  decidable=False))
            continue
        if it.kind == "low_confidence" and it.column and tg is not None \
                and (tg.get("semantic_type") or "unknown") == "unknown" \
                and tg.get("entity_role", "dimension") == "dimension":
            untyped.setdefault(it.column, []).append(it)
            continue
        key, what, raw, words, conf, acc, rej = _classify(it, tg, raw, tables)
        q = asked.get(key)
        if q is None:
            q = asked[key] = Question(key, it.kind, what, words, raw, conf, [], acc, rej)
        q.items.append(it)
    out = sorted(asked.values(), key=lambda q: -len(q.items))
    not_asked: list[Question] = []
    if untyped:
        flat = [i for its in untyped.values() for i in its]
        names = sorted(untyped, key=lambda c: -len(untyped[c]))
        not_asked.append(Question(
            _key("untyped"), "untyped", "could not be typed at all: nothing to accept or reject",
            "; ".join(f"{c} ({len(untyped[c])} tables)" for c in names), "no rule matched",
            None, flat, "nothing changes", "nothing changes: they stay untyped until a rule "
            "or a reviewer names them", decidable=False))
    not_asked += tiers
    for n, q in enumerate(out + not_asked, 1):
        q.n = n
    notes = sl.get("review_notes") or []
    for q in out + not_asked:
        q.notes = [x for x in notes if x.get("question") == q.key]
    return out, not_asked


def answer(doc: dict, q: Question, verdict: str, exclude: set[str] | None = None,
           hub: dict | None = None, members: dict[str, dict] | None = None,
           touched: set[str] | None = None) -> int:
    """Apply one verdict to every object in the question; returns how many it reached.

    On a hub, an object the hub owns (its seam, its own tables) is applied to
    the hub document. With `members` (name -> that member's own document) an
    object a member owns is applied to the member, so one verdict settles the
    same question in every file that holds it; `touched` collects the names
    of the members written to, so the caller saves exactly those. Without
    `members`, member objects are not the hub's to settle and are skipped.
    """
    if not q.decidable:
        return 0
    n = 0
    for it in q.items:
        if exclude and it.location in exclude:
            continue
        if hub is None:
            apply(doc, it, verdict)
            n += 1
        elif apply_to_hub(hub, it, verdict):
            n += 1
        elif members is not None and _owner(it.table) in members:
            owner = _owner(it.table)
            bare = ReviewItem(it.kind, it.table.split("/", 1)[1], it.column, it.claim,
                              it.evidence, ref=it.ref)
            apply(members[owner], bare, verdict)
            if touched is not None:
                touched.add(owner)
            n += 1
    return n


def decisions_across(hub: dict, members: dict[str, dict]) -> list[Decision]:
    """The hub's decisions and every member's, member objects prefixed `member/`."""
    out = decisions(hub)
    for name, mdoc in members.items():
        for d in decisions(mdoc):
            d.where = f"{name}/{d.where}"
            out.append(d)
    dated = [d for d in out if d.date]
    dated.sort(key=lambda d: (d.signal, d.by or ""))
    dated.sort(key=lambda d: d.date or "", reverse=True)
    return dated + [d for d in out if not d.date]


def note(doc: dict, q: Question, text: str) -> dict:
    """Leave a note on a question without deciding it.

    An agent that could not decide says why; a human parks one for later.
    """
    entry: dict = {"date": _dt.date.today().isoformat(), "signal": _SIGNAL,
                   "question": q.key, "about": f"{len(q.items)} {q.noun}(s): {q.what}",
                   "note": text}
    if _AUTHOR:
        entry["by"] = _AUTHOR
    doc["semantic_layer"].setdefault("review_notes", []).append(entry)
    q.notes.append(entry)
    return entry


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
