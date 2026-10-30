# SPDX-License-Identifier: FSL-1.1-ALv2
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
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from typing import Any

from semlayer.source import row_cap

_LEGACY_RE = re.compile(r"(_legacy|_old|_bak|_deprecated|_archive)$")
_AGG_NAME_RE = re.compile(r"(^|_)(agg|summary|rollup)($|_)|^(dly|mth|wkly)_")


class _CachingSource:
    """A read-through cache over `source.query`, for the life of one Enrich run.

    Aggregate reconciliation tests every rollup candidate against every fact
    table, and for each surviving pair runs per-group SUM queries on both
    sides, for each (aggregate measure, fact measure) pair, and again under
    each exclusion hypothesis. The fact side of that is the same query for
    every candidate tried against the fact; the aggregate side is the same
    query for every fact tried against the candidate. On a 15,000-table
    warehouse the stage took 477 s, growing as the square of the table count,
    almost all of it re-running identical SQL.

    The warehouse is read-only for the duration of a run, so the same SQL
    returns the same rows and caching it changes nothing in the output.
    Exceptions are not cached (a failing query is re-raised each time, as
    `_grain_covers` expects). The cache is bounded by rows held, not entries,
    because one per-group result can be 50,000 rows.
    """

    MAX_ROWS = 2_000_000

    def __init__(self, source) -> None:
        self._source = source
        self._cache: OrderedDict[str, list[tuple]] = OrderedDict()
        self._rows = 0
        self.hits = 0
        self.misses = 0

    def __getattr__(self, name: str) -> Any:        # qualify, row_cap_clause, ...
        return getattr(self._source, name)

    def query(self, sql: str) -> list[tuple]:
        hit = self._cache.get(sql)
        if hit is not None:
            self._cache.move_to_end(sql)
            self.hits += 1
            return hit
        self.misses += 1
        rows = self._source.query(sql)
        n = max(1, len(rows))
        while self._cache and self._rows + n > self.MAX_ROWS:
            _, old = self._cache.popitem(last=False)
            self._rows -= max(1, len(old))
        self._cache[sql] = rows
        self._rows += n
        return rows


def _modelled_view(sl: dict) -> dict:
    """`sl` with inventory-only tables hidden from every Enrich step.

    The table dicts are the same objects, so per-table writes land in the
    document; the layer-level keys Enrich adds (metrics, aggregate_tables,
    domains, routing) are written to this view and copied back by
    `_write_back`. An inventory-only table (semlayer.tier: raw and scratch)
    therefore gets no metrics, no aggregate claims, no routing entry and no
    freshness, grain, SCD or filter claims -- it is listed, not modelled.
    """
    view = dict(sl)
    view["tables"] = [t for t in sl["tables"] if t.get("modeling") != "inventory_only"]
    return view


def _write_back(view: dict, sl: dict) -> None:
    for k, v in view.items():
        if k != "tables":
            sl[k] = v


def enrich_source(source, doc: dict, stats: dict) -> dict:
    """Run the deterministic enrich tier.

    Covers decodes, deprecation, freshness, metrics, aggregate
    reconciliation, routing/domains, and snapshot filters.
    """
    source = _CachingSource(source)
    sl = _modelled_view(doc["semantic_layer"])
    _upgrade_enum_decodes(source, sl, stats)
    # after Link, because "no relationship was discovered for this column" is
    # what separates a natural key from a foreign key
    _detect_natural_keys(sl)
    _detect_deprecation(sl, stats)
    _infer_freshness(sl, stats)
    _classify_time_attributes(source, sl, stats)
    _level_hierarchies(source, sl, stats)
    # BEFORE the metric producers: every metric copies its base table's grain,
    # and reading it earlier silently gave all of them an empty string.
    _infer_grain(sl)
    metrics = _metric_candidates(sl)
    if metrics:
        sl["metrics"] = metrics
    aggs = _detect_aggregates(source, sl, stats)
    if aggs:
        sl["aggregate_tables"] = aggs
    _grain_from_reconciliation(sl, aggs)
    _apply_discovered_filters_to_metrics(sl)
    _propagate_parent_filters(source, sl, stats)
    _domains_and_routing(sl, aggs)
    _snapshot_filters(sl, stats)
    _infer_scd(sl)
    _write_back(sl, doc["semantic_layer"])
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
            pairs = source.query(
                f'SELECT "{pcn}", "{label_cols[0]}" FROM {q}{row_cap(source, 100)}')
            c["enum_values"] = [
                {"value": v, "meaning": str(m), "decode_source": "dictionary_join"}
                for v, m in pairs if v is not None
            ]
            c.setdefault("provenance", []).append({
                "signal": "statistic",
                "detail": f"enum decoded via join to {ptn}.{label_cols[0]}",
            })


