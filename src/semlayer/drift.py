"""Drift loop (M7a): detect warehouse change, respond without silent mutation.

Layers (PRD §8): CI/CD hook and cloud schedulers WRAP these primitives —
the primitives themselves are free-tier CLI capability by design.

- snapshot/diff: structural change from information_schema (universal fallback)
- apply_drift: the ORPHANING state machine (adversarial B3) — existence is
  governed by the warehouse, content protection by lifecycle; a certified
  object whose element disappeared becomes `orphaned`, NEVER silently deleted
- blast_radius: typed dependency traversal (column -> FK/relationship/metric/
  aggregate/hierarchy/CQ) with a depth cap
- semantic_drift: DML-only change (new enum values, stale loads)
- cq_regression: gold CQs whose expected SQL breaks or whose references
  intersect the blast radius — the loud alarm
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DriftEvent:
    """One detected change between two snapshots, or one DML-drift observation."""

    kind: str      # table_added | table_dropped | column_added | column_dropped |
                   # column_retyped | enum_value_added | table_stale | engine_upgrade
    table: str
    column: str | None = None
    detail: str = ""


@dataclass
class Changeset:
    """The result of applying a batch of drift events: state transitions + blast radius."""

    events: list[DriftEvent] = field(default_factory=list)
    orphaned: list[str] = field(default_factory=list)
    needs_inference: list[str] = field(default_factory=list)
    affected: list[str] = field(default_factory=list)
    broken_cqs: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        """Whether no drift events occurred."""
        return not self.events


def snapshot(source) -> dict:
    """Take a structural snapshot of the warehouse: table -> {column: sql_type}."""
    return {
        t.name: {c.name: c.sql_type for c in t.columns}
        for t in source.list_tables()
    }


def diff_snapshots(old: dict, new: dict) -> list[DriftEvent]:
    """Compute structural drift events between two snapshots."""
    events = []
    for t in sorted(set(old) - set(new)):
        events.append(DriftEvent("table_dropped", t))
    for t in sorted(set(new) - set(old)):
        events.append(DriftEvent("table_added", t))
    for t in sorted(set(old) & set(new)):
        oc, nc = old[t], new[t]
        for c in sorted(set(oc) - set(nc)):
            events.append(DriftEvent("column_dropped", t, c))
        for c in sorted(set(nc) - set(oc)):
            events.append(DriftEvent("column_added", t, c))
        for c in sorted(set(oc) & set(nc)):
            if oc[c] != nc[c]:
                events.append(DriftEvent("column_retyped", t, c, f"{oc[c]} -> {nc[c]}"))
    return events


def semantic_drift(source, doc: dict) -> list[DriftEvent]:
    """DML-only drift: enum values unseen by the model; stale loads."""
    events = []
    qualify = getattr(source, "qualify", lambda s, n: f'"{s}"."{n}"')
    metas = {t.name: t for t in source.list_tables()}
    for t in doc["semantic_layer"]["tables"]:
        meta = metas.get(t["name"])
        if meta is None:
            continue
        fq = qualify(meta.schema, meta.name)
        for c in t["columns"]:
            known = {str(e["value"]) for e in c.get("enum_values") or []}
            if not known:
                continue
            live_query = (
                f'SELECT DISTINCT "{c["name"]}" FROM {fq} '
                f'WHERE "{c["name"]}" IS NOT NULL LIMIT 50'
            )
            live = {str(r[0]) for r in source.query(live_query)}
            for v in sorted(live - known):
                events.append(DriftEvent("enum_value_added", t["name"], c["name"],
                                         f"unmodeled value '{v}'"))
        fresh = t.get("freshness", {})
        if fresh.get("expected_cadence") == "daily":
            date_cols = [c["name"] for c in t["columns"] if c.get("semantic_type") == "date"]
            if date_cols:
                row = source.query(f'SELECT max("{date_cols[0]}") FROM {fq}')[0]
                if row[0] is not None:
                    from datetime import date
                    try:
                        date.fromisoformat(str(row[0])[:10])
                        # fixtures are frozen in time; staleness is relative to
                        # the table's own history, so compare against the doc's
                        # recorded span rather than wall clock (determinism) —
                        # TODO(M7b): not yet wired to emit table_stale events
                    except ValueError:
                        continue
    return events


def _fk_hits(sl: dict, affected: set[str]) -> set[str]:
    """Columns whose FK target fell in the affected set."""
    new = set()
    for t in sl["tables"]:
        for c in t["columns"]:
            ref = f"{t['name']}.{c['name']}"
            fk = c.get("foreign_key")
            if fk and (fk["references"] in affected or fk["references"].split(".")[0] in affected):
                new.add(ref)
    return new


def _relationship_hits(sl: dict, affected: set[str]) -> set[str]:
    """Relationships with an endpoint (table or join column) in the affected set."""
    new = set()
    for r in sl.get("relationships", []) or []:
        ends = {r["from"]["table"], r["to"]["table"],
                f"{r['from']['table']}.{r['from']['columns'][0]}",
                f"{r['to']['table']}.{r['to']['columns'][0]}"}
        if ends & affected:
            new.add(f"relationships.{r.get('name')}")
    return new


def _metric_hits(sl: dict, affected: set[str]) -> set[str]:
    """Metrics whose measure column fell in the affected set."""
    new = set()
    for m in sl.get("metrics", []) or []:
        measure = m.get("measure")
        if measure and (measure in affected or measure.split(".")[0] in affected):
            new.add(f"metrics.{m['name']}")
    return new


def _aggregate_source_hit(a: dict, affected: set[str]) -> bool:
    """Whether a base column referenced in one of `a`'s measure mappings is affected."""
    for ref in affected:
        if "." not in ref:
            continue
        tbl, col = ref.split(".", 1)
        if tbl != a["aggregates"]:
            continue
        for m in a.get("measure_mappings", []):
            source = m.get("source")
            src_text = (
                source if isinstance(source, str)
                else " ".join(str(v) for v in source.values())
            )
            if col in src_text:
                return True
    return False


