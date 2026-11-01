# SPDX-License-Identifier: FSL-1.1-ALv2
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
    out = []
    if sl.get("members"):
        # a resolved set: each member document is a domain (the hub too, if it
        # owns tables), ahead of any domains the members declared themselves.
        # One pass over the tables: 41 members x 36,000 tables per call was
        # 58 ms at 15,000 tables and growing with both.
        counts: dict[str, int] = {}
        for t in sl.get("tables", []):
            owner = t["name"].split("/", 1)[0] if "/" in t["name"] else ""
            counts[owner] = counts.get(owner, 0) + 1
        for n in [sl.get("name", ""), *(m["name"] for m in sl["members"])]:
            count = counts.get(n, 0)
            if count or n != sl.get("name", ""):
                out.append({"name": n, "tables": count,
                            "description": "hub" if n == sl.get("name", "") else "member document"})
    if not domains and not out:
        return [{"name": "all", "tables": len(sl["tables"])}]
    return out + [{"name": d["name"], "tables": len(d["tables"]),
                   "description": d.get("description", "")} for d in domains]


def list_tables(doc: dict, domain: str | None = None) -> list[dict]:
    """List table summaries, optionally scoped to one domain; flags UNUSABLE tables."""
    sl = doc["semantic_layer"]
    names = None
    if domain:
        d = next((d for d in sl.get("repo_knowledge", {}).get("domains", [])
                  if d["name"] == domain), None)
        if d is None and sl.get("members"):
            names = {t["name"] for t in sl["tables"] if t["name"].startswith(f"{domain}/")}
        else:
            names = set(d["tables"]) if d else set()
    out = []
    for t in sl["tables"]:
        if names is not None and t["name"] not in names:
            continue
        entry = {"name": t["name"], "type": t.get("table_type", "unknown"),
                 "description": (t.get("description") or "")[:140],
                 "lifecycle": t.get("lifecycle", "inferred")}
        # how sure we are of `type` -- NOT a quality or canonical-ness score.
        # "which table should I use" is answered by lifecycle + route_intent.
        if t.get("confidence") is not None:
            entry["type_confidence"] = t["confidence"]
        if t.get("lifecycle") in ("deprecated", "orphaned"):
            entry["UNUSABLE"] = True
            rep = t.get("deprecation", {}).get("replacement")
            if rep:
                entry["use_instead"] = rep
        out.append(entry)
    return out


def _skipped_or_unknown(sl: dict, name: str) -> dict:
    """A table not in `tables` may still be known to exist (SPEC.md §2.12).

    Reporting a deliberately-skipped duplicate as unknown is the one answer
    the spec forbids: an agent told "no such table" concludes the warehouse
    does not have it, and stops. Coverage names are schema-qualified while
    `tables[].name` is not, so match either spelling.
    """
    want = name.lower()
    for entry in (sl.get("coverage") or {}).get("skipped") or []:
        got = entry["name"].lower()
        if want not in (got, got.rsplit(".", 1)[-1]):
            continue
        out = {"name": entry["name"], "NOT_MODELLED": True,
               "reason": entry["reason"],
               "exists": "this table is in the warehouse but was not modelled"}
        same = entry.get("same_shape_as")
        if same:
            out["same_shape_as"] = same
            out["guidance"] = (
                f"identical column signature to '{same}', which IS modelled -- "
                "read that table for the shape, but do not assume the rows "
                "agree, and profile this table before querying it")
        return out
    return {"error": f"unknown table '{name}'"}


