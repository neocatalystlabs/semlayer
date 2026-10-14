"""ETL lineage reading, and the parent choice it is there to settle.

The unit tests build a manifest in memory, so they run in CI with no dbt and
no external dataset. The integration test needs `dbt_synthea`, which is
gitignored and built by its own ETL, so it skips unless someone has fetched it.
"""

from __future__ import annotations

import json

import pytest

from semlayer.errors import SemlayerError
from semlayer.lineage import LineageBundle, load_dbt_manifest
from semlayer.link.candidates import FKCandidate
from semlayer.link.run import _best_parent_per_child

EXT = "fixtures/external/data/dbt_synthea"


def _node(name, deps=(), materialized="table", rtype="model", alias=None):
    return {
        "resource_type": rtype,
        "name": name,
        "alias": alias,
        "config": {"materialized": materialized},
        "depends_on": {"nodes": list(deps)},
    }


# The shape that motivated all of this: a published table, the staging step it
# is built from, and a fact table downstream of that staging step.
MANIFEST = {
    "nodes": {
        "model.p.stg_patients": _node("stg_patients"),
        "model.p.int__person": _node("int__person", ["model.p.stg_patients"]),
        "model.p.person": _node("person", ["model.p.int__person"]),
        "model.p.observation_period": _node("observation_period", ["model.p.int__person"]),
        "seed.p.states": _node("states", rtype="seed"),
        "test.p.whatever": {"resource_type": "test", "name": "t"},
    },
    "sources": {"source.p.raw.pat": {"identifier": "raw_patients"}},
}


@pytest.fixture
def bundle(tmp_path):
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(MANIFEST))
    return load_dbt_manifest(p)


def test_reads_models_seeds_and_sources_but_not_tests(bundle):
    assert "person" in bundle.parents
    assert "states" in bundle.parents, "seeds are queryable, so they are join targets"
    assert "t" not in bundle.parents, "a test is not a relation"
    assert "raw_patients" in bundle.node_table.values()


def test_transitive_ancestors(bundle):
    assert bundle.ancestors("person") == {"int__person", "stg_patients"}
    assert bundle.ancestors("stg_patients") == set()


def test_is_built_from_is_directional(bundle):
    assert bundle.is_built_from("person", "int__person")
    assert bundle.is_built_from("person", "stg_patients"), "transitive, not just direct"
    assert not bundle.is_built_from("int__person", "person")


def test_prefer_picks_the_downstream_table_either_way_round(bundle):
    assert bundle.prefer("person", "int__person") == "person"
    assert bundle.prefer("int__person", "person") == "person"


def test_prefer_declines_on_unrelated_tables(bundle):
    # person and observation_period share an ancestor but neither builds the
    # other. Declining is what keeps a manifest from reordering candidates it
    # has no opinion about.
    assert bundle.prefer("person", "observation_period") is None


def test_alias_wins_over_model_name(tmp_path):
    """The warehouse shows us the alias, so that is what table names match."""
    doc = {"nodes": {"model.p.m": _node("model_file_name", alias="actual_table")},
           "sources": {}}
    p = tmp_path / "m.json"
    p.write_text(json.dumps(doc))
    b = load_dbt_manifest(p)
    assert "actual_table" in b.parents
    assert "model_file_name" not in b.parents


def test_empty_bundle_is_falsey_so_callers_can_skip():
    assert not LineageBundle()
    assert LineageBundle(parents={"a": set()})


def test_cycle_in_a_malformed_manifest_does_not_hang(tmp_path):
    doc = {"nodes": {"model.a": _node("a", ["model.b"]), "model.b": _node("b", ["model.a"])}}
    p = tmp_path / "cyc.json"
    p.write_text(json.dumps(doc))
    b = load_dbt_manifest(p)
    assert b.ancestors("a") == {"b"}