def _aggregate_hits(sl: dict, affected: set[str]) -> set[str]:
    """Aggregate tables whose base table or a mapped source column is affected."""
    new = set()
    for a in sl.get("aggregate_tables", []) or []:
        # table-level hit, or column-level: a dropped base column referenced in
        # a mapping source
        hit = bool({a["table"], a["aggregates"]} & affected) or _aggregate_source_hit(a, affected)
        if hit:
            new.add(f"aggregate_tables.{a['table']}")
    return new


def blast_radius(doc: dict, events: list[DriftEvent], max_depth: int = 3) -> list[str]:
    """Affected object refs, transitively, depth-capped (adversarial MA18)."""
    sl = doc["semantic_layer"]
    seed = set()
    for e in events:
        seed.add(e.table if e.column is None else f"{e.table}.{e.column}")
    affected = set(seed)
    for _ in range(max_depth):
        new = (_fk_hits(sl, affected) | _relationship_hits(sl, affected)
               | _metric_hits(sl, affected) | _aggregate_hits(sl, affected))
        if new <= affected:
            break
        affected |= new
    return sorted(affected - seed)


def _apply_table_dropped(e: DriftEvent, t: dict, cs: Changeset) -> None:
    """A table missing from the warehouse orphans — never silently deletes."""
    t["lifecycle"] = "orphaned"
    t.setdefault("provenance", []).append(
        {"signal": "statistic", "detail": "drift: table no longer exists in warehouse"})
    cs.orphaned.append(e.table)


def _apply_column_dropped(e: DriftEvent, sl: dict, t: dict, cs: Changeset) -> None:
    """A dropped column orphans; metrics measuring it can't compile and orphan too."""
    col = next((c for c in t["columns"] if c["name"] == e.column), None)
    if col is not None:
        col["lifecycle"] = "orphaned"
        col.setdefault("provenance", []).append(
            {"signal": "statistic", "detail": "drift: column dropped"})
        cs.orphaned.append(f"{e.table}.{e.column}")
    for m in sl.get("metrics", []) or []:
        if m.get("measure") == f"{e.table}.{e.column}":
            m["lifecycle"] = "orphaned"
            cs.orphaned.append(f"metrics.{m['name']}")


