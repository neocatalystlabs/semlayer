# SPDX-License-Identifier: FSL-1.1-ALv2
"""Local MCP server: the agent-facing consumption surface.

Progressive disclosure (a 1,000-table model never dumps whole): domains ->
table summaries -> full table detail. The consumer contract is enforced at
the surface: deprecated/orphaned objects are flagged unusable, confidence +
lifecycle ride along on every answer, required filters are always included
in table detail.

The server holds a `Session`, not a document: it starts with or without a
layer, and `status` is the tool that says which. Every other tool answers
with `status()` when there is no layer to query, so an agent that arrives
before an inference has happened is told what to do rather than handed an
error. The pure query functions below are unchanged by that -- they take a
document, and a session is only what chooses one.
"""

from __future__ import annotations

import json
import re

from mcp.server.fastmcp import Context

from semlayer.session import Session

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
    """Which tables to use (and avoid) for an analytical intent; ranked by token overlap.

    `repo_knowledge.routing[].use` is the producer's pointer list: the fact,
    then every aggregate that reconciled against it. SPEC 2.7 is the
    consumer's rule for that second part, and this function applies it,
    because an agent handed `use` has no reason to look further. An
    aggregate whose routing `status` is `verified` stays in `use`. An
    `advisory` one -- which is every `mapping_source: heuristic` aggregate
    the engine emits -- moves to `aggregates`, with its grain and the caveat
    2.7 asks for, so it is offered and never instructed. A deprecated or
    orphaned aggregate appears in neither, since 2.1 rules it out anyway.
    The document is not modified; the split is made on copies.
    """
    sl = doc["semantic_layer"]
    routes = sl.get("repo_knowledge", {}).get("routing", [])
    if intent:
        toks = set(re.split(r"\W+", intent.lower()))

        def _overlap(r: dict) -> int:
            return -len(toks & set(re.split(r"\W+", r["intent"].lower())))

        routes = sorted(routes, key=_overlap)
    aggs = {a["table"]: a for a in sl.get("aggregate_tables", []) or []}
    return [_split_use(r, aggs) for r in routes[:5]]


def _split_use(route: dict, aggs: dict[str, dict]) -> dict:
    """One routing entry with its `use` list split per SPEC 2.7 (see `routing`)."""
    out = dict(route)
    use: list[str] = []
    offered: list[dict] = []
    for name in route.get("use") or []:
        a = aggs.get(name)
        if a is None:
            use.append(name)
            continue
        if a.get("lifecycle") in ("deprecated", "orphaned"):
            continue
        status = (a.get("routing") or {}).get("status") or "advisory"
        if status == "verified":
            use.append(name)
            continue
        grain = [str(g) for g in (a.get("grain") or [])]
        source = a.get("mapping_source") or "heuristic"
        offered.append({
            "table": name, "aggregates": a.get("aggregates"), "grain": grain,
            "status": status, "mapping_source": source,
            "consistency": (a.get("consistency") or {}).get("status"),
            "caveat": (f"{status} routing over a {source} mapping: not for automatic "
                       f"use (SPEC 2.7). Only for a question at or above grain "
                       f"[{', '.join(grain)}], and say in the answer that a summary "
                       f"table was used."),
        })
    out["use"] = use
    if offered:
        out["aggregates"] = offered
    return out


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


# Which tools this process has served. Process-scoped and never reset, the
# same shape as `telemetry._pending` and for the same reason: one server
# process serves one session, and the `mcp` event is written once at exit.
#
# Bounded, because the set is keyed on whatever a client asks for and a
# client is not ours to trust. Past the cap we stop collecting rather than
# classify here -- `telemetry_schema._tools` decides what an unknown name
# becomes, and keeping that decision in one place is worth more than
# recording the 33rd distinct tool name of a session that is clearly not a
# person using eight tools.
_TOOLS_SEEN: set[str] = set()
_TOOLS_CAP = 32