@pytest.mark.parametrize("body,needle", [
    ("not json at all", "cannot read"),
    ('{"catalog": {}}', "no `nodes`"),
])
def test_unusable_manifest_fails_loudly(tmp_path, body, needle):
    p = tmp_path / "bad.json"
    p.write_text(body)
    with pytest.raises(SemlayerError) as e:
        load_dbt_manifest(p)
    assert needle in str(e.value)


def test_missing_manifest_fails_loudly(tmp_path):
    with pytest.raises(SemlayerError):
        load_dbt_manifest(tmp_path / "nope.json")


# --- the parent choice -----------------------------------------------------

def _cand(child, col, parent, pcol, name_score=0.8, incl=1.0):
    return FKCandidate(child, col, parent, pcol, incl, 10, 10, name_score)


PKS = {
    "person": ["person_id"],
    "int__person": ["person_id"],
    "observation_period": ["observation_period_id"],
}


def test_without_lineage_the_tie_is_left_exactly_as_it_was(bundle):
    """No manifest must mean no behaviour change -- messy_mart depends on it."""
    cands = [_cand("measurement", "person_id", "int__person", "person_id"),
             _cand("measurement", "person_id", "person", "person_id")]
    best, wins = _best_parent_per_child(cands, PKS, lineage=None)
    assert best[("measurement", "person_id")].parent_table == "int__person", \
        "first-seen wins, which is the pre-existing behaviour"
    assert wins == set()


def test_lineage_prefers_the_published_table_over_its_staging_step(bundle):
    cands = [_cand("measurement", "person_id", "int__person", "person_id"),
             _cand("measurement", "person_id", "person", "person_id")]
    best, wins = _best_parent_per_child(cands, PKS, lineage=bundle)
    assert best[("measurement", "person_id")].parent_table == "person"
    assert ("measurement", "person_id", "person", "person_id") in wins


def test_order_of_candidates_does_not_change_the_answer(bundle):
    a = _cand("measurement", "person_id", "person", "person_id")
    b = _cand("measurement", "person_id", "int__person", "person_id")
    for pair in ([a, b], [b, a]):
        best, _ = _best_parent_per_child(pair, PKS, lineage=bundle)
        assert best[("measurement", "person_id")].parent_table == "person"


def test_pk_guard_stops_lineage_walking_onto_a_fact_table(bundle):
    """observation_period is downstream of int__person but is NOT the entity.

    Without this guard the lineage rule moved 26 person_id edges onto it,
    because `person_id` is merely a column there -- the table's own key is
    `observation_period_id`.
    """
    cands = [_cand("measurement", "person_id", "int__person", "person_id"),
             _cand("measurement", "person_id", "observation_period", "person_id")]
    best, _ = _best_parent_per_child(cands, PKS, lineage=bundle)
    assert best[("measurement", "person_id")].parent_table == "int__person"


def test_lineage_never_overrides_a_stronger_naming_signal(bundle):
    """A manifest breaks ties; it does not outvote evidence."""
    cands = [_cand("measurement", "person_id", "person", "person_id", name_score=0.3),
             _cand("measurement", "person_id", "int__person", "person_id", name_score=0.9)]
    best, wins = _best_parent_per_child(cands, PKS, lineage=bundle)
    assert best[("measurement", "person_id")].parent_table == "int__person"
    assert wins == set()


# --- integration, only if the fixture has been fetched ---------------------

@pytest.mark.skipif(not __import__("pathlib").Path(f"{EXT}/manifest.json").exists(),
                    reason="dbt_synthea not fetched (fixtures/external/fetch_dbt_synthea.py)")
def test_real_manifest_resolves_the_person_pair():
    b = load_dbt_manifest(f"{EXT}/manifest.json")
    assert b.is_built_from("person", "int__person")
    assert b.prefer("person", "int__person") == "person"
    assert b.prefer("person", "observation_period") is None


# --- documentation and metrics from the same manifest ---------------------
#
# 447 column descriptions sat in the dbt_synthea manifest -- a file we already
# open and parse for the DAG -- and every one was thrown away.

