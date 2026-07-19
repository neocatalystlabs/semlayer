"""Spec and gold-file validation tests (test plan Layer 1)."""

import copy
import json
from pathlib import Path

import jsonschema
import pytest
import yaml

from semlayer.validate import validate_document, validate_file

OSS = Path(__file__).resolve().parent.parent
SCHEMA_PATH = OSS / "spec" / "semantic-layer.schema.json"
GOLDS = sorted((OSS / "fixtures" / "golds").glob("*.yaml"))


def test_schema_is_valid_draft_2020_12():
    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize("gold", GOLDS, ids=[g.stem for g in GOLDS])
def test_gold_validates(gold):
    result = validate_file(gold)
    assert result.ok, "\n".join(result.errors)


def _load_fan_trap() -> dict:
    return yaml.safe_load((OSS / "fixtures" / "golds" / "fan_trap.yaml").read_text())


def test_unknown_fk_target_rejected():
    doc = _load_fan_trap()
    doc["semantic_layer"]["tables"][1]["columns"][1]["foreign_key"]["references"] = "nonexistent.column"
    result = validate_document(doc)
    assert not result.ok
    assert any("unknown column reference" in e for e in result.errors)


def test_undeclared_hierarchy_level_rejected():
    """The level vocabulary is closed (SPEC.md 2.5)."""
    doc = _load_fan_trap()
    doc["semantic_layer"]["metrics"][0]["dimensions"].append("order_time.fortnight")
    result = validate_document(doc)
    assert not result.ok
    assert any("not a declared level" in e for e in result.errors)


def test_metric_on_unmodeled_measure_rejected():
    """Metrics may only reference modeled measures (SPEC.md 3.1)."""
    doc = _load_fan_trap()
    # customers.name has no aggregations block
    doc["semantic_layer"]["metrics"].append(
        {"name": "bad_metric", "type": "simple", "measure": "customers.name", "agg": "count"}
    )
    result = validate_document(doc)
    assert not result.ok
    assert any("no aggregations block" in e for e in result.errors)


def test_heuristic_aggregate_cannot_be_verified_routing():
    doc = _load_fan_trap()
    doc["semantic_layer"]["aggregate_tables"] = [
        {
            "table": "orders",
            "aggregates": "payments",
            "mapping_source": "heuristic",
            "routing": {"rule": "always", "status": "verified"},
        }
    ]
    result = validate_document(doc)
    assert not result.ok
    assert any("heuristic mappings cannot have verified routing" in e for e in result.errors)


def test_invalid_lifecycle_rejected():
    doc = _load_fan_trap()
    doc["semantic_layer"]["tables"][0]["lifecycle"] = "blessed"
    result = validate_document(doc)
    assert not result.ok


def test_llm_guess_enum_produces_warning():
    doc = _load_fan_trap()
    col = doc["semantic_layer"]["tables"][2]["columns"][3]  # payments.method
    col["enum_values"][0]["decode_source"] = "llm_guess"
    result = validate_document(doc)
    assert result.ok
    assert any("LLM guess" in w for w in result.warnings)


def test_unknown_top_level_key_rejected():
    doc = _load_fan_trap()
    doc["semantic_layer"]["surprise_field"] = True
    result = validate_document(doc)
    assert not result.ok


def test_ratio_metric_requires_declared_parts():
    doc = _load_fan_trap()
    doc["semantic_layer"]["metrics"].append(
        {"name": "bad_ratio", "type": "ratio", "numerator": "total_revenue", "denominator": "ghost_metric"}
    )
    result = validate_document(doc)
    assert not result.ok
    assert any("ghost_metric" in e for e in result.errors)


def test_deep_copy_of_valid_doc_still_valid():
    doc = copy.deepcopy(_load_fan_trap())
    assert validate_document(doc).ok


def test_packaged_schemas_match_canonical():
    """The wheel ships copies of the spec schemas; they must never drift
    from the canonical repo-root spec/ files."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for name in ("semantic-layer.schema.json", "cq-suite.schema.json"):
        canonical = (root / "spec" / name).read_bytes()
        packaged = (root / "src" / "semlayer" / "spec" / name).read_bytes()
        assert canonical == packaged, f"{name}: run cp spec/{name} src/semlayer/spec/"
