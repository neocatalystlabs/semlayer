"""Merging a partial re-inference into a committed layer.

The cases that matter are the ones where merging wrongly is invisible in a
large diff: a certified description quietly replaced, a column outside the
scope quietly dropped, a reviewed flag quietly reset to `inferred`.
"""

from __future__ import annotations

import copy

from semlayer.merge import merge_layer, render_report


def _doc(tables):
    return {"semantic_layer": {"spec_version": "0.4", "name": "t", "tables": tables}}


def _col(name, **kw):
    c = {"name": name, "sql_type": "VARCHAR"}
    c.update(kw)
    return c


def _tab(name, cols, **kw):
    t = {"name": name, "source": {"table": name}, "columns": cols}
    t.update(kw)
    return t


BASE = _doc([
    _tab("ord_hdr", [
        _col("ord_id", description="order id", lifecycle="certified"),
        _col("promo_id", description="the promotion applied", lifecycle="inferred"),
        _col("tot_amt", description="total", lifecycle="reviewed"),
    ]),
    _tab("cust_mstr", [_col("cust_id", description="customer id")]),
])


def test_untouched_outside_scope():
    """A partial layer's silence about a table is not evidence about it."""
    base = copy.deepcopy(BASE)
    incoming = _doc([_tab("ord_hdr", [_col("ord_id"), _col("promo_id"), _col("tot_amt")])])
    merge_layer(base, incoming)
    def cust(doc):
        return next(t for t in doc["semantic_layer"]["tables"]
                    if t["name"] == "cust_mstr")

    assert cust(base) == cust(BASE)


def test_certified_content_survives_disagreement():
    base = copy.deepcopy(BASE)
    incoming = _doc([_tab("ord_hdr", [
        _col("ord_id", description="SOMETHING ELSE ENTIRELY"),
    ])])
    r = merge_layer(base, incoming, scope={"ord_hdr"})
    col = _find(base, "ord_hdr", "ord_id")
    assert col["description"] == "order id"          # kept
    assert col["lifecycle"] == "certified"           # not reset
    assert "ord_hdr.ord_id" in r.protected
    assert "ord_hdr.ord_id" in r.conflicts
    assert col["conflicts"][0]["detail"].startswith("re-inference disagrees")


def test_reviewed_content_survives_too():
    base = copy.deepcopy(BASE)
    incoming = _doc([_tab("ord_hdr", [_col("tot_amt", description="guessed")])])
    merge_layer(base, incoming, scope={"ord_hdr"})
    assert _find(base, "ord_hdr", "tot_amt")["description"] == "total"


def test_inferred_content_is_replaced():
    base = copy.deepcopy(BASE)
    incoming = _doc([_tab("ord_hdr", [
        _col("promo_id", description="a better description", confidence=0.9),
    ])])
    r = merge_layer(base, incoming, scope={"ord_hdr"})
    col = _find(base, "ord_hdr", "promo_id")
    assert col["description"] == "a better description"
    assert col["confidence"] == 0.9
    assert "ord_hdr.promo_id" in r.columns_updated


def test_missing_column_is_orphaned_not_deleted():
    """Existence is the warehouse's call; the description is still ours to keep."""
    base = copy.deepcopy(BASE)
    incoming = _doc([_tab("ord_hdr", [_col("ord_id"), _col("tot_amt")])])
    r = merge_layer(base, incoming, scope={"ord_hdr"})
    col = _find(base, "ord_hdr", "promo_id")
    assert col is not None, "a dropped column must not be deleted"
    assert col["lifecycle"] == "orphaned"
    assert col["description"] == "the promotion applied"
    assert "ord_hdr.promo_id" in r.orphaned


def test_new_table_and_column_are_added():
    base = copy.deepcopy(BASE)
    incoming = _doc([
        _tab("ord_hdr", [_col("ord_id"), _col("promo_id"), _col("tot_amt"),
                         _col("fx_rate", description="fx")]),
        _tab("ord_hdr_v2", [_col("ord_id")]),
    ])
    r = merge_layer(base, incoming)
    assert r.tables_added == ["ord_hdr_v2"]
    assert "ord_hdr.fx_rate" in r.columns_added
    assert len(base["semantic_layer"]["tables"]) == 3


