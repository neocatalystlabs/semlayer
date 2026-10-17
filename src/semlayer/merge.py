# SPDX-License-Identifier: FSL-1.1-ALv2
"""Fold a partial re-inference back into an existing layer.

`infer --only ord_hdr` profiles two tables and produces a layer covering two
tables. Without this module there is nothing to do with that file: the layer
you committed covers two hundred. An agent could splice the YAML by hand, and
the result would land in a pull request as a three-thousand-line diff in which
a dropped `lifecycle: certified` is invisible. The point of a primitive here is
a diff a human can actually read -- four lines when four things changed.

The rule is the one `drift.apply_drift` already follows, applied to content
instead of existence:

    CERTIFICATION PROTECTS CONTENT.  THE WAREHOUSE GOVERNS EXISTENCE.

so, per element:

  reviewed / certified   content is kept. A disagreeing re-inference is
                         recorded as a conflict, never written over the top.
  inferred               content is replaced by the new inference.
  absent from base       inserted.
  absent from incoming   the warehouse no longer has it -> `orphaned`, the
                         same transition a drop gets. Never deleted.

Scope matters as much as the rule. The incoming document knows about the
tables it profiled and nothing else, so its silence about `cust_mstr` is not
evidence that `cust_mstr` is gone. Everything outside the scope is left
untouched -- which is what makes the diff small.
"""

from __future__ import annotations

from dataclasses import dataclass, field

PROTECTED = ("reviewed", "certified")

# envelope fields a re-inference may replace on an unprotected element; `name`,
# lifecycle and anything a human set are not in this list
CONTENT_KEYS = ("description", "ai_context", "synonyms", "confidence", "provenance",
                "display_name", "semantic_type", "entity_role", "cardinality",
                "enum_values", "foreign_key", "aggregations", "pii",
                "canonical_entity", "temporal", "time_attribute", "paths",
                "sql_type", "table_type", "grain", "primary_key", "scd",
                "freshness", "knowledge", "source")


@dataclass
class MergeReport:
    """What the merge did, in the terms a reviewer checks."""

    scope: list[str] = field(default_factory=list)
    tables_added: list[str] = field(default_factory=list)
    tables_updated: list[str] = field(default_factory=list)
    columns_added: list[str] = field(default_factory=list)
    columns_updated: list[str] = field(default_factory=list)
    orphaned: list[str] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)
    preserved: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    relationships_added: list[str] = field(default_factory=list)
    metrics_added: list[str] = field(default_factory=list)

    @property
    def touched(self) -> int:
        """How many elements actually moved — what a reviewer counts."""
        return (len(self.tables_added) + len(self.tables_updated)
                + len(self.columns_added) + len(self.columns_updated)
                + len(self.orphaned) + len(self.relationships_added)
                + len(self.metrics_added))


def _protected(obj: dict) -> bool:
    return obj.get("lifecycle") in PROTECTED


def _note_conflict(obj: dict, detail: str, report: MergeReport, where: str) -> None:
    """Record disagreement on the object itself, so it travels with the layer."""
    entry = {"between": ["human", "llm"], "detail": detail}
    conflicts = obj.setdefault("conflicts", [])
    if entry not in conflicts:
        conflicts.append(entry)
    report.conflicts.append(where)


def _replace_content(base: dict, new: dict) -> bool:
    """Overwrite the inferable fields of `base` from `new`. True if anything moved."""
    changed = False
    for k in CONTENT_KEYS:
        if k in new and base.get(k) != new[k]:
            base[k] = new[k]
            changed = True
        elif k not in new and k in base and k not in ("name",):
            # the new inference asserts nothing here; leave what is there
            continue
    return changed


# evidence that can only come from comparing two tables. A partial inference
# that never profiled the OTHER table cannot have it, so its silence -- or its
# replacement guess -- is not evidence of anything.
CROSS_TABLE_KEYS = ("foreign_key", "entity_role", "confidence", "provenance")
TABLE_CROSS_KEYS = ("table_type", "confidence", "provenance", "grain")


def _keeps_cross_table_evidence(base_c: dict, scope: set[str]) -> bool:
    """True if base holds an FK to a table this run did not look at.

    `infer --only ord_hdr` runs Link over one table. Every FK from it to a
    dimension outside the scope comes back missing, with `entity_role` demoted
    and the inclusion-dependency provenance dropped. Writing that over the top
    would quietly delete the join graph -- the exact silent corruption a diff
    is supposed to prevent, arriving through the diff instead.
    """
    fk = base_c.get("foreign_key")
    if not isinstance(fk, dict):
        return False
    ref = str(fk.get("references", ""))
    target = ref.split(".")[0] if ref else ""
    return bool(target) and target not in scope


