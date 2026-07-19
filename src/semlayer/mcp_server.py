"""Local MCP server: the agent-facing consumption surface.

Progressive disclosure (a 1,000-table model never dumps whole): domains ->
table summaries -> full table detail. The consumer contract is enforced at
the surface: deprecated/orphaned objects are flagged unusable, confidence +
lifecycle ride along on every answer, required filters are always included
in table detail.
"""

from __future__ import annotations

import json
import re

# ---------- pure query functions (unit-tested; MCP tools are thin wrappers) --

def list_domains(doc: dict) -> list[dict]:
    """List warehouse domains (table groupings), or a single 'all' domain if none defined."""
    sl = doc["semantic_layer"]
    domains = sl.get("repo_knowledge", {}).get("domains", [])
    if not domains:
        return [{"name": "all", "tables": len(sl["tables"])}]
    return [{"name": d["name"], "tables": len(d["tables"]),
             "description": d.get("description", "")} for d in domains]


def list_tables(doc: dict, domain: str | None = None) -> list[dict]:
    """List table summaries, optionally scoped to one domain; flags UNUSABLE tables."""
    sl = doc["semantic_layer"]
    names = None
    if domain:
        d = next((d for d in sl.get("repo_knowledge", {}).get("domains", [])
                  if d["name"] == domain), None)
        names = set(d["tables"]) if d else set()
    out = []
    for t in sl["tables"]:
        if names is not None and t["name"] not in names:
            continue
        entry = {"name": t["name"], "type": t.get("table_type", "unknown"),
                 "description": (t.get("description") or "")[:140],
                 "lifecycle": t.get("lifecycle", "inferred")}
        if t.get("lifecycle") in ("deprecated", "orphaned"):
            entry["UNUSABLE"] = True
            rep = t.get("deprecation", {}).get("replacement")
            if rep:
                entry["use_instead"] = rep
        out.append(entry)
    return out


def get_table(doc: dict, name: str) -> dict:
    """Full detail for one table: columns, keys, relationships, required filters."""
    sl = doc["semantic_layer"]
    t = next((x for x in sl["tables"] if x["name"] == name), None)
    if t is None:
        return {"error": f"unknown table '{name}'"}
    out = {k: t[k] for k in ("name", "description", "ai_context", "table_type",
                             "grain", "source", "primary_key", "freshness",
                             "lifecycle", "confidence") if k in t}
    if t.get("lifecycle") in ("deprecated", "orphaned"):
        out["UNUSABLE"] = True
        out["deprecation"] = t.get("deprecation", {})
    out["columns"] = [
        {k: c[k] for k in ("name", "sql_type", "semantic_type", "entity_role",
                           "description", "foreign_key", "enum_values", "pii",
                           "confidence", "lifecycle") if k in c}
        for c in t["columns"]
    ]
    rf = t.get("knowledge", {}).get("required_filters", [])
    if rf:
        out["required_filters"] = rf  # contract: consumers MUST apply/caveat
    notes = t.get("knowledge", {}).get("usage_notes", [])
    if notes:
        out["usage_notes"] = notes
    rels = [r for r in sl.get("relationships", [])
            if r["from"]["table"] == name or r["to"]["table"] == name]
    if rels:
        out["relationships"] = rels
    return out


def search(doc: dict, query: str, limit: int = 10) -> list[dict]:
    """Keyword-search tables, columns, and metrics; ranked by keyword hits."""
    toks = [w for w in re.split(r"\W+", query.lower()) if w]
    scored = []
    sl = doc["semantic_layer"]
    for t in sl["tables"]:
        hay_t = " ".join([t["name"], t.get("description", ""), t.get("ai_context", "")]).lower()
        score = sum(2 for w in toks if w in t["name"].lower()) + sum(1 for w in toks if w in hay_t)
        if score:
            scored.append((score, {"kind": "table", "name": t["name"],
                                   "description": (t.get("description") or "")[:100],
                                   "lifecycle": t.get("lifecycle", "inferred")}))
        for c in t["columns"]:
            hay_c = " ".join([c["name"], c.get("description", "") or ""]).lower()
            cscore = (sum(2 for w in toks if w in c["name"].lower())
                      + sum(1 for w in toks if w in hay_c))
            if cscore:
                scored.append((cscore, {"kind": "column", "name": f"{t['name']}.{c['name']}",
                                        "semantic_type": c.get("semantic_type"),
                                        "description": (c.get("description") or "")[:80]}))
    for m in sl.get("metrics", []):
        hay_m = f"{m['name']} {m.get('display_name', '')}".lower()
        mscore = sum(2 for w in toks if w in hay_m)
        if mscore:
            scored.append((mscore + 1, {"kind": "metric", "name": m["name"],
                                        "measure": m.get("measure"), "filter": m.get("filter")}))
    scored.sort(key=lambda x: -x[0])
    return [s for _, s in scored[:limit]]


def list_metrics(doc: dict) -> list[dict]:
    """List defined metrics: name, measure, aggregation, filter, lifecycle."""
    return [{"name": m["name"], "type": m["type"], "measure": m.get("measure"),
             "agg": m.get("agg"), "filter": m.get("filter"),
             "lifecycle": m.get("lifecycle", "inferred"), "confidence": m.get("confidence")}
            for m in doc["semantic_layer"].get("metrics", [])]


def routing(doc: dict, intent: str | None = None) -> list[dict]:
    """Which tables to use (and avoid) for an analytical intent; ranked by token overlap."""
    routes = doc["semantic_layer"].get("repo_knowledge", {}).get("routing", [])
    if intent:
        toks = set(re.split(r"\W+", intent.lower()))

        def _overlap(r: dict) -> int:
            return -len(toks & set(re.split(r"\W+", r["intent"].lower())))

        routes = sorted(routes, key=_overlap)
    return routes[:5]


# ------------------------------------------------------------- MCP wiring --

def build_server(doc: dict):
    """Wire the pure query functions above as MCP tools on a FastMCP server."""
    from mcp.server.fastmcp import FastMCP
    srv = FastMCP("semlayer",
                  instructions="Semantic layer for this warehouse. Start with "
                               "semantic_search or list_domains; use get_table before writing SQL. "
                               "ALWAYS apply required_filters; never use UNUSABLE objects; caveat "
                               "answers built on lifecycle=inferred elements.")

    @srv.tool()
    def semantic_search(query: str) -> str:
        """Search tables, columns, and metrics by keywords. The starting point."""
        return json.dumps(search(doc, query), default=str)

    @srv.tool()
    def get_domains() -> str:
        """List warehouse domains (table groupings) for navigation."""
        return json.dumps(list_domains(doc), default=str)

    @srv.tool()
    def get_tables(domain: str = "") -> str:
        """List table summaries, optionally within a domain."""
        return json.dumps(list_tables(doc, domain or None), default=str)

    @srv.tool()
    def table_detail(name: str) -> str:
        """Full detail for one table: columns, keys, relationships, REQUIRED FILTERS."""
        return json.dumps(get_table(doc, name), default=str)

    @srv.tool()
    def get_metrics() -> str:
        """List defined metrics (name, measure, aggregation, filter)."""
        return json.dumps(list_metrics(doc), default=str)

    @srv.tool()
    def route_intent(intent: str) -> str:
        """Which tables to use (and avoid) for an analytical intent."""
        return json.dumps(routing(doc, intent), default=str)

    return srv
