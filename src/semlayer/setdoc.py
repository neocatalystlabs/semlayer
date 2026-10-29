# SPDX-License-Identifier: FSL-1.1-ALv2
"""Several documents, one surface: resolve a hub and its members into one document.

Finance, Sales and Platform each infer their own layer in their own repo.
None of those files knows about the others. The analyst who wants to work
across them writes (or `stitch` writes) a HUB: a document with a `members`
list naming the three files, usually no tables of its own, and the
relationships that cross between members -- the seam. This module turns
that hub plus its members into ONE in-memory document that every existing
consumer (validate, mcp, compile, lint, review) already understands.

The rule is a single rename. Inside the resolved document every table,
hierarchy and metric is called `<document name>/<name>`, and every
reference to one of them is rewritten the same way, so `fct_gl` in
finance.yaml becomes `finance/fct_gl` and the metric `gl_amount` becomes
`finance/gl_amount`. A reference that already contains `/` is a cross-
member reference written by the hub's author and is left alone. `source`
(the warehouse's own schema.table) is never touched: it is what compiled
SQL uses, and it is the same string whichever document it sits in.

Members are not modified. A member is any valid layer; the same file can
be a member of several hubs.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

from semlayer.errors import SemlayerError

SEP = "/"


class SetError(SemlayerError):
    """The hub or one of its members cannot be resolved into a set."""


@dataclass
class ResolvedSet:
    """A hub and its members as one document, plus what was learnt resolving it."""

    doc: dict
    hub_name: str
    member_names: list[str]
    warehouses: dict[str, str | None]          # document name -> warehouse
    problems: list[str] = field(default_factory=list)


def is_hub(doc: dict) -> bool:
    """A document with a `members` list is a hub."""
    return bool(doc.get("semantic_layer", {}).get("members"))


def _qual(ref: str, prefix: str) -> str:
    """Qualify a reference's head: `table`, `table.column`, `hier.level`, `hier.*`, `metric`."""
    if not ref or SEP in ref.split(".", 1)[0]:
        return ref
    head, dot, tail = ref.partition(".")
    return f"{prefix}{SEP}{head}{dot}{tail}"


def _qual_list(refs: list | None, prefix: str) -> list | None:
    return None if refs is None else [_qual(r, prefix) for r in refs]


def _qual_dep(obj: dict, prefix: str) -> None:
    dep = obj.get("deprecation")
    if dep and dep.get("replacement"):
        dep["replacement"] = _qual(dep["replacement"], prefix)


def _qual_tables(sl: dict, prefix: str) -> None:
    for t in sl.get("tables", []):
        t["name"] = _qual(t["name"], prefix)
        _qual_dep(t, prefix)
        for c in t.get("columns", []):
            fk = c.get("foreign_key")
            if fk and fk.get("references"):
                fk["references"] = _qual(fk["references"], prefix)
            _qual_dep(c, prefix)


def _qual_relationships(sl: dict, prefix: str) -> None:
    for r in sl.get("relationships", []):
        for side in ("from", "to"):
            r[side]["table"] = _qual(r[side]["table"], prefix)
        if r.get("asof", {}).get("on"):
            r["asof"]["on"] = _qual(r["asof"]["on"], prefix)


def _qual_hierarchies(sl: dict, prefix: str) -> None:
    for h in sl.get("hierarchies", []):
        h["name"] = _qual(h["name"], prefix)
        for key in ("dimension_table", "base_column"):
            if h.get(key):
                h[key] = _qual(h[key], prefix)
        for lv in h.get("levels", []):
            # a bare level column is relative to `dimension_table`; only a
            # `table.column` form names a table of its own
            if lv.get("column") and "." in lv["column"]:
                lv["column"] = _qual(lv["column"], prefix)


def _qual_aggregates(sl: dict, prefix: str) -> None:
    for a in sl.get("aggregate_tables", []):
        a["table"] = _qual(a["table"], prefix)
        if a.get("aggregates"):
            a["aggregates"] = _qual(a["aggregates"], prefix)
        if a.get("grain"):
            a["grain"] = [_qual(g, prefix) if "." in g else g for g in a["grain"]]


# references to objects of the set; `entity`, `grain` and a bare
# `agg_time_dimension` name columns of the base table and stay as written
_METRIC_REFS = ("measure", "numerator", "denominator", "base_metric", "conversion_metric")


def _qual_metrics(sl: dict, prefix: str) -> None:
    for m in sl.get("metrics", []):
        m["name"] = _qual(m["name"], prefix)
        for key in _METRIC_REFS:
            if isinstance(m.get(key), str):
                m[key] = _qual(m[key], prefix)
        if isinstance(m.get("agg_time_dimension"), str) and "." in m["agg_time_dimension"]:
            m["agg_time_dimension"] = _qual(m["agg_time_dimension"], prefix)
        for key in ("input_metrics", "dimensions"):
            if m.get(key):
                m[key] = _qual_list(m[key], prefix)
        _qual_dep(m, prefix)


def _qual_routing(sl: dict, prefix: str) -> None:
    rk = sl.get("repo_knowledge") or {}
    for entry in rk.get("routing", []):
        if entry.get("use"):
            entry["use"] = _qual_list(entry["use"], prefix)
        for av in entry.get("avoid", []):
            av["table"] = _qual(av["table"], prefix)
    for dom in rk.get("domains", []):
        dom["tables"] = _qual_list(dom.get("tables", []), prefix)


