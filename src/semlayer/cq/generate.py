"""CQ generation + adversarial verification (M5, prompt v1).

Generator (Haiku) proposes CQs from the semantic layer (metrics, dimensions,
enums, routing). Skeptic (Sonnet — different model, adversarial stance)
verifies each: well-posed? answerable from the layer? unambiguous? The suite
only admits skeptic-approved CQs; the seeded-bad gate (tests) proves the
skeptic actually rejects ill-posed questions.
"""

from __future__ import annotations

import json
import re

GEN_SYSTEM = """You generate Competency Questions for a data warehouse: concrete
business questions the semantic layer should answer. Base them ONLY on the
provided metrics/tables/dimensions. Mix ~60% simple (one metric/dimension)
and ~40% complex (multi-table, filters, comparisons). No hypotheticals about
data that isn't modeled. Respond ONLY JSON:
[{"question": "...", "complexity": "simple"|"complex",
  "targets": ["metric or table.column refs used"]}]"""

SKEPTIC_SYSTEM = """You are an adversarial reviewer of Competency Questions for a
data warehouse. REJECT a question if it is: ambiguous (multiple defensible
readings), unanswerable from the provided semantic layer, trivially vague,
about unmodeled data, or dependent on undefined business terms. Default to
reject when uncertain. Respond ONLY JSON:
[{"id": n, "verdict": "accept"|"reject", "reason": "<=15 words"}]"""


def generate(llm, doc: dict, n: int = 15) -> list[dict]:
    """Ask the generator LLM for n candidate CQs grounded in the semantic layer."""
    from semlayer import mcp_server
    ctx = {
        "metrics": mcp_server.list_metrics(doc),
        "tables": mcp_server.list_tables(doc),
        "routing": mcp_server.routing(doc),
    }
    raw = llm.complete(GEN_SYSTEM + f"\nGenerate exactly {n} questions.",
                       json.dumps(ctx, default=str), max_tokens=2500)
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    valid = [
        i for i in items
        if i.get("question") and i.get("complexity") in ("simple", "complex")
    ]
    return valid[:n]


def skeptic_verify(skeptic_llm, doc: dict, questions: list[dict]) -> list[dict]:
    """Returns questions annotated with verdict/reason; only 'accept' enter suites."""
    from semlayer import mcp_server
    ctx = {
        "metrics": mcp_server.list_metrics(doc),
        "tables": mcp_server.list_tables(doc),
    }
    out = []
    for i in range(0, len(questions), 15):
        chunk = questions[i:i + 15]
        payload = {
            "semantic_layer": ctx,
            "questions": [{"id": j, "question": q["question"]} for j, q in enumerate(chunk)],
        }
        raw = skeptic_llm.complete(
            SKEPTIC_SYSTEM, json.dumps(payload, default=str), max_tokens=4000,
        )
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        verdicts = {}
        if m:
            try:
                for v in json.loads(m.group(0)):
                    verdicts[v.get("id")] = v
            except json.JSONDecodeError:
                pass
        for j, q in enumerate(chunk):
            v = verdicts.get(j, {"verdict": "reject", "reason": "no skeptic verdict"})
            out.append({**q, "verdict": v.get("verdict", "reject"), "reason": v.get("reason", "")})
    return out
