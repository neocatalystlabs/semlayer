"""Describe stage: iterative context propagation (prompt version 1).

Pass 1: each table described from its own evidence (one Haiku call/table).
Pass 2: refined with NEIGHBOR context — the descriptions of tables it joins
to — which is what disambiguates `sts_cd` once you know the table is an
order header linked to customers (DBAutoDoc's core insight). Converges in
2 passes; input-hash cassettes make unchanged tables free on re-runs.

The same call carries the table-type LLM assist: when the heuristic
classifier was low-confidence, the LLM's reading (with the same evidence)
takes over, with a conflict record.
"""

from __future__ import annotations

import json
import re

PROMPT_VERSION = "v1"
TABLE_TYPES = ["fact", "dimension", "denormalized", "aggregate",
               "snapshot_scd2", "operational", "staging", "unknown"]

SYSTEM = f"""You document data warehouse tables for both humans and AI agents.
Prompt version {PROMPT_VERSION}. Given a table's evidence (columns, types,
statistics, sample values, relationships, neighbor context), respond ONLY JSON:
{{"table_description": "<=35 words, factual, no speculation beyond evidence",
 "ai_context": "<=25 words of guidance an AI querying this table needs (join keys, filters, caveats)",
 "table_type": one of {TABLE_TYPES},
 "columns": [{{"name": "...", "description": "<=20 words"}}]}}
Expand cryptic abbreviations when the evidence supports it (sts=status,
amt=amount, cd=code). Describe every column you are given. State enum
meanings only when decodes are provided or values make them obvious."""


def _evidence(t: dict, stats, neighbors: dict[str, str], no_samples: bool) -> str:
    ts = stats[t["name"]]
    cols = []
    for c in t["columns"]:
        cs = ts.columns[c["name"]]
        d = {"name": c["name"], "sql_type": c["sql_type"],
             "semantic_type": c.get("semantic_type"),
             "role": c.get("entity_role"),
             "n_distinct": cs.n_distinct, "null_rate": round(cs.null_rate, 2)}
        if c.get("foreign_key"):
            d["references"] = c["foreign_key"]["references"]
        if c.get("enum_values"):
            d["enum_decodes"] = {e["value"]: e["meaning"] for e in c["enum_values"]}
        if not no_samples and cs.top_values:
            d["sample_values"] = [v for v, _ in cs.top_values[:6]]
        cols.append(d)
    ev = {
        "table": t["name"],
        "row_count": ts.row_count,
        "heuristic_table_type": {"type": t.get("table_type", "unknown"),
                                 "note": "may be wrong; judge from evidence"},
        "columns": cols,
    }
    if neighbors:
        ev["joined_neighbor_tables"] = neighbors
    return json.dumps(ev, indent=1)


def describe_source(doc: dict, stats: dict, llm, no_samples: bool = False,
                    passes: int = 2) -> dict:
    """Describe every table via iterative context propagation (see module docstring)."""
    tables = doc["semantic_layer"]["tables"]
    rels = doc["semantic_layer"].get("relationships", [])
    neigh_map: dict[str, set[str]] = {}
    for r in rels:
        a, b = r["from"]["table"], r["to"]["table"]
        neigh_map.setdefault(a, set()).add(b)
        neigh_map.setdefault(b, set()).add(a)

    descriptions: dict[str, str] = {}
    for pass_n in range(passes):
        for t in tables:
            neighbors = {}
            if pass_n > 0:
                neighbors = {n: descriptions.get(n, "")
                             for n in sorted(neigh_map.get(t["name"], set()))
                             if descriptions.get(n)}
                if not neighbors and t.get("description"):
                    continue  # isolated table: pass 1 result is final
            raw = llm.complete(SYSTEM, _evidence(t, stats, neighbors, no_samples))
            parsed = _parse(raw)
            if not parsed:
                continue
            t["description"] = parsed.get("table_description", t.get("description", ""))
            if parsed.get("ai_context"):
                t["ai_context"] = parsed["ai_context"]
            descriptions[t["name"]] = t["description"]
            _apply_table_type(t, parsed.get("table_type"))
            by_name = {c["name"]: c for c in t["columns"]}
            for cd in parsed.get("columns", []):
                c = by_name.get(cd.get("name"))
                if c is not None and cd.get("description"):
                    c["description"] = cd["description"][:200]
            prov = {"signal": "llm", "detail": f"describe pass {pass_n + 1}"}
            t.setdefault("provenance", []).append(prov)
    return doc


def _apply_table_type(t: dict, llm_type: str | None) -> None:
    """LLM fills ONLY 'unknown' table types.

    Measured 2026-07-18: letting it override low-confidence heuristics
    REDUCED accuracy (it over-assigns 'operational' to minimal tables).
    Disagreements with a decided heuristic are recorded as conflicts for
    the review queue, never applied.
    """
    if llm_type not in TABLE_TYPES:
        return
    heur = t.get("table_type", "unknown")
    if heur == "unknown":
        t["table_type"] = llm_type
    elif llm_type != heur:
        t.setdefault("conflicts", []).append({
            "between": ["statistic", "llm"],
            "detail": f"table_type: heuristic said {heur}, llm said {llm_type}",
        })


def _parse(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
