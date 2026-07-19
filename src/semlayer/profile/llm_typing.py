"""LLM escalation tier for semantic typing (prompt version 1).

Only columns below the escalation threshold reach here, batched ONE CALL PER
TABLE (frugality: sibling context improves accuracy AND amortizes tokens).
Answers land with provenance signal `llm`; enum decodes are tagged
decode_source: llm_guess (review-gated per SPEC.md 2.8).
"""

from __future__ import annotations

import json
import re

PROMPT_VERSION = "v1"

_TAXONOMY = [
    "identifier", "monetary_value", "quantity", "rate", "percentage",
    "timestamp_event", "timestamp_effective", "date", "duration",
    "status_code", "enum", "flag", "free_text", "name", "url", "code",
    "geo_country", "geo_region", "geo_city", "geo_postal", "geo_coordinates",
    "pii_email", "pii_name", "pii_phone", "pii_address", "pii_national_id", "pii_other",
    "json_object", "unknown",
]
_ROLES = ["primary_key", "unique_key", "natural_key", "foreign_key",
          "dimension", "measure", "metadata"]

SYSTEM = f"""You classify data warehouse columns. Prompt version {PROMPT_VERSION}.
For each column, choose semantic_type from exactly this list: {", ".join(_TAXONOMY)}.
Choose entity_role from: {", ".join(_ROLES)}.
If the column holds coded values you can decode (e.g. status letters), provide enum_decodes.
Respond with ONLY a JSON array, one object per column:
[{{"column": "...", "semantic_type": "...", "entity_role": "...", "confidence": 0.0-1.0,
  "enum_decodes": {{"VALUE": "meaning", ...}} | null, "rationale": "<=15 words"}}]
Be conservative: use "unknown" and low confidence when genuinely unclear.
Cryptic abbreviations are common (sts=status, amt=amount, cd=code, nm=name, dt=date)."""


def _column_block(cs, table_name: str) -> dict:
    d: dict = {
        "column": cs.name, "sql_type": cs.sql_type,
        "n_distinct": cs.n_distinct, "null_rate": round(cs.null_rate, 3),
        "unique": cs.is_unique,
    }
    if cs.top_values:
        d["top_values"] = [v for v, _ in cs.top_values[:8]]
    if cs.min_val is not None:
        d["range"] = [cs.min_val[:40], cs.max_val[:40] if cs.max_val else None]
    return d


def escalate_table(provider, table_name: str, sibling_columns: list[str],
                   escalated: list, no_sample_egress: bool = False,
                   doc_excerpts: list[dict] | None = None) -> dict[str, dict]:
    """escalated: list of ColumnStats needing LLM help. Returns {col_name: verdict}.

    doc_excerpts (v0.2 knowledge-doc priors) join the user content additively,
    so no-context calls stay byte-identical to v0.1 and cassettes survive.
    """
    if not escalated:
        return {}
    blocks = []
    for cs in escalated:
        b = _column_block(cs, table_name)
        if no_sample_egress:
            b.pop("top_values", None)
            b.pop("range", None)
        blocks.append(b)
    payload: dict = {
        "table": table_name,
        "all_columns_in_table": sibling_columns,
        "classify_these": blocks,
    }
    if doc_excerpts:
        payload["reference_docs"] = {
            "note": ("customer-provided documentation; treat as PRIOR, not truth "
                     "— observed statistics win on conflict"),
            "excerpts": doc_excerpts,
        }
    user = json.dumps(payload, indent=1)
    raw = provider.complete(SYSTEM, user)
    return _parse(raw, {cs.name for cs in escalated})


def _parse(raw: str, expected: set[str]) -> dict[str, dict]:
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return {}
    try:
        items = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    out = {}
    for item in items:
        col = item.get("column")
        if col not in expected:
            continue
        st = item.get("semantic_type")
        er = item.get("entity_role")
        if st not in _TAXONOMY or er not in _ROLES:
            continue
        conf = item.get("confidence", 0.6)
        out[col] = {
            "semantic_type": st,
            "entity_role": er,
            # cap self-reported confidence: calibration gate owns the ceiling
            "confidence": max(0.5, min(0.85, float(conf))),
            "enum_decodes": item.get("enum_decodes") or None,
            "rationale": str(item.get("rationale", ""))[:120],
        }
    return out
