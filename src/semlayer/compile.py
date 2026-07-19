"""Metric compilation with constructive refusals (Phase A, council-reviewed).

`compile_metric` turns a declared metric into executable SQL — the one
assembly step agents get silently wrong (joins, business-rule filters, time
bucketing). Design rules (prd/prd-metrics.md §3, design doc amendments):

- Group-by legality: a column is legal iff it is a non-measure column on the
  metric's base table or on a dimension one N:1 hop away (fan-out paths are
  refused in Phase A; SPEC §2.3 fanout_safety strategies arrive with Phase B).
- Refusals are CONSTRUCTIVE: every refusal names the reason and enumerates
  legal alternatives — a bare refusal would push agents back to hand-rolled
  SQL, which is the failure mode this module exists to remove (SPEC §2.10).
- Time is first-class: metrics carry `agg_time_dimension` (inferred by
  Enrich); time bucketing/windowing compiles through it, never through an
  agent-guessed date column.
"""

from __future__ import annotations

import re

TIME_GRAINS = ("day", "week", "month", "quarter", "year")
_AGG_SQL = {"sum": "SUM({})", "avg": "AVG({})", "min": "MIN({})", "max": "MAX({})",
            "count": "COUNT({})", "count_distinct": "COUNT(DISTINCT {})",
            "median": "MEDIAN({})"}
_SQL_WORDS = {"and", "or", "not", "in", "like", "is", "null", "between",
              "true", "false", "case", "when", "then", "else", "end"}


class _Ctx:
    """Compile state: metric + document indexes + join paths + accumulated joins."""

    def __init__(self, sl: dict, metric: dict, base: str, dialect: str):
        self.sl, self.metric, self.base, self.dialect = sl, metric, base, dialect
        self.tables = {t["name"]: t for t in sl["tables"]}
        self.paths, self.ambiguous = _join_paths(sl, self.tables, base)
        self.reach = _reachable(self.tables, base, self.paths)
        self.legal = sorted(self.reach)
        # dim table -> (parent table, parent cols, dim cols), insertion-ordered
        self.joins: dict[str, tuple] = {}
        self.time_col: str | None = None


def compile_metric(doc: dict, name: str, group_by: list[str] | None = None,  # noqa: PLR0913 — flat signature mirrors the MCP tool schema
                   time_grain: str | None = None, time_start: str | None = None,
                   time_end: str | None = None, extra_filter: str | None = None,
                   dialect: str = "duckdb", calendar: str | None = None) -> dict:
    """Compile a metric to SQL, or refuse constructively.

    `calendar`: "fiscal" | "calendar" — REQUIRED for quarter/year grains when
    the metric's date dimension carries a verified fiscal calendar (refusing
    to silently pick is SPEC 2.6 applied to time).
    Returns {"sql": str} on success; on refusal {"refused": True,
    "reason": str, "legal_group_by": [...], "legal_time_grains": [...]}.
    """
    ctx = _make_ctx(doc, name, dialect)
    if isinstance(ctx, dict):
        return ctx
    value_sql = _value_expression(ctx)
    if isinstance(value_sql, dict):
        return value_sql
    cols = _resolve_group_by(ctx, group_by or [])
    if isinstance(cols, dict):
        return cols
    time_expr = _resolve_time(ctx, time_grain, time_start, time_end, calendar)
    if isinstance(time_expr, dict):
        return time_expr
    where = _build_where(ctx, time_start, time_end, extra_filter)
    if isinstance(where, dict):
        return where
    return {"sql": _emit(ctx, cols, time_expr, where, value_sql)}


