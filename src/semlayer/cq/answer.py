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

import json
import re

PROMPT_VERSION = "v1"

SYSTEM = f"""You are a data analyst agent. Answer the business question by writing
ONE DuckDB SQL query using ONLY the context provided. Prompt version {PROMPT_VERSION}.
Rules:
- If required filters are listed for a table you use, APPLY them (or the
  business-rule filters attached to metrics).
- Never use tables marked UNUSABLE/deprecated — use their replacement.
- If the question is ambiguous or unanswerable from the context, do NOT guess.
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


def _select_tables(doc: dict, question: str) -> tuple[list[str], list[dict]]:
    """Progressive disclosure, as an agent would use the MCP surface.

    search -> routing -> join-neighbor expansion. Returns (detailed table names,
    compact index of the rest).
    """
    from semlayer import mcp_server
    hits = mcp_server.search(doc, question, limit=8)
    tables = []
    for h in hits:
        name = h["name"].split(".")[0] if h["kind"] == "column" else h["name"]
        if h["kind"] in ("table", "column") and name not in tables:
            tables.append(name)
    for r in mcp_server.routing(doc, question)[:2]:
        for u in r.get("use", []):
            if u not in tables:
                tables.append(u)
    # join-neighbor expansion: facts are useless without their dims
    # ("quarterly revenue" needs date_dim even if no keyword matches it)
    rels = doc["semantic_layer"].get("relationships", [])
    for name in list(tables[:4]):
        for r in rels:
            for other in ({r["to"]["table"]} if r["from"]["table"] == name
                          else {r["from"]["table"]} if r["to"]["table"] == name else set()):
                if other not in tables:
                    tables.append(other)
    detailed = tables[:8]
    # compact index of everything else so the agent knows what exists
    index = [{"name": t["name"], "type": t.get("table_type"),
              "description": (t.get("description") or "")[:60]}
             for t in doc["semantic_layer"]["tables"]
             if t["name"] not in detailed and t.get("lifecycle") not in ("deprecated", "orphaned")]
    return detailed, index


def _compact_table(doc: dict, name: str) -> str:
    """Render one table's full detail block for the semantic context."""
    from semlayer import mcp_server
    d = mcp_server.get_table(doc, name)
    if "error" in d:
        return ""
    lines = [f"TABLE {d['name']} ({d.get('table_type')}) — {d.get('description', '')[:100]}"]
    if d.get("UNUSABLE"):
        lines[0] += "  [UNUSABLE — use " + d.get("deprecation", {}).get("replacement", "?") + "]"
    if d.get("ai_context"):
        lines.append(f"  note: {d['ai_context'][:120]}")
    for n in d.get("usage_notes", []):
        lines.append(f"  rule: {n[:140]}")
    for f in d.get("required_filters", []):
        lines.append(f"  required_filter: {f['expr']}")
    pk = d.get("primary_key")
    if pk:
        lines.append(f"  pk: {', '.join(pk)}")
    for c in d.get("columns", []):
        bits = [c["name"], c.get("sql_type", ""), c.get("semantic_type", "")]
        fk = c.get("foreign_key")
        if fk:
            bits.append(f"-> {fk['references']}")
        if c.get("enum_values"):
            decs = ", ".join(f"{e['value']}={e['meaning']}" for e in c["enum_values"][:6])
            bits.append(f"[{decs}]")
        desc = (c.get("description") or "")[:50]
        lines.append("  " + " ".join(b for b in bits if b) + (f" — {desc}" if desc else ""))
    return "\n".join(lines)


def _render_sections(doc: dict, detailed: list[str], index: list[dict], question: str) -> str:
    """Assemble the detailed tables, metrics, routing, and index into one context string."""
    from semlayer import mcp_server
    parts = [_compact_table(doc, n) for n in detailed]
    metrics_lines = [
        f"METRIC {m['name']}: {m.get('agg')}({m.get('measure')})"
        + (f" WHERE {m['filter']}" if m.get("filter") else "")
        for m in mcp_server.list_metrics(doc)
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


def _normalize_rows(rows: list) -> set:
    """Normalize row-set results for order-independent, float-tolerant comparison."""
    return {
        tuple(str(round(v, 2)) if isinstance(v, float) else str(v) for v in r)
        for r in rows
    }


def _score_scalar(got: list, expected: list, tol: float) -> tuple[bool, str]:
    """Compare a scalar CQ result against expected within tolerance."""
    try:
        expected_val = float(expected[0][0])
        got_val = (
            float(got[0][0]) if got and got[0][0] is not None
            else (0.0 if expected_val == 0 else None)
        )
    except (TypeError, ValueError, IndexError):
        return False, "non-numeric scalar"
    if got_val is None:
        return False, "null result"
    ok = abs(got_val - expected_val) <= max(tol, abs(expected_val) * 0.001)
    return ok, f"got {got_val}, expected {expected_val}"


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
        got = con.execute(result["sql"]).fetchall()
    except Exception as e:
        return False, f"sql error: {str(e)[:80]}"
    expected = con.execute(cq["expected_sql"]["duckdb"]).fetchall()
    if kind == "scalar":
        return _score_scalar(got, expected, cq.get("tolerance", 0.01))
    # rows: compare as normalized sets of stringified rows
    ok = _normalize_rows(got) == _normalize_rows(expected)
    return ok, f"{len(got)} rows vs {len(expected)} expected"


def answer_with_repair(llm, con, context: str, question: str) -> dict:
    """One-shot answer plus a single error-repair round.

    The realistic agent loop: agents see execution errors and fix their SQL.
    """
    res = answer(llm, context, question)
    if res.get("action") != "sql" or not res.get("sql"):
        return res
    try:
        con.execute(res["sql"]).fetchall()
        return res
    except Exception as e:
        repair_prompt = (
            f"{question}\n\nYour previous SQL failed:\n{res['sql']}\n"
            f"ERROR: {str(e)[:300]}\nFix it."
        )
        repair = answer(llm, context, repair_prompt)
        return repair if repair.get("sql") else res
