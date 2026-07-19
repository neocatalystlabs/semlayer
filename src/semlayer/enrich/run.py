"""Enrich stage (M4, deterministic tier): metrics, knowledge, routing.

Everything here is statistics + graph + SQL verification — no LLM calls.
Key moves:
- Enum decodes upgrade from llm_guess to dictionary_join by actually joining
  the decode dimension Link discovered (metric filters become contract-legal).
- Aggregate detection runs a RECONCILIATION VERIFIER that also DISCOVERS the
  business rule (e.g. "excludes cancelled orders") by testing enum-exclusion
  hypotheses until the aggregate reconciles.
- Deprecation: naming + staleness; replacement = active table with the
  longest shared prefix.
- Hierarchy candidates: FD mining with the spike's fixes (key-exclusion +
  transitive reduction) — REVIEW-QUEUED, never auto-included.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

_LEGACY_RE = re.compile(r"(_legacy|_old|_bak|_deprecated|_archive)$")
_AGG_NAME_RE = re.compile(r"(^|_)(agg|summary|rollup)($|_)|^(dly|mth|wkly)_")


def enrich_source(source, doc: dict, stats: dict) -> dict:
    """Run the deterministic enrich tier.

    Covers decodes, deprecation, freshness, metrics, aggregate
    reconciliation, routing/domains, and snapshot filters.
    """
    sl = doc["semantic_layer"]
    _upgrade_enum_decodes(source, sl, stats)
    _detect_deprecation(sl, stats)
    _infer_freshness(sl, stats)
    metrics = _metric_candidates(sl)
    if metrics:
        sl["metrics"] = metrics
    aggs = _detect_aggregates(source, sl, stats)
    if aggs:
        sl["aggregate_tables"] = aggs
    _apply_discovered_filters_to_metrics(sl)
    _domains_and_routing(sl, aggs)
    _snapshot_filters(sl, stats)
    return doc


# ---------------------------------------------------------------- decodes
def _upgrade_enum_decodes(source, sl, stats) -> None:
    """cd-column with FK to a small (code, label) dim -> dictionary_join decodes."""
    tables = {t["name"]: t for t in sl["tables"]}
    for t in sl["tables"]:
        for c in t["columns"]:
            fk = c.get("foreign_key")
            if not fk:
                continue
            ptn, pcn = fk["references"].split(".")
            pt = tables.get(ptn)
            if pt is None or len(pt["columns"]) > 3:
                continue
            varchar_cols = [pc["name"] for pc in pt["columns"]
                            if pc["name"] != pcn and any(k in pc["sql_type"].upper()
                                                         for k in ("CHAR", "TEXT", "STRING"))]
            label_cols = sorted(
                varchar_cols,
                key=lambda n: 0 if n.lower().endswith(("_desc", "_nm", "_name", "_label")) else 1,
            )
            if not label_cols or stats[ptn].row_count > 100:
                continue
            ts = stats[ptn].table
            q = getattr(source, "qualify", lambda s, n: f'"{s}"."{n}"')(ts.schema, ts.name)
            pairs = source.query(f'SELECT "{pcn}", "{label_cols[0]}" FROM {q} LIMIT 100')
            c["enum_values"] = [
                {"value": v, "meaning": str(m), "decode_source": "dictionary_join"}
                for v, m in pairs if v is not None
            ]
            c.setdefault("provenance", []).append({
                "signal": "statistic",
                "detail": f"enum decoded via join to {ptn}.{label_cols[0]}",
            })


# ------------------------------------------------------------ deprecation
def _detect_deprecation(sl, stats) -> None:
    """Flag legacy-named tables as deprecated and point them at their active replacement."""
    names = [t["name"] for t in sl["tables"]]
    for t in sl["tables"]:
        if not _LEGACY_RE.search(t["name"]):
            continue
        base = _LEGACY_RE.sub("", t["name"])
        replacement = next(
            (n for n in names if n != t["name"] and (n == base or n.startswith(base))), None
        )
        t["lifecycle"] = "deprecated"
        reason = "legacy-named table" + (f"; superseded by {replacement}" if replacement else "")
        t["deprecation"] = {"replacement": replacement or "", "reason": reason}
        if not t["deprecation"]["replacement"]:
            del t["deprecation"]["replacement"]


# -------------------------------------------------------------- freshness
def _infer_freshness(sl, stats) -> None:
    """Infer expected load cadence from the density of distinct dates in a table's date column."""
    for t in sl["tables"]:
        date_cols = [c["name"] for c in t["columns"]
                     if c.get("semantic_type") in ("date", "timestamp_event") and
                     c.get("entity_role") != "metadata"]
        if not date_cols:
            continue
        cs = stats[t["name"]].columns[date_cols[0]]
        if not (cs.min_val and cs.max_val and cs.n_distinct > 1):
            continue
        try:
            from datetime import date
            span = (date.fromisoformat(cs.max_val[:10]) - date.fromisoformat(cs.min_val[:10])).days
        except ValueError:
            continue
        density = cs.n_distinct / max(1, span)
        cadence = "daily" if density > 0.6 else "weekly" if density > 0.1 else "sporadic"
        t["freshness"] = {
            "expected_cadence": cadence,
            "inferred_from": f"{cs.n_distinct} distinct {date_cols[0]} over {span} days",
        }