def _make_ctx(doc: dict, name: str, dialect: str) -> _Ctx | dict:
    sl = doc["semantic_layer"]
    metric = next((m for m in sl.get("metrics", []) if m["name"] == name), None)
    if metric is None:
        return _refuse(f"no metric named '{name}'",
                       [m["name"] for m in sl.get("metrics", [])][:25])
    if metric.get("lifecycle") in ("deprecated", "orphaned"):
        return _refuse(f"metric '{name}' is {metric['lifecycle']} and unusable per SPEC", [])
    base = (metric.get("measure") or metric.get("numerator") or ".").split(".", 1)[0]
    tables = {t["name"]: t for t in sl["tables"]}
    if base not in tables:
        return _refuse(f"metric base table '{base}' not in document", [])
    if tables[base].get("lifecycle") in ("deprecated", "orphaned"):
        return _refuse(f"base table '{base}' is {tables[base]['lifecycle']}", [])
    return _Ctx(sl, metric, base, dialect)


# ------------------------------------------------------------ value stage --

def _value_expression(ctx: _Ctx) -> str | dict:
    m = ctx.metric
    if m["type"] == "simple":
        col = m["measure"].split(".", 1)[1]
        return _AGG_SQL.get(m.get("agg", "sum"), "SUM({})").format(f"{ctx.base}.{col}")
    if m["type"] == "ratio":
        if "." not in m.get("numerator", "") or "." not in m.get("denominator", ""):
            return _refuse("ratio numerator/denominator must be 'table.column' "
                           "measure references", ctx.legal)
        num_t, num_c = m["numerator"].split(".", 1)
        den_t, den_c = m["denominator"].split(".", 1)
        if num_t != den_t:
            return _refuse("cross-table ratio compilation is not yet supported "
                           "(Phase B); numerator and denominator must share a "
                           "base table", ctx.legal)
        return (f"CAST(SUM({num_t}.{num_c}) AS DOUBLE) / "
                f"NULLIF(SUM({den_t}.{den_c}), 0)")
    return _refuse(f"metric type '{m['type']}' compilation is not yet supported; "
                   "consume the definition from get_metrics instead", ctx.legal)


# ------------------------------------------------------- reachability map --

MAX_HOPS = 3


def _join_paths(sl: dict, tables: dict, base: str) -> tuple[dict, set]:
    """BFS the N:1-only relationship graph from `base` (depth-capped).

    Returns (paths, ambiguous): paths maps each reachable table to its edge
    list [(parent, parent_cols, table_cols, table), ...]; SHORTEST path wins.
    Tables reached by two distinct equally-short paths (role-playing dims,
    true diamonds) land in `ambiguous` — grouping through them is refused
    rather than silently picking a path (SPEC 2.6 disambiguation applied to
    compilation).
    """
    edges: dict[str, list] = {}
    for r in sl.get("relationships", []):
        if r["cardinality"] in ("many_to_one", "one_to_one"):
            edges.setdefault(r["from"]["table"], []).append(
                (r["to"]["table"], r["from"]["columns"], r["to"]["columns"]))
    paths: dict[str, list] = {}
    depth: dict[str, int] = {base: 0}
    ambiguous: set[str] = set()
    frontier = [base]
    for d in range(1, MAX_HOPS + 1):
        nxt = []
        for parent in frontier:
            for to_t, fcols, tcols in edges.get(parent, []):
                t = tables.get(to_t)
                if to_t == base or t is None or t.get("lifecycle") in ("deprecated", "orphaned"):
                    continue
                if to_t in depth:
                    if depth[to_t] == d:
                        ambiguous.add(to_t)  # equally-short second path
                    continue
                depth[to_t] = d
                paths[to_t] = [*paths.get(parent, []), (parent, fcols, tcols, to_t)]
                nxt.append(to_t)
        frontier = nxt
    return paths, ambiguous


def _reachable(tables: dict, base: str, paths: dict) -> dict[str, str]:
    """Legal group-by columns: 'table.column' -> owning table.

    Base-table non-measure columns plus non-measure columns of every
    unambiguously N:1-reachable table (multi-hop; snowflaked dims included).
    """
    legal: dict[str, str] = {}
    for owner in [base, *paths.keys()]:
        for c in tables[owner]["columns"]:
            if c.get("entity_role") != "measure":
                legal[f"{owner}.{c['name']}"] = owner
    return legal