def qualify(sl: dict, prefix: str) -> dict:
    """A copy of a document body with every object name and reference qualified by `prefix`.

    Covers every reference site the validator checks (tests/test_set.py
    runs the validator over every fixture resolved this way, so a site
    missed here shows up as a dangling reference there).
    """
    sl = copy.deepcopy(sl)
    for f in (_qual_tables, _qual_relationships, _qual_hierarchies, _qual_aggregates,
              _qual_metrics, _qual_routing):
        f(sl, prefix)
    return sl


_SECTIONS = ("tables", "relationships", "hierarchies", "aggregate_tables", "metrics")


def _body(sl: dict) -> dict:
    """The sections of a document body that carry names and references."""
    out = {k: sl[k] for k in _SECTIONS if sl.get(k) is not None}
    if sl.get("repo_knowledge"):
        out["repo_knowledge"] = sl["repo_knowledge"]
    return out


def _append(osl: dict, q: dict) -> None:
    for sec in _SECTIONS:
        osl.setdefault(sec, []).extend(q.get(sec) or [])
    rk = q.get("repo_knowledge") or {}
    for key in ("routing", "domains"):
        if rk.get(key):
            osl.setdefault("repo_knowledge", {}).setdefault(key, []).extend(rk[key])
    if rk.get("conventions", {}).get("notes"):
        (osl.setdefault("repo_knowledge", {}).setdefault("conventions", {})
            .setdefault("notes", []).extend(rk["conventions"]["notes"]))


def resolve(hub: dict, members: dict[str, dict]) -> ResolvedSet:
    """One document from a hub and its already-loaded members, keyed by declared name.

    Does not read files; `load_set` does. Problems that do not stop
    resolution (a member whose `name` disagrees with the hub) are recorded
    in `problems` for the validator to report; the resolved document still
    uses the hub's name for that member, so references keep resolving.
    """
    hsl = hub["semantic_layer"]
    hub_name = hsl["name"]
    problems: list[str] = []
    out = copy.deepcopy(hub)
    osl = out["semantic_layer"]
    for sec in (*_SECTIONS, "repo_knowledge"):
        osl.pop(sec, None)
    _append(osl, qualify(_body(hsl), hub_name))
    warehouses: dict[str, str | None] = {hub_name: hsl.get("warehouse")}
    names: list[str] = []
    for m in hsl["members"]:
        mname = m["name"]
        msl = members[mname]["semantic_layer"]
        if msl.get("name") != mname:
            problems.append(f"member '{mname}': the file calls itself "
                            f"'{msl.get('name')}'; the hub's name is used")
        if mname == hub_name or mname in names:
            problems.append(f"member '{mname}': name is not unique in the set")
        names.append(mname)
        warehouses[mname] = msl.get("warehouse")
        if msl.get("warehouse"):
            # consumers of the resolved document (compile, lint) read it here
            next(e for e in osl["members"] if e["name"] == mname)["warehouse"] = msl["warehouse"]
        _append(osl, qualify(_body(msl), mname))
    for sec in _SECTIONS:
        if not osl.get(sec):
            osl.pop(sec, None)
    return ResolvedSet(out, hub_name, names, warehouses, problems)


def load_set(path: str | Path) -> ResolvedSet:
    """Load a hub and every member it names and resolve them into one document.

    A document without `members` resolves to itself, so callers can pass
    any layer here. Member paths are relative to the hub file.
    """
    from semlayer.validate import load_yaml

    hub_path = Path(path)
    hub = load_yaml(hub_path.read_text())
    hsl = hub.get("semantic_layer") or {}
    if not hsl.get("members"):
        name = hsl.get("name", "")
        return ResolvedSet(hub, name, [], {name: hsl.get("warehouse")})
    members: dict[str, dict] = {}
    for m in hsl["members"]:
        mpath = Path(m["path"])
        if not mpath.is_absolute():
            mpath = hub_path.parent / mpath
        try:
            doc = load_yaml(mpath.read_text())
        except FileNotFoundError as e:
            raise SetError(f"member '{m['name']}': file not found: {mpath}",
                           hint="paths in `members` are relative to the hub file") from e
        if not isinstance(doc, dict) or "semantic_layer" not in doc:
            raise SetError(f"member '{m['name']}': {mpath} is not a semantic layer document")
        members[m["name"]] = doc
    return resolve(hub, members)


def set_checks(rs: ResolvedSet) -> list[str]:
    """Errors a set has that no single document can: identity, shared sources, warehouses.

    - a member's `name` must match the hub's entry, and names must be unique;
    - two members must not describe the same `source` in the same warehouse;
    - a relationship between tables in different warehouses needs `federation`.
    """
    errors = list(rs.problems)
    sl = rs.doc["semantic_layer"]
    owner_of: dict[tuple[str | None, str], str] = {}
    for t in sl.get("tables", []):
        doc_name = t["name"].split(SEP, 1)[0]
        key = (rs.warehouses.get(doc_name), t.get("source"))
        if t.get("source") and key in owner_of and owner_of[key] != doc_name:
            errors.append(f"set: {t['source']} is described by both '{owner_of[key]}' and "
                          f"'{doc_name}' in the same warehouse; one document must own it")
        owner_of.setdefault(key, doc_name)
    federated = bool(sl.get("federation"))
    for r in sl.get("relationships", []):
        a = r["from"]["table"].split(SEP, 1)[0]
        b = r["to"]["table"].split(SEP, 1)[0]
        wa, wb = rs.warehouses.get(a), rs.warehouses.get(b)
        if wa and wb and wa != wb and not federated:
            errors.append(f"set: relationship {r['from']['table']} -> {r['to']['table']} spans "
                          f"warehouses {wa} and {wb}; no `federation` engine is declared, so no "
                          f"single statement can join them")
    return errors
