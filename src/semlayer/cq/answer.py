"""CQ answerer: simulates an agent answering a business question under three context conditions.

This is the ablation instrument (M5) and benchmark core (M6).

Conditions:
  schema_only      raw DDL-ish column listing (what agents get today)
  semantic         + the semantic layer (descriptions, types, FKs, metrics,
                     required filters, routing, decodes)
  semantic+ontology + the base graph (join paths, aggregate edges, groups)

The answerer writes DuckDB SQL; we execute it and compare to the gold CQ's
expected result (scalar tolerance / row-set equality). Refusal/clarification
CQs score by behavior, not values.
"""

from __future__ import annotations

import itertools
import json
import re
import threading

QUERY_TIMEOUT_S = 60.0  # an agent's cross join must not hang the benchmark


def _execute(con, sql: str) -> list:
    """Run `sql` with a watchdog: interrupt DuckDB after QUERY_TIMEOUT_S seconds."""
    timer = threading.Timer(QUERY_TIMEOUT_S, con.interrupt)
    timer.start()
    try:
        return con.execute(sql).fetchall()
    finally:
        timer.cancel()

PROMPT_VERSION = "v3"

SYSTEM = f"""You are a data analyst agent. Answer the business question by writing
ONE DuckDB SQL query using ONLY the context provided. Prompt version {PROMPT_VERSION}.
Rules:
- APPLY every required_filter listed for a table you use, within its stated
  scope, even if the question's wording seems to contradict it: the filter
  encodes a business rule the data owner has verified.
- If the question names a table marked UNUSABLE/deprecated, answer from its
  listed replacement instead (say so in "reason"); never read the deprecated
  table and never refuse just because the deprecated name was used.
- Follow the join/grain notes: do not sum a table's measures after joining
  a many-row child (fan-out); use as-of joins for SCD2 dimensions when the
  question asks for attributes at the time of an event.
- If the question asks for a derived metric that is not listed but can be
  composed from listed metrics or columns (a ratio, an average per X),
  compute it; only refuse or clarify when the context truly cannot answer.
- Use only columns listed in the context; if a needed column is not shown,
  clarify rather than invent one.
- A question asking for one number (a total, a count, whether two figures
  reconcile) gets ONE row with ONE value (for "reconcile": the difference).
Respond ONLY JSON:
{{"action": "sql" | "refuse" | "clarify",
 "sql": "..." | null,
 "reason": "<=20 words when refusing/clarifying"}}"""


def schema_only_context(doc: dict) -> str:
    """Render the raw-schema baseline context: table/column names and SQL types only."""
    lines = []
    for t in doc["semantic_layer"]["tables"]:
        cols = ", ".join(f"{c['name']} {c['sql_type']}" for c in t["columns"])
        lines.append(f"TABLE {t['name']} ({cols})")
    return "\n".join(lines)


_QUESTION_STOP = {
    "what", "was", "were", "is", "are", "the", "a", "an", "of", "by", "for", "in", "on", "to",
    "how", "many", "much", "do", "we", "have", "had", "has", "total", "which", "with", "and",
    "or", "per", "each", "all", "from", "at", "that", "this", "their", "our", "using", "as",
    "did", "does", "it", "its", "be", "been", "there", "into", "than", "over", "across",
}


def _hit_tables(doc: dict, question: str) -> list[str]:
    """Tables owning a keyword hit (table, column, or metric measure), best first."""
    from semlayer import mcp_server
    out: list[str] = []
    for h in mcp_server.search(doc, question, limit=12):
        name = None
        if h["kind"] == "column":
            name = h["name"].split(".")[0]
        elif h["kind"] == "table":
            name = h["name"]
        elif h["kind"] == "metric" and h.get("measure"):
            name = str(h["measure"]).split(".")[0]
        if name and name not in out:
            out.append(name)
    return out


def _coverage_tables(doc: dict, question: str, tables: list[str]) -> list[str]:
    """One owning table per question content word not yet covered by `tables`.

    A single-token hit ("class" -> item.i_class) must not be crowded out by
    two-token hits ("net profit") on three sales facts.
    """
    from semlayer import mcp_server
    covered: set[str] = set()
    for t in doc["semantic_layer"]["tables"]:
        if t["name"] in tables:
            covered |= mcp_server._tokens(t["name"])
            for c in t["columns"]:
                covered |= mcp_server._tokens(c["name"])
    out: list[str] = []
    for tok in sorted(mcp_server._tokens(question) - covered - _QUESTION_STOP):
        for h in mcp_server.search(doc, tok, limit=3):
            if h["kind"] in ("column", "table"):
                out.append(h["name"].split(".")[0])
                break
    return out


