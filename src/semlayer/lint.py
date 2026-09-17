"""Semantic SQL linter: check SQL an agent (or a human) wrote against the layer.

Every vendor performs these checks inside a closed compiler; here they run
over arbitrary SQL against the open document, before execution. Findings
are deterministic, explainable, and cheap (no LLM):

  parse_error                 the SQL does not parse in the given dialect
  unknown_table               a referenced table is not in the layer
  unknown_column              a referenced column does not exist on its table
  correlated_reference        an unqualified column that is NOT on the tables
                              of its own query block silently resolves to an
                              outer block (the classic vacuous `IN (SELECT ...)`)
  deprecated_table            a deprecated/orphaned table is read (replacement named)
  missing_required_filter     a table's required filter is absent for a query
                              that its scope covers (all / amount aggregations)
  fanout_aggregate            SUM/AVG over a parent table's column in a block
                              that also joins a many-rows child of that parent
  scd2_without_validity       an SCD2 table is used without its validity window
                              or current-row flag

Severity: `error` findings mean the answer is wrong or unsafe; `warning`
findings mean it is probably wrong; `info` is advisory. Consumers SHOULD
repair on errors and warnings before executing.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import cast

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

_ADDITIVE = (exp.Sum, exp.Avg)
_MEASURE_TYPES = {"monetary_value", "quantity", "rate", "percentage", "measure"}


@dataclass
class Finding:
    """One linter finding; `fix` is the actionable hint a consumer feeds back to the agent."""

    rule: str
    severity: str  # error | warning | info
    message: str
    table: str | None = None
    column: str | None = None
    fix: str | None = None


class _Layer:
    """Lookup views over the semantic layer, all lower-cased."""

    def __init__(self, doc: dict, dialect: str):
        sl = doc["semantic_layer"]
        self.dialect = dialect
        self.tables: dict[str, dict] = {t["name"].lower(): t for t in sl["tables"]}
        self.columns: dict[str, dict[str, dict]] = {
            n: {c["name"].lower(): c for c in t["columns"]} for n, t in self.tables.items()
        }
        # child -> parent for every many_to_one relationship that fans out from the parent side
        self.children: dict[str, set[str]] = {}
        for r in sl.get("relationships", []):
            if r.get("fanout_risk"):
                child, parent = r["from"]["table"].lower(), r["to"]["table"].lower()
                self.children.setdefault(parent, set()).add(child)

    def is_measure(self, table: str, column: str) -> bool:
        c = self.columns.get(table, {}).get(column)
        if not c:
            return False
        return c.get("entity_role") == "measure" or c.get("semantic_type") in _MEASURE_TYPES


def _filter_expr(f: dict, dialect: str) -> str:
    """A required filter's expression for this dialect (dialect_sql may be a string or a map)."""
    e = f.get("expr")
    if isinstance(e, dict):
        return e.get(dialect) or next(iter(e.values()), "")
    return str(e or "")


def _bare(node: exp.Expression, dialect: str) -> str:
    """Normalised predicate text with table qualifiers stripped, for structural comparison."""
    stripped = node.transform(
        lambda n: exp.column(n.name) if isinstance(n, exp.Column) else n
    )
    return stripped.sql(dialect=dialect).lower()


def _conjuncts(scope: Scope) -> list[exp.Expression]:
    """All top-level AND-ed predicates of a query block: WHERE plus every JOIN ... ON."""
    out: list[exp.Expression] = []
    sel = scope.expression
    where = sel.args.get("where")
    if where is not None:
        out.extend(_split_and(where.this))
    for j in sel.args.get("joins", []) or []:
        on = j.args.get("on")
        if on is not None:
            out.extend(_split_and(on))
    return out


def _split_and(node: exp.Expression) -> list[exp.Expression]:
    """Top-level conjuncts of a predicate (the predicate itself when it is not an AND).

    Parentheses are unwrapped first, at every level: `(a)` is one conjunct and
    `(a AND b)` is two. Without this a parenthesised predicate normalises with
    its brackets attached and can never equal a required filter -- and
    `compile_metric` emits exactly that form, so the linter reported our own
    compiler's output as missing the filter the compiler had just applied.
    """
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.And):
        return [c for n in node.flatten() for c in _split_and(cast(exp.Expression, n))]
    return [node]


