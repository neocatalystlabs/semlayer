"""ETL lineage as inference input: read how the tables were authored.

The warehouse says what the data IS. The ETL that built it says what it MEANS,
and says so in a form a human wrote and a machine can read. This module turns
a dbt `manifest.json` into a neutral bundle the inference stages can consult.

It is deliberately NOT a dbt integration. The bundle exposes only questions a
stage actually asks -- "is this table built from that one?" -- so a future
reader for Spark, Airflow SQL or a view-definition crawler can populate the
same shape without the stages changing.

Why dbt's manifest and not the .sql files: `compiled_code` has already had
`ref()` and every macro resolved by dbt itself, and `depends_on` is the
resolved DAG. We do not write a Jinja evaluator or a reference resolver, and
we inherit dbt's answer rather than competing with it.

Scope here is ONE question, because it is the one with no attribution
ambiguity. A filter in a model might be a rule about the table or might be
that model's own business, and telling those apart needs evidence we do not
have yet. But "x is built from y" is a fact about the warehouse's shape, and
it settles a choice statistics provably cannot -- see
docs/calibration.md 2026-10-12: `person` and `int__person` carry identical
key sets, so uniqueness, inclusion and naming all tie, and the winner was
decided by dict iteration order.
"""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass, field

# dbt resource types that become a queryable relation, so can be a join target.
_RELATION_TYPES = ("model", "seed", "snapshot")


# Conventional ETL-stage prefixes. A warehouse built by dbt, Dataform, SQLMesh
# or by hand almost always names its stages, because humans reading the table
# list need the same signal the engine does.
_STAGE_PREFIX = re.compile(
    r"^((?:stg|staging|int|intermediate|base|raw|tmp|temp|src|wrk|work)"
    r"_{1,2}(?:[a-z0-9]+__)?)", re.IGNORECASE)


def naming_layer(table: str) -> str:
    """The ETL stage a table's NAME claims, or "" for an unprefixed table.

    The manifest states the layout when there is a manifest. Without one the
    layer signal is not absent -- it is in the names, because the convention
    exists to be read: `stg_synthea__patients` is staging, `int__person` is
    intermediate, and a bare `person` is the published table.

    Returning "" for unprefixed tables is deliberate and load-bearing: two
    published tables share the layer "", which is what lets a published child
    prefer a published parent over the intermediate copy that holds the same
    key values.
    """
    bare = table.rsplit(".", 1)[-1]
    m = _STAGE_PREFIX.match(bare)
    return m.group(1).lower() if m else ""


def same_naming_layer(a: str, b: str) -> bool:
    """Do these two table names claim the same ETL stage?"""
    return naming_layer(a) == naming_layer(b)