def _neighbors(doc: dict, seeds: list[str]) -> list[str]:
    """Direct join neighbours of the seed tables (facts are useless without their dims)."""
    out: list[str] = []
    for r in doc["semantic_layer"].get("relationships", []):
        a, b = r["from"]["table"], r["to"]["table"]
        for name, other in ((a, b), (b, a)):
            if name in seeds and other not in out:
                out.append(other)
    return out


def _select_tables(doc: dict, question: str) -> tuple[list[str], list[dict]]:
    """Progressive disclosure, as an agent would use the MCP surface.

    search -> coverage -> routing -> join-neighbor expansion. Returns
    (detailed table names, compact index of the rest).
    """
    from semlayer import mcp_server
    tables: list[str] = []

    def _add_all(names: list[str]) -> None:
        for n in names:
            if n not in tables:
                tables.append(n)

    _add_all(_hit_tables(doc, question))
    _add_all(_coverage_tables(doc, question, tables))
    for r in mcp_server.routing(doc, question)[:2]:
        _add_all(r.get("use", []))
    _add_all(_neighbors(doc, tables[:4]))
    detailed = tables[:8]
    # compact index of everything else so the agent knows what exists
    index = [{"name": t["name"], "type": t.get("table_type"),
              "description": (t.get("description") or "")[:60]}
             for t in doc["semantic_layer"]["tables"]
             if t["name"] not in detailed and t.get("lifecycle") not in ("deprecated", "orphaned")]
    return detailed, index


def _table_header(d: dict) -> list[str]:
    """Header lines for one table: identity, grain, SCD mechanics, notes, rules, filters, pk."""
    lines = [f"TABLE {d['name']} ({d.get('table_type')}) — {d.get('description', '')[:100]}"]
    if d.get("UNUSABLE"):
        rep = d.get("deprecation", {}).get("replacement", "?")
        lines[0] += f"  [UNUSABLE: deprecated — columns withheld; answer from {rep} instead]"
        return lines
    for f in d.get("required_filters", []):
        scope = ("amount aggregations (SUM/AVG), not counts" if f.get("scope") == "measures"
                 else "all queries")
        reason = f" — {f['reason'][:90]}" if f.get("reason") else ""
        lines.append(f"  required_filter [{f.get('enforcement', 'required')}; {scope}]: "
                     f"{f['expr']}{reason}")
    if d.get("grain"):
        lines.append(f"  grain: {d['grain']}")
    if d.get("scd"):
        sc = d["scd"]
        bits = [f"valid_from={sc.get('valid_from')}", f"valid_to={sc.get('valid_to')}"]
        if sc.get("is_current_flag"):
            bits.append(f"current_flag={sc['is_current_flag']}")
        if sc.get("natural_key"):
            bits.append(f"natural_key={','.join(sc['natural_key'])}")
        lines.append("  scd2: " + " ".join(bits))
    if d.get("ai_context"):
        lines.append(f"  note: {d['ai_context'][:120]}")
    for n in d.get("usage_notes", []):
        lines.append(f"  rule: {n[:220]}")
    pk = d.get("primary_key")
    if pk:
        lines.append(f"  pk: {', '.join(pk)}")
    return lines


def _compact_table(doc: dict, name: str) -> str:
    """Render one table's full detail block for the semantic context."""
    from semlayer import mcp_server
    d = mcp_server.get_table(doc, name)
    if "error" in d:
        return ""
    lines = _table_header(d)
    if d.get("UNUSABLE"):
        return "\n".join(lines)
    for r in d.get("relationships", []):
        frm, to = r["from"], r["to"]
        fc, tc = ",".join(frm["columns"]), ",".join(to["columns"])
        if frm["table"] == d["name"]:
            lines.append(f"  join: {frm['table']}.{fc} -> {to['table']}.{tc}"
                         f" ({r.get('cardinality', 'many_to_one')})")
        elif r.get("fanout_risk"):
            lines.append(f"  child: {frm['table']}.{fc} -> {d['name']}.{tc}"
                         f" (one_to_many: joining {frm['table']} multiplies {d['name']} rows —"
                         f" aggregate {frm['table']} first or use EXISTS/IN)")
    for c in d.get("columns", []):
        bits = [c["name"], c.get("sql_type", ""), c.get("semantic_type", "")]
        fk = c.get("foreign_key")
        if fk:
            bits.append(f"-> {fk['references']}")
        if c.get("enum_values"):
            decs = ", ".join(f"{e['value']}={e['meaning']}" for e in c["enum_values"][:12])
            bits.append(f"[{decs}]")
        desc = (c.get("description") or "")[:50]
        lines.append("  " + " ".join(b for b in bits if b) + (f" — {desc}" if desc else ""))
    return "\n".join(lines)


