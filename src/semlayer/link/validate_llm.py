"""LLM semantic validation of FK candidates (prompt version 1).

The DBAutoDoc pattern: statistics generate, the LLM judges semantic
plausibility. Batched in ONE call per warehouse chunk (frugality). The LLM
sees the statistical evidence AND the naming signal — its job is exactly the
trap case: high inclusion ratio, absurd semantics (shoe_size ⊆ dept_id).
"""

from __future__ import annotations

import json
import re

PROMPT_VERSION = "v1"
BATCH = 30

SYSTEM = f"""You judge whether candidate foreign-key relationships in a data
warehouse are semantically plausible. Prompt version {PROMPT_VERSION}.
Each candidate has statistical evidence (inclusion ratio: fraction of child
values present in the parent). High inclusion does NOT imply a real FK —
small integer domains overlap by coincidence (a shoe_size column's values may
all fall inside a dept_id column's range). Judge MEANING: would a schema
designer intend this reference?
Respond ONLY a JSON array:
[{{"id": <n>, "verdict": "plausible"|"implausible", "confidence": 0.0-1.0,
  "reason": "<=12 words"}}]
Abbreviations are common: cust=customer, ord=order, prod=product, whs=warehouse."""


def validate_candidates(provider, candidates: list) -> dict[tuple, dict]:
    """Returns {candidate.key(): {"verdict", "confidence", "reason"}}."""
    out: dict[tuple, dict] = {}
    for i in range(0, len(candidates), BATCH):
        chunk = candidates[i:i + BATCH]
        items = [
            {
                "id": j,
                "child": f"{c.child_table}.{c.child_column}",
                "parent": f"{c.parent_table}.{c.parent_column}",
                "inclusion_ratio": c.inclusion_ratio,
                "child_distinct": c.child_distinct,
                "parent_distinct": c.parent_distinct,
            }
            for j, c in enumerate(chunk)
        ]
        raw = provider.complete(SYSTEM, json.dumps(items, indent=1))
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if not m:
            continue
        try:
            verdicts = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        for v in verdicts:
            j = v.get("id")
            if isinstance(j, int) and 0 <= j < len(chunk):
                out[chunk[j].key()] = {
                    "verdict": v.get("verdict", "implausible"),
                    "confidence": max(0.0, min(1.0, float(v.get("confidence", 0.5)))),
                    "reason": str(v.get("reason", ""))[:100],
                }
    return out
