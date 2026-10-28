"""Link's inclusion queries fan out over a pool; the candidates do not change.

The query plan is fixed before any query runs, so the only thing a worker
count may change is wall-clock. A counting source proves the fan-out is real
and that each worker used its own cursor; the fixtures prove the Link output
is byte-identical at 1 and 4 workers; a dead worker fails the run; a source
without `for_worker` stays serial instead of sharing a connection.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import duckdb
import pytest

OSS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OSS / "fixtures"))

from semlayer.link import link_source  # noqa: E402
from semlayer.link.candidates import generate_candidates  # noqa: E402
from semlayer.profile.run import profile_with_stats  # noqa: E402
from semlayer.source import DuckDBSource  # noqa: E402


def _build(fixture: str):
    import importlib
    mod = importlib.import_module(f"generators.{fixture}")
    con = duckdb.connect(":memory:")
    mod.build(con)
    return con


class _Counting(DuckDBSource):
    """Records which thread and which cursor served each inclusion query."""

    def __init__(self, con):
        super().__init__(con)
        self.seen: list[tuple[int, int]] = []
        self.workers_made = 0
        self._lock = threading.Lock()

    def for_worker(self):
        with self._lock:
            self.workers_made += 1
        w = super().for_worker()
        outer = self

        class _W(type(w)):
            def query(self, sql):
                with outer._lock:
                    outer.seen.append((threading.get_ident(), id(self._cursor)))
                return super().query(sql)
        return _W(self, w._cursor)


@pytest.mark.parametrize("fixture", ["messy_mart", "fan_trap", "multi_tenant"])
def test_link_output_identical_at_any_worker_count(fixture):
    docs = []
    for workers in (1, 4):
        con = _build(fixture)
        try:
            src = DuckDBSource(con)
            doc, stats = profile_with_stats(src)
            link_source(src, doc, stats, use_declared=False, query_workers=workers)
        finally:
            con.close()
        docs.append(json.dumps(doc, sort_keys=True))
    assert docs[0] == docs[1]


def test_workers_use_their_own_cursors():
    con = _build("messy_mart")
    try:
        src = _Counting(con)
        doc, stats = profile_with_stats(src)
        cands = generate_candidates(src, stats, doc, query_workers=4)
    finally:
        con.close()
    assert cands, "fixture produced no candidates"
    threads = {t for t, _ in src.seen}
    cursors = {c for _, c in src.seen}
    assert len(threads) > 1, "pool of one"
    assert len(cursors) == len(threads), "a cursor was shared across threads"
    assert src.workers_made == len(threads)


def test_a_source_without_for_worker_stays_serial():
    class _Bare:
        def __init__(self, inner):
            self._inner = inner
            self.threads: set[int] = set()

        def list_tables(self):
            return self._inner.list_tables()

        def query(self, sql):
            self.threads.add(threading.get_ident())
            return self._inner.query(sql)

    con = _build("fan_trap")
    try:
        bare = _Bare(DuckDBSource(con))
        doc, stats = profile_with_stats(bare)
        generate_candidates(bare, stats, doc, query_workers=4)
    finally:
        con.close()
    assert bare.threads == {threading.get_ident()}


def test_a_dead_worker_fails_the_run():
    class _Dying(DuckDBSource):
        def for_worker(self):
            w = super().for_worker()

            class _W(type(w)):
                def query(self, sql):
                    raise RuntimeError("warehouse went away")
            return _W(self, w._cursor)

    con = _build("fan_trap")
    try:
        src = _Dying(con)
        doc, stats = profile_with_stats(src)
        with pytest.raises(RuntimeError, match="went away"):
            generate_candidates(src, stats, doc, query_workers=4)
    finally:
        con.close()