def _note_tool(name: object) -> None:
    """Note that a tool was called, for the `mcp` telemetry event.

    The name only -- never the arguments, which carry the user's query text
    and table names. Deposited as the whole set on every call because
    `telemetry.note` replaces a field rather than appending to it.

    This is the half of the MCP picture `host` cannot give: `host` says who
    connected, and a launch that connects and calls nothing is a probe
    whatever client it claims to be.
    """
    try:
        if isinstance(name, str) and name and len(_TOOLS_SEEN) < _TOOLS_CAP:
            _TOOLS_SEEN.add(name)
        if _TOOLS_SEEN:
            from semlayer import telemetry
            telemetry.note(tools=sorted(_TOOLS_SEEN))
    except Exception:
        # Bare, deliberately: a telemetry note may never break a tool call.
        pass


def _note_requested(name: object) -> None:
    """Note which warehouse a user asked for that was not in the list.

    CLASSIFIED, never echoed: `telemetry_schema._requested` maps the words to
    a fixed vocabulary and cannot return its input, the same rule `host`
    follows. The point is to choose the next connector from demand instead of
    guesswork -- "eleven people asked for Databricks" is a roadmap, and it is
    not obtainable any other way, because a person whose warehouse we cannot
    open leaves without a trace today.
    """
    try:
        from semlayer import telemetry
        telemetry.note(requested_connector=name)
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
        cached the tool list and calls straight in -- and it is the only
        hook that can carry `tools`, since listing the tools says nothing
        about using them. Signatures match the base exactly, because the
        SDK inspects them to decide whether a handler takes the request
        object.
        """

        async def list_tools(self):
            _note_host(self)
            return await super().list_tools()

        async def call_tool(self, name, arguments):
            _note_host(self)
            _note_tool(name)
            return await super().call_tool(name, arguments)

        async def get_prompt(self, name, arguments=None):
            # A prompt is a surface too, and the one we most need to know
            # about: an optional walkthrough nobody invokes is a walkthrough
            # to delete. Noted by NAME only, like a tool.
            _note_host(self)
            _note_tool(name)
            return await super().get_prompt(name, arguments)

    return _Server


# Handed to the model at the handshake, which is the only moment we are
# certain of its attention. Everything here is a fact the agent cannot
# discover by trying: that a layer has to be built before anything works,
# that it must never ask for a secret, that Redshift is postgres, that an
# unsupported warehouse has an honest answer waiting in a tool, and the
# order to answer a question in.
#
# This is now the wording Raj reviewed on 2026-11-01, with no deviation:
# the two clauses that had to be changed for slice 1 -- "how to start
# inference", and `context_for_question` as the opener -- are both true as
# of this slice, because `infer` and `context_for_question` exist.
_INSTRUCTIONS = (
    "semlayer models a data warehouse into a semantic layer and serves it to "
    "you. Call `status` first. A semantic layer is not something this machine "
    "has by default: it has to be BUILT from a warehouse, and until it is "
    "there is nothing to search or query. If no layer is loaded, `status` "
    "tells you which warehouse connections were found on this machine, which "
    "layers were found already built, and how to start inference -- every "
    "other tool answers with that same object when no layer is loaded, so "
    "read `next` and act on it rather than retrying. "
    "Never ask the user for a password or a key in chat. Credentials live in "
    "their environment, their `.env` file, their dbt profile, or their "
    "warehouse's connections file; you only ever pass names or URIs. "
    "Redshift, Supabase, Neon, and anything speaking the Postgres protocol "
    "use the postgres connection. If the user names a warehouse that is not "
    "listed as supported, say so plainly, pass the name to "
    "`discover_connections` as `requested`, and offer the nearest route it "
    "returns. "
    "`infer` builds the layer: it reads their warehouse, takes minutes on a "
    "real one, and spends their own LLM budget, so ASK THEM FIRST and say "
    "which warehouse you are about to model. Pass their question to it; when "
    "the layer is ready, `status` reminds you to answer it. "
    "Once a layer is loaded, start every question with "
    "`context_for_question`, apply required filters, never use UNUSABLE "
    "objects, run `check_sql` before any SQL runs, and run it with `run_sql`, "
    "which executes one read-only SELECT against their warehouse. Answer in "
    "words first, then state the assumptions the answer rests on, and caveat "
    "answers built on lifecycle=inferred elements."
)


def _answer(session: Session, query) -> str:
    """Run a query function against the loaded layer, or say why you cannot.

    The fallback is the contract: a tool call never dead-ends. Before this,
    every tool indexed into `doc["semantic_layer"]`, so a server started
    without a layer raised on the first call -- and an agent's only options
    then are to retry or to give up. Most of the population this server
    actually has is in exactly that state.
    """
    if session.doc is None:
        return json.dumps(session.status(), default=str)
    return json.dumps(query(session.doc), default=str)


def _register_session_tools(srv, session: Session) -> None:
    """The two tools that answer with no layer loaded.

    Registered apart from the rest because every tool is a nested function
    and `build_server` is at the complexity limit -- and because these two
    are about the SESSION, where the eight below are about a document.
    """

    @srv.tool()
    def status() -> str:
        """What this server has, what it can see, and what to do next.

        Call this first. It answers with the loaded layer (its size, age, and
        how many claims nobody has reviewed yet), the warehouse connections
        visible from this machine, and a `next` field saying what to do --
        including when there is no layer at all, which is the state a fresh
        install is in.
        """
        return json.dumps(session.status(), default=str)

    @srv.tool()
    def discover_connections(requested: str = "") -> str:
        """Which warehouses are reachable from this machine, by name.

        Reads DuckDB files in the working directory, warehouse environment
        variables, `$SEMLAYER_HOME/.env`, dbt's profiles.yml and Snowflake's
        connections.toml. Names only: no credential is read, no value that
        could be one is reported, and no connection is opened. Each entry says
        whether semlayer can open it (`supported`) and what it would need if
        not (`note`).

        requested: the warehouse the user NAMED, in their words, when it is
        not in the list -- "databricks", "our iceberg tables in s3", "redshift".
        The answer says whether semlayer opens it after all (Redshift is the
        postgres connection), and if not, the nearest route and what to tell
        them. Pass it rather than guessing: a wrong guess here is either a
        promise we cannot keep or a user sent away who did not need to be.
        """
        if requested.strip():
            _note_requested(requested)
            return json.dumps(session.route(requested), default=str)
        return json.dumps(session.discover(), default=str)

    @srv.tool()
    def load_layer(ref: str) -> str:
        """Load one of the layers `status` listed under `layers`.

        ref: the `ref` from that list, the connection it is for, or a path to
        a layer file. Returns the new status. Use it when several layers are
        on this machine and the user has said which warehouse they mean --
        and after it, tell them which one you loaded.
        """
        return json.dumps(session.load_layer(ref), default=str)


# How often to look at the job while an `infer` call is open. Twice a second
# is below what a person notices and far above what the notifications cost.
_WATCH_TICK_S = 0.5


async def _watch(job, wait_seconds: float, ctx) -> None:
    """Wait for the job without blocking the server, reporting as it goes.

    This is why `infer` is async. FastMCP calls a SYNC tool function
    directly on the event loop, so the `thread.join` this used to do made
    the whole server unresponsive for the length of the wait -- measured
    2026-11-02: a `status` call sent one second into a run was not answered
    for 5.9 more. With a 60-second cap that is a minute of dead server, and
    no progress notification could be sent either, since sending one needs
    the loop.

    `report_progress` is best-effort. A client that did not send a progress
    token gets nothing and must not see a failed tool call because of it.
    """
    import anyio

    from semlayer.session import INFER_WAIT_CAP_S

    deadline = min(max(0.0, float(wait_seconds)), INFER_WAIT_CAP_S)
    waited, last = 0.0, None
    while job.running and waited < deadline:
        await anyio.sleep(_WATCH_TICK_S)
        waited += _WATCH_TICK_S
        if ctx is None:
            continue
        here = (job.fraction, job.label or job.stage)
        if here == last:
            continue            # nothing moved; do not spend a notification
        last = here
        try:
            await ctx.report_progress(progress=job.fraction, total=1.0,
                                      message=_progress_message(job))
        except Exception:
            # No progress token, or a client that closed the stream. The run
            # is unaffected and the result still comes back.
            pass


def _progress_message(job) -> str:
    """One line for a progress bar: what it is doing, and how far in."""
    from semlayer.session import STAGE_WORDS

    doing = job.label or STAGE_WORDS.get(job.stage or "") or "starting"
    eta = job.eta_s
    return f"{doing} ({round(job.fraction * 100)}%" + (
        f", roughly {eta}s left)" if eta else ")")


def _register_answer_tools(srv, session: Session) -> None:
    """Build a layer, and answer a question with it.

    These four are what make the server a front door rather than a reference
    book: it can create the thing it serves, and it can run the SQL it helped
    write. Registered apart from the document tools because they are the ones
    that touch the warehouse.
    """

    @srv.tool()
    async def infer(connection: str = "", question: str = "", scope: str = "",
                    wait_seconds: int = 25, use_llm: bool = True,
                    ctx: Context | None = None) -> str:
        """Build the semantic layer for a warehouse. Ask the user first.

        This READS THEIR WAREHOUSE -- metadata and sample values -- under the
        connection given, and on a real warehouse it takes minutes and uses
        their own LLM key, which costs them money. Ask before calling it, and
        say which warehouse you are about to model.

        connection: a uri from `status.connections`, or one the user gave.
        Omit it to use the session's.
        question: what the user actually asked. Keep it here: when the layer
        is ready, `status` reminds you to answer it, so a run that outlives
        this turn does not lose the point of itself.
        scope: schema names, comma-separated, to model only those. The way to
        make a big warehouse finish quickly.
        wait_seconds: how long to hold this call open (capped at 60). A DuckDB
        file finishes inside it and comes back `ready`, so you can answer in
        the same turn. Anything larger comes back `inferring`.
        use_llm: false for a statistics-only layer -- no descriptions, no
        decode validation, no cost, and much faster.

        While this call is open it streams progress notifications, so a
        client that renders them shows a bar. Nothing will wake you AFTER it
        returns. Tell the user that, and call `status` when they next speak.
        """
        out = session.start_infer(
            connection or None, question=question or None, scope=scope or None,
            wait_seconds=0, use_llm=use_llm)
        job = session.job
        if job is None or out.get("error") or not job.running:
            return json.dumps(out, default=str)
        await _watch(job, wait_seconds, ctx)
        return json.dumps(session.after_infer(job, session.target() or ""),
                          default=str)

    @srv.tool()
    def cancel_infer() -> str:
        """Stop the inference that is running. Takes effect at the next stage."""
        return json.dumps(session.cancel_infer(), default=str)

    @srv.tool()
    def context_for_question(question: str) -> str:
        """START EVERY QUESTION HERE once a layer is loaded.

        Returns the part of the layer that bears on this question -- the
        tables it selected, their columns, keys, required filters and
        metrics, the routing knowledge for choosing between them -- plus
        `open_questions`: the claims underneath that nobody has confirmed.

        Write your SQL from this, `check_sql` it, then `run_sql`. Put the
        open questions under your answer as assumptions.
        """
        return _answer(session, lambda doc: agent_context(doc, question, session))

    @srv.tool()
    def run_sql(sql: str) -> str:
        """Run ONE read-only SELECT against the session's warehouse.

        `check_sql` runs first and an error means the SQL is NOT executed --
        a query that sums cancelled orders runs perfectly well, so "it ran"
        was never the standard. Warnings do not block, and come back with the
        rows because they are about whether the number means what the user
        thinks.

        Rows are capped and there is a ten-minute time limit; the cap, the
        limit and whether rows were truncated are all stated in the result,
        so you can tell the user what actually applied. Nothing is written,
        nothing is persisted, and your SQL is never rewritten: a required
        filter is reported, never silently applied.

        Refused while an inference is running -- one connection is not shared
        between the two. Ask again when `status` says `ready`.

        The result carries `assumptions` -- the unconfirmed claims the answer
        rests on. Answer the user in words first, then render those under it.
        """
        return _answer(session, lambda doc: agent_run(doc, sql, session))


def _register_review_tools(srv, session: Session) -> None:
    """Recording a verdict from the conversation, and keeping the result.

    Apart from the answer tools because these WRITE: to the document in
    memory, to the file it came from, and -- for save_layer and
    export_layer -- to wherever the user asked.
    """

    @srv.tool()
    def review_questions(tables: str = "", limit: int = 20) -> str:
        """What is uncertain in this layer, as questions you can answer.

        Pull this when the user asks what is unreviewed or unreliable. Do
        NOT walk the user through it after inference: the questions that
        matter are the ones an answer rested on, and `run_sql` hands you
        those as `assumptions`.

        tables: comma-separated, to scope it to what you are looking at.
        """
        from semlayer import agent_review
        names = [t.strip() for t in tables.split(",") if t.strip()]
        return _answer(session, lambda doc: agent_review.questions(
            doc, names or None, limit))

    @srv.tool()
    def review_question(key: str) -> str:
        """One question in full: every object it covers, and the evidence."""
        from semlayer import agent_review
        return _answer(session, lambda doc: agent_review.question(doc, key))

    @srv.tool()
    def review_answer(key: str, verdict: str, decided_by: str,
                      by: str = "", exclude: str = "", note: str = "") -> str:
        """Record a verdict. Takes effect immediately, and says who decided.

        verdict: `accept`, `reject`, or `set:<value>` -- `review_question`
        gives the shape `set` takes.

        decided_by: `agent` if this is YOUR call, `person` if the user said
        it. Never guess this. You may accept a claim whose accept changes no
        behaviour; rejecting, setting, and accepting anything that unlocks
        behaviour are the person's, and you will be told which this is.

        by: the name to record. For a person, what they told you -- ask once
        if you do not know, never invent it, and never use your own client's
        name. For yourself, something recognisable like `claude-code/opus`.

        exclude: comma-separated object locations the verdict should skip.
        note: your reasoning, kept on the question whether or not the verdict
        was recorded. This is how you PROPOSE one you may not decide.

        If the layer is a set (a hub with members) this is refused and tells
        the user which terminal command records it, so do not promise them it
        is kept until you have the result.
        """
        skip = [x.strip() for x in exclude.split(",") if x.strip()]
        if session.doc is None:
            return json.dumps(session.status(), default=str)
        return json.dumps(session.review_answer(
            key, verdict, decided_by, by=by or None, exclude=skip or None,
            note=note or None), default=str)

    @srv.tool()
    def review_decisions() -> str:
        """Every verdict in this layer and who stands behind each one.

        Read `signal`: `agent` means nobody has confirmed it. Say so when an
        answer rests on one.
        """
        from semlayer import agent_review
        return _answer(session, agent_review.decisions)

    @srv.tool()
    def save_layer(path: str) -> str:
        """Write the layer into the user's project so their team has it.

        The cache is per-machine; a file in the repository is what a
        colleague reads. From then on this session's verdicts land there.
        Ask where they want it rather than choosing for them.

        Refused on a set: a hub and its members are several files the user
        already chose the paths for, and one file cannot hold them.
        """
        if session.doc is None:
            return json.dumps(session.status(), default=str)
        return json.dumps(session.save_layer(path), default=str)

    @srv.tool()
    def export_layer(target: str, path: str) -> str:
        """Write a snapshot of the layer in another tool's format.

        target: `dbt` today. On request only, and lossy -- the result says
        what the format could not carry. Nothing keeps it in step with the
        layer afterwards and you must keep answering from the layer, never
        from the export.
        """
        if session.doc is None:
            return json.dumps(session.status(), default=str)
        return json.dumps(session.export_layer(target, path), default=str)


def agent_context(doc: dict, question: str, session: Session) -> dict:
    """`context_for_question`, and the question stops being pending.

    Clearing it here is the point of having kept it: the reminder exists so
    that an agent returning to a finished run knows what it was for, and an
    agent that has just asked for the context is already answering it.
    """
    from semlayer import agent_query

    if question.strip():
        session.pending_question = None
    return agent_query.context_for_question(doc, question)


def agent_run(doc: dict, sql: str, session: Session) -> dict:
    """`run_sql`, under the session's own connection and dialect.

    The session owns the connection and decides whether it is free, so the
    refusal while an inference is running lives there rather than here.
    """
    try:
        return session.run_query(doc, sql)
    except Exception as e:
        # The connection could not be opened at all, so `run_query` never
        # reached a result to classify. Counted here because this is the
        # only outcome that happens outside it.
        session.count_query("warehouse_error")
        return {"ran": False, "sql": sql,
                "error": f"{type(e).__name__}: {str(e)[:300]}",
                "next": ("No warehouse connection is open for this session. "
                         "Call status: if a layer was loaded from a file, the "
                         "connection it names may not be reachable from here, "
                         "and the user has to say how to reach it.")}


# The user-invoked walkthrough. A PROMPT rather than a questionnaire, and
# optional: the lazy flow IS the setup, so a person who asks a data question
# never sees this. Someone who types "set up semlayer" gets walked through
# it, and the text is written for the AGENT, reusing the same tools and the
# same `next` sentences underneath -- one flow, not two.
_SETUP = """\
The user asked to set semlayer up. Walk them through it with the tools, one
step at a time, and do not ask anything you can discover or anything you do
not need yet.

