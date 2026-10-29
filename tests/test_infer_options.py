"""InferOptions: the settings object and the keyword compatibility shim."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import duckdb
import pytest

OSS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OSS / "fixtures"))

from semlayer.errors import SemlayerError  # noqa: E402
from semlayer.pipeline import InferOptions, infer, infer_with_report  # noqa: E402
from semlayer.source import DuckDBSource  # noqa: E402


def _con(fixture: str = "fan_trap"):
    import importlib
    con = duckdb.connect(":memory:")
    importlib.import_module(f"generators.{fixture}").build(con)
    return con


def test_defaults_are_the_published_serial_behaviour():
    o = InferOptions()
    assert (o.no_sample_egress, o.only, o.use_declared, o.llm_workers, o.query_workers) == (
        False, None, True, 1, 1)


def test_object_and_keywords_produce_the_same_document():
    con = _con()
    try:
        src = DuckDBSource(con)
        a = infer(src, options=InferOptions(use_declared=False, query_workers=4))
        b = infer(src, use_declared=False, query_workers=4)
    finally:
        con.close()
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_report_records_the_options():
    con = _con()
    try:
        opts = InferOptions(only={"orders"}, use_declared=False, llm_workers=3)
        _, report = infer_with_report(DuckDBSource(con), options=opts)
    finally:
        con.close()
    assert report["options"] == {"no_sample_egress": False, "only": ["orders"],
                                 "use_declared": False, "llm_workers": 3, "query_workers": 1}
    json.dumps(report)        # the report must stay serialisable


def test_only_is_normalised_to_a_frozenset():
    o = InferOptions(only=["b", "a"])
    assert o.only == frozenset({"a", "b"})
    assert isinstance(o.only, frozenset)
    assert replace(o, query_workers=8).only == o.only


@pytest.mark.parametrize("bad", [0, -1, 2.0, True, "4"])
def test_worker_counts_below_one_are_refused(bad):
    with pytest.raises(SemlayerError, match="whole number of at least 1"):
        InferOptions(llm_workers=bad)
    with pytest.raises(SemlayerError, match="whole number of at least 1"):
        InferOptions(query_workers=bad)


def test_mixing_object_and_keywords_is_an_error():
    con = _con()
    try:
        with pytest.raises(TypeError, match="not both"):
            infer(DuckDBSource(con), options=InferOptions(), query_workers=2)
    finally:
        con.close()


def test_unknown_keyword_is_still_a_type_error():
    con = _con()
    try:
        with pytest.raises(TypeError, match=r"unexpected keyword.*workres"):
            infer(DuckDBSource(con), workres=2)
    finally:
        con.close()


def test_query_workers_none_is_decided_by_the_source():
    """None means "the source decides": every core for in-process DuckDB, 4 elsewhere."""
    import os

    import duckdb

    from semlayer.pipeline import default_query_workers
    from semlayer.source import DuckDBSource

    assert InferOptions(query_workers=None).query_workers is None
    assert default_query_workers(DuckDBSource(duckdb.connect())) == max(1, os.cpu_count() or 1)

    class _Network:
        pass
    assert default_query_workers(_Network()) == 4
