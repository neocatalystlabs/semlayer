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
    """Compile state: metric + document indexes + accumulated joins."""

    def __init__(self, sl: dict, metric: dict, base: str, dialect: str):
        self.sl, self.metric, self.base, self.dialect = sl, metric, base, dialect
        self.tables = {t["name"]: t for t in sl["tables"]}
        self.reach = _reachable(sl, self.tables, base)
        self.legal = sorted(self.reach)
        self.joins: dict[str, tuple] = {}   # dim table -> (from_cols, to_cols)
        self.time_col: str | None = None


def compile_metric(doc: dict, name: str, group_by: list[str] | None = None,  # noqa: PLR0913 — flat signature mirrors the MCP tool schema
                   time_grain: str | None = None, time_start: str | None = None,
                   time_end: str | None = None, extra_filter: str | None = None,
                   dialect: str = "duckdb") -> dict:
    """Compile a metric to SQL, or refuse constructively.

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
    time_expr = _resolve_time(ctx, time_grain, time_start, time_end)
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

def _reachable(sl: dict, tables: dict, base: str) -> dict[str, str]:
    """Legal group-by columns: 'table.column' -> owning table.

    Base-table non-measure columns plus non-measure columns of dimensions one
    N:1 hop away. Depth is capped at one hop in Phase A so every emitted join
    is a directly-inferred relationship, never a composed path.
    """
    legal: dict[str, str] = {}
    for c in tables[base]["columns"]:
        if c.get("entity_role") != "measure":
            legal[f"{base}.{c['name']}"] = base
    for r in sl.get("relationships", []):
        if r["from"]["table"] != base:
            continue
        if r["cardinality"] not in ("many_to_one", "one_to_one"):
            continue
        dim = r["to"]["table"]
        t = tables.get(dim)
        if t is None or t.get("lifecycle") in ("deprecated", "orphaned"):
            continue
        for c in t["columns"]:
            if c.get("entity_role") != "measure":
                legal[f"{dim}.{c['name']}"] = dim
    return legal


def _join_edge(sl: dict, base: str, dim: str) -> tuple | None:
    for r in sl.get("relationships", []):
        if (r["from"]["table"] == base and r["to"]["table"] == dim
                and r["cardinality"] in ("many_to_one", "one_to_one")):
            return (r["from"]["columns"], r["to"]["columns"])
    return None


def _require_join(ctx: _Ctx, owner: str) -> dict | None:
    if owner == ctx.base or owner in ctx.joins:
        return None
    edge = _join_edge(ctx.sl, ctx.base, owner)
    if edge is None:
        return _refuse(f"no N:1 join edge from {ctx.base} to {owner}", ctx.legal)
    ctx.joins[owner] = edge
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


def _resolve_time(ctx: _Ctx, time_grain, time_start, time_end) -> str | dict | None:
    if not (time_grain or time_start or time_end):
        return None
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
    if time_grain not in TIME_GRAINS:
        return _refuse(f"unknown time_grain '{time_grain}'", ctx.legal)
    return _grain_sql(ctx.dialect, time_grain, qual)


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

def _emit(ctx: _Ctx, cols: list[str], time_expr: str | None,
          where: list[str], value_sql: str) -> str:
    select, group = [], []
    if time_expr:
        select.append(f"{time_expr} AS period")
        group.append(time_expr)
    for qual in cols:
        select.append(f"{qual} AS {qual.split('.', 1)[1]}")
        group.append(qual)
    select.append(f"{value_sql} AS {ctx.metric['name']}")
    sql = f"SELECT {', '.join(select)}\nFROM {ctx.base}"
    for dim, (fcols, tcols) in ctx.joins.items():
        on = " AND ".join(f"{ctx.base}.{f} = {dim}.{t}" for f, t in zip(fcols, tcols, strict=False))
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
