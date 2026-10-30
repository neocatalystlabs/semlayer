# SPDX-License-Identifier: FSL-1.1-ALv2
"""Maintain: the loop that keeps a layer true, as one command.

detect (drift) -> changeset -> plan -> [apply: targeted re-inference -> merge]
-> what the review queue gained. Every step is a shipped primitive; this
module only chains them and says, before anything is written, what it would
do. Dry run is the default: a scheduled job can print its plan for a week
before anyone lets it write.

The re-inference makes the same choices `semlayer infer` makes -- the LLM
when a key is present, the same workers, the same manifest -- so a layer is
maintained by the engine that built it. A maintainer that quietly used a
different engine would drift the layer for reasons no diff could show.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from semlayer import drift as drift_mod
from semlayer.merge import MergeReport, merge_layer, render_report
from semlayer.review import collect


@dataclass
class MaintainResult:
    """What maintain found, planned, and (with apply) did."""

    events: list[drift_mod.DriftEvent] = field(default_factory=list)
    changeset: drift_mod.Changeset | None = None
    retable: list[str] = field(default_factory=list)   # tables the plan re-infers
    with_llm: bool = False
    applied: bool = False
    merge: MergeReport | None = None
    queue_before: int = 0
    queue_after: int = 0
    valid: bool | None = None

    @property
    def empty(self) -> bool:
        """Nothing changed in the warehouse."""
        return not self.events

    def as_dict(self) -> dict:
        """One record for an agent or a CI log."""
        cs = self.changeset
        return {
            "events": [{"kind": e.kind, "table": e.table, "column": e.column, "detail": e.detail}
                       for e in self.events],
            "changeset": None if cs is None else {
                "orphaned": cs.orphaned, "needs_inference": cs.needs_inference,
                "affected": cs.affected, "broken_cqs": cs.broken_cqs, "demoted": cs.demoted},
            "plan": {"reinfer": self.retable, "with_llm": self.with_llm},
            "applied": self.applied,
            "merge": None if self.merge is None else {
                "tables_added": self.merge.tables_added,
                "tables_updated": self.merge.tables_updated,
                "columns_added": self.merge.columns_added,
                "columns_updated": self.merge.columns_updated, "orphaned": self.merge.orphaned,
                "protected": self.merge.protected, "preserved": self.merge.preserved,
                "conflicts": self.merge.conflicts, "touched": self.merge.touched},
            "review_queue": {"before": self.queue_before, "after": self.queue_after,
                             "new": max(0, self.queue_after - self.queue_before)},
            "valid": self.valid,
        }


def plan(doc: dict, source, cq_suite: list[dict] | None = None, con=None,
         with_llm: bool = False) -> MaintainResult:
    """Detect drift and say what re-inference it calls for.

    Mutates `doc` the way `drift --apply` would (orphaning, conflicts) but
    writes nothing; the caller decides whether to save.
    """
    r = MaintainResult(with_llm=with_llm, queue_before=len(collect(doc)))
    events = drift_mod.diff_snapshots(drift_mod.baseline(doc), drift_mod.snapshot(source))
    events += drift_mod.semantic_drift(source, doc)
    r.events = events
    if not events:
        return r
    cs = drift_mod.apply_drift(doc, events)
    if cq_suite is not None and con is not None:
        cs.broken_cqs = drift_mod.cq_regression(con, cq_suite, events, doc)
    r.changeset = cs
    r.retable = sorted({n.split(".", 1)[0] for n in cs.needs_inference})
    return r


def apply(doc: dict, source, r: MaintainResult, llm=None, *, options_kw: dict | None = None,
          lineage=None) -> MaintainResult:
    """Re-infer the planned tables and merge them into `doc`; the caller saves."""
    from semlayer.pipeline import InferOptions, infer
    from semlayer.validate import validate_document

    if r.retable:
        options = InferOptions(only=r.retable, **(options_kw or {}))
        partial = infer(source, llm=llm, options=options, lineage=lineage)
        r.merge = merge_layer(doc, partial, scope=set(r.retable))
    r.applied = True
    r.valid = validate_document(doc).ok
    r.queue_after = len(collect(doc))
    return r


def render(r: MaintainResult, doc_path: str, source_label: str) -> str:
    """The plan, or the outcome, in the words a person reads before trusting a job."""
    if r.empty:
        return f"maintain: no change in {source_label} against {doc_path}; nothing to do"
    lines = _render_plan(r, doc_path, source_label)
    if not r.applied:
        lines.append("dry run: nothing written. Add --apply to do it.")
        return "\n".join(lines)
    if r.merge is not None:
        lines += ["", render_report(r.merge)]
    delta = r.queue_after - r.queue_before
    more = f" (+{delta}): semlayer review {doc_path}" if delta > 0 else ""
    lines.append(f"\nwrote {doc_path}: valid={r.valid}; review queue {r.queue_before} -> "
                 f"{r.queue_after}{more}")
    return "\n".join(lines)


def _render_plan(r: MaintainResult, doc_path: str, source_label: str) -> list[str]:
    lines = [f"maintain: {len(r.events)} change(s) in {source_label} against {doc_path}"]
    for e in r.events[:20]:
        where = f"{e.table}.{e.column}" if e.column else e.table
        lines.append(f"  {e.kind:16} {where}" + (f"  ({e.detail})" if e.detail else ""))
    if len(r.events) > 20:
        lines.append(f"  ... and {len(r.events) - 20} more")
    cs = r.changeset
    bits = []
    if cs is not None and cs.orphaned:
        bits.append(f"{len(cs.orphaned)} element(s) orphaned, never deleted")
    if cs is not None and cs.demoted:
        bits.append(f"{len(cs.demoted)} demoted")
    if cs is not None and cs.broken_cqs:
        bits.append(f"{len(cs.broken_cqs)} competency question(s) flagged")
    if bits:
        lines.append("changeset: " + "; ".join(bits))
    engine = "with the LLM (key present)" if r.with_llm else "without an LLM"
    if r.retable:
        lines.append(f"plan: re-infer {len(r.retable)} table(s) {engine}: {', '.join(r.retable)}; "
                     f"merge into {doc_path} keeping reviewed and certified content")
    else:
        lines.append("plan: nothing to re-infer; the changeset is orphaning and conflicts only")
    return lines
