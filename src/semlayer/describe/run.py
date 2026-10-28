# SPDX-License-Identifier: FSL-1.1-ALv2
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
from concurrent.futures import ThreadPoolExecutor

from semlayer.llm.provider import output_budget

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


def _evidence(t: dict, stats, neighbors: dict[str, str], no_samples: bool,
              context=None) -> str:
    ts = stats[t["name"]]
    from semlayer.context import dictionary_for_table, relevant_excerpts
    dict_entries = dictionary_for_table(context.dictionary, t["name"]) if context else {}
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
        entry = dict_entries.get(c["name"].lower())
        if entry is not None:
            d["doc_description"] = entry.description[:200]
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
    if context and context.chunks:
        excerpts = relevant_excerpts(context.chunks, t["name"],
                                     [c["name"] for c in t["columns"]])
        if excerpts:
            ev["reference_docs"] = {
                "note": ("customer-provided documentation; treat as PRIOR, not "
                         "truth — observed statistics win on conflict"),
                "excerpts": excerpts,
            }
    return json.dumps(ev, indent=1)


def describe_source(doc: dict, stats: dict, llm, no_samples: bool = False,
                    passes: int = 2, context=None, llm_workers: int = 1) -> dict:
    """Describe every table via iterative context propagation (see module docstring).

    `context` (v0.2): a semlayer.context.ContextBundle of knowledge-doc priors;
    relevant excerpts and dictionary descriptions ride into the evidence.

    `llm_workers`: how many calls are in flight at once, in either pass.
    Pass 1 reads only a table's own evidence. Pass 2 reads its neighbours'
    descriptions from a snapshot taken at the pass boundary, so every table
    sees the text its neighbours had when the pass began, whatever the order
    the calls complete in. The document is byte-identical at any worker
    count. Until 2026-10-28 pass 2 read `descriptions` as it wrote them, so a
    table described later saw a neighbour's pass-2 text: deterministic only
    because iteration was serial and sorted, and the one loop the pool could
    not reach. The snapshot changed every pass-2 prompt that had an
    earlier-described neighbour; the description-judge floor in
    tests/test_describe.py adjudicated the change (numbers there).
    """
    # Inventory-only tables (semlayer.tier) keep their shape and get no
    # description: they are the part of the warehouse nobody models against,
    # and on a large estate they are most of the model spend.
    tables = [t for t in doc["semantic_layer"]["tables"] if t.get("modeling") != "inventory_only"]
    rels = doc["semantic_layer"].get("relationships", [])
    neigh_map: dict[str, set[str]] = {}
    for r in rels:
        a, b = r["from"]["table"], r["to"]["table"]
        neigh_map.setdefault(a, set()).add(b)
        neigh_map.setdefault(b, set()).add(a)

    descriptions: dict[str, str] = {}
    for pass_n in range(passes):
        if pass_n == 0:
            # Workers do only the pure part -- build evidence, call the model,
            # parse -- and the results are applied here, serially, in table
            # order. Nothing mutable is shared across threads, and provenance
            # entries land in the same order the serial loop produced.
            evidence = [_evidence(t, stats, {}, no_samples, context=context)
                        for t in tables]
            budgets = [_budget(t) for t in tables]
            results = _complete_many(llm, evidence, budgets, max(1, llm_workers))
            for t, ev, parsed in zip(tables, evidence, results, strict=True):
                if not parsed:
                    continue
                if '"reference_docs"' in ev:
                    t.setdefault("provenance", []).append(
                        {"signal": "docs", "detail": "context docs in describe evidence"})
                _apply_parsed(t, parsed, pass_n)
                descriptions[t["name"]] = t["description"]
            continue
        # Pass boundary: every prompt in this pass reads the previous pass's
        # text, never a neighbour's result from this one. That is what makes
        # the prompts a pure function of the pass input, and the pool safe.
        jobs = _pass_jobs(tables, neigh_map, dict(descriptions), stats, no_samples, context)
        results = _complete_many(llm, [ev for _, ev in jobs], [_budget(t) for t, _ in jobs],
                                 max(1, llm_workers))
        for (t, _ev), parsed in zip(jobs, results, strict=True):
            if not parsed:
                continue
            _apply_parsed(t, parsed, pass_n)
            descriptions[t["name"]] = t["description"]
    return doc