1. Call `status`. It says what is on this machine already.
2. If a layer was found, say which warehouse it is for and its age, and ask
   whether that is the one they mean. If several were found, ask which and
   call `load_layer`. If none, name the warehouse connections `status` found
   and ask them to confirm one -- or, if it found none, ask where their data
   lives in the words `next` uses.
3. NEVER ask for a password, a key or a connection string with a secret in
   it. If a credential is missing, `status` names the exact line and the
   file: give them that line, say you will wait, and call `status` again
   when they say it is done. It is re-read every time; nothing needs
   restarting.
4. Say what inference will do before you start it: it reads their warehouse,
   it takes minutes on a real one, and it spends their own model budget if a
   key is set. Then call `infer` with the connection and whatever question
   they came with.
5. While it runs, tell them roughly where it is from `status.job`. Nothing
   will wake you when it finishes, so check when they next speak.
6. Ask their name ONCE, and only because verdicts are recorded with it --
   `status` may already know it from git. Never invent it and never use your
   own client's name.
7. Answer their question: `context_for_question`, then `check_sql`, then
   `run_sql`. Answer in words first and put the assumptions underneath.
8. After that first answer, if the layer was built without a model, say in
   one sentence what a key would add -- once, and not as a requirement.

Do not ask which model they want, do not ask for a key up front, and do not
walk them through the review queue: the claims that matter are the ones
their answer rested on.
"""


def build_server(layer: Session | dict):
    """Wire the pure query functions above as MCP tools on a FastMCP server.

    Takes a session, or a bare document for the in-process callers that have
    one and no connection -- `Session.of` normalises both.
    """
    session = Session.of(layer)
    srv = _server_class()("semlayer", instructions=_INSTRUCTIONS)
    _register_session_tools(srv, session)
    _register_answer_tools(srv, session)
    _register_review_tools(srv, session)

    @srv.prompt(name="setup", description="Walk me through setting up semlayer")
    def setup() -> str:
        """The optional walkthrough, for someone who asks to be set up."""
        return _SETUP

    @srv.tool()
    def semantic_search(query: str) -> str:
        """Search tables, columns, and metrics by keywords. The starting point."""
        return _answer(session, lambda doc: search(doc, query))

    @srv.tool()
    def get_domains() -> str:
        """List warehouse domains (table groupings) for navigation."""
        return _answer(session, list_domains)

    @srv.tool()
    def get_tables(domain: str = "") -> str:
        """List table summaries, optionally within a domain."""
        return _answer(session, lambda doc: list_tables(doc, domain or None))

    @srv.tool()
    def table_detail(name: str) -> str:
        """Full detail for one table: columns, keys, relationships, REQUIRED FILTERS."""
        return _answer(session, lambda doc: get_table(doc, name))

    @srv.tool()
    def get_metrics() -> str:
        """List defined metrics (name, measure, aggregation, filter)."""
        return _answer(session, list_metrics)

    @srv.tool()
    def route_intent(intent: str) -> str:
        """Which tables to use (and avoid) for an analytical intent."""
        return _answer(session, lambda doc: routing(doc, intent))

    @srv.tool()
    def check_sql(sql: str, dialect: str = "duckdb") -> str:
        """Lint SQL you wrote against the semantic layer BEFORE running it.

        Deterministic checks: unknown tables/columns, correlated references,
        deprecated tables, missing required filters, fan-out aggregates,
        SCD2 joins without a validity window. Fix every error and warning
        (each finding carries a fix hint), then re-check.
        """
        from semlayer.lint import lint_sql
        return _answer(session, lambda doc: lint_sql(doc, sql, dialect=dialect))

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
        return _answer(session, lambda doc: _compile(
            doc, name,
            group_by=[g.strip() for g in group_by.split(",") if g.strip()],
            time_grain=time_grain or None,
            time_start=time_start or None, time_end=time_end or None,
            extra_filter=filter or None, dialect=dialect,
            calendar=calendar or None, scd=scd or None))

    return srv