def _render_sections(doc: dict, detailed: list[str], index: list[dict], question: str) -> str:
    """Assemble the detailed tables, metrics, routing, and index into one context string."""
    from semlayer import mcp_server
    parts = [_compact_table(doc, n) for n in detailed]
    shown = set(detailed)

    qtoks = mcp_server._tokens(question) - _QUESTION_STOP

    def _relevant(m: dict) -> bool:
        refs = [m.get("measure"), m.get("numerator"), m.get("denominator")]
        if not any(str(r).split(".")[0] in shown for r in refs if r):
            return False
        # a plain sum/count over a listed column adds nothing the column list
        # lacks; show a metric when it carries a business rule, is composed
        # (ratio/derived), or its name matches the question
        if m.get("filter") or m.get("type") not in (None, "simple"):
            return True
        hay = mcp_server._tokens(" ".join([m["name"], *(m.get("synonyms") or [])]))
        return bool(qtoks & hay)

    def _formula(m: dict) -> str:
        if m.get("type") == "ratio":
            return f"ratio SUM({m.get('numerator')}) / COUNT({m.get('denominator')})"
        return f"{m.get('agg')}({m.get('measure')})"

    metrics_lines = [
        f"METRIC {m['name']}: {_formula(m)}"
        + (f" WHERE {m['filter']}" if m.get("filter") else "")
        + (f"  [aka {', '.join(m['synonyms'][:2])}]" if m.get("synonyms") else "")
        for m in mcp_server.list_metrics(doc) if _relevant(m)
    ]
    routing_lines = [
        f"ROUTING '{r['intent']}': use {', '.join(r.get('use', []))}"
        + ("; avoid " + ", ".join(a["table"] for a in r["avoid"]) if r.get("avoid") else "")
        for r in mcp_server.routing(doc, question)[:4]
    ]
    index_lines = [f"{i['name']} ({i['type']}) {i['description']}" for i in index]
    return ("\n\n".join(p for p in parts if p)
            + "\n\nMETRICS:\n" + "\n".join(metrics_lines)
            + "\nROUTING:\n" + "\n".join(routing_lines)
            + "\nOTHER TABLES:\n" + "\n".join(index_lines))


def semantic_context(doc: dict, question: str) -> str:
    """Progressive disclosure, as an agent would use the MCP surface.

    search -> routing -> full detail for the top relevant tables.
    """
    detailed, index = _select_tables(doc, question)
    return _render_sections(doc, detailed, index, question)


def ontology_context(doc: dict, graph: dict, question: str) -> str:
    """Extend the semantic context with the ontology base graph's edges and groups."""
    base = semantic_context(doc, question)
    onto = {
        "edges": graph["ontology"]["edges"],
        "entity_groups": graph["ontology"]["entity_groups"],
    }
    return base + "\n\nONTOLOGY GRAPH:\n" + json.dumps(onto, default=str)


def answer(llm, context: str, question: str) -> dict:
    """Ask the LLM to answer one question over the given context; parse its JSON reply."""
    raw = llm.complete(SYSTEM, f"CONTEXT:\n{context}\n\nQUESTION: {question}",
                       max_tokens=800)
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {"action": "error", "sql": None}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"action": "error", "sql": None}


_LABELS: dict[int, dict[str, str]] = {}


