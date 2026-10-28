"""Link's LLM validation fans out over chunks; verdicts are identical at any width."""

from __future__ import annotations

import json
import threading
import time

from semlayer.link.candidates import FKCandidate
from semlayer.link.validate_llm import BATCH, validate_candidates


class _Fake:
    """Answers every chunk after a short sleep and records thread overlap."""

    def __init__(self, bad_chunk_first_child: str | None = None) -> None:
        self.lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0
        self.calls = 0
        self.bad = bad_chunk_first_child

    def complete(self, system: str, user: str, max_tokens: int = 1500) -> str:
        with self.lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            self.calls += 1
        time.sleep(0.02)
        items = json.loads(user)
        with self.lock:
            self.in_flight -= 1
        if self.bad and items[0]["child"].startswith(self.bad):
            return "I cannot judge these."
        return json.dumps([
            {"id": it["id"], "verdict": "plausible" if it["inclusion_ratio"] > 0.97
             else "implausible", "confidence": 0.9, "reason": it["child"]}
            for it in items
        ])


def _cands(n: int) -> list[FKCandidate]:
    return [FKCandidate(f"c{i}", "k", f"p{i}", "id", 0.96 + (i % 5) / 100, 10, 10, 0.5)
            for i in range(n)]


def test_verdicts_identical_at_any_width() -> None:
    cands = _cands(BATCH * 4 + 7)
    serial = validate_candidates(_Fake(), cands, workers=1)
    fake = _Fake()
    wide = validate_candidates(fake, cands, workers=8)
    assert wide == serial
    assert len(wide) == len(cands)
    assert fake.calls == 5
    assert fake.peak > 1, "chunks were not in flight concurrently"


def test_unparseable_chunk_stays_uncorroborated_at_any_width() -> None:
    cands = _cands(BATCH * 3)
    first_of_second_chunk = cands[BATCH].child_table
    serial = validate_candidates(_Fake(first_of_second_chunk), cands, workers=1)
    wide = validate_candidates(_Fake(first_of_second_chunk), cands, workers=4)
    assert wide == serial
    assert len(wide) == BATCH * 2
    assert all(c.key() not in wide for c in cands[BATCH:2 * BATCH])


def test_single_chunk_never_spawns_pool() -> None:
    fake = _Fake()
    out = validate_candidates(fake, _cands(5), workers=8)
    assert len(out) == 5 and fake.peak == 1
