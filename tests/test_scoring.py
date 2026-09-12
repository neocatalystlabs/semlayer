"""Benchmark scoring: projection-tolerant row matching must stay cheap on wide results."""

import time

from semlayer.cq.answer import _rows_match, _score_scalar


def test_extra_columns_and_reordering_are_tolerated():
    expected = [("CALL", 10), ("STORE", 20)]
    got = [(10, "Call Center", "CALL", "x"), (20, "Physical Store", "STORE", "y")]
    assert _rows_match(got, expected)


def test_missing_column_or_row_is_not_tolerated():
    assert not _rows_match([("CALL",), ("STORE",)], [("CALL", 10), ("STORE", 20)])
    assert not _rows_match([("CALL", 10)], [("CALL", 10), ("STORE", 20)])


def test_labels_resolve_to_codes():
    labels = {"Call Center": "CALL"}
    assert _rows_match([("Call Center", 10)], [("CALL", 10)], labels)


def test_wide_result_does_not_explode():
    expected = [(i, i * 2.0, f"k{i}", i % 3, i % 5, i % 7, i % 11) for i in range(300)]
    got = [(f"noise{i}", i % 3, i, i % 11, i * 2.0, i % 5, "z", i % 7, f"k{i}", i)
           for i in range(300)]
    t = time.perf_counter()
    assert _rows_match(got, expected)
    assert time.perf_counter() - t < 2.0


def test_scalar_any_column_single_row():
    ok, _ = _score_scalar([("total", 14621503.3)], [(14621503.3,)], 0.01)
    assert ok
    ok, _ = _score_scalar([(1,), (2,)], [(1,)], 0.01)
    assert not ok


def test_query_watchdog_interrupts_runaway_sql(monkeypatch):
    import duckdb

    from semlayer.cq import answer
    monkeypatch.setattr(answer, "QUERY_TIMEOUT_S", 1.0)
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE t AS SELECT range AS i FROM range(2000000)")
    t = time.perf_counter()
    try:
        answer._execute(con, "SELECT count(*) FROM t a, t b, t c")
        raise AssertionError("runaway query was not interrupted")
    except Exception as e:  # duckdb raises its own InterruptException
        assert "nterrupt" in str(e)
    assert time.perf_counter() - t < 10
    assert answer._execute(con, "SELECT count(*) FROM t") == [(2000000,)]