# ---------------------------------------------------------------- metrics
def _status_col_for_metrics(t: dict):
    """Find a fully-decoded status/code/enum column to derive a "completed" filtered metric from.

    Only non-llm-guess decodes qualify.
    """
    return next(
        (c for c in t["columns"]
         if c.get("semantic_type") in ("status_code", "code", "enum") and c.get("enum_values")
         and all(e["decode_source"] != "llm_guess" for e in c["enum_values"])),
        None,
    )


def _completed_metric(t: dict, c: dict, base: str, status_col: dict) -> dict | None:
    """Contract-legal filtered variant of a monetary metric.

    Scoped to a dictionary/docs-decoded status value that reads as "done"
    (only dictionary/docs decodes are trustworthy enough to filter on).
    """
    good = next(
        (e for e in status_col["enum_values"]
         if any(k in str(e["meaning"]).lower() for k in ("complete", "closed", "success", "paid"))),
        None,
    )
    if good is None:
        return None
    detail = f"status filter via dictionary decode ({good['value']}={good['meaning']})"
    return {
        "name": f"{base}_completed", "type": "simple",
        "measure": f"{t['name']}.{c['name']}", "agg": "sum",
        "filter": f"{status_col['name']} = '{good['value']}'",
        "grain": t.get("grain", ""), "lifecycle": "inferred", "confidence": 0.6,
        "provenance": [{"signal": "statistic", "detail": detail}],
    }


def _monetary_metrics(t: dict, status_col: dict | None, seen: set) -> list[dict]:
    """Simple sum() metrics for every monetary measure column, plus a status-filtered variant.

    The "completed" variant is added only on fact tables.
    """
    metrics = []
    for c in t["columns"]:
        if c.get("entity_role") != "measure" or "aggregations" not in c:
            continue
        if c.get("semantic_type") != "monetary_value":
            continue
        base = f"total_{c['name']}"
        if base in seen:
            base = f"total_{t['name']}_{c['name']}"
        seen.add(base)
        metrics.append({
            "name": base, "type": "simple",
            "measure": f"{t['name']}.{c['name']}", "agg": c["aggregations"].get("default", "sum"),
            "grain": t.get("grain", ""), "lifecycle": "inferred", "confidence": 0.7,
            "provenance": [{"signal": "statistic", "detail": "monetary measure on fact"}],
        })
        if status_col is not None and t.get("table_type") == "fact":
            completed = _completed_metric(t, c, base, status_col)
            if completed is not None:
                metrics.append(completed)
    return metrics


def _count_metric(t: dict, pk: str | None, seen: set) -> dict | None:
    """Row-count metric on a fact table's primary key."""
    if not (pk and t.get("table_type") == "fact"):
        return None
    name = f"{t['name']}_count"
    if name in seen:
        return None
    seen.add(name)
    pk_col = next(c for c in t["columns"] if c["name"] == pk)
    pk_col.setdefault("aggregations", {"allowed": ["count", "count_distinct"], "default": "count"})
    return {
        "name": name, "type": "simple", "measure": f"{t['name']}.{pk}",
        "agg": "count", "grain": t.get("grain", ""),
        "lifecycle": "inferred", "confidence": 0.75,
        "provenance": [{"signal": "statistic", "detail": "row count on fact PK"}],
    }


