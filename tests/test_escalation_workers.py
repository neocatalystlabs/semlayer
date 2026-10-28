"""Profile's typing escalation fans out; the document does not change.

Same contract as tests/test_describe_workers.py: a fake provider proves the
fan-out is real and the verdicts land identically; the committed messy_mart
cassettes prove the real prompts replay byte-identically at 1 and 8 workers.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import duckdb
import pytest

OSS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OSS / "fixtures"))

from semlayer.llm.provider import AnthropicProvider, CassetteMiss  # noqa: E402
from semlayer.profile.run import profile_with_stats  # noqa: E402
from semlayer.source import DuckDBSource  # noqa: E402


def _profile(fixture: str, llm, workers: int) -> dict:
    import importlib
    mod = importlib.import_module(f"generators.{fixture}")
    con = duckdb.connect(":memory:")
    try:
        mod.build(con)
        doc, _ = profile_with_stats(DuckDBSource(con), llm=llm, llm_workers=workers)
    finally:
        con.close()
    return doc


class _FakeLLM:
    model = "fake"

    def __init__(self, delay: float = 0.0, fail_on: str | None = None):
        self.delay = delay
        self.fail_on = fail_on
        self.threads: list[int] = []
        self._lock = threading.Lock()

    def complete(self, system: str, user: str, max_tokens: int = 1500) -> str:
        payload = json.loads(user)
        with self._lock:
            self.threads.append(threading.get_ident())
        if self.fail_on and payload["table"] == self.fail_on:
            raise RuntimeError(f"boom on {payload['table']}")
        time.sleep(self.delay)
        return json.dumps([
            {"column": b["column"], "semantic_type": "code", "entity_role": "dimension",
             "confidence": 0.7, "rationale": f"{b['column']} in {payload['table']}"}
            for b in payload["classify_these"]
        ])


def test_workers_change_nothing_but_the_thread():
    serial = _profile("messy_mart", _FakeLLM(), workers=1)
    pooled_llm = _FakeLLM(delay=0.02)
    pooled = _profile("messy_mart", pooled_llm, workers=4)
    assert json.dumps(serial, sort_keys=True) == json.dumps(pooled, sort_keys=True)
    assert len(pooled_llm.threads) > 1, "fixture no longer escalates anything"
    assert len(set(pooled_llm.threads)) > 1


def test_a_dead_worker_fails_the_run():
    class _Spy(_FakeLLM):
        def __init__(self):
            super().__init__()
            self.tables: list[str] = []

        def complete(self, system, user, max_tokens=1500):
            self.tables.append(json.loads(user)["table"])
            return super().complete(system, user, max_tokens)

    spy = _Spy()
    _profile("messy_mart", spy, workers=1)
    victim = spy.tables[-1]        # a table that really escalates
    with pytest.raises(RuntimeError, match=victim):
        _profile("messy_mart", _FakeLLM(fail_on=victim), workers=4)


def test_messy_mart_replays_identically_at_any_worker_count():
    try:
        from dotenv import load_dotenv
        load_dotenv(OSS.parent / ".env")
    except ImportError:
        pass
    docs = []
    for workers in (1, 8):
        try:
            docs.append(json.dumps(_profile("messy_mart", AnthropicProvider(), workers),
                                   sort_keys=True))
        except CassetteMiss as e:
            pytest.skip(str(e))
    assert docs[0] == docs[1]