def _merge_columns(base_t: dict, new_t: dict, report: MergeReport,
                   scope: set[str]) -> None:
    base_cols = {c["name"]: c for c in base_t.get("columns", [])}
    new_cols = {c["name"]: c for c in new_t.get("columns", [])}
    tname = base_t["name"]

    for name, new_c in new_cols.items():
        where = f"{tname}.{name}"
        base_c = base_cols.get(name)
        if base_c is None:
            base_t.setdefault("columns", []).append(new_c)
            report.columns_added.append(where)
            continue
        if _protected(base_c):
            report.protected.append(where)
            if base_c.get("description") and new_c.get("description") \
                    and base_c["description"] != new_c["description"]:
                _note_conflict(base_c, "re-inference disagrees with the reviewed "
                                       "description; reviewed content kept",
                               report, where)
            continue
        before = _snapshot(base_c)
        if _keeps_cross_table_evidence(base_c, scope):
            held = {k: base_c[k] for k in CROSS_TABLE_KEYS if k in base_c}
            _replace_content(base_c, new_c)
            base_c.update(held)                     # cross-table evidence stands
            report.preserved.append(where)
        else:
            _replace_content(base_c, new_c)
        # report what MOVED, not what was considered: the earlier version listed
        # eight columns it had restored to exactly their previous value
        if _snapshot(base_c) != before:
            report.columns_updated.append(where)

    # in scope, the incoming document is authoritative about what exists
    for name, base_c in base_cols.items():
        if name in new_cols or base_c.get("lifecycle") == "orphaned":
            continue
        base_c["lifecycle"] = "orphaned"
        report.orphaned.append(f"{tname}.{name}")


def _key(rel: dict) -> tuple:
    f, t = rel.get("from", {}), rel.get("to", {})
    return (rel.get("name"), f.get("table"), tuple(f.get("columns", []) or []),
            t.get("table"), tuple(t.get("columns", []) or []))


def _merge_list(base_sl: dict, new_sl: dict, key: str, identity, scope: set[str],
                in_scope, out: list[str]) -> None:
    """Append in-scope items the base does not have. Never rewrites an existing one."""
    existing = {identity(x) for x in base_sl.get(key, []) or []}
    for item in new_sl.get(key, []) or []:
        if not in_scope(item, scope) or identity(item) in existing:
            continue
        base_sl.setdefault(key, []).append(item)
        out.append(str(identity(item)[0] or identity(item)))


def merge_layer(base: dict, incoming: dict, scope: set[str] | None = None) -> MergeReport:
    """Merge `incoming` into `base` in place. Returns what changed.

    `scope` is the set of table names the incoming document is authoritative
    about; it defaults to the tables the incoming document actually contains.
    Anything outside it is not read and not touched.
    """
    base_sl, new_sl = base["semantic_layer"], incoming["semantic_layer"]
    new_tables = {t["name"]: t for t in new_sl.get("tables", [])}
    scope = set(scope) if scope else set(new_tables)
    report = MergeReport(scope=sorted(scope))

    base_tables = {t["name"]: t for t in base_sl.get("tables", [])}
    for name in sorted(scope):
        new_t = new_tables.get(name)
        if new_t is None:
            continue                      # asked for, not profiled: say nothing
        base_t = base_tables.get(name)
        if base_t is None:
            base_sl.setdefault("tables", []).append(new_t)
            report.tables_added.append(name)
            continue
        before = _snapshot(base_t)
        blind = any(_keeps_cross_table_evidence(c, scope)
                    for c in base_t.get("columns", []))
        if _protected(base_t):
            report.protected.append(name)
        else:
            held = ({k: base_t[k] for k in TABLE_CROSS_KEYS if k in base_t}
                    if blind else {})
            _replace_content(base_t, {k: v for k, v in new_t.items() if k != "columns"})
            # `table_type` is a count of outbound FKs; with the dimensions out
            # of scope that count is zero and a fact table reads as a dimension
            base_t.update(held)
            if blind:
                report.preserved.append(name)
        _merge_columns(base_t, new_t, report, scope)
        if _snapshot(base_t) != before:
            report.tables_updated.append(name)

    def rel_in_scope(r, sc):
        return r.get("from", {}).get("table") in sc or r.get("to", {}).get("table") in sc

    _merge_list(base_sl, new_sl, "relationships", _key, scope, rel_in_scope,
                report.relationships_added)
    _merge_list(base_sl, new_sl, "metrics", lambda m: (m.get("name"),), scope,
                lambda m, sc: True, report.metrics_added)
    return report


def _snapshot(obj: dict) -> str:
    import json

    return json.dumps(obj, sort_keys=True, default=str)


def render_report(r: MergeReport) -> str:
    """The changeset a reviewer reads before opening the diff."""
    if not r.touched and not r.protected:
        return "merge: nothing changed"
    out = [f"# Merge report ({len(r.scope)} table(s) in scope)"]
    for label, items in (("Tables added", r.tables_added),
                         ("Tables updated", r.tables_updated),
                         ("Columns added", r.columns_added),
                         ("Columns updated", r.columns_updated),
                         ("Orphaned (gone from the warehouse)", r.orphaned),
                         ("Relationships added", r.relationships_added),
                         ("Metrics added", r.metrics_added),
                         ("Protected (reviewed/certified, content kept)", r.protected),
                         ("Kept (FK evidence from tables outside the scope)", r.preserved),
                         ("CONFLICTS (recorded, not resolved)", r.conflicts)):
        if items:
            out.append(f"\n## {label}")
            out += [f"- {x}" for x in items]
    return "\n".join(out)
