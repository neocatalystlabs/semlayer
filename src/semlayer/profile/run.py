"""Profile stage runner: source -> draft semantic layer document.

Output is a spec-conformant document with lifecycle: inferred and
confidence + provenance on every inferred element (dogfooding the format
from the first stage). Aggregation blocks are attached to measure columns;
PK candidates come from full-column uniqueness + id-naming.
"""

from __future__ import annotations

from semlayer.profile.stats import TableStats, profile_table
from semlayer.profile.typing_rules import classify

ENGINE_VERSION = "0.1.0.dev0"


ESCALATE_BELOW = 0.7  # tier-1 confidence under this goes to the LLM (if provided)


def profile_source(source, no_sample_values: bool = False, llm=None) -> dict:
    """Run the profile stage and return the draft semantic-layer document (stats discarded)."""
    doc, _ = profile_with_stats(source, no_sample_values=no_sample_values, llm=llm)
    return doc


def profile_with_stats(source, no_sample_values: bool = False, llm=None):
    """Returns (document, stats_by_table) — Link consumes both."""
    tables_meta = source.list_tables()
    out_tables = []
    stats_by_table: dict = {}
    for t in sorted(tables_meta, key=lambda x: (x.schema, x.name)):
        ts = profile_table(source, t)
        stats_by_table[t.name] = ts
        doc = _table_doc(ts, no_sample_values)
        if llm is not None:
            _escalate(llm, ts, doc, no_sample_values)
        out_tables.append(doc)
    model = "none/heuristic" if llm is None else f"{llm.model}/escalation"
    return {
        "semantic_layer": {
            "spec_version": "0.1.0",
            "name": "profiled",
            "generated_by": {"engine": ENGINE_VERSION, "model": model},
            "tables": out_tables,
        }
    }, stats_by_table


def _escalate(llm, ts: TableStats, doc: dict, no_sample_values: bool) -> None:
    from semlayer.profile.llm_typing import escalate_table

    low = [c for c in doc["columns"] if c["confidence"] < ESCALATE_BELOW]
    if not low:
        return
    stats = [ts.columns[c["name"]] for c in low]
    verdicts = escalate_table(
        llm, ts.table.name, [c.name for c in ts.table.columns], stats,
        no_sample_egress=no_sample_values,
    )
    for c in low:
        v = verdicts.get(c["name"])
        if not v:
            continue
        prior_signal = c["provenance"][0]["signal"] if c["provenance"] else "statistic"
        if v["semantic_type"] != c["semantic_type"] and prior_signal == "naming":
            # LLM overriding a name-rule answer: record the disagreement so
            # reviewers see both readings (conflicts envelope, SPEC.md)
            c.setdefault("conflicts", []).append({
                "between": ["naming", "llm"],
                "detail": f"naming said {c['semantic_type']}, llm said {v['semantic_type']}",
            })
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
