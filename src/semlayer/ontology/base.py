"""Deterministic ontology base graph (M5).

The W3C Direct-Mapping-style compilation of the validated semantic layer.
Mechanical and reproducible — no LLM, no discretion, no staleness
(re-derived on every change).

Property graph in JSON: entity nodes (from tables), relationship edges (from
the join graph, typed + cardinality-annotated), recursive-hierarchy edges,
canonical-entity groupings. Every element carries provenance back to the
semantic-layer object it was derived from (SPEC anchor rule).

The LLM ENRICHMENT projection (subclasses, event->state rules) is NOT here —
it is internal/experimental until it passes the honest ablation gate (PRD §6).
"""

from __future__ import annotations

from collections import defaultdict


def build_base_graph(doc: dict) -> dict:
    """Compile the semantic layer into the deterministic ontology base graph."""
    sl = doc["semantic_layer"]
    nodes, edges = [], []

    for t in sl["tables"]:
        table_kind = (
            "entity" if t.get("table_type") == "dimension" else t.get("table_type", "unknown")
        )
        nodes.append({
            "id": t["name"],
            "kind": table_kind,
            "label": t.get("display_name", t["name"]),
            "description": (t.get("description") or "")[:140],
            "lifecycle": t.get("lifecycle", "inferred"),
            "derived_from": f"tables.{t['name']}",
        })

    for r in sl.get("relationships", []):
        edges.append({
            "from": r["from"]["table"], "to": r["to"]["table"],
            "kind": "references",
            "cardinality": r.get("cardinality"),
            "via": f"{r['from']['columns'][0]} -> {r['to']['columns'][0]}",
            "fanout_risk": r.get("fanout_risk", False),
            "derived_from": f"relationships.{r.get('name')}",
        })

    for h in sl.get("hierarchies", []) or []:
        if h.get("kind") == "recursive":
            edges.append({
                "from": h["dimension_table"], "to": h["dimension_table"],
                "kind": "recursive_hierarchy",
                "via": f"{h['recursive']['parent_column']} -> {h['recursive']['child_column']}",
                "derived_from": f"hierarchies.{h['name']}",
            })

    canon = defaultdict(list)
    for t in sl["tables"]:
        for c in t["columns"]:
            if c.get("canonical_entity"):
                canon[c["canonical_entity"]].append(f"{t['name']}.{c['name']}")
            fk = c.get("foreign_key")
            if fk:
                canon[fk["references"]].append(f"{t['name']}.{c['name']}")
    entity_groups = [
        {"entity": k, "columns": sorted({*v, k})}
        for k, v in canon.items() if len(v) > 1
    ]

    for a in sl.get("aggregate_tables", []) or []:
        edges.append({
            "from": a["table"], "to": a["aggregates"], "kind": "aggregates",
            "via": ", ".join(a.get("grain", [])),
            "derived_from": f"aggregate_tables.{a['table']}",
        })

    return {
        "ontology": {
            "kind": "base_graph",
            "derivation": "deterministic (direct mapping of the semantic layer)",
            "nodes": nodes, "edges": edges,
            "entity_groups": entity_groups,
        }
    }


def join_path(graph: dict, a: str, b: str, max_hops: int = 4) -> list[str] | None:
    """Shortest path between two tables over reference edges (BFS)."""
    adj = defaultdict(set)
    for e in graph["ontology"]["edges"]:
        if e["kind"] in ("references", "aggregates"):
            adj[e["from"]].add(e["to"])
            adj[e["to"]].add(e["from"])
    frontier, seen = [[a]], {a}
    while frontier:
        path = frontier.pop(0)
        if len(path) > max_hops + 1:
            return None
        if path[-1] == b:
            return path
        for n in sorted(adj[path[-1]] - seen):
            seen.add(n)
            frontier.append([*path, n])
    return None