DOCS_MANIFEST = {
    "nodes": {
        "model.p.orders": {
            "resource_type": "model", "name": "orders", "alias": None,
            "config": {"materialized": "table"}, "depends_on": {"nodes": []},
            "description": "  One row per customer order.  ",
            "columns": {
                "order_id": {"description": "Surrogate key."},
                "amount": {"description": "  Gross, excluding tax.  "},
                "blank": {"description": "   "},
                "nodesc": {},
            },
        },
        "model.p.undocumented": {
            "resource_type": "model", "name": "undocumented", "alias": None,
            "config": {"materialized": "view"}, "depends_on": {"nodes": []},
        },
    },
    "sources": {},
    "semantic_models": {
        "semantic_model.p.orders": {
            "model": "ref('orders')",
            "node_relation": {"alias": "orders", "schema_name": "mart"},
            "measures": [
                {"name": "order_total", "agg": "sum", "expr": "amount"},
                {"name": "order_count", "agg": "count_distinct", "expr": "order_id"},
            ],
        }
    },
    "metrics": {
        "metric.p.revenue": {
            "name": "revenue", "label": "Revenue", "type": "simple",
            "description": "Gross revenue, cancellations excluded.",
            "type_params": {"measure": {"name": "order_total"}},
            "filter": {"where_filters": [
                {"where_sql_template": "{{ Dimension('status') }} <> 'cancelled'"}]},
        },
        "metric.p.orders_placed": {
            "name": "orders_placed", "type": "simple",
            "type_params": {"measure": "order_count"},
        },
        "metric.p.nameless": {"type": "simple"},     # malformed: must be skipped
    },
}


@pytest.fixture
def docs(tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps(DOCS_MANIFEST))
    return load_dbt_manifest(p)


def test_table_and_column_descriptions_are_read_and_trimmed(docs):
    assert docs.table_descriptions["orders"] == "One row per customer order."
    assert docs.column_descriptions[("orders", "order_id")] == "Surrogate key."
    assert docs.column_descriptions[("orders", "amount")] == "Gross, excluding tax."


def test_blank_and_absent_descriptions_are_not_recorded(docs):
    """A whitespace-only description is not documentation."""
    assert ("orders", "blank") not in docs.column_descriptions
    assert ("orders", "nodesc") not in docs.column_descriptions
    assert "undocumented" not in docs.table_descriptions


def test_metrics_are_read_with_their_measure_and_aggregation(docs):
    by = {m["name"]: m for m in docs.metrics}
    assert set(by) == {"revenue", "orders_placed"}, "the nameless one is skipped"
    r = by["revenue"]
    assert r["label"] == "Revenue"
    assert r["type"] == "simple"
    assert r["measure"] == "order_total"
    # the aggregation lives on the SEMANTIC MODEL, not the metric
    assert r["agg"] == "sum"
    assert r["expr"] == "amount"
    assert r["table"] == "orders"


def test_a_metric_filter_is_flattened_from_where_filters(docs):
    by = {m["name"]: m for m in docs.metrics}
    assert "cancelled" in by["revenue"]["filter"]
    assert by["orders_placed"]["filter"] is None


def test_a_measure_given_as_a_bare_string_still_resolves(docs):
    by = {m["name"]: m for m in docs.metrics}
    assert by["orders_placed"]["measure"] == "order_count"
    assert by["orders_placed"]["agg"] == "count_distinct"


def test_a_project_with_no_metrics_yields_an_empty_tuple(bundle):
    assert bundle.metrics == ()


@pytest.mark.skipif(not __import__("pathlib").Path(f"{EXT}/manifest.json").exists(),
                    reason="dbt_synthea not fetched")
def test_real_manifest_documentation_is_read():
    b = load_dbt_manifest(f"{EXT}/manifest.json")
    assert len(b.table_descriptions) == 56
    assert len(b.column_descriptions) == 447
    assert b.metrics == (), "this project defines none, and that is stated"