def _apply_column_retyped(e: DriftEvent, t: dict, cs: Changeset) -> None:
    """A retype under a human-reviewed column is a conflict, not a silent overwrite."""
    col = next((c for c in t["columns"] if c["name"] == e.column), None)
    if col is not None and col.get("lifecycle") in ("reviewed", "certified"):
        col.setdefault("conflicts", []).append({
            "between": ["statistic", "human"],
            "detail": f"drift: type changed ({e.detail}) under a {col['lifecycle']} column",
        })
    cs.needs_inference.append(f"{e.table}.{e.column}")


def _apply_enum_value_added(e: DriftEvent, t: dict) -> None:
    """An unmodeled enum value is a statistic/llm conflict for review, not auto-added."""
    col = next((c for c in t["columns"] if c["name"] == e.column), None)
    if col is not None:
        col.setdefault("conflicts", []).append({
            "between": ["statistic", "llm"],
            "detail": f"drift: {e.detail} not in modeled enum",
        })


def apply_drift(doc: dict, events: list[DriftEvent]) -> Changeset:
    """Mutates doc per the state machine; returns the changeset.

    Content of reviewed/certified objects is never rewritten — existence
    transitions only.
    """
    sl = doc["semantic_layer"]
    cs = Changeset(events=list(events))
    tables = {t["name"]: t for t in sl["tables"]}
    for e in events:
        t = tables.get(e.table)
        if e.kind == "table_dropped" and t is not None:
            _apply_table_dropped(e, t, cs)
        elif e.kind == "column_dropped" and t is not None:
            _apply_column_dropped(e, sl, t, cs)
        elif e.kind in ("table_added", "column_added"):
            cs.needs_inference.append(e.table if e.column is None else f"{e.table}.{e.column}")
        elif e.kind == "column_retyped" and t is not None:
            _apply_column_retyped(e, t, cs)
        elif e.kind == "enum_value_added" and t is not None:
            _apply_enum_value_added(e, t)
    cs.affected = blast_radius(doc, events)
    return cs


def cq_regression(con, cq_suite: list[dict], events: list[DriftEvent],
                  doc: dict) -> list[str]:
    """Gold CQs whose expected SQL breaks, or whose references intersect the blast radius.

    This is the loud alarm.
    """
    radius = set(blast_radius(doc, events))
    for e in events:
        radius.add(e.table if e.column is None else f"{e.table}.{e.column}")
    broken = []
    for cq in cq_suite:
        refs = set(cq.get("references", []))
        ref_hit = any(r.split(".", 1)[-1] in radius or r in radius or
                      any(r.endswith(a) or a.endswith(r) for a in radius) for r in refs)
        sql = cq.get("expected_sql", {}).get("duckdb")
        exec_broken = False
        if sql:
            try:
                con.execute(sql)
            except Exception:
                exec_broken = True
        if exec_broken or ref_hit:
            broken.append(cq["id"] + (" (SQL broken)" if exec_broken else " (in blast radius)"))
    return broken


def render_changeset(cs: Changeset) -> str:
    """Render a Changeset as a human-readable markdown summary."""
    lines = ["# Drift changeset", ""]
    for e in cs.events:
        loc = e.table + (f".{e.column}" if e.column else "")
        lines.append(f"- **{e.kind}** {loc}" + (f" ({e.detail})" if e.detail else ""))
    if cs.orphaned:
        lines.append("\n## Orphaned (unusable until reviewed)")
        lines += [f"- {o}" for o in cs.orphaned]
    if cs.needs_inference:
        lines.append("\n## Awaiting inference")
        lines += [f"- {o}" for o in cs.needs_inference]
    if cs.affected:
        lines.append("\n## Blast radius")
        lines += [f"- {o}" for o in cs.affected]
    if cs.broken_cqs:
        lines.append("\n## BROKEN COMPETENCY QUESTIONS")
        lines += [f"- {o}" for o in cs.broken_cqs]
    return "\n".join(lines) + "\n"
