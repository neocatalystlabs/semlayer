"""dbt semantic layer exporter (v1 target: dbt semantic_models + metrics YAML).

Lossy fields are REPORTED, never silently dropped (SPEC.md §5.6): confidence/
provenance/lifecycle, hierarchies, aggregate routing, and repo knowledge have
no dbt primitives — they land in the loss report and as `meta:` where safe.
"""

from __future__ import annotations

_AGG_MAP = {"sum": "sum", "avg": "average", "min": "min", "max": "max",
            "count": "count", "count_distinct": "count_distinct",
            "median": "median", "percentile": "percentile"}


def _export_column(c: dict, pk: set[str]) -> tuple[str, dict]:
    """Classify one column into a dbt entity/dimension/measure field.

    Returns (bucket, field) where bucket is "entity", "dimension", "measure",
    or "" for a column that maps to no dbt primitive.
    """
    role = c.get("entity_role")
    if c["name"] in pk:
        return "entity", {"name": c["name"], "type": "primary"}
    fk_types = {"foreign_key": "foreign", "unique_key": "unique", "natural_key": "natural"}
    fk_type = fk_types.get(role or "")
    if fk_type is not None:
        return "entity", {"name": c["name"], "type": fk_type}
    if c.get("semantic_type") in ("date", "timestamp_event", "timestamp_effective"):
        return "dimension", {"name": c["name"], "type": "time",
                             "type_params": {"time_granularity": "day"}}
    if role == "measure" and "aggregations" in c:
        agg = _AGG_MAP.get(c["aggregations"].get("default", "sum"), "sum")
        m = {"name": c["name"], "agg": agg}
        if c.get("description"):
            m["description"] = c["description"]
        return "measure", m
    if role in ("dimension", "metadata"):
        d = {"name": c["name"], "type": "categorical"}
        if c.get("description"):
            d["description"] = c["description"]
        return "dimension", d
    return "", {}


def _export_table(t: dict) -> dict:
    """Build one dbt semantic_models entry for an active (non-deprecated) table."""
    entities, dims, measures = [], [], []
    pk = set(t.get("primary_key") or [])
    for c in t["columns"]:
        bucket, field = _export_column(c, pk)
        if bucket == "entity":
            entities.append(field)
        elif bucket == "dimension":
            dims.append(field)
        elif bucket == "measure":
            measures.append(field)
    agg_time_dim = next((d["name"] for d in dims if d.get("type") == "time"), None)
    sm = {
        "name": t["name"],
        "model": f"ref('{t['name']}')",
        "description": t.get("description", ""),
        "defaults": {"agg_time_dimension": agg_time_dim},
        "entities": entities, "dimensions": dims, "measures": measures,
        "meta": {"semlayer": {"table_type": t.get("table_type"),
                              "lifecycle": t.get("lifecycle", "inferred")}},
    }
    if sm["defaults"]["agg_time_dimension"] is None:
        del sm["defaults"]
    return sm


def _export_tables(sl: dict, losses: list[str]) -> list[dict]:
    """Export every non-deprecated, non-orphaned table; record the rest as losses."""
    semantic_models = []
    for t in sl["tables"]:
        if t.get("lifecycle") in ("deprecated", "orphaned"):
            losses.append(f"table {t['name']}: {t.get('lifecycle')} — not exported")
            continue
        semantic_models.append(_export_table(t))
    return semantic_models


def _export_metrics(sl: dict, losses: list[str]) -> list[dict]:
    """Export simple + ratio metrics (MetricFlow shapes); other types are losses."""
    metrics = []
    for m in sl.get("metrics", []):
        if m.get("type") == "simple":
            entry = {"name": m["name"], "type": "simple",
                     "label": m.get("display_name", m["name"]),
                     "type_params": {"measure": m["measure"].split(".", 1)[1]}}
        elif m.get("type") == "ratio":
            entry = {"name": m["name"], "type": "ratio",
                     "label": m.get("display_name", m["name"]),
                     "type_params": {
                         "numerator": m["numerator"].split(".", 1)[1],
                         "denominator": m["denominator"].split(".", 1)[1]}}
        else:
            losses.append(f"metric {m['name']}: type {m.get('type')} not exported "
                          "(exports simple + ratio)")
            continue
        if m.get("filter"):
            entry["filter"] = m["filter"]
        metrics.append(entry)
    return metrics


def _collect_losses(sl: dict, losses: list[str]) -> None:
    """Record semlayer sections with no dbt primitive.

    SPEC.md §5.6: report, never drop silently.
    """
    sections = [
        ("hierarchies", "hierarchies"),
        ("aggregate_tables", "aggregate routing"),
        ("repo_knowledge", "repo knowledge"),
    ]
    for section, label in sections:
        if sl.get(section):
            losses.append(f"{label}: no dbt primitive — retained only in the semlayer document")
    losses.append("confidence/provenance: no dbt primitive — lifecycle carried in meta.semlayer")


def export_dbt(doc: dict) -> tuple[dict, list[str]]:
    """Export a semlayer document to dbt semantic_models + metrics YAML shapes."""
    sl = doc["semantic_layer"]
    losses: list[str] = []
    semantic_models = _export_tables(sl, losses)
    metrics = _export_metrics(sl, losses)
    _collect_losses(sl, losses)
    return {"semantic_models": semantic_models, "metrics": metrics}, losses