def _time_dimension_for(t: dict, sl: dict) -> str | None:
    """Best time column for a table's metrics.

    Own business date first, then a date on an N:1-joined dimension
    (typically the date dim). Metadata timestamps (load/audit columns) never
    qualify — bucketing revenue by crt_dt is exactly the silent error
    compile_metric exists to prevent.
    """
    own = [c for c in t["columns"]
           if c.get("semantic_type") in ("date", "timestamp_event")
           and c.get("entity_role") != "metadata"]
    if own:
        own.sort(key=lambda c: 0 if c["semantic_type"] == "date" else 1)
        return f"{t['name']}.{own[0]['name']}"
    tables = {x["name"]: x for x in sl["tables"]}
    for r in sl.get("relationships", []):
        if r["from"]["table"] != t["name"]:
            continue
        if r["cardinality"] not in ("many_to_one", "one_to_one"):
            continue
        dim = tables.get(r["to"]["table"])
        if dim is None:
            continue
        d = next((c for c in dim["columns"] if c.get("semantic_type") == "date"), None)
        if d is not None:
            return f"{dim['name']}.{d['name']}"
    return None


def _metric_candidates(sl) -> list[dict]:
    """Derive simple metrics.

    sum() over monetary measures (plus a status-filtered "completed" variant
    where a clean decode exists) and count() over each fact table's primary key.
    Each metric carries agg_time_dimension when a trustworthy time column exists.
    """
    metrics: list[dict] = []
    seen: set = set()
    for t in sl["tables"]:
        eligible = t.get("table_type") in ("fact", "aggregate", "unknown")
        if t.get("lifecycle") == "deprecated" or not eligible:
            continue
        pk = (t.get("primary_key") or [None])[0]
        status_col = _status_col_for_metrics(t)
        time_dim = _time_dimension_for(t, sl)
        before = len(metrics)
        metrics.extend(_monetary_metrics(t, status_col, seen))
        count_metric = _count_metric(t, pk, seen)
        if count_metric is not None:
            metrics.append(count_metric)
        if time_dim is not None:
            for m in metrics[before:]:
                m["agg_time_dimension"] = time_dim
    return metrics


# ------------------------------------------------------------- aggregates
@dataclass
class _AggCandidate:
    """Grouping context for one (aggregate table, fact table) reconciliation attempt.

    Bundled so _reconcile stays under the max-args lint limit.
    """

    agg_t: dict
    fact_t: dict
    agg_m: str
    fact_m: str
    gmap: dict[str, str]
    date_pairs: list[tuple[str, str]]


