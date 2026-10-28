"""Output ceilings are sized to the payload and never below the default."""

from __future__ import annotations

import json

from semlayer.link.candidates import FKCandidate
from semlayer.link.validate_llm import validate_candidates
from semlayer.llm.provider import DEFAULT_MAX_TOKENS, output_budget


def test_budget_floor_and_growth() -> None:
    assert output_budget(0, per_item=180) == DEFAULT_MAX_TOKENS
    assert output_budget(5, per_item=180) == DEFAULT_MAX_TOKENS
    assert output_budget(25, per_item=180) == 300 + 25 * 180
    assert output_budget(10_000, per_item=180) == 24_000


class _Spy:
    def __init__(self) -> None:
        self.budgets: list[int] = []

    def complete(self, system: str, user: str, max_tokens: int = 1500) -> str:
        self.budgets.append(max_tokens)
        items = json.loads(user)
        return json.dumps([{"id": it["id"], "verdict": "plausible", "confidence": 0.9,
                            "reason": "x"} for it in items])


def test_validator_passes_a_chunk_sized_budget() -> None:
    spy = _Spy()
    cands = [FKCandidate(f"c{i}", "k", "p", "id", 0.99, 10, 10, 0.5) for i in range(35)]
    validate_candidates(spy, cands)
    assert spy.budgets == [300 + 30 * 90, DEFAULT_MAX_TOKENS]