# ------------------------------------------------------------ level hierarchies
# Containment chains INSIDE one dimension: country > state > city,
# department > category > product. `kind: "level"` was the one hierarchy kind
# nothing produced -- Link makes `recursive`, Enrich makes `time` -- while gold
# asserted 8 of them, so the gap was scoreable and unscored.
#
# Ordering cannot come from cardinality alone. messy_mart's store_dim has 12
# distinct states and 12 distinct cities, so the functional dependency holds in
# BOTH directions and cardinality is silent. It comes from the semantic types
# the profiler already assigns, with the FD as verification -- which is what
# docs/ predicted and the one live design decision of the four it listed.
_GEO_ORDER = {"geo_country": 0, "geo_region": 1, "geo_city": 2, "geo_postal": 3}
# messy_mart's postal_cd -> state_cd is 2% violated BY DESIGN: the dirty
# hierarchy that an exact-FD rule misses entirely.
_FD_TOLERANCE = 0.05
_MAX_LEVEL_CANDIDATES = 12   # cap the FD queries on a wide denormalized table
# A level is a grouping ATTRIBUTE. Keys are not levels (`cust_id > cust_nm` is
# a key and its label, not containment), and neither are flags, names, free
# text or dates.
_NEVER_LEVELS = frozenset({"measure", "metadata", "primary_key", "natural_key",
                           "unique_key"})
_NEVER_LEVEL_TYPES = frozenset({"flag", "name", "free_text", "date",
                                "timestamp_event", "identifier", "url",
                                "json_object", "geo_coordinates"})
# Two columns at 1:1 are usually ONE level in two encodings -- `mth_nbr` and
# `mth_nm`, `day_nm` and `day_of_wk` -- and their FD holds in both directions,
# so it proves nothing. The exception is a known containment family: messy_mart
# has 12 states and 12 cities and country > state > city is still real, which
# is why the semantic family and not cardinality decides the order.
_MIN_LEVEL_GROWTH = 1.5


def _level_candidates(t: dict, ts) -> list[dict]:
    """Columns that could be a level: low-cardinality, not a measure, not unique."""
    out = []
    for c in t["columns"]:
        if c.get("entity_role") in _NEVER_LEVELS:
            continue
        stype = c.get("semantic_type") or ""
        if stype in _NEVER_LEVEL_TYPES:
            continue
        # A level GROUPS rows; PII identifies an individual. `cust_email` and
        # `c_email_address` both formed chains with a low-cardinality column
        # above them, and an email is never a level of anything.
        if stype.startswith("pii_"):
            continue
        st = ts.columns.get(c["name"])
        if st is None or st.is_unique or not 2 <= st.n_distinct <= 100_000:
            continue
        # An UNTYPED near-unique column is an identifier we failed to name --
        # `sku_xref.ext_sku` at 399/400 distinct. Cardinality alone cannot
        # decide this: `postal_cd` is 2213/2252 and a real geographic level,
        # which is why the test is "untyped AND near-unique" rather than just
        # near-unique.
        if stype == "unknown" and st.cardinality_ratio > 0.9:
            continue
        out.append(c)
    return out


def _level_groups(cands: list[dict], denormalized: bool) -> list[list[dict]]:
    """Split candidates into chains that could plausibly belong together.

    Two groupings, because the fixtures need both. A denormalized table carries
    several entities' attributes side by side -- obt has cust_country/cust_state/
    cust_city AND prod_department/prod_category -- so a single chain over every
    candidate would interleave them. But messy_mart's store_dim uses
    cntry_cd/state_cd/city_nm, where the prefixes all differ and only the
    semantic family holds them together.
    """
    geo = [c for c in cands if c.get("semantic_type") in _GEO_ORDER]
    rest = [c for c in cands if c.get("semantic_type") not in _GEO_ORDER]
    groups = [geo] if len(geo) >= 2 else []
    if denormalized:
        # Several entities' attributes sit side by side, so a single chain over
        # every candidate would interleave them: obt carries cust_country /
        # cust_state / cust_city AND prod_department / prod_category.
        by_prefix: dict[str, list[dict]] = {}
        for c in rest:
            by_prefix.setdefault(c["name"].split("_")[0].lower(), []).append(c)
        groups.extend(g for g in by_prefix.values() if len(g) >= 2)
    elif len(rest) >= 2:
        # A dimension table describes ONE entity, so its codes plausibly form a
        # single chain -- and prefixes cannot group them: messy_mart's product
        # hierarchy is dept_cd > category_cd, two different prefixes.
        groups.append(rest)
    return groups


def _order_levels(group: list[dict], ts) -> list[dict]:
    """Coarse to fine: the semantic family where it knows, cardinality otherwise."""
    return sorted(group, key=lambda c: (
        # `or ""` because semantic_type may be absent or None, and _GEO_ORDER
        # is keyed by str -- an untyped .get() here is what broke `mypy`.
        _GEO_ORDER.get(c.get("semantic_type") or "", 99),
        ts.columns[c["name"]].n_distinct,
        c["name"]))


def _fd_holds(source, tq: str, where: str, coarse: str, fine: str) -> tuple[bool, float]:
    """Does `fine` determine `coarse`? Returns (holds, clean fraction).

    Scoped by `where` to current records on a versioned dimension. Not a
    nicety: TPC-DS's `i_item_id -> i_class` is 34% clean across all history and
    100% clean within the current version, because an item's class changes
    between versions. Without the scope the detector rejects a real hierarchy.
    """
    # SUM(CASE ...) rather than COUNT(*) FILTER (WHERE ...): FILTER is ANSI but
    # invalid on Oracle, MySQL and T-SQL, and sqlglot passes it through verbatim
    # rather than rewriting it. Introduced here on 2026-10-12 and caught by the
    # portability audit the same day.
    row = source.query(
        f'SELECT sum(CASE WHEN n > 1 THEN 1 ELSE 0 END), count(*) FROM '
        f'(SELECT "{fine}", count(DISTINCT "{coarse}") n FROM {tq}{where} '
        f'GROUP BY 1) g')[0]
    bad, tot = int(row[0] or 0), int(row[1] or 0)
    if not tot:
        return False, 0.0
    clean = (tot - bad) / tot
    return clean >= 1 - _FD_TOLERANCE, clean