def _pass_jobs(tables: list[dict], neigh_map: dict, snapshot: dict[str, str], stats,
               no_samples: bool, context) -> list[tuple[dict, str]]:
    """(table, evidence) for every table that gets a later-pass call.

    Neighbour text comes from `snapshot`, never from this pass's results, so
    the prompts are a pure function of the pass input. A table with no
    described neighbour keeps its pass-1 result.
    """
    jobs: list[tuple[dict, str]] = []
    for t in tables:
        neighbors = {n: snapshot[n]
                     for n in sorted(neigh_map.get(t["name"], set()))
                     if snapshot.get(n)}
        if not neighbors and t.get("description"):
            continue
        jobs.append((t, _evidence(t, stats, neighbors, no_samples, context=context)))
    return jobs


def _budget(t: dict) -> int:
    """Output ceiling for one table's reply: ~53 tokens per column measured live."""
    return output_budget(len(t["columns"]), per_item=110, base=400)


def _complete_many(llm, evidence: list[str], budgets: list[int], workers: int) -> list[dict]:
    """One parsed response per evidence string, in input order.

    A worker that raises fails the run: `executor.map` re-raises when its
    result is read, so a table is never silently left undescribed by a thread
    that died. The cassette layer already writes atomically, so concurrent
    recorders cannot interleave a file.
    """
    def one(ev: str, budget: int) -> dict:
        return _parse(llm.complete(SYSTEM, ev, max_tokens=budget))

    if workers <= 1 or len(evidence) <= 1:
        return [one(ev, b) for ev, b in zip(evidence, budgets, strict=True)]
    with ThreadPoolExecutor(max_workers=min(workers, len(evidence))) as ex:
        return list(ex.map(one, evidence, budgets))


def _from_catalog(obj: dict) -> bool:
    """Was this description read from the warehouse's own catalog?

    A human wrote it. Our generated sentence does not get to replace it --
    CLAUDE.md's rule for prior knowledge is "never overwritten, gaps filled,
    conflicts flagged", and this is the first place that rule is enforceable
    because native comments were not read at all until now.
    """
    return any(pv.get("signal") == "catalog_prior" for pv in (obj.get("provenance") or []))


def _apply_parsed(t: dict, parsed: dict, pass_n: int) -> None:
    if not _from_catalog(t):
        t["description"] = parsed.get("table_description", t.get("description", ""))
    if parsed.get("ai_context"):
        t["ai_context"] = parsed["ai_context"]
    _apply_table_type(t, parsed.get("table_type"))
    by_name = {c["name"]: c for c in t["columns"]}
    for cd in parsed.get("columns", []):
        c = by_name.get(cd.get("name"))
        if c is not None and cd.get("description") and not _from_catalog(c):
            c["description"] = cd["description"][:200]
    prov = {"signal": "llm", "detail": f"describe pass {pass_n + 1}"}
    t.setdefault("provenance", []).append(prov)


LLM_FILL_CONFIDENCE = 0.65   # measured 7/10 against gold, shrunk


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
        # The heuristic's confidence and provenance describe the `unknown`
        # verdict we just replaced -- leaving them makes the document assert a
        # type at the no-decisive-signal confidence of 0.05, and cite a rule
        # that did not produce it. Measured against gold: the fill is right
        # 7 of 10, shrunk toward 0.5 with a pseudo-count of 2 -> 0.65.
        # See docs/calibration.md.
        t["confidence"] = LLM_FILL_CONFIDENCE
        t.setdefault("provenance", []).append({
            "signal": "llm",
            "detail": f"table_type: llm filled unknown -> {llm_type}",
        })
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
