# SPDX-License-Identifier: FSL-1.1-ALv2
"""LLM semantic validation of FK candidates (prompt version 1).

The DBAutoDoc pattern: statistics generate, the LLM judges semantic
plausibility. Batched in ONE call per warehouse chunk (frugality). The LLM
sees the statistical evidence AND the naming signal — its job is exactly the
trap case: high inclusion ratio, absurd semantics (shoe_size ⊆ dept_id).
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from semlayer.llm.provider import output_budget

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


logger = logging.getLogger(__name__)

def validate_candidates(provider, candidates: list, workers: int = 1) -> dict[tuple, dict]:
    """Returns {candidate.key(): {"verdict", "confidence", "reason"}}.

    One call per chunk of BATCH candidates. `workers` chunks are in flight
    at once; each worker only calls the model and parses its reply, and the
    verdicts are merged on the calling thread in chunk order, so the result
    is identical at any count. Measured live on a 116-table warehouse: 92
    chunks at ~10 s each were 910 s in series -- the largest single LLM term
    in the run, larger than the second Describe pass (docs/scale.md).

    A worker that raises fails the run: `executor.map` re-raises when its
    result is read, so a chunk is never silently left uncorroborated by a
    thread that died.
    """
    chunks = [candidates[i:i + BATCH] for i in range(0, len(candidates), BATCH)]
    if workers <= 1 or len(chunks) <= 1:
        parsed = [_judge_chunk(provider, chunk) for chunk in chunks]
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(chunks))) as ex:
            parsed = list(ex.map(lambda chunk: _judge_chunk(provider, chunk), chunks))
    out: dict[tuple, dict] = {}
    for chunk, verdicts in zip(chunks, parsed, strict=True):
        for v in verdicts:
            j = v.get("id")
            if isinstance(j, int) and 0 <= j < len(chunk):
                out[chunk[j].key()] = {
                    "verdict": v.get("verdict", "implausible"),
                    "confidence": max(0.0, min(1.0, float(v.get("confidence", 0.5)))),
                    "reason": str(v.get("reason", ""))[:100],
                }
    return out


def _judge_chunk(provider, chunk: list) -> list[dict]:
    """Ask the model about one chunk; the parsed verdict list, or [] when unparseable."""
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
    # ~42 output tokens per verdict measured live (a 30-candidate chunk
    # returned 790-1,440); budget twice that.
    raw = provider.complete(SYSTEM, json.dumps(items, indent=1),
                            max_tokens=output_budget(len(chunk), per_item=90))
    # A dropped chunk silently removes the LLM corroboration for EVERY
    # candidate in it, and the corroboration rule means an uncorroborated
    # candidate can never be accepted -- so a quiet `return []` here reads
    # downstream as "the model rejected these". It did not; we lost them.
    # This cost FK precision 0.000 on three fixtures when a chattier model
    # overran the token ceiling and the array came back unclosed. The
    # provider now escalates on truncation; this stays loud for the
    # remaining ways a model can return something unparseable.
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        logger.warning(
            "LLM validation returned no JSON array for %d candidates "
            "(%s...); they stay uncorroborated and cannot be auto-accepted",
            len(chunk), raw[:80].replace("\n", " "))
        return []
    try:
        verdicts = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        logger.warning(
            "LLM validation returned unparseable JSON for %d candidates "
            "(%s); they stay uncorroborated and cannot be auto-accepted",
            len(chunk), e)
        return []
    return verdicts if isinstance(verdicts, list) else []
