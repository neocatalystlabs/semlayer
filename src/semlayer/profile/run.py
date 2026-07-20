"""Profile stage runner: source -> draft semantic layer document.

Output is a spec-conformant document with lifecycle: inferred and
confidence + provenance on every inferred element (dogfooding the format
from the first stage). Aggregation blocks are attached to measure columns;
PK candidates come from full-column uniqueness + id-naming.
"""

from __future__ import annotations

from semlayer.profile.stats import TableStats, profile_table
from semlayer.profile.typing_rules import classify

ENGINE_VERSION = "0.3.0b2"


ESCALATE_BELOW = 0.7  # tier-1 confidence under this goes to the LLM (if provided)


def profile_source(source, no_sample_values: bool = False, llm=None) -> dict:
    """Run the profile stage and return the draft semantic-layer document (stats discarded)."""
    doc, _ = profile_with_stats(source, no_sample_values=no_sample_values, llm=llm)
    return doc


def profile_with_stats(source, no_sample_values: bool = False, llm=None, context=None):
    """Returns (document, stats_by_table) — Link consumes both.

    `context` (v0.2): a semlayer.context.ContextBundle; relevant prose excerpts
    join the typing-escalation evidence as priors.
    """
    tables_meta = source.list_tables()
    out_tables = []
    stats_by_table: dict = {}
    for t in sorted(tables_meta, key=lambda x: (x.schema, x.name)):
        ts = profile_table(source, t)
        stats_by_table[t.name] = ts
        doc = _table_doc(ts, no_sample_values)
        if llm is not None:
            _escalate(llm, ts, doc, no_sample_values, context=context)
        out_tables.append(doc)
    model = "none/heuristic" if llm is None else f"{llm.model}/escalation"
    return {
        "semantic_layer": {
            "spec_version": "0.2.0",
            "name": "profiled",
            "generated_by": {"engine": ENGINE_VERSION, "model": model},
            "tables": out_tables,
        }
    }, stats_by_table


def _escalate(llm, ts: TableStats, doc: dict, no_sample_values: bool, context=None) -> None:
    from semlayer.profile.llm_typing import escalate_table

    low = [c for c in doc["columns"] if c["confidence"] < ESCALATE_BELOW]
    promoted = _doc_promoted(context, ts.table.name, doc["columns"], low)
    if not low and not promoted:
        return
    excerpts = None
    if context is not None and context.chunks:
        from semlayer.context import relevant_excerpts
        excerpts = relevant_excerpts(
            context.chunks, ts.table.name, [c.name for c in ts.table.columns]) or None
    batch = low + promoted
    stats = [ts.columns[c["name"]] for c in batch]
    verdicts = escalate_table(
        llm, ts.table.name, [c.name for c in ts.table.columns], stats,
        no_sample_egress=no_sample_values, doc_excerpts=excerpts,
    )
    promoted_names = {c["name"] for c in promoted}
    for c in batch:
        v = verdicts.get(c["name"])
        if not v:
            continue
        if c["name"] in promoted_names:
            _apply_promoted_verdict(c, v)
        else:
            _apply_verdict(c, v)


def _doc_promoted(context, table_name: str, cols: list[dict], low: list[dict]) -> list[dict]:
    """Columns whose confident heuristic answer gets a doc-prompted second look.

    Docs can flag what statistics can't. Requires column AND table tokens in
    the same chunk — the same anti-cross-talk scoping as decode claims.
    """
    if context is None or not context.chunks:
        return []
    low_names = {c["name"] for c in low}
    tn = table_name.lower()
    out = []
    for c in cols:
        if c["name"] in low_names:
            continue
        if any(c["name"].lower() in ch.tokens and tn in ch.tokens
               for ch in context.chunks):
            out.append(c)
    return out


def _apply_verdict(c: dict, v: dict) -> None:
    prior_signal = c["provenance"][0]["signal"] if c["provenance"] else "statistic"
    if v["semantic_type"] != c["semantic_type"] and prior_signal == "naming":
        # LLM overriding a name-rule answer: record the disagreement so
        # reviewers see both readings (conflicts envelope, SPEC.md)
        c.setdefault("conflicts", []).append({
            "between": ["naming", "llm"],
            "detail": f"naming said {c['semantic_type']}, llm said {v['semantic_type']}",
        })
    _apply_core(c, v)


def _apply_core(c: dict, v: dict) -> None:
    c["semantic_type"] = v["semantic_type"]
    c["entity_role"] = v["entity_role"]
    c["confidence"] = round(v["confidence"], 2)
    c["provenance"].append({"signal": "llm", "detail": v["rationale"]})
    if v["semantic_type"].startswith("pii_"):
        c["pii"] = True
    if v["entity_role"] == "measure" and "aggregations" not in c:
        c["aggregations"] = {"allowed": ["sum", "avg", "min", "max", "count"], "default": "sum"}
    if v.get("enum_decodes"):
        c["enum_values"] = [
            {"value": k, "meaning": m, "decode_source": "llm_guess"}
            for k, m in v["enum_decodes"].items()
        ]


def _apply_promoted_verdict(c: dict, v: dict) -> None:
    """Apply a verdict to a doc-promoted column (heuristic was CONFIDENT).

    A correction always lands in the conflicts envelope for review — a
    doc-prompted override of a decided answer is never silent. Agreement
    corroborates without confidence churn.
    """
    if v["semantic_type"] == c["semantic_type"]:
        c["provenance"].append(
            {"signal": "docs", "detail": "doc-prompted re-check corroborated the heuristic"})
        return
    prior_signal = c["provenance"][0]["signal"] if c["provenance"] else "statistic"
    prior = f"{c['semantic_type']} (conf {c['confidence']})"
    c.setdefault("conflicts", []).append({
        "between": [prior_signal, "llm"],
        "detail": f"doc-prompted re-escalation: heuristic said {prior}, "
                  f"llm+docs said {v['semantic_type']}",
    })
    c["provenance"].append(
        {"signal": "docs", "detail": "re-escalated: column named in context docs"})
    _apply_core(c, v)


def _table_doc(ts: TableStats, no_sample_values: bool) -> dict:
    cols = []
    pk_candidates = []
    for name, cs in ts.columns.items():
        typ = classify(cs, no_sample_values=no_sample_values)
        col: dict = {
            "name": name,
            "sql_type": cs.sql_type,
            "semantic_type": typ.semantic_type,
            "entity_role": typ.entity_role,
            "confidence": round(typ.confidence, 2),
            "lifecycle": "inferred",
            "provenance": [{"signal": typ.signal, "detail": typ.detail}],
        }
        if typ.semantic_type.startswith("pii_"):
            col["pii"] = True
        if typ.entity_role == "measure":
            col["aggregations"] = {
                "allowed": ["sum", "avg", "min", "max", "count"],
                "default": "sum",
            }
        if typ.entity_role == "primary_key":
            pk_candidates.append((name, typ.confidence))
        cols.append(col)

    doc: dict = {
        "name": ts.table.name,
        "source": ts.table.qualified,
        "table_type": "unknown",   # Link-stage decision
        "lifecycle": "inferred",
        "columns": cols,
    }
    if pk_candidates:
        best = max(pk_candidates, key=lambda x: x[1])
        doc["primary_key"] = [best[0]]
        # demote other unique id columns to unique_key
        for col in cols:
            if col["entity_role"] == "primary_key" and col["name"] != best[0]:
                col["entity_role"] = "unique_key"
    return doc