def _require_join(ctx: _Ctx, owner: str) -> dict | None:
    if owner == ctx.base:
        return None
    if owner in ctx.ambiguous:
        return _refuse(
            f"'{owner}' is reachable from {ctx.base} via multiple equally-short "
            f"join paths — ambiguous; group by the intermediate table's own "
            f"column instead (e.g. the code column carrying the relationship)",
            ctx.legal)
    path = ctx.paths.get(owner)
    if path is None:
        return _refuse(f"no N:1 join path from {ctx.base} to {owner}", ctx.legal)
    for parent, fcols, tcols, to_t in path:
        if to_t not in ctx.joins:
            ctx.joins[to_t] = (parent, fcols, tcols)
    return None


# -------------------------------------------------------- request stages --

def _resolve_group_by(ctx: _Ctx, group_by: list[str]) -> list | dict:
    cols = []
    for g in group_by:
        qual = g if "." in g else f"{ctx.base}.{g}"
        if qual not in ctx.reach:
            return _refuse(f"cannot group '{ctx.metric['name']}' by '{g}': "
                           f"{_why_illegal(ctx, qual)}", ctx.legal)
        refusal = _require_join(ctx, ctx.reach[qual])
        if refusal is not None:
            return refusal
        cols.append(qual)
    return cols


def _why_illegal(ctx: _Ctx, qual: str) -> str:
    tname, cname = qual.split(".", 1)
    t = ctx.tables.get(tname)
    if t is not None:
        c = next((c for c in t["columns"] if c["name"] == cname), None)
        if c is not None and c.get("entity_role") == "measure":
            return "it is a measure, not a dimension"
    return "not on the base table or any N:1-reachable dimension"


def _resolve_time(ctx: _Ctx, time_grain, time_start, time_end,
                  calendar: str | None = None) -> str | list | dict | None:
    if not (time_grain or time_start or time_end):
        return None
    err = _time_request_error(ctx, time_grain, calendar)
    if err is not None:
        return err
    atd = ctx.metric.get("agg_time_dimension")
    if not atd:
        return _refuse(f"metric '{ctx.metric['name']}' has no agg_time_dimension; "
                       "time bucketing would require guessing a date column, "
                       "which this compiler refuses to do", ctx.legal)
    qual = atd if "." in atd else f"{ctx.base}.{atd}"
    refusal = _require_join(ctx, qual.split(".", 1)[0])
    if refusal is not None:
        return refusal
    ctx.time_col = qual
    if time_grain is None:
        return None
    return _bucket_sql(ctx, time_grain, calendar, qual)


def _time_request_error(ctx: _Ctx, time_grain, calendar) -> dict | None:
    if calendar not in (None, "fiscal", "calendar"):
        return _refuse(f"unknown calendar '{calendar}' (use 'fiscal' or 'calendar')",
                       ctx.legal)
    if time_grain is not None and time_grain not in TIME_GRAINS:
        return _refuse(f"unknown time_grain '{time_grain}'", ctx.legal)
    return None


def _bucket_sql(ctx: _Ctx, time_grain: str, calendar: str | None,
                qual: str) -> str | list | dict:
    if time_grain in ("quarter", "year"):
        fiscal = _fiscal_cols(ctx, qual.split(".", 1)[0])
        if fiscal and calendar is None:
            names = ", ".join(fiscal.values())
            return _refuse(
                f"this warehouse carries a verified fiscal calendar ({names}); "
                f"pass calendar='fiscal' or calendar='calendar' — refusing to "
                f"pick silently", ctx.legal)
        if calendar == "fiscal":
            if not fiscal:
                return _refuse("no verified fiscal calendar columns on the "
                               "metric's date dimension", ctx.legal)
            return _fiscal_group_cols(qual.split(".", 1)[0], fiscal, time_grain)
    return _grain_sql(ctx.dialect, time_grain, qual)