@dataclass
class LineageBundle:
    """Resolved ETL lineage, keyed by the table names the warehouse exposes."""

    # table name -> the node ids it is built from (direct parents)
    parents: dict[str, set[str]] = field(default_factory=dict)
    # node id -> table name, for resolving `parents` back to tables
    node_table: dict[str, str] = field(default_factory=dict)
    # table name -> materialisation ('table', 'view', 'incremental', 'ephemeral')
    materialised: dict[str, str] = field(default_factory=dict)
    # Documentation a human wrote in the dbt project. Same standing as a native
    # warehouse COMMENT -- a sentence someone wrote beats anything we generate --
    # and already sitting in a file we open for the DAG anyway.
    table_descriptions: dict[str, str] = field(default_factory=dict)
    column_descriptions: dict[tuple[str, str], str] = field(default_factory=dict)
    metrics: tuple[dict, ...] = ()
    # table name -> the ETL's own name for the stage it belongs to ('staging',
    # 'intermediate', 'omop', 'marts'...). Taken from the project's directory
    # layout, which is the only place the ETL states this; nothing in the
    # warehouse records it.
    layers: dict[str, str] = field(default_factory=dict)
    source: str = ""

    def __bool__(self) -> bool:
        return bool(self.parents)

    def same_layer(self, a: str, b: str) -> bool:
        """Are these two tables in the same stage of the ETL?

        The signal that breaks the parent-selection contest §4.3 of the design
        note describes. When several parents fully contain a child's values,
        the one the declaring model meant is overwhelmingly the one in its OWN
        layer: `stg_synthea__allergies.encounter_id` means
        `stg_synthea__encounters`, not the `int__encounters` built from it, and
        `condition_occurrence.condition_concept_id` means `concept`, not an
        intermediate vocabulary map that happens to contain the same ids.

        This is deliberately NOT `prefer()`, which returns the downstream
        table. Downstream is the right answer for the person/int__person pair
        and the WRONG answer for the encounters pairs, where gold is the
        upstream staging table -- so DAG direction cannot decide this and
        co-location can. Unknown layers return False, so a project without a
        directory convention changes nothing.
        """
        la, lb = self.layers.get(a), self.layers.get(b)
        return la is not None and la == lb

    def ancestors(self, table: str) -> set[str]:
        """Every table `table` is transitively built from.

        Cycles cannot occur in a dbt DAG, but the walk is guarded anyway: this
        reads a file we did not write, and a malformed manifest must not hang
        inference.
        """
        seen: set[str] = set()
        stack = list(self.parents.get(table, ()))
        while stack:
            node = stack.pop()
            name = self.node_table.get(node)
            if name is None or name in seen or name == table:
                continue
            seen.add(name)
            stack.extend(self.parents.get(name, ()))
        return seen

    def is_built_from(self, table: str, other: str) -> bool:
        """Is `table` downstream of `other` -- i.e. built from it, directly or not?"""
        return other in self.ancestors(table)

    def prefer(self, a: str, b: str) -> str | None:
        """Of two candidate join targets, the one consumers should join to.

        If one is built from the other, the DOWNSTREAM table is the published
        one and the upstream is a staging step on the way to it. Returns None
        when the two are unrelated, which is the common case and must leave the
        existing decision untouched.
        """
        if a == b:
            return a
        if self.is_built_from(a, b):
            return a
        if self.is_built_from(b, a):
            return b
        return None


def _record_layer(bundle: LineageBundle, table: str, node: dict) -> None:
    """Record which stage of the ETL a node belongs to, if it says.

    `fqn` is [package, ...directories..., model], so element 1 is the top-level
    stage directory -- 'staging', 'intermediate', 'omop'. A model sitting
    directly in models/ has no stage and is left unlabelled rather than
    labelled with the package name, which would put every such model into one
    bogus shared layer.
    """
    fqn = node.get("fqn") or []
    if len(fqn) > 2:
        bundle.layers[table] = fqn[1]


def _read_docs(bundle: LineageBundle, table: str, node: dict) -> None:
    """Pull a node's own documentation into the bundle, in place."""
    desc = (node.get("description") or "").strip()
    if desc:
        bundle.table_descriptions[table] = desc
    for col_name, col in (node.get("columns") or {}).items():
        cd = ((col or {}).get("description") or "").strip()
        if cd:
            bundle.column_descriptions[(table, col_name)] = cd