def get_table(doc: dict, name: str) -> dict:
    """Full detail for one table: columns, keys, relationships, required filters."""
    sl = doc["semantic_layer"]
    t = next((x for x in sl["tables"] if x["name"] == name), None)
    if t is None:
        return _skipped_or_unknown(sl, name)
    out = {k: t[k] for k in ("name", "description", "ai_context", "table_type",
                             "grain", "scd", "source", "primary_key", "freshness",
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


_NOISE_TYPES = ("staging", "operational", "log", "control", "metadata", "audit", "etl")


def _stem(w: str) -> str:
    """Cheap English stemmer for plural/singular agreement (customers == customer)."""
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith(("sses", "shes", "ches", "xes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


# search-only expansions on top of the Link stage's shared table (kept separate so
# FK naming scores are untouched)
_SEARCH_ABBREV = {
    "tot": "total", "cnt": "count", "nm": "name", "desc": "description", "flg": "flag",
    "hdr": "header", "ln": "line", "rsn": "reason", "pmt": "payment", "mthd": "method",
    "typ": "type", "snpsht": "snapshot", "inv": "inventory", "promo": "promotion",
    "curr": "currency", "seg": "segment", "actv": "active", "dly": "daily", "yr": "year",
    "num": "number", "ref": "reference", "attr": "attribute", "ext": "external",
    "avg": "average",
}


def _tokens(text: str) -> set[str]:
    """Tokenize, expand warehouse abbreviations (cust -> customer), and stem."""
    from semlayer.link.candidates import ABBREV
    out = set()
    for w in re.split(r"[\W_]+", (text or "").lower()):
        if w:
            out.add(_stem(_SEARCH_ABBREV.get(w, ABBREV.get(w, w))))
    return out


def _type_penalty(t: dict) -> int:
    """Rank canonical fact/dimension tables above staging/operational/log tables on equal hits."""
    pen = 0
    tt = (t.get("table_type") or "")
    if any(k in tt for k in _NOISE_TYPES) or t["name"].startswith(("stg_", "tmp_", "etl_")):
        pen += 2
    if t.get("lifecycle") in ("deprecated", "orphaned"):
        pen += 3
    return pen


def search(doc: dict, query: str, limit: int = 10) -> list[dict]:
    """Keyword-search tables, columns, and metrics.

    Ranking: abbreviation-expanded, stemmed token overlap (name hits weigh
    more than description hits); staging/operational and deprecated tables
    are ranked below canonical tables with the same hits.
    """
    toks = _tokens(query)
    scored = []
    sl = doc["semantic_layer"]
    for t in sl["tables"]:
        name_t = _tokens(t["name"])
        desc_t = _tokens(" ".join([t.get("description") or "", t.get("ai_context") or ""]))
        score = 3 * len(toks & name_t) + len(toks & desc_t)
        if score:
            score -= _type_penalty(t)
            scored.append((score, {"kind": "table", "name": t["name"],
                                   "description": (t.get("description") or "")[:100],
                                   "lifecycle": t.get("lifecycle", "inferred")}))
        for c in t["columns"]:
            cname_t = _tokens(c["name"])
            cdesc_t = _tokens(c.get("description") or "")
            cscore = 2 * len(toks & cname_t) + len(toks & cdesc_t)
            if cscore:
                cscore -= _type_penalty(t)
                scored.append((cscore, {"kind": "column", "name": f"{t['name']}.{c['name']}",
                                        "semantic_type": c.get("semantic_type"),
                                        "description": (c.get("description") or "")[:80]}))
    for m in sl.get("metrics", []):
        hay_m = _tokens(" ".join([m["name"], m.get("display_name") or "",
                                  *(m.get("synonyms") or [])]))
        mscore = 2 * len(toks & hay_m)
        if mscore:
            scored.append((mscore + 1, {"kind": "metric", "name": m["name"],
                                        "measure": m.get("measure"), "filter": m.get("filter")}))
    scored.sort(key=lambda x: -x[0])
    return [s for _, s in scored[:limit]]


def list_metrics(doc: dict) -> list[dict]:
    """List defined metrics: name, measure, aggregation, filter, lifecycle."""
    return [{"name": m["name"], "type": m["type"], "measure": m.get("measure"),
             "agg": m.get("agg"), "filter": m.get("filter"),
             "numerator": m.get("numerator"), "denominator": m.get("denominator"),
             "agg_time_dimension": m.get("agg_time_dimension"),
             "synonyms": m.get("synonyms"),
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

def _note_host(srv) -> None:
    """Note which MCP client launched us, for the `mcp` telemetry event.

    The raw client name is passed to `telemetry.note` and classified by
    `telemetry_schema._host` on the way out, which can only return one of a
    fixed set of labels -- the name itself has no path to an event. That is
    the same division of labour as every other field: call sites deposit,
    the serializer decides.

    Guarded whole and silent. Telemetry must never affect the server a
    client is talking to, and `get_context()` is valid only inside a
    request -- the in-process tests build a server with no request at all.
    """
    try:
        from semlayer import telemetry
        telemetry.note(host=srv.get_context().session.client_params.clientInfo.name)
    except Exception:
        # Bare, deliberately: a telemetry note may never break a tool call.
        pass


def _server_class():
    """FastMCP subclassed to note which client connected.

    A function rather than a module-level class because the `mcp` import
    has to stay optional -- and rather than a class statement inside
    `build_server`, which pushed that function past the complexity limit.
    """
    from mcp.server.fastmcp import FastMCP

    class _Server(FastMCP):
        """FastMCP, plus a note of which client connected.

        Overriding the handlers rather than wrapping them after the fact:
        FastMCP binds `self.list_tools` and `self.call_tool` into the
        low-level server during construction, so an attribute assigned
        afterwards is never called.

        `list_tools` is the hook that matters. It is the first request a
        client makes after `initialize`, so it fires even for a session that
        disconnects without calling a tool -- which is most of the
        population this field exists to identify, since a launch that
        ends in under a second never reaches one. `call_tool` is covered
        too, for a client that
        cached the tool list and calls straight in. Signatures match the
        base exactly, because the SDK inspects them to decide whether a
        handler takes the request object.
        """

        async def list_tools(self):
            _note_host(self)
            return await super().list_tools()

        async def call_tool(self, name, arguments):
            _note_host(self)
            return await super().call_tool(name, arguments)

    return _Server


def build_server(doc: dict):
    """Wire the pure query functions above as MCP tools on a FastMCP server."""
    srv = _server_class()("semlayer",
                  instructions="Semantic layer for this warehouse. Start with "
                               "semantic_search or list_domains; use get_table before writing SQL. "
                               "ALWAYS apply required_filters; never use UNUSABLE objects; run "
                               "check_sql on every query before executing it; caveat "
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

    @srv.tool()
    def check_sql(sql: str, dialect: str = "duckdb") -> str:
        """Lint SQL you wrote against the semantic layer BEFORE running it.

        Deterministic checks: unknown tables/columns, correlated references,
        deprecated tables, missing required filters, fan-out aggregates,
        SCD2 joins without a validity window. Fix every error and warning
        (each finding carries a fix hint), then re-check.
        """
        from semlayer.lint import lint_sql
        return json.dumps(lint_sql(doc, sql, dialect=dialect), default=str)

    @srv.tool()
    def compile_metric(name: str, group_by: str = "", time_grain: str = "",  # noqa: PLR0913, PLR0917 — tool schema is intentionally flat
                       time_start: str = "", time_end: str = "",
                       filter: str = "", dialect: str = "duckdb",
                       calendar: str = "", scd: str = "") -> str:
        """Compile a metric to correct SQL (joins, filters, time bucketing).

        group_by: comma-separated columns. On refusal the response lists
        legal alternatives — retry with those or ask the user; do NOT
        hand-assemble SQL for a refused request.

        scd: "current" or "asof", required when the join path crosses a
        history table. These are different questions — a customer's segment
        now, or their segment when they ordered — so ask the user which they
        meant rather than guessing.
        """
        from semlayer.compile import compile_metric as _compile
        result = _compile(doc, name,
                          group_by=[g.strip() for g in group_by.split(",") if g.strip()],
                          time_grain=time_grain or None,
                          time_start=time_start or None, time_end=time_end or None,
                          extra_filter=filter or None, dialect=dialect,
                          calendar=calendar or None, scd=scd or None)
        return json.dumps(result, default=str)

    return srv
