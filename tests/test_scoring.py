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


def test_table_type_is_scored_and_reported_directionally():
    """table_type must be in the Score, and its two error directions separated.

    It was unscored until 2026-10-11 and computed only by
    fixtures/coverage_audit.py, so a classifier sitting at 0.65 corpus-wide
    never appeared in any gate. The directions are not equally costly: a
    dimension called a fact produces spurious metrics that land in the review
    queue for a human to reject, while a fact called a dimension produces
    nothing and silently drops the table out of aggregate reconciliation.
    """
    from semlayer.scoring import score_types

    def doc(types):
        return {"semantic_layer": {"tables": [
            {"name": n, "table_type": t, "columns": [
                {"name": "c", "semantic_type": "identifier", "entity_role": "primary_key"}]}
            for n, t in types.items()]}}

    gold = doc({"a": "dimension", "b": "fact", "c": "dimension"})
    draft = doc({"a": "fact", "b": "dimension", "c": "dimension"})
    s = score_types(draft, gold)
    assert s.n_tables == 3
    assert s.table_type_accuracy == 1 / 3
    assert s.dim_called_fact == 1
    assert s.fact_called_dim == 1
    assert len(s.table_type_confusions) == 2


def test_strict_role_accuracy_exposes_key_confusions_leniency_credits():
    """The lenient role metric credits primary/unique/natural key confusions.

    Reasonable while nothing emitted `natural_key`, and misleading once
    something did: on tpcds_clean the lenient number moved +0.009 when the
    detector landed while the strict number moved +0.054, because the leniency
    had been crediting the very confusion the fix removed. Reported alongside
    rather than replacing, so no published number moves.
    """
    from semlayer.scoring import score_types

    def doc(role):
        return {"semantic_layer": {"tables": [{"name": "t", "columns": [
            {"name": "c", "semantic_type": "identifier", "entity_role": role}]}]}}

    gold = doc("natural_key")
    adjacent = score_types(doc("unique_key"), gold)
    assert adjacent.role_accuracy == 1.0        # credited as adjacent
    assert adjacent.role_accuracy_strict == 0.0  # and visibly not exact

    exact = score_types(doc("natural_key"), gold)
    assert exact.role_accuracy == exact.role_accuracy_strict == 1.0