def _read_metrics(doc: dict) -> list[dict]:
    """dbt/MetricFlow metric definitions, in the manifest's own vocabulary.

    Normalised only enough to be recognisable -- name, label, type, the measure
    and its aggregation, and any filter -- and deliberately NOT mapped onto our
    spec here. A metric a human defined is a claim about the business, and the
    Enrich stage decides what to do with it; this function only reads.

    Covers both shapes: `metrics` (the metric itself) and `semantic_models`
    (where the measures and their aggregations live). A project with neither
    yields an empty tuple and nothing downstream changes.
    """
    out: list[dict] = []
    measures: dict[str, dict] = {}
    for sm in (doc.get("semantic_models") or {}).values():
        if not isinstance(sm, dict):
            continue
        model = sm.get("node_relation") or {}
        for ms in (sm.get("measures") or []):
            if isinstance(ms, dict) and ms.get("name"):
                measures[ms["name"]] = {
                    "agg": ms.get("agg"), "expr": ms.get("expr") or ms["name"],
                    "table": model.get("alias") or sm.get("model"),
                }
    for mt in (doc.get("metrics") or {}).values():
        if not isinstance(mt, dict) or not mt.get("name"):
            continue
        tp = mt.get("type_params") or {}
        measure = tp.get("measure") or {}
        mname = measure.get("name") if isinstance(measure, dict) else measure
        rec = {
            "name": mt["name"],
            "label": (mt.get("label") or "").strip() or None,
            "description": (mt.get("description") or "").strip() or None,
            "type": mt.get("type"),
            "measure": mname,
            "filter": _metric_filter(mt.get("filter") or tp.get("filter")),
        }
        if mname and mname in measures:
            rec["agg"] = measures[mname].get("agg")
            rec["expr"] = measures[mname].get("expr")
            rec["table"] = measures[mname].get("table")
        out.append(rec)
    return out


def _metric_filter(f) -> str | None:
    """A MetricFlow filter is a list of `where_sql_template` entries, or absent."""
    if not f:
        return None
    if isinstance(f, str):
        return f.strip() or None
    items = f.get("where_filters") if isinstance(f, dict) else f
    if not isinstance(items, list):
        return None
    parts = [str(i.get("where_sql_template")).strip()
             for i in items if isinstance(i, dict) and i.get("where_sql_template")]
    return " AND ".join(p for p in parts if p) or None


def load_dbt_manifest(path: str | pathlib.Path) -> LineageBundle:
    """Read a dbt manifest.json into a LineageBundle.

    Accepts a manifest from `dbt parse` as well as a full `dbt build`: only
    `depends_on`, `alias`/`name` and the materialisation config are used, and
    all three are present after a bare parse.
    """
    p = pathlib.Path(path)
    try:
        doc = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        from semlayer.errors import SemlayerError

        raise SemlayerError(f"cannot read dbt manifest {p}: {e}",
                            hint="point --manifest at a target/manifest.json") from None

    nodes = doc.get("nodes")
    # Empty counts as absent, and both are errors rather than an empty bundle.
    # `catalog.json` sits next to the manifest and has no `nodes`, so pointing
    # --manifest at it is a realistic mistake -- and silently inferring with no
    # lineage would be indistinguishable from a project that has none.
    if not isinstance(nodes, dict) or not nodes:
        from semlayer.errors import SemlayerError

        raise SemlayerError(f"{p} has no `nodes`: is it a dbt manifest?",
                            hint="expected target/manifest.json, not catalog.json")

    bundle = LineageBundle(source=str(p))
    for node_id, n in nodes.items():
        if not isinstance(n, dict) or n.get("resource_type") not in _RELATION_TYPES:
            continue
        # `alias` is the relation actually created; `name` is the model file.
        # They differ whenever a project aliases a model, and the warehouse
        # shows us the alias.
        table = n.get("alias") or n.get("name")
        if not table:
            continue
        bundle.node_table[node_id] = table
        deps = ((n.get("depends_on") or {}).get("nodes")) or []
        bundle.parents.setdefault(table, set()).update(deps)
        mat = (n.get("config") or {}).get("materialized")
        if mat:
            bundle.materialised[table] = mat
        # `fqn` is [package, ...directories..., model], so element 1 is the
        # top-level stage directory -- 'staging', 'intermediate', 'omop'. A
        # model sitting directly in models/ has no stage and is left unlabelled
        # rather than labelled with the package name, which would put every
        # such model in one bogus shared layer.
        _record_layer(bundle, table, n)
        _read_docs(bundle, table, n)
    bundle.metrics = tuple(_read_metrics(doc))
    # Sources are join targets too, but they have no parents; record their
    # names so `parents` lookups resolve rather than silently missing.
    for node_id, s in (doc.get("sources") or {}).items():
        if isinstance(s, dict):
            name = s.get("identifier") or s.get("name")
            if name:
                bundle.node_table[node_id] = name
    return bundle