def _scd_scope(t: dict) -> str:
    """Restrict to current records when the table versions its rows."""
    scd = t.get("scd") or {}
    if scd.get("is_current_flag"):
        return f" WHERE \"{scd['is_current_flag']}\""
    if scd.get("valid_to"):
        return f" WHERE \"{scd['valid_to']}\" IS NULL"
    return ""


def _level_hierarchies(source, sl: dict, stats) -> None:
    """Emit `kind: level` hierarchies verified column-pair by column-pair."""
    qualify = getattr(source, "qualify", lambda s, n: f'"{s}"."{n}"')
    for t in sl["tables"]:
        if t.get("table_type") in ("staging", "operational", "fact", "aggregate"):
            continue
        ts = stats.get(t["name"])
        if ts is None or not ts.row_count:
            continue
        cands = _level_candidates(t, ts)
        tq = qualify(ts.table.schema, ts.table.name)
        where = _scd_scope(t)
        denorm = t.get("table_type") == "denormalized"
        for group in _level_groups(cands, denorm):
            # Cap PER GROUP, after grouping. Capping the candidate pool first
            # kept the lowest-cardinality columns globally and so cut the FINE
            # end off real chains: obt lost cust_city (13 distinct) from
            # country > state > city while keeping a dozen unrelated flags.
            ordered = _order_levels(group, ts)[:_MAX_LEVEL_CANDIDATES]
            _emit_chain(source, sl, t, ts, tq, where, ordered)


def _emit_chain(source, sl, t, ts, tq, where, ordered) -> None:
    """Walk an ordered group, keeping each column whose FD to the last level holds."""
    levels: list[dict] = []
    evidence: list[str] = []
    for c in ordered:
        if not levels:
            levels.append(c)
            continue
        prev = levels[-1]
        same_family = (prev.get("semantic_type") in _GEO_ORDER
                       and c.get("semantic_type") in _GEO_ORDER)
        grew = (ts.columns[c["name"]].n_distinct
                >= _MIN_LEVEL_GROWTH * ts.columns[prev["name"]].n_distinct)
        if not (grew or same_family):
            continue   # 1:1 with the level above: one level in two encodings
        holds, clean = _fd_holds(source, tq, where, prev["name"], c["name"])
        if holds:
            levels.append(c)
            evidence.append(f"{c['name']} -> {prev['name']} {clean:.0%}")
    if len(levels) < 2:
        return
    hs = sl.setdefault("hierarchies", [])
    name = f"{t['name']}_{levels[0]['name']}_levels"
    if any(h["name"] == name for h in hs):
        return
    hs.append({
        "name": name, "kind": "level", "dimension_table": t["name"],
        "levels": [{"name": c["name"], "column": c["name"]} for c in levels],
        "lifecycle": "inferred", "confidence": 0.7,
        "provenance": [{"signal": "functional_dependency",
                        "detail": "containment verified per level: " + "; ".join(evidence)
                                  + (f" (current records only{where})" if where else "")}],
    })