def _label_map(con) -> dict[str, str]:
    """Label -> code map from the warehouse's two-column dictionary tables.

    Lets a value-equivalent answer ("Call Center") score against the code
    ("CALL") the gold SQL happens to return. Built once per connection.
    """
    key = id(con)
    if key not in _LABELS:
        m: dict[str, str] = {}
        try:
            tables = [r[0] for r in con.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main'").fetchall()]
            for t in tables:
                cols = con.execute(f'DESCRIBE "{t}"').fetchall()
                if len(cols) != 2 or not all("VARCHAR" in c[1] for c in cols):
                    continue
                rows = con.execute(f'SELECT "{cols[0][0]}", "{cols[1][0]}" FROM "{t}"').fetchall()
                for code, label in rows:
                    if code is not None and label is not None and str(label) not in m:
                        m[str(label)] = str(code)
        except Exception:  # scoring aid only: never fail scoring on dictionary lookup
            pass
        _LABELS[key] = m
    return _LABELS[key]


def _norm_cell(v, labels: dict[str, str] | None = None) -> str:
    """Canonical string for one result cell: numbers rounded, midnight timestamps as dates."""
    import datetime as _dt
    import decimal
    if isinstance(v, (float, decimal.Decimal)):
        return str(round(float(v), 2))
    if isinstance(v, _dt.datetime) and (v.hour, v.minute, v.second) == (0, 0, 0):
        return str(v.date())
    sv = str(v)
    if labels and sv in labels:
        return labels[sv]
    return sv


def _normalize_rows(rows: list, labels: dict[str, str] | None = None) -> set:
    """Normalize row-set results for order-independent, float-tolerant comparison."""
    return {tuple(_norm_cell(v, labels) for v in r) for r in rows}


def _score_scalar(got: list, expected: list, tol: float) -> tuple[bool, str]:
    """Compare a scalar CQ result against expected within tolerance.

    The expected value may appear in ANY column of the first result row
    (agents legitimately return supporting columns alongside the answer);
    a multi-row result is still wrong.
    """
    try:
        expected_val = float(expected[0][0])
    except (TypeError, ValueError, IndexError):
        return False, "non-numeric scalar"
    if len(got) != 1:
        if not got and expected_val == 0:
            return True, "empty result, expected 0"
        return False, f"{len(got)} rows for a scalar question"
    cands: list[float] = []
    for v in got[0]:
        if v is None:
            if expected_val == 0:
                cands.append(0.0)
            continue
        try:
            cands.append(float(v))
        except (TypeError, ValueError):
            continue
    if not cands:
        return False, "null result"
    ok = any(abs(c - expected_val) <= max(tol, abs(expected_val) * 0.001) for c in cands)
    return ok, f"got {cands[0] if len(cands) == 1 else cands}, expected {expected_val}"


def _rows_match(got: list, expected: list, labels: dict[str, str] | None = None) -> bool:
    """Row-set equality tolerant of extra/reordered columns in the agent's result.

    Every expected row must be recoverable as the same projection of the
    agent's rows (a superset of columns is accepted; a missing column, a
    missing row, or a different grain is not). Candidate columns are matched
    by content first, so a wide result never triggers a permutation search.
    """
    from collections import Counter
    exp = _normalize_rows(expected, labels)
    if not expected or not got:
        return not expected and not got
    k, n = len(expected[0]), len(got[0])
    if n == k:
        return _normalize_rows(got, labels) == exp
    if n < k or n > 16:
        return False
    exp_cols = [Counter(_norm_cell(r[j], labels) for r in expected) for j in range(k)]
    got_cols = [Counter(_norm_cell(r[i], labels) for r in got) for i in range(n)]
    cands = [[i for i in range(n) if got_cols[i] == exp_cols[j]] for j in range(k)]
    return any(
        len(set(idx)) == k
        and _normalize_rows([tuple(r[i] for i in idx) for r in got], labels) == exp
        for idx in itertools.product(*cands)
    )


def score_answer(con, cq: dict, result: dict) -> tuple[bool, str]:
    """Returns (passed, detail)."""
    kind = cq["expected_kind"]
    action = result.get("action")
    if kind in ("refusal", "clarification"):
        ok = action in ("refuse", "clarify")
        return ok, f"expected {kind}, got {action}"
    if action != "sql" or not result.get("sql"):
        return False, f"expected sql, got {action}"
    try:
        got = _execute(con, result["sql"])
    except Exception as e:
        return False, f"sql error: {str(e)[:80]}"
    expected = _execute(con, cq["expected_sql"]["duckdb"])
    if kind == "scalar":
        return _score_scalar(got, expected, cq.get("tolerance", 0.01))
    # rows: compare as normalized sets of stringified rows (extra columns tolerated)
    ok = _rows_match(got, expected, _label_map(con))
    return ok, f"{len(got)} rows vs {len(expected)} expected"


def answer_with_repair(llm, con, context: str, question: str, doc: dict | None = None) -> dict:
    """One-shot answer plus repair rounds.

    The realistic agent loop: agents see execution errors and fix their SQL.
    With `doc`, the semantic linter runs first and its error/warning findings
    are fed back for one repair round BEFORE execution (the `check_sql`
    MCP tool, as an agent would call it).
    """
    res = answer(llm, context, question)
    if res.get("action") != "sql" or not res.get("sql"):
        return res
    if doc is not None:
        from semlayer.lint import lint_sql, render_findings
        lint = lint_sql(doc, res["sql"])
        if lint["errors"] or lint["warnings"]:
            lint_prompt = (
                f"{question}\n\nYour previous SQL:\n{res['sql']}\n"
                f"check_sql found problems against the semantic layer:\n"
                f"{render_findings(lint)}\n"
                "Fix the SQL. Required filters are verified business rules and take "
                "precedence over the question's wording: apply them and note it in "
                "\"reason\". Clarify or refuse only if no correct SQL exists."
            )
            fixed = answer(llm, context, lint_prompt)
            if fixed.get("action") != "sql" or not fixed.get("sql"):
                return fixed
            res = fixed
    try:
        _execute(con, res["sql"])
        return res
    except Exception as e:
        repair_prompt = (
            f"{question}\n\nYour previous SQL failed:\n{res['sql']}\n"
            f"ERROR: {str(e)[:300]}\nFix it."
        )
        repair = answer(llm, context, repair_prompt)
        return repair if repair.get("sql") else res
