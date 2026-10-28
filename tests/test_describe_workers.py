"""Both Describe passes fan out across a thread pool.

The property that matters is not speed but sameness: the document produced
with N workers must be byte-identical to the serial one. Pass-1 prompts
depend only on a table's own evidence; pass-2 prompts read neighbours from a
snapshot taken at the pass boundary. Tests prove it two ways:
a fake provider that records which thread served each call (so the fan-out
is real, not a pool of one), and the committed messy_mart cassettes (so the
real prompts replay identically at any worker count).
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

from semlayer.describe import describe_source  # noqa: E402
from semlayer.link import link_source  # noqa: E402
from semlayer.llm.provider import AnthropicProvider, CassetteMiss  # noqa: E402
from semlayer.profile.run import profile_with_stats  # noqa: E402
from semlayer.source import DuckDBSource  # noqa: E402


def _profiled(fixture: str, llm=None):
    import importlib
    mod = importlib.import_module(f"generators.{fixture}")
    con = duckdb.connect(":memory:")
    try:
        mod.build(con)
        src = DuckDBSource(con)
        doc, stats = profile_with_stats(src, llm=llm)
        link_source(src, doc, stats, llm=llm, use_declared=False)
    finally:
        con.close()
    doc.pop("_link_audit", None)
    return doc, stats


class _FakeLLM:
    """Answers from the evidence it is given; records the serving thread."""

    model = "fake"

    def __init__(self, delay: float = 0.0, fail_on: str | None = None):
        self.delay = delay
        self.fail_on = fail_on
        self.threads: list[int] = []
        self.calls = 0
        self._lock = threading.Lock()

    def complete(self, system: str, user: str, max_tokens: int = 1500) -> str:
        ev = json.loads(user)
        with self._lock:
            self.calls += 1
            self.threads.append(threading.get_ident())
        if self.fail_on and ev["table"] == self.fail_on:
            raise RuntimeError(f"boom on {ev['table']}")
        time.sleep(self.delay)
        n_neigh = len(ev.get("joined_neighbor_tables", {}))
        return json.dumps({
            "table_description": f"{ev['table']} with {n_neigh} neighbours",
            "ai_context": "join on the keys",
            "table_type": "unknown",
            "columns": [{"name": c["name"], "description": f"{c['name']} of {ev['table']}"}
                        for c in ev["columns"]],
        })


def test_workers_change_nothing_but_the_thread():
    serial_doc, stats = _profiled("fan_trap")
    describe_source(serial_doc, stats, _FakeLLM(), llm_workers=1)

    pooled = _FakeLLM(delay=0.02)
    pooled_doc, stats2 = _profiled("fan_trap")
    describe_source(pooled_doc, stats2, pooled, llm_workers=4)

    assert json.dumps(serial_doc, sort_keys=True) == json.dumps(pooled_doc, sort_keys=True)
    n_tables = len(serial_doc["semantic_layer"]["tables"])
    # the fan-out happened in both passes: more than one thread served each
    assert len(set(pooled.threads[:n_tables])) > 1
    assert len(set(pooled.threads[n_tables:])) > 1
    # and nothing ran on the calling thread, which only applies results
    assert threading.get_ident() not in pooled.threads


class _PassAwareLLM(_FakeLLM):
    """Says which pass produced each description, and echoes what it was shown."""

    def complete(self, system: str, user: str, max_tokens: int = 1500) -> str:
        ev = json.loads(user)
        with self._lock:
            self.calls += 1
            self.threads.append(threading.get_ident())
        shown = ev.get("joined_neighbor_tables", {})
        pass_n = 2 if shown else 1
        time.sleep(self.delay)
        return json.dumps({
            "table_description": f"{ev['table']} pass {pass_n} saw "
                                 + ";".join(f"{k}=[{v}]" for k, v in sorted(shown.items())),
            "ai_context": "x",
            "table_type": "unknown",
            "columns": [{"name": c["name"], "description": "c"} for c in ev["columns"]],
        })


def test_pass_two_reads_the_pass_boundary_snapshot():
    """No table sees a neighbour's pass-2 text, whatever the completion order."""
    for workers in (1, 4):
        doc, stats = _profiled("fan_trap")
        describe_source(doc, stats, _PassAwareLLM(delay=0.01), llm_workers=workers)
        for t in doc["semantic_layer"]["tables"]:
            d = t["description"]
            if "pass 2 saw" in d:
                assert "pass 2 saw" not in d.split("saw", 1)[1], (workers, d)
                assert "pass 1 saw" in d, (workers, d)


def test_a_dead_worker_fails_the_run():
    doc, stats = _profiled("fan_trap")
    victim = doc["semantic_layer"]["tables"][-1]["name"]
    with pytest.raises(RuntimeError, match=victim):
        describe_source(doc, stats, _FakeLLM(fail_on=victim), llm_workers=4)


def test_messy_mart_replays_identically_at_any_worker_count():
    """The committed cassettes are the real prompts; same bytes at 1 and 8."""
    try:
        from dotenv import load_dotenv
        load_dotenv(OSS.parent / ".env")
    except ImportError:
        pass
    docs = []
    for workers in (1, 8):
        llm = AnthropicProvider()
        try:
            doc, stats = _profiled("messy_mart", llm=llm)
            describe_source(doc, stats, llm, llm_workers=workers)
        except CassetteMiss as e:
            pytest.skip(str(e))
        docs.append(json.dumps(doc, sort_keys=True))
    assert docs[0] == docs[1]