def _physical(scope: Scope) -> dict[str, str]:
    """Alias -> physical table for the real tables in this block (CTEs/subqueries excluded)."""
    out: dict[str, str] = {}
    for alias, src in scope.sources.items():
        if isinstance(src, exp.Table):
            out[alias.lower()] = src.name.lower()
    return out


def _block_aggregates(scope: Scope) -> list[exp.AggFunc]:
    """Aggregate calls that belong to this block (not to a nested subquery)."""
    return [a for a in scope.expression.find_all(exp.AggFunc)
            if a.find_ancestor(exp.Select) is scope.expression]


def _outer_tables(scope: Scope) -> set[str]:
    tables: set[str] = set()
    p = scope.parent
    while p is not None:
        tables |= set(_physical(p).values())
        p = p.parent
    return tables


class _Linter:
    def __init__(self, layer: _Layer):
        self.L = layer
        self.findings: list[Finding] = []
        self._seen: set[tuple] = set()

    # ---------------------------------------------------------- per block
    def block(self, scope: Scope) -> None:
        phys = _physical(scope)
        self._tables(phys)
        self._columns(scope, phys)
        self._required_filters(scope, phys)
        self._fanout(scope, phys)
        self._scd2(scope, phys)

    def _tables(self, phys: dict[str, str]) -> None:
        for name in set(phys.values()):
            t = self.L.tables.get(name)
            if t is None:
                self._add(Finding("unknown_table", "error",
                                             f"table {name} is not in the semantic layer", name))
            elif t.get("lifecycle") in ("deprecated", "orphaned"):
                rep = t.get("deprecation", {}).get("replacement")
                self._add(Finding(
                    "deprecated_table", "error",
                    f"{name} is {t['lifecycle']}"
                    + (f"; use {rep} instead" if rep else ""),
                    name, fix=f"rewrite the query against {rep}" if rep else None))

    def _resolve_column(self, col: exp.Column, scope: Scope, phys: dict[str, str]) -> str | None:
        """Physical table owning this column reference, or None when unresolved."""
        if col.table:
            return phys.get(col.table.lower())
        owners = [t for t in set(phys.values()) if col.name.lower() in self.L.columns.get(t, {})]
        return owners[0] if owners else None

    def _add(self, f: Finding) -> None:
        key = (f.rule, f.table, f.column)
        if key not in self._seen:
            self._seen.add(key)
            self.findings.append(f)

    def _columns(self, scope: Scope, phys: dict[str, str]) -> None:
        # explicit `expr AS alias` names are legal in GROUP BY / ORDER BY / HAVING
        aliases = {e.alias.lower() for e in scope.expression.expressions
                   if isinstance(e, exp.Alias)}
        for col in scope.columns:
            if col.find_ancestor(exp.Select) is not scope.expression:
                continue  # belongs to a nested subquery block, linted in its own scope
            if col.table:
                self._qualified_column(col, scope, phys)
            elif col.name.lower() not in aliases and not self._resolve_column(col, scope, phys):
                self._unqualified_column(col, scope, phys)

    def _qualified_column(self, col: exp.Column, scope: Scope, phys: dict[str, str]) -> None:
        name, alias = col.name.lower(), col.table.lower()
        src = scope.sources.get(alias) or scope.sources.get(col.table)
        if isinstance(src, Scope):  # CTE / derived table: validate against its outputs
            outs = {a.lower() for a in getattr(src.expression, "named_selects", [])}
            if outs and name not in outs and not src.expression.is_star:
                self._add(Finding("unknown_column", "error",
                                  f"{col.table}.{col.name}: {col.table} does not output {col.name}",
                                  col.table, col.name))
            return
        table = phys.get(alias)
        if table is None or table not in self.L.columns:
            return  # outer alias, or unknown table (reported by _tables)
        if name not in self.L.columns[table]:
            cols = ", ".join(list(self.L.columns[table])[:12])
            self._add(Finding("unknown_column", "error", f"{table}.{col.name} does not exist",
                              table, col.name, fix=f"columns of {table}: {cols}"))

    def _unqualified_column(self, col: exp.Column, scope: Scope, phys: dict[str, str]) -> None:
        name = col.name.lower()
        here = {t for t in set(phys.values()) if t in self.L.columns}
        if not here:
            return  # block reads only CTEs / unknown tables; cannot validate
        outer = [t for t in _outer_tables(scope) if name in self.L.columns.get(t, {})]
        if outer:
            self._add(Finding(
                "correlated_reference", "error",
                f"{col.name} is not a column of {', '.join(sorted(here))}; it silently "
                f"resolves to the outer query's {outer[0]}.{col.name}, which makes this "
                f"subquery match every row",
                outer[0], col.name,
                fix=f"qualify the column or join {outer[0]} inside the subquery"))
        else:
            self._add(Finding("unknown_column", "error",
                              f"{col.name} is not a column of {', '.join(sorted(here))}",
                              None, col.name))

    def _required_filters(self, scope: Scope, phys: dict[str, str]) -> None:
        preds = [_bare(p, self.L.dialect) for p in _conjuncts(scope)]
        aggs = _block_aggregates(scope)
        for table in set(phys.values()):
            t = self.L.tables.get(table)
            if not t:
                continue
            for f in t.get("knowledge", {}).get("required_filters", []):
                expr = _filter_expr(f, self.L.dialect)
                if not expr:
                    continue
                if f.get("scope") == "measures" and not self._sums_measure_of(aggs, table, phys):
                    continue
                try:
                    parsed = cast(exp.Expression, sqlglot.parse_one(expr, read=self.L.dialect))
                    want = _bare(parsed, self.L.dialect)
                except sqlglot.errors.ParseError:
                    want = expr.lower()
                if (any(p == want for p in preds)
                        or self._semijoin_satisfied(expr, scope, phys, preds)):
                    continue
                cols = {c.name.lower() for c in sqlglot.parse_one(expr, read=self.L.dialect)
                        .find_all(exp.Column)}
                touched = any(c in p for p in preds for c in cols)
                sev = "advisory" if f.get("enforcement") == "advisory" else "required"
                self._add(Finding(
                    "missing_required_filter",
                    "warning" if touched or sev == "advisory" else "error",
                    f"{table} requires `{expr}`"
                    + (" for amount aggregations" if f.get("scope") == "measures"
                       else " for all queries")
                    + (f" — {f['reason']}" if f.get("reason") else "")
                    + ("; the query filters on that column differently" if touched else ""),
                    table, fix=f"add `{expr}` to the WHERE clause for {table}"))

    def _semijoin_satisfied(self, expr: str, scope: Scope, phys: dict[str, str],
                            preds: list[str]) -> bool:
        """Inherited `fk IN (SELECT pk FROM parent WHERE rule)` filters have a second form.

        The block may instead join the parent and apply the parent's rule directly.
        """
        try:
            node = sqlglot.parse_one(expr, read=self.L.dialect)
        except sqlglot.errors.ParseError:
            return False
        if not isinstance(node, exp.In) or not node.args.get("query"):
            return False
        query = node.args["query"]
        inner = query.this if isinstance(query, exp.Subquery) else query
        if not isinstance(inner, exp.Select):
            return False
        parent = inner.args.get("from") or inner.args.get("from_")
        where = inner.args.get("where")
        if parent is None or where is None:
            return False
        pname = parent.this.name.lower() if isinstance(parent.this, exp.Table) else ""
        if pname not in set(phys.values()):
            return False
        want = _bare(where.this, self.L.dialect)
        return any(p == want for p in preds)

    def _sums_measure_of(self, aggs: list[exp.AggFunc], table: str,
                         phys: dict[str, str]) -> bool:
        for a in aggs:
            if not isinstance(a, _ADDITIVE):
                continue
            for col in a.find_all(exp.Column):
                owner = phys.get(col.table.lower()) if col.table else (
                    table if col.name.lower() in self.L.columns.get(table, {}) else None)
                if owner == table and self.L.is_measure(table, col.name.lower()):
                    return True
        return False

    def _fanout(self, scope: Scope, phys: dict[str, str]) -> None:
        tables = set(phys.values())
        if len(tables) < 2:
            return
        seen: set[tuple[str, str]] = set()
        for a in _block_aggregates(scope):
            if not isinstance(a, _ADDITIVE):
                continue
            for col in a.find_all(exp.Column):
                parent = phys.get(col.table.lower()) if col.table else next(
                    (t for t in tables if col.name.lower() in self.L.columns.get(t, {})), None)
                if not parent:
                    continue
                for child in self.L.children.get(parent, set()) & tables:
                    if (parent, child) in seen:
                        continue
                    seen.add((parent, child))
                    self._add(Finding(
                        "fanout_aggregate", "error",
                        f"{a.sql_name()}({parent}.{col.name}) in a block that joins {child} "
                        f"(many rows per {parent}) multiplies {parent}'s values",
                        parent, col.name,
                        fix=f"aggregate {child} in a subquery first, or restrict {parent} with "
                            f"EXISTS/IN (SELECT ... FROM {child}) instead of joining it"))

    def _scd2(self, scope: Scope, phys: dict[str, str]) -> None:
        for alias, table in phys.items():
            t = self.L.tables.get(table)
            scd = (t or {}).get("scd")
            if not scd:
                continue
            markers = {str(scd.get(k, "")).lower()
                       for k in ("valid_from", "valid_to", "is_current_flag")}
            markers.discard("")
            used = {c.name.lower() for c in scope.expression.find_all(exp.Column)
                    if not c.table or c.table.lower() == alias}
            if markers & used:
                continue
            nk = ",".join(scd.get("natural_key") or []) or "the natural key"
            nks = [k.lower() for k in scd.get("natural_key") or []]
            distinct_nk = any(
                isinstance(a, exp.Count) and isinstance(a.this, exp.Distinct)
                and any(c.name.lower() in nks for c in a.find_all(exp.Column))
                for a in _block_aggregates(scope))
            if distinct_nk and len(set(phys.values())) == 1:
                continue
            self._add(Finding(
                "scd2_without_validity", "warning",
                f"{table} is SCD type 2 (several rows per {nk}) and the query uses neither "
                f"{scd.get('valid_from')}/{scd.get('valid_to')} nor "
                f"{scd.get('is_current_flag') or 'a current-row flag'}",
                table,
                fix=f"for attributes as of an event: AND event_date BETWEEN "
                    f"{scd.get('valid_from')} AND COALESCE({scd.get('valid_to')}, "
                    f"DATE '9999-12-31'); for current values: "
                    + (f"{scd['is_current_flag']} = 1" if scd.get("is_current_flag")
                       else f"{scd.get('valid_to')} IS NULL")))