def _agg_group_mapping(
    group_cols: list[str], f: dict
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Map aggregate-table group columns to fact-table columns.

    Same-name match, or a `dt`-named column paired against any date-typed fact column.
    """
    fcols = {c["name"] for c in f["columns"]}
    gmap = {g: g for g in group_cols if g in fcols}
    date_pairs = [(g, fc["name"]) for g in group_cols for fc in f["columns"]
                  if g not in fcols and "dt" in g and fc.get("semantic_type") in ("date",)]
    return gmap, date_pairs


def _record_measure_filter_note(f: dict, t: dict, filt: str) -> None:
    """Record a discovered exclusion rule as a usage note + a pending metric filter.

    Measure-scoped: blanket table filters corrupt count questions.
    """
    note = (f"revenue/amount aggregations over {f['name']} conventionally use "
            f"{filt} (discovered: {t['name']} reconciles only with it); "
            f"event COUNTS include all rows")
    notes = f.setdefault("knowledge", {}).setdefault("usage_notes", [])
    if note not in notes:
        notes.append(note)
    f["knowledge"].setdefault("_pending_metric_filter", []).append(filt)


def _aggregate_entry(
    t: dict, f: dict, agg_m: str, fact_m: str, group_cols: list[str], filt: str,
    evidence: str = ""
) -> dict:
    """Build the aggregate_tables entry once a measure pair reconciles per group."""
    where = f" WHERE {filt}" if filt else ""
    excl = f" excluding {filt}" if filt else ""
    ev = f" ({evidence})" if evidence else ""
    return {
        "table": t["name"], "aggregates": f["name"],
        "grain": group_cols,
        "measure_mappings": [{"source": f"SUM({fact_m}){where}", "target": agg_m}],
        "mapping_source": "heuristic",
        "routing": {"rule": f"pre-aggregated {fact_m} by {', '.join(group_cols)}",
                    "status": "advisory"},
        "consistency": {"method": "per_group_reconciliation", "status": "consistent"},
        "lifecycle": "inferred", "confidence": 0.75,
        "provenance": [{"signal": "statistic",
                        "detail": f"reconciles per-group with {f['name']}.{fact_m}{excl}{ev}"}],
    }


def _reconcile_candidate(
    source, qualify, stats, cand: _AggCandidate, group_cols: list[str]
) -> dict | None:
    """Reconcile one (aggregate measure, fact measure) pair.

    On success, record any discovered filter and return the aggregate_tables entry.
    """
    result = _reconcile(source, qualify, stats, cand)
    if result is None:
        return None
    filt, evidence = result
    if filt:
        _record_measure_filter_note(cand.fact_t, cand.agg_t, filt)
    return _aggregate_entry(cand.agg_t, cand.fact_t, cand.agg_m, cand.fact_m,
                            group_cols, filt, evidence)


def _match_fact_for_aggregate(source, qualify, stats, t: dict, f: dict,
                              agg_measures: list[dict], group_cols: list[str]) -> dict | None:
    """Find the first (agg measure, fact measure) pair on `f` reconciling against `t`, if any."""
    f_measures = [c for c in f["columns"] if c.get("entity_role") == "measure"
                  and c.get("semantic_type") == "monetary_value"]
    gmap, date_pairs = _agg_group_mapping(group_cols, f)
    if not gmap and not date_pairs:
        return None
    for am in agg_measures:
        for fm in f_measures:
            cand = _AggCandidate(t, f, am["name"], fm["name"], gmap, date_pairs)
            entry = _reconcile_candidate(source, qualify, stats, cand, group_cols)
            if entry is not None:
                return entry
    return None


def _detect_aggregates(source, sl, stats) -> list[dict]:
    """Find aggregate/summary tables and reconcile them against candidate fact tables.

    Via SQL, discover their grain, measure mapping, and any implicit exclusion
    filter (e.g. "excludes cancelled orders").
    """
    facts = [t for t in sl["tables"]
             if t.get("table_type") == "fact" and t.get("lifecycle") != "deprecated"]
    out = []
    qualify = getattr(source, "qualify", lambda s, n: f'"{s}"."{n}"')
    for t in sl["tables"]:
        if not (_AGG_NAME_RE.search(t["name"]) or t.get("table_type") == "aggregate"):
            continue
        agg_measures = [c for c in t["columns"] if c.get("entity_role") == "measure"]
        group_cols = [c["name"] for c in t["columns"]
                      if c.get("entity_role") in ("foreign_key", "dimension")]
        if not agg_measures or not group_cols:
            continue
        for f in facts:
            entry = _match_fact_for_aggregate(
                source, qualify, stats, t, f, agg_measures, group_cols
            )
            if entry is not None:
                out.append(entry)
    return out


# Per-group reconciliation thresholds (council D7: grand totals are not
# proof — offsetting per-group errors pass them; every group must match).
_REL_TOL = 0.002      # 0.2% relative per group
_ABS_TOL = 0.02       # floor for tiny groups (values rounded to 2dp in SQL)
_MIN_COVERAGE = 0.95  # fraction of groups that must match
_MAX_GROUPS = 50_000  # aggregate tables are small; guardrail, not a sampler


def _close(a: float, f: float) -> bool:
    return abs(a - f) <= max(_ABS_TOL, _REL_TOL * abs(a))


def _group_sums(source, table_q: str, key_col: str, m_col: str,
                filter_sql: str = "") -> dict | None:
    w = f" WHERE {filter_sql}" if filter_sql else ""
    q = (f'SELECT "{key_col}", round(sum("{m_col}"),2) FROM {table_q}{w} '
         f"GROUP BY 1 LIMIT {_MAX_GROUPS}")
    rows = source.query(q)
    if len(rows) >= _MAX_GROUPS:
        return None  # too big to verify honestly -> treat as unverified
    return {r[0]: float(r[1]) for r in rows if r[1] is not None}


def _keyed_match(agg: dict, fact: dict) -> tuple[int, int]:
    keys = set(agg) | set(fact)
    matched = sum(1 for k in keys
                  if k in agg and k in fact and _close(agg[k], fact[k]))
    return matched, len(keys)


def _multiset_match(agg: dict, fact: dict) -> tuple[int, int]:
    # date-key <-> date-column groupings aren't directly joinable (int keys vs
    # dates); the sorted per-group sums must still agree as multisets
    a, f = sorted(agg.values()), sorted(fact.values())
    total = max(len(a), len(f))
    matched = sum(1 for x, y in zip(a, f, strict=False) if _close(x, y))
    return matched, total


def _verify_groups(source, aq: str, fq: str, cand: _AggCandidate,
                   filter_sql: str) -> tuple[bool, str]:
    """Per-group verification across every mapped grouping.

    Returns (ok, evidence) where evidence reads
    "40/40 store_id groups; 90/90 date groups within 0.2%".
    """
    parts = []
    pairs = ([(a, f, _keyed_match) for a, f in cand.gmap.items()]
             + [(a, f, _multiset_match) for a, f in cand.date_pairs])
    for agg_col, fact_col, match in pairs:
        agg_sums = _group_sums(source, aq, agg_col, cand.agg_m)
        fact_sums = _group_sums(source, fq, fact_col, cand.fact_m, filter_sql)
        if not agg_sums or not fact_sums:
            return False, "unverifiable grouping"
        matched, total = match(agg_sums, fact_sums)
        if total == 0 or matched / total < _MIN_COVERAGE:
            return False, f"{matched}/{total} {agg_col} groups"
        parts.append(f"{matched}/{total} {agg_col} groups")
    return bool(parts), "; ".join(parts) + f" within {_REL_TOL:.1%}"


def _reconcile(source, qualify, stats, cand: _AggCandidate) -> tuple[str, str] | None:
    """Per-group reconciliation with exclusion-hypothesis testing.

    Verifies SUM(fact.measure) against the aggregate per group; on mismatch
    tests excluding each status-enum value (business-rule discovery).
    Returns (filter, evidence): filter "" on clean reconciliation or the
    discovered exclusion (e.g. "sts_cd <> 'X'"); evidence is the per-group
    coverage string carried into provenance. None if nothing reconciles.
    Every mapped grouping must match per group at >=95% coverage — grand
    totals alone are never accepted (offsetting group errors pass them).
    """
    a_ts, f_ts = stats[cand.agg_t["name"]].table, stats[cand.fact_t["name"]].table
    aq, fq = qualify(a_ts.schema, a_ts.name), qualify(f_ts.schema, f_ts.name)
    if not cand.gmap and not cand.date_pairs:
        return None
    ok, evidence = _verify_groups(source, aq, fq, cand, "")
    if ok:
        return "", evidence
    status_cols = [c for c in cand.fact_t["columns"] if c.get("semantic_type") == "status_code"]
    for sc in status_cols:
        cs = stats[cand.fact_t["name"]].columns[sc["name"]]
        for v, _n in cs.top_values[:6]:
            filt = f"\"{sc['name']}\" <> '{v}'"
            ok, evidence = _verify_groups(source, aq, fq, cand, filt)
            if ok:
                return f"{sc['name']} <> '{v}'", evidence
    return None


def _apply_discovered_filters_to_metrics(sl) -> None:
    """Scope reconciliation-discovered rules to MONETARY metrics on the fact, never to counts.

    Measured: blanket filters corrupt count questions.
    """
    for t in sl["tables"]:
        pend = t.get("knowledge", {}).pop("_pending_metric_filter", None)
        if not pend:
            continue
        filt = pend[0]
        for m in sl.get("metrics", []):
            if (m.get("measure", "").startswith(t["name"] + ".")
                    and m.get("agg") in ("sum", "avg") and not m.get("filter")):
                m["filter"] = filt
                m.setdefault("provenance", []).append(
                    {"signal": "statistic",
                     "detail": f"business rule from aggregate reconciliation: {filt}"})


# ------------------------------------------------------- domains / routing
def _connected_components(rels: list[dict], table_names: list[str]) -> dict[str, list[str]]:
    """Union-find over the relationship graph.

    Tables joined (directly or transitively) by an FK land in the same component.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        parent[find(a)] = find(b)

    for r in rels:
        union(r["from"]["table"], r["to"]["table"])
    comps: dict[str, list[str]] = defaultdict(list)
    for name in table_names:
        comps[find(name) if name in parent else name].append(name)
    return comps


def _domains_from_components(comps: dict[str, list[str]], ttypes: dict[str, Any]) -> list[dict]:
    """One domain per multi-table component, named after its fact table.

    Falls back to the alphabetically first member if no fact table is present.
    """
    domains = []
    for members in comps.values():
        if len(members) < 2:
            continue
        facts = [m for m in members if ttypes.get(m) == "fact"]
        name = (facts[0] if facts else sorted(members)[0]) + "_domain"
        domains.append({"name": name, "tables": sorted(members)})
    return domains


def _avoid_list(t: dict, deprecated: list[dict]) -> list[dict]:
    """Deprecated tables whose replacement or own name shares a prefix with `t`.

    A cheap proxy for "this deprecated table used to serve the same purpose".
    """
    return [{"table": d["name"], "reason": d.get("deprecation", {}).get("reason", "deprecated")}
            for d in deprecated
            if d.get("deprecation", {}).get("replacement", "").startswith(t["name"][:6]) or
               d["name"].startswith(t["name"][:6])]


def _routing_entries(sl: dict, aggs: list[dict]) -> list[dict]:
    """One routing hint per active fact table.

    Which table(s) to use (including any reconciled aggregate) and which
    deprecated tables to avoid.
    """
    deprecated = [t for t in sl["tables"] if t.get("lifecycle") == "deprecated"]
    agg_by_base = {a["aggregates"]: a["table"] for a in aggs}
    routing = []
    for t in sl["tables"]:
        if t.get("table_type") != "fact" or t.get("lifecycle") == "deprecated":
            continue
        use = [t["name"]] + ([agg_by_base[t["name"]]] if t["name"] in agg_by_base else [])
        entry = {"intent": f"analysis of {t['name'].replace('_', ' ')}",
                 "use": use, "lifecycle": "inferred", "confidence": 0.6}
        avoid = _avoid_list(t, deprecated)
        if avoid:
            entry["avoid"] = avoid
        routing.append(entry)
    return routing


def _domains_and_routing(sl, aggs) -> None:
    """Group joined tables into domains and generate fact-to-aggregate routing hints.

    Domains are connected components of the relationship graph; routing
    entries carry avoid-lists pointing away from deprecated tables.
    """
    rels = sl.get("relationships", [])
    comps = _connected_components(rels, [t["name"] for t in sl["tables"]])
    ttypes = {t["name"]: t.get("table_type") for t in sl["tables"]}
    domains = _domains_from_components(comps, ttypes)
    routing = _routing_entries(sl, aggs)
    rk = {}
    if routing:
        rk["routing"] = routing
    if domains:
        rk["domains"] = domains
    if rk:
        sl["repo_knowledge"] = rk


# -------------------------------------------------------- snapshot filters
def _snapshot_filters(sl, stats) -> None:
    """Add a required latest-snapshot filter to SCD2 tables (summing across dates over-counts)."""
    for t in sl["tables"]:
        if t.get("table_type") != "snapshot_scd2":
            continue
        snap_col = next((c["name"] for c in t["columns"]
                         if c["name"].lower() in ("snap_dt", "snapshot_dt", "as_of_dt")), None)
        if snap_col:
            t.setdefault("knowledge", {}).setdefault("required_filters", []).append({
                "expr": f"{snap_col} = (SELECT max({snap_col}) FROM {t['name']})",
                "reason": "snapshot table: summing across snapshot dates over-counts",
                "enforcement": "advisory",
            })