def _fiscal_cols(ctx: _Ctx, owner: str) -> dict[str, str]:
    """{time_attribute: column_name} for verified fiscal columns on `owner`."""
    t = ctx.tables.get(owner)
    if t is None:
        return {}
    return {c["time_attribute"]: c["name"] for c in t["columns"]
            if c.get("time_attribute", "").startswith("fiscal_")}


def _fiscal_group_cols(owner: str, fiscal: dict[str, str], grain: str) -> list:
    cols = []
    if "fiscal_year" in fiscal:
        cols.append((f"{owner}.{fiscal['fiscal_year']}", "fiscal_year"))
    if grain == "quarter" and "fiscal_quarter" in fiscal:
        cols.append((f"{owner}.{fiscal['fiscal_quarter']}", "fiscal_quarter"))
    return cols


def _grain_sql(dialect: str, grain: str, col: str) -> str:
    if dialect == "bigquery":
        return f"DATE_TRUNC({col}, {grain.upper()})"
    return f"date_trunc('{grain}', {col})"


def _build_where(ctx: _Ctx, time_start, time_end, extra_filter) -> list | dict:
    where = []
    if ctx.metric.get("filter"):
        where.append(f"({ctx.metric['filter']})")
    if ctx.time_col is not None:
        if time_start:
            where.append(f"{ctx.time_col} >= '{time_start}'")
        if time_end:
            where.append(f"{ctx.time_col} < '{time_end}'")
    if extra_filter:
        bad = _unknown_identifiers(ctx, extra_filter)
        if bad:
            return _refuse("filter references unknown columns: "
                           + ", ".join(sorted(bad)), ctx.legal)
        for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z0-9_]+", extra_filter):
            refusal = _require_join(ctx, tok.split(".", 1)[0])
            if refusal is not None:
                return refusal
        where.append(f"({extra_filter})")
    return where


def _unknown_identifiers(ctx: _Ctx, predicate: str) -> set[str]:
    """Identifiers in a user predicate that resolve to no legal column."""
    bare_legal = {q.split(".", 1)[1] for q in ctx.reach} | set(ctx.reach)
    out = set()
    no_strings = re.sub(r"'[^']*'", "", predicate)
    for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_.]*", no_strings):
        if tok.lower() in _SQL_WORDS:
            continue
        if tok not in bare_legal and tok.lower() not in bare_legal:
            out.add(tok)
    return out


# ------------------------------------------------------------- emission --

def _emit(ctx: _Ctx, cols: list[str], time_expr: str | list | None,
          where: list[str], value_sql: str) -> str:
    select, group = [], []
    if isinstance(time_expr, list):
        for qual, alias in time_expr:
            select.append(f"{qual} AS {alias}")
            group.append(qual)
    elif time_expr:
        select.append(f"{time_expr} AS period")
        group.append(time_expr)
    for qual in cols:
        select.append(f"{qual} AS {qual.split('.', 1)[1]}")
        group.append(qual)
    select.append(f"{value_sql} AS {ctx.metric['name']}")
    sql = f"SELECT {', '.join(select)}\nFROM {ctx.base}"
    for dim, (parent, fcols, tcols) in ctx.joins.items():
        on = " AND ".join(f"{parent}.{f} = {dim}.{t}" for f, t in zip(fcols, tcols, strict=False))
        sql += f"\nLEFT JOIN {dim} ON {on}"
    if where:
        sql += "\nWHERE " + " AND ".join(where)
    if group:
        sql += "\nGROUP BY " + ", ".join(group) + "\nORDER BY " + ", ".join(group)
    return sql


def _refuse(reason: str, legal_group_by: list[str]) -> dict:
    return {"refused": True, "reason": reason,
            "legal_group_by": legal_group_by[:25],
            "legal_time_grains": list(TIME_GRAINS)}