def lint_sql(doc: dict, sql: str, dialect: str = "duckdb") -> dict:
    """Lint one SQL statement against the semantic layer.

    Returns {"ok": bool, "findings": [...], "errors": n, "warnings": n}.
    `ok` is False when any error-level finding exists.
    """
    layer = _Layer(doc, dialect)
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.ParseError as e:
        f = Finding("parse_error", "error", f"SQL does not parse as {dialect}: {str(e)[:160]}")
        return {"ok": False, "findings": [asdict(f)], "errors": 1, "warnings": 0}
    linter = _Linter(layer)
    try:
        scopes = traverse_scope(tree)
    except Exception:  # pragma: no cover - sqlglot cannot scope exotic statements
        scopes = []
    for scope in scopes:
        if isinstance(scope.expression, exp.Select):
            linter.block(scope)
    findings = [asdict(f) for f in linter.findings]
    errors = sum(1 for f in findings if f["severity"] == "error")
    warnings = sum(1 for f in findings if f["severity"] == "warning")
    return {"ok": errors == 0, "findings": findings, "errors": errors, "warnings": warnings}


def render_findings(result: dict) -> str:
    """Plain-text rendering used by the CLI and by consumers feeding findings back to an agent."""
    if not result["findings"]:
        return "OK: no findings"
    lines = []
    for f in result["findings"]:
        loc = (f"{f['table']}.{f['column']}" if f.get("table") and f.get("column")
               else (f.get("table") or ""))
        lines.append(f"[{f['severity']}] {f['rule']}{' ' + loc if loc else ''}: {f['message']}")
        if f.get("fix"):
            lines.append(f"    fix: {f['fix']}")
    return "\n".join(lines)