# ------------------------------------------------------------ natural keys
def _detect_natural_keys(sl: dict) -> None:
    """Mark a dimension's business key as `natural_key`, not a foreign key.

    The star-schema pair: a dimension carries a surrogate key as its primary
    key (`s_store_sk`) and the source system's own identifier beside it
    (`s_store_id`). The second one is a natural key -- per SPEC it is what
    `COUNT(DISTINCT ...)` counts entities by, and what an SCD table repeats
    across versions.

    Nothing emitted this role before: the typing rule is "unique -> key,
    otherwise foreign key", so a business key came out `unique_key` when the
    table happened to be small enough for it to be unique and `foreign_key`
    when it was not. Both are wrong, and which one you got depended on the row
    count -- `store.s_store_id` is unique at 1 row and repeats at 12 once SCD2
    versioning kicks in. Gold asserts 31 of these across the corpus, 29 of them
    on plain dimension tables.

    The discriminator is that a foreign key POINTS somewhere. This runs after
    Link, so a column with no discovered relationship, on a dimension, with an
    identifier name, and not itself the primary key, is a business key.
    """
    linked = {(r.get("from", {}).get("table", "").lower(), c.lower())
              for r in sl.get("relationships") or []
              for c in r.get("from", {}).get("columns") or []}

    for t in sl["tables"]:
        # Surrogate-vs-business key is a dimensional-modelling distinction and
        # does not apply to raw landing tables, where an id is ETL bookkeeping.
        # Note this uses table_type only to EXCLUDE, so it does not inherit the
        # dimension -> fact confusion that makes gating on `dimension` useless.
        if t.get("table_type") in ("staging", "operational"):
            continue
        pk = {c.lower() for c in t.get("primary_key") or []}
        if not pk:
            continue  # no surrogate to be the business key's counterpart
        for col in t["columns"]:
            name = col["name"]
            if name.lower() in pk:
                continue
            if (t["name"].lower(), name.lower()) in linked:
                continue  # it points at something: genuinely a foreign key
            if col.get("entity_role") not in ("foreign_key", "unique_key"):
                continue
            if col.get("semantic_type") != "identifier":
                continue
            col["entity_role"] = "natural_key"
            col["confidence"] = min(col.get("confidence", 0.7), 0.7)
            col.setdefault("provenance", []).append({
                "signal": "statistic",
                "detail": "business key on a dimension: identifier-named, not "
                          "the primary key, and Link found no relationship for it",
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


# ----------------------------------------------------------- time attrs
_INT_TYPES = ("INT", "BIGINT", "SMALLINT", "TINYINT", "NUMBER", "NUMERIC", "DECIMAL")


def _classify_time_attributes(source, sl, stats) -> None:
    """Classify date-dimension attributes as calendar or FISCAL, from data.

    The customer's date dim materializes their fiscal calendar as derived
    columns; we never infer calendar rules — we verify each integer time
    attribute against Gregorian extraction of the dim's unique date column.
    Full agreement -> calendar_*; shape-matching divergence -> fiscal_*
    (a Feb-start fiscal quarter disagrees with EXTRACT(QUARTER) on most rows).
    Compilers use the tags to route/disambiguate time bucketing (SPEC 2.6).
    """
    qualify = getattr(source, "qualify", lambda s, n: f'"{s}"."{n}"')
    for t in sl["tables"]:
        date_col = next(
            (c for c in t["columns"] if c.get("semantic_type") == "date"
             and stats[t["name"]].columns[c["name"]].is_unique), None)
        if date_col is None:
            continue
        ts = stats[t["name"]].table
        tq = qualify(ts.schema, ts.name)
        n = stats[t["name"]].row_count
        if not n:
            continue
        for c in t["columns"]:
            if c is date_col or not any(k in c["sql_type"].upper() for k in _INT_TYPES):
                continue
            tag = _time_attribute_tag(source, tq, date_col["name"], c, stats[t["name"]], n)
            if tag is not None:
                c["time_attribute"] = tag
                c.setdefault("provenance", []).append({
                    "signal": "statistic",
                    "detail": f"{tag}: verified against {date_col['name']}"})
        _time_hierarchies(sl, t, date_col["name"])


# The tags above are the levels of a calendar; nothing used to assemble them
# into one. SPEC 2.5 requires time bucketing to go through a declared time
# hierarchy, so without this the compiler could only fall back to ad hoc date
# truncation -- and would ignore the customer's own calendar columns sitting
# right there, having just verified them row by row.
_CAL_LEVELS = [("calendar_year", "year"), ("calendar_quarter", "quarter"),
               ("calendar_month", "month")]
_FIS_LEVELS = [("fiscal_year", "year"), ("fiscal_quarter", "quarter"),
               ("fiscal_period", "period")]


def _time_hierarchies(sl: dict, t: dict, date_col: str) -> None:
    """Assemble one date dimension's verified time attributes into hierarchies.

    Levels run most-general to most-granular, which is what the format
    requires and what lets a compiler pick the year level to pair with a
    month or quarter.
    """
    tagged = {c.get("time_attribute"): c["name"] for c in t["columns"]
              if c.get("time_attribute")}
    for kind, wanted in (("calendar", _CAL_LEVELS), ("fiscal", _FIS_LEVELS)):
        levels = [{"name": level, "column": tagged[tag]}
                  for tag, level in wanted if tag in tagged]
        if not levels:
            continue
        if kind == "calendar":
            levels.append({"name": "day", "column": date_col})
        hs = sl.setdefault("hierarchies", [])
        name = f"{t['name']}_{kind}"
        if any(h["name"] == name for h in hs):
            continue
        hs.append({
            "name": name, "kind": "time", "dimension_table": t["name"],
            "levels": levels, "lifecycle": "inferred", "confidence": 0.9,
            "provenance": [{"signal": "statistic",
                            "detail": f"{kind} levels verified against {date_col} "
                                      f"on every row"}],
        })


def _time_attribute_tag(source, tq: str, d: str, c: dict, tstats, n: int) -> str | None:
    cs = tstats.columns[c["name"]]
    name = c["name"]
    quarter_shaped = 2 <= cs.n_distinct <= 4
    month_shaped = 12 <= cs.n_distinct <= 13
    row = source.query(
        f'SELECT sum(CASE WHEN "{name}" = EXTRACT(QUARTER FROM "{d}") THEN 1 ELSE 0 END),'
        f' sum(CASE WHEN "{name}" = EXTRACT(MONTH FROM "{d}") THEN 1 ELSE 0 END),'
        f' sum(CASE WHEN "{name}" = EXTRACT(YEAR FROM "{d}") THEN 1 ELSE 0 END),'
        f' max(abs("{name}" - EXTRACT(YEAR FROM "{d}"))), min("{name}"), max("{name}")'
        f" FROM {tq}")[0]
    q_match, m_match, y_match, y_dev, lo, hi = (float(x or 0) for x in row)
    if quarter_shaped and 1 <= lo and hi <= 4:
        if q_match == n:
            return "calendar_quarter"
        # A TWO-valued column on a date dimension is a semester or half, not a
        # fiscal quarter: its values 1 and 2 pass the quarter shape test, fail
        # the exact-match check, and used to fall through to `fiscal_quarter`.
        # AdventureWorksDW has both CalendarSemester and FiscalSemester, and
        # BOTH came out as fiscal_quarter -- the first of them not even fiscal.
        # The spec enum has no semester tag, so emit nothing rather than a
        # wrong one; adding one is a MINOR bump and needs a decision. Every
        # real quarter column in the corpus has exactly 4 distinct values.
        if cs.n_distinct >= 3 and q_match < 0.9 * n:
            return "fiscal_quarter"
        return None
    if month_shaped and 1 <= lo and hi <= 13:
        return "calendar_month" if m_match == n else (
            "fiscal_period" if m_match < 0.9 * n else None)
    if y_dev <= 1 and hi > 1900:
        return "calendar_year" if y_match == n else "fiscal_year"
    return None


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
        # `total_tax` already says total; `total_total_tax` said it twice.
        # Only the full word: messy_mart's `tot_amt` keeps `total_tot_amt`,
        # which its gold, docs and export tests all name.
        already = c["name"].startswith("total_")
        base = c["name"] if already else f"total_{c['name']}"
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
    """Row-count metric on a fact table's SINGLE-column primary key.

    Skipped for a compound key. The caller passes `primary_key[0]`, and one
    member of a compound key is not the grain: `COUNT(DISTINCT ss_item_sk)` on
    tpcds_clean's store_sales is 180 against 28,810 rows. The metric would also
    grant that column a `count_distinct` affordance that silently answers a
    different question. Composite-key row counts need a tuple-aware metric,
    which the format has no vocabulary for yet.
    """
    if len(t.get("primary_key") or []) > 1:
        return None
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


def _entity_word(table: str) -> str:
    """Singular business noun for a table name: ord_hdr -> order, store_sales -> store_sale."""
    from semlayer.link.candidates import ABBREV
    extra = {"ln": "line", "hdr": "header", "dtl": "detail", "evt": "event", "txn": "transaction"}
    toks = [extra.get(w, ABBREV.get(w, w)) for w in re.split(r"[_\W]+", table.lower()) if w]
    toks = [w for w in toks if w not in ("hdr", "header", "fact", "tbl", "table", "dtl", "detail")]
    if not toks:
        return table
    return "_".join(toks).rstrip("s")


def _ratio_metrics(t: dict, pk: str | None, own: list[dict], seen: set) -> list[dict]:
    """Average <measure> per <entity>: SUM(measure) / COUNT(pk) on the same fact.

    The one derived metric every warehouse asks for ("average order value")
    and the one compile_metric can already emit safely (same base table).
    Inherits the measure metric's filter later, with it.
    """
    if not pk:
        return []
    entity = _entity_word(t["name"])
    out = []
    for m in own:
        if m.get("type") != "simple" or m.get("agg") != "sum" or "completed" in m["name"]:
            continue
        col = m["measure"].split(".", 1)[1]
        name = f"avg_{col}_per_{entity}"
        if name in seen:
            continue
        seen.add(name)
        out.append({
            "name": name, "type": "ratio",
            "numerator": m["measure"], "denominator": f"{t['name']}.{pk}",
            "display_name": f"Average {col} per {entity}",
            "synonyms": [f"average {col} per {entity}", f"{col} per {entity}",
                         f"average {entity} value"],
            "grain": t.get("grain", ""), "lifecycle": "inferred", "confidence": 0.6,
            "provenance": [{"signal": "statistic",
                            "detail": f"SUM({col}) / COUNT({pk}) on {t['name']}"}],
        })
    return out


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
            metrics.extend(_ratio_metrics(t, pk, metrics[before:], seen))
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
    f.setdefault("knowledge", {}).setdefault("_pending_metric_filter", []).append(filt)
    rfs = f["knowledge"].setdefault("required_filters", [])
    if not any(r["expr"] == filt for r in rfs):
        rfs.append({
            "expr": filt, "enforcement": "required", "scope": "measures",
            "reason": (f"amount aggregations over {f['name']} must exclude these rows: "
                       f"{t['name']} reconciles with {f['name']} only under this filter "
                       f"(discovered by per-group reconciliation); counts include all rows"),
        })


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


_SUMMABLE = ("INT", "DECIMAL", "NUMERIC", "DOUBLE", "FLOAT", "REAL", "BIGNUM")


def _summable(c: dict) -> bool:
    """Can SQL actually SUM this column?

    `semantic_type` is inferred partly from the NAME, so a text column called
    `Tot_Mdcr_Pymt_Amt` is typed `monetary_value` while being VARCHAR. CMS's
    API returns every value as a string, and real warehouses store numbers as
    text all the time -- CSV loads, staging tables, anything built by hand.
    Reconciliation emitted `SUM("...")` over it and DuckDB raised, which
    crashed `infer` outright. A warehouse we cannot reconcile must still
    profile.
    """
    return any(k in (c.get("sql_type") or "").upper() for k in _SUMMABLE)


def _match_fact_for_aggregate(source, qualify, stats, t: dict, f: dict,
                              agg_measures: list[dict], group_cols: list[str]) -> dict | None:
    """Find the first (agg measure, fact measure) pair on `f` reconciling against `t`, if any."""
    f_measures = [c for c in f["columns"] if c.get("entity_role") == "measure"
                  and c.get("semantic_type") == "monetary_value" and _summable(c)]
    agg_measures = [c for c in agg_measures if _summable(c)]
    if not agg_measures:
        return None
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
        # Decided by SHAPE, not by name. The gate used to be
        # `_AGG_NAME_RE.search(t["name"])`, so rule recovery -- the capability
        # we describe as central -- only ever ran on summary tables NAMED the
        # way we named ours (`dly_sls_agg`, `mth_cust_agg`). CMS calls its
        # per-provider totals `Prov`; a real warehouse might call its rollup
        # `sales_monthly` or `t_sls_mth`, and every one was skipped in silence.
        #
        # The name is still used, as confidence rather than as a gate.
        if t.get("table_type") in ("staging", "operational"):
            continue  # ETL bookkeeping is not a rollup of anything
        agg_measures = [c for c in t["columns"] if c.get("entity_role") == "measure"]
        group_cols = [c["name"] for c in t["columns"]
                      if c.get("entity_role") in ("foreign_key", "dimension")]
        if not agg_measures or not group_cols:
            continue
        named = bool(_AGG_NAME_RE.search(t["name"]) or t.get("table_type") == "aggregate")
        out.extend(_aggregates_for_candidate(
            source, qualify, stats, t, facts, agg_measures, group_cols, named))
    return out


def _aggregates_for_candidate(source, qualify, stats, t, facts,  # noqa: PLR0913, PLR0917
                              agg_measures, group_cols, named) -> list[dict]:
    """Reconcile one rollup candidate against every plausible fact."""
    found: list[dict] = []
    attempted: list[str] = []
    for f in facts:
        if f["name"] == t["name"]:
            continue
        # An aggregate is strictly smaller than the detail it rolls up. This is
        # the prune that makes dropping the name gate affordable: it runs
        # before any reconciliation SQL, and `_agg_group_mapping` then requires
        # the group columns to actually map onto the fact.
        if not _is_coarser(stats, t["name"], f["name"]):
            continue
        gmap, _date_pairs = _agg_group_mapping(group_cols, f)
        if _grain_covers(source, qualify, stats, t, f, gmap):
            attempted.append(f["name"])
        entry = _match_fact_for_aggregate(
            source, qualify, stats, t, f, agg_measures, group_cols)
        if entry is None:
            continue
        if not named:
            entry["confidence"] = round(entry["confidence"] - 0.1, 2)
            entry["provenance"].append({
                "signal": "statistic",
                "detail": "identified by shape, not by name: coarser grain over "
                          f"{f['name']} with reconciling measures",
            })
        found.append(entry)
    if attempted and not found:
        # the SUMMABLE count, not the raw measure count: the note is about
        # what reconciliation could actually have tried
        _record_unreconciled(t, attempted, len([c for c in agg_measures if _summable(c)]))
    return found


_NEAR_MISS_GROUPS = 2        # mapped group columns needed before the test is worth running
_GRAIN_SLACK = 0.05         # a rollup may cover grain combinations absent from the detail


def _grain_covers(source, qualify, stats, t: dict, f: dict, gmap: dict) -> bool:
    """Does `t` have exactly one row per distinct combination of its grain in `f`?

    This is what actually separates a rollup from a dimension or a sibling
    fact, and counting mapped column NAMES does not. Any two tables sharing
    two column names pass a name count -- which claimed `DimEmployee` was a
    rollup of `ProspectiveBuyer`, and `FactInternetSales` a rollup of
    `FactResellerSales`. Seven such notes on AdventureWorksDW, almost all
    nonsense, which is worse than silence.

    A genuine rollup's row count equals the number of distinct grain
    combinations in its detail: `dly_sls_agg` has one row per day x store in
    `ord_hdr`. One extra query per surviving pair, and the pairs are already
    pruned by row count and group mapping.
    """
    if len(gmap) < _NEAR_MISS_GROUPS:
        return False
    a_rows = stats[t["name"]].row_count
    if not a_rows:
        return False
    f_ts = stats[f["name"]].table
    cols = ", ".join(f'"{c}"' for c in gmap.values())
    try:
        distinct = source.query(
            f"SELECT count(*) FROM (SELECT DISTINCT {cols} "
            f"FROM {qualify(f_ts.schema, f_ts.name)})")[0][0]
    except Exception:  # an unsupported grouping expression is not a rollup claim
        return False
    if not distinct:
        return False
    # One-directional on purpose. A rollup may carry rows for grain
    # combinations that have NO detail rows -- which is exactly CMS, where 13
    # of 400 providers have every service line suppressed, a 3.25% gap that a
    # symmetric 2% tolerance rejected while being the very phenomenon the note
    # exists to report. It may not have FEWER rows than the detail's distinct
    # combinations: then it is not a rollup of that table at all.
    excess = a_rows - distinct
    return 0 <= excess <= max(1.0, _GRAIN_SLACK * a_rows)


def _record_unreconciled(t: dict, attempted: list[str], n_measures: int) -> None:
    """Say so when a table LOOKS like a rollup and none of its measures tie out.

    Before aggregate detection became shape-based, "no aggregate_tables entry"
    and "never even examined" were the same thing, so silence was honest. Now
    a candidate can be examined against several facts, have its grouping
    columns map, and still reconcile with nothing -- and silence reads as "no
    summary table here", which is a different and wrong statement.

    The near miss is worth reporting because it is usually informative rather
    than absent: CMS's per-provider totals do not tie out against its detail
    because the detail carries AVERAGES, so the totals reconcile as
    SUM(avg * count) -- a product the engine cannot express. "This looks like a
    rollup whose measure is derived" is actionable; nothing is not.
    """
    notes = t.setdefault("knowledge", {}).setdefault("usage_notes", [])
    note = (f"Looks like a pre-aggregated rollup of {', '.join(sorted(attempted))} "
            f"— its grouping columns map and it carries {n_measures} summable "
            "measures — but no measure reconciled per group. Its measures may be "
            "derived (an average or ratio rather than a sum), or the detail may be "
            "incomplete. Treat as unverified: do NOT route queries to it as a "
            "pre-aggregate without checking how its measures are built.")
    if note not in notes:
        notes.append(note)


def _is_coarser(stats, agg_table: str, fact_table: str) -> bool:
    """Is `agg_table` plausibly a rollup of `fact_table` on row count alone?"""
    a, f = stats.get(agg_table), stats.get(fact_table)
    if a is None or f is None:
        return False
    return 0 < a.row_count < f.row_count


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
         f"GROUP BY 1{row_cap(source, _MAX_GROUPS)}")
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


def _grain_from_reconciliation(sl, aggs: list[dict]) -> None:
    """A summary table has no primary key, but reconciliation proved its grain.

    `_infer_grain` reads a primary key and these tables have none, so their
    grain stayed empty even though the group columns were measured against the
    fact table. Backfill the table and any metric already built on it.
    """
    tables = {t["name"]: t for t in sl["tables"]}
    for a in aggs:
        t = tables.get(a["table"])
        if t is None or t.get("grain") or not a.get("grain"):
            continue
        t["grain"] = f"one row per {', '.join(a['grain'])} (pre-aggregated)"
        for m in sl.get("metrics", []) or []:
            base = (m.get("measure") or m.get("numerator") or ".").split(".", 1)[0]
            if base == t["name"] and not m.get("grain"):
                m["grain"] = t["grain"]


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
            simple = (m.get("measure", "").startswith(t["name"] + ".")
                      and m.get("agg") in ("sum", "avg"))
            ratio = m.get("type") == "ratio" and m.get("numerator", "").startswith(t["name"] + ".")
            if (simple or ratio) and not m.get("filter"):
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
    # a fact may have several reconciled aggregates (daily-by-store AND
    # monthly-by-customer); keying by base name alone used to drop all but the
    # last. Order is insertion order of aggregate_tables, deduped.
    aggs_by_base: dict[str, list[str]] = {}
    for a in aggs:
        names = aggs_by_base.setdefault(a["aggregates"], [])
        if a["table"] not in names:
            names.append(a["table"])
    routing = []
    for t in sl["tables"]:
        if t.get("table_type") != "fact" or t.get("lifecycle") == "deprecated":
            continue
        use = [t["name"], *aggs_by_base.get(t["name"], [])]
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


# ------------------------------------------------------ filter propagation
def _propagate_parent_filters(source, sl, stats) -> None:
    """A parent fact's measure-scoped rule reaches its child fact when the data proves it.

    ord_hdr excludes cancelled orders from revenue; ord_ln rows for those
    orders still exist. If SUM(child.measure) per parent key reconciles with
    parent.measure (>=95% of keys within 0.2%), the child measure IS the
    parent's measure decomposed, so the rule is inherited as a semi-join
    required filter and applied to the child's sum/ratio metrics.
    Without that proof nothing is inherited: whether e.g. RETURN amounts on
    cancelled orders count is a business question the data has not answered.
    """
    tables = {t["name"]: t for t in sl["tables"]}
    qualify = getattr(source, "qualify", lambda s, n: f'"{s}"."{n}"')
    for r in sl.get("relationships", []):
        if r.get("cardinality") != "many_to_one":
            continue
        child, parent = tables.get(r["from"]["table"]), tables.get(r["to"]["table"])
        if not child or not parent or child.get("table_type") != "fact":
            continue
        if parent.get("table_type") != "fact":
            continue
        rules = [f for f in parent.get("knowledge", {}).get("required_filters", [])
                 if f.get("scope") == "measures" and f.get("enforcement") == "required"]
        if not rules:
            continue
        _inherit_rules(source, qualify, stats, sl, child, parent, r, rules)


def _inherit_rules(source, qualify, stats, sl, child: dict, parent: dict,  # noqa: PLR0913, PLR0917
                   r: dict, rules: list[dict]) -> None:
    """Attach the parent's measure rules to one child fact when per-key reconciliation proves it."""
    fk, pk = r["from"]["columns"][0], r["to"]["columns"][0]
    verified = _child_measures_reconcile(source, qualify, stats, child, parent, fk, pk)
    if not verified:
        return
    evidence = (f"SUM({verified[0]}) per {fk} reconciles with {parent['name']}.{verified[1]} "
                f"({verified[2]})")
    rfs = child.setdefault("knowledge", {}).setdefault("required_filters", [])
    for f in rules:
        expr = f["expr"] if isinstance(f["expr"], str) else next(iter(f["expr"].values()))
        semi = f"{child['name']}.{fk} IN (SELECT {pk} FROM {parent['name']} WHERE {expr})"
        if any(x["expr"] == semi for x in rfs):
            continue
        rfs.append({"expr": semi, "scope": "measures", "enforcement": "required",
                    "reason": f"inherited from {parent['name']} ({expr}): {evidence}"})
        child.setdefault("provenance", []).append(
            {"signal": "statistic",
             "detail": f"required filter inherited from {parent['name']} via {fk} -> {pk}"})
        _apply_inherited_filter(sl, child["name"], parent["name"], fk, semi)


def _apply_inherited_filter(sl, child: str, parent: str, fk: str, semi: str) -> None:
    for m in sl.get("metrics", []):
        owns = (m.get("measure", "").startswith(child + ".")
                or m.get("numerator", "").startswith(child + "."))
        if owns and m.get("agg", "sum") in ("sum", "avg") and not m.get("filter"):
            m["filter"] = semi
            m.setdefault("provenance", []).append(
                {"signal": "statistic", "detail": f"inherits {parent}'s rule via {fk}"})


def _child_measures_reconcile(source, qualify, stats, child: dict, parent: dict,
                              fk: str, pk: str) -> tuple[str, str, str] | None:
    """(child_measure, parent_measure, evidence) if a child measure sums to the parent's per key."""
    c_ts, p_ts = stats[child["name"]].table, stats[parent["name"]].table
    cq, pq = qualify(c_ts.schema, c_ts.name), qualify(p_ts.schema, p_ts.name)
    c_meas = [c["name"] for c in child["columns"]
              if c.get("entity_role") == "measure" and c.get("semantic_type") == "monetary_value"]
    p_meas = [c["name"] for c in parent["columns"]
              if c.get("entity_role") == "measure" and c.get("semantic_type") == "monetary_value"]
    for cm in c_meas:
        sums = _group_sums(source, cq, fk, cm)
        if not sums:
            continue
        for pm in p_meas:
            rows = source.query(f'SELECT "{pk}", round("{pm}", 2) FROM {pq}'
                                f'{row_cap(source, _MAX_GROUPS)}')
            pvals = {k: float(v) for k, v in rows if k is not None and v is not None}
            common = [k for k in sums if k in pvals]
            if len(common) < 20:
                continue
            matched = sum(1 for k in common if _close(pvals[k], float(sums[k])))
            if matched / len(common) >= _MIN_COVERAGE:
                return cm, pm, f"{matched}/{len(common)} {fk} keys within {_REL_TOL:.1%}"
    return None


# ------------------------------------------------------------ grain / scd
_SCD_FROM = ("eff_start_dt", "valid_from", "eff_from_dt", "start_dt", "effective_from",
             "valid_from_dt")
_SCD_TO = ("eff_end_dt", "valid_to", "eff_to_dt", "end_dt", "effective_to", "valid_to_dt")
_SCD_CURR = ("is_curr_flg", "is_current", "is_curr", "current_flag", "curr_flg",
             "is_active_version")


def _infer_grain(sl) -> None:
    """State each table's grain in words, derived from its primary key (never guessed)."""
    for t in sl["tables"]:
        if t.get("grain"):
            continue
        pk = t.get("primary_key") or []
        if not pk:
            continue
        if t.get("table_type") == "snapshot_scd2":
            t["grain"] = f"one row per {', '.join(pk)} (one row per version of each entity)"
        elif t.get("table_type") == "aggregate":
            t["grain"] = f"one row per {', '.join(pk)} (pre-aggregated)"
        else:
            t["grain"] = f"one row per {', '.join(pk)}"


def _infer_scd(sl) -> None:
    """Populate `scd` (type 2 mechanics) on tables classified snapshot_scd2 from validity columns.

    Naming heuristic only; the block is what compile/lint/consumers use to
    build as-of joins instead of a plain current-row join.
    """
    for t in sl["tables"]:
        if t.get("table_type") != "snapshot_scd2" or t.get("scd"):
            continue
        names = {c["name"].lower(): c["name"] for c in t["columns"]}
        vf = next((names[n] for n in _SCD_FROM if n in names), None)
        vt = next((names[n] for n in _SCD_TO if n in names), None)
        cf = next((names[n] for n in _SCD_CURR if n in names), None)
        if not (vf and vt):
            continue
        scd = {"type": 2, "valid_from": vf, "valid_to": vt}
        if cf:
            scd["is_current_flag"] = cf
        pk = set(t.get("primary_key") or [])
        nk = sorted((c["name"] for c in t["columns"]
                     if c["name"] not in pk and c["name"].lower().endswith("_id")
                     and c.get("entity_role") in ("natural_key", "unique_key",
                                                  "foreign_key", "dimension")),
                    key=lambda n: (n.lower() not in ("cust_id", "customer_id"),
                                   not n.lower().startswith(t["name"][:4].lower()), n))
        if nk:
            scd["natural_key"] = nk[:1]
        t["scd"] = scd
        notes = t.setdefault("knowledge", {}).setdefault("usage_notes", [])
        rule = (f"SCD type 2: multiple rows per entity. For attributes as of a fact's date, join "
                f"ON key AND fact_date BETWEEN {vf} AND COALESCE({vt}, DATE '9999-12-31'); "
                + (f"use {cf} = 1 only for 'current' questions; " if cf else "")
                + "count entities with COUNT(DISTINCT key), not COUNT(*)")
        if rule not in notes:
            notes.append(rule)
        detail = f"scd2 validity columns {vf}/{vt}" + (f", current flag {cf}" if cf else "")
        t.setdefault("provenance", []).append({"signal": "naming", "detail": detail})


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