def test_relationship_added_once_and_not_duplicated():
    base = copy.deepcopy(BASE)
    rel = {"name": "ord_hdr_to_cust", "from": {"table": "ord_hdr", "columns": ["cust_id"]},
           "to": {"table": "cust_mstr", "columns": ["cust_id"]}}
    incoming = _doc([_tab("ord_hdr", [_col("ord_id"), _col("promo_id"), _col("tot_amt")])])
    incoming["semantic_layer"]["relationships"] = [rel]
    merge_layer(base, incoming, scope={"ord_hdr"})
    merge_layer(base, copy.deepcopy(incoming), scope={"ord_hdr"})
    assert len(base["semantic_layer"]["relationships"]) == 1


def test_merge_is_idempotent():
    """Running the same re-inference twice produces no second diff."""
    base = copy.deepcopy(BASE)
    incoming = _doc([_tab("ord_hdr", [
        _col("ord_id"), _col("promo_id", description="new"), _col("tot_amt"),
    ])])
    merge_layer(base, copy.deepcopy(incoming), scope={"ord_hdr"})
    after_first = copy.deepcopy(base)
    r2 = merge_layer(base, copy.deepcopy(incoming), scope={"ord_hdr"})
    assert base == after_first, "a second identical merge must change nothing"
    assert r2.touched == 0


def test_report_renders_only_what_happened():
    base = copy.deepcopy(BASE)
    incoming = _doc([_tab("ord_hdr", [_col("ord_id"), _col("tot_amt"),
                                      _col("fx_rate", description="fx")])])
    text = render_report(merge_layer(base, incoming, scope={"ord_hdr"}))
    assert "ord_hdr.fx_rate" in text
    assert "ord_hdr.promo_id" in text          # orphaned
    assert "Metrics added" not in text         # nothing happened there


def _find(doc, table, column):
    for t in doc["semantic_layer"]["tables"]:
        if t["name"] == table:
            for c in t["columns"]:
                if c["name"] == column:
                    return c
    return None


def test_fk_to_out_of_scope_table_is_not_demoted():
    """`infer --only` cannot see the dimension, so its silence is not evidence.

    Without this rule a targeted re-inference deletes the join graph of the
    tables it re-inferred -- measured on messy_mart: 8 of 14 ord_hdr columns
    lost `entity_role: foreign_key` and their inclusion-dependency provenance.
    """
    base = _doc([_tab("ord_hdr", [
        _col("cust_id", entity_role="foreign_key", confidence=0.84,
             foreign_key={"references": "cust_mstr.cust_id",
                          "relationship": "many_to_one"},
             provenance=[{"signal": "inclusion_dependency", "detail": "ratio 1.0"}]),
    ])])
    # what Link produces when it only looked at ord_hdr
    incoming = _doc([_tab("ord_hdr", [
        _col("cust_id", entity_role="natural_key", confidence=0.7,
             provenance=[{"signal": "naming", "detail": "id-like name"}]),
    ])])
    r = merge_layer(base, incoming, scope={"ord_hdr"})
    col = _find(base, "ord_hdr", "cust_id")
    assert col["foreign_key"]["references"] == "cust_mstr.cust_id"
    assert col["entity_role"] == "foreign_key"
    assert col["confidence"] == 0.84
    assert col["provenance"][0]["signal"] == "inclusion_dependency"
    assert "ord_hdr.cust_id" in r.preserved


def test_fk_inside_scope_is_still_updated():
    """Both ends profiled: the new inference IS the better evidence."""
    base = _doc([
        _tab("ord_hdr", [_col("cust_id", entity_role="foreign_key", confidence=0.6,
                              foreign_key={"references": "cust_mstr.cust_id"})]),
        _tab("cust_mstr", [_col("cust_id")]),
    ])
    incoming = _doc([
        _tab("ord_hdr", [_col("cust_id", entity_role="foreign_key", confidence=0.95,
                              foreign_key={"references": "cust_mstr.cust_id"})]),
        _tab("cust_mstr", [_col("cust_id")]),
    ])
    r = merge_layer(base, incoming, scope={"ord_hdr", "cust_mstr"})
    assert _find(base, "ord_hdr", "cust_id")["confidence"] == 0.95
    assert "ord_hdr.cust_id" not in r.preserved
