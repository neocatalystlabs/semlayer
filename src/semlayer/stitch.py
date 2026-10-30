# SPDX-License-Identifier: FSL-1.1-ALv2
"""Infer the seam: find, across several layers, the parents of keys that point outside their own.

Finance, Sales and Platform each inferred their own layer. Inside finance.yaml
`fct_gl.cost_center_id` is a foreign-key column with no target, because
finance has no cost-center table: an **unresolved reference**. `stitch`
takes the member files, finds every such column, and tests it as Link would
inside one layer -- name-ranked candidate parents, one inclusion statement
per child -- except that the parents come from the OTHER members and the
queries run through the connection the analyst supplies (`--via`), so they
run with the analyst's own grants. It writes one new file, the hub, and
touches no member.

What it asserts and what it does not:
- an edge is written only for a parent that was tested; an inclusion ratio
  and a name score travel with it as provenance;
- a parent with name evidence is accepted at Link's confidence; one found by
  statistics alone is written below the review threshold, so it appears in
  the hub's review queue as a `seam_edge` rather than being assumed;
- with no connection at all (`--no-queries`, or warehouses the analyst
  cannot reach), candidates are proposed from names and distinct counts
  only, marked so, at low confidence;
- a key that found nothing goes in the hub's `unresolved` list, so nobody
  assumes it is fine.

Statistics come from the member documents themselves (`cardinality`, roles,
types), not from re-profiling: the members were profiled by their owners,
and the only queries stitch issues are the inclusion tests.

**Copies.** Finance keeps its own `dim_customer`: the platform rows for its
own customers plus two columns of its own. Two tables in different members
whose single-column keys share a name and whose key values are contained
one in the other are the SAME ENTITY, not a fact and its dimension. Stitch
writes those as a `one_to_one` relationship with `same_entity: true`, the
columns both carry (`shared_columns`), the columns only the copy has
(`extends_with`), the measured `key_overlap` in both directions, and
`canonical`: the table the organisation stands behind, a certified copy
first, else the one whose keys contain the other's. When neither rule
decides, the edge is written below the review threshold without
`canonical`, and the analyst chooses.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from semlayer import __version__
from semlayer.lineage import same_naming_layer
from semlayer.link.candidates import (
    MAX_QUERIES_PER_CHILD,
    MIN_INCLUSION,
    FKCandidate,
    ParentIndex,
    _inclusion_many,
    _type_family,
    generate_candidates,
    name_similarity,
    select_parents_and_children,
)
from semlayer.link.run import AUTO_NAME_THRESHOLD, _decide_candidates, _resolve_directions
from semlayer.profile.stats import ColumnStats, TableStats
from semlayer.setdoc import SEP, ResolvedSet, resolve
from semlayer.source import ColumnMeta, TableMeta
from semlayer.validate import load_yaml

logger = logging.getLogger(__name__)

NAMES_ONLY_CONFIDENCE = 0.3
REVIEW_CONFIDENCE = 0.5          # a tested parent without name evidence: below the review line
COPY_CONFIDENCE = 0.85           # same entity, canonical decided by certification or containment


@dataclass
class StitchResult:
    """The hub document and what stitch found on the way to it."""

    hub: dict
    tested: int = 0
    accepted: int = 0
    review: int = 0
    unresolved: list[dict] = field(default_factory=list)
    mode: str = "inclusion"      # or "names_only"
    copies: int = 0              # same_entity edges written


def _owner(table: str) -> str:
    return table.split(SEP, 1)[0]


def unresolved_references(sl: dict, stats: dict | None = None) -> list[tuple[str, str]]:
    """(table, column) pairs that may point outside their document.

    Every column Link would test as a child inside one layer -- key roles,
    identifiers, key-named columns, low-cardinality numeric dimensions --
    that ended up with no `foreign_key` target. Link only promotes a column
    to `foreign_key` when it finds the parent, so a key whose parent lives
    in another document keeps its profile role; looking only at the role
    would miss exactly the references stitch exists for. ETL bookkeeping
    columns (`etl_batch_id`, `record_hash`) are excluded: they are keys to
    nothing in any document.
    """
    from semlayer.link.candidates import _KEYISH_RE
    from semlayer.tier import ETL_METADATA_COLUMNS
    stats = stats if stats is not None else stats_from_documents(sl)
    _, children = select_parents_and_children(stats, {"semantic_layer": sl})
    cols = {(t["name"], c["name"]): c for t in sl.get("tables", []) for c in t.get("columns", [])}
    out = []
    for tn, cn in children:
        c = cols[(tn, cn)]
        if c.get("foreign_key") or cn.lower() in ETL_METADATA_COLUMNS:
            continue
        # a reference looks like a key: by role or by name. A low-cardinality
        # attribute (`segment`, `status`) is a child Link tests on the chance
        # of a lookup table, but listing it as "unresolved" would be noise.
        if c.get("entity_role") in ("foreign_key", "identifier") or _KEYISH_RE.search(cn.lower()):
            out.append((tn, cn))
    return out


def stats_from_documents(sl: dict) -> dict[str, TableStats]:
    """TableStats for Link's candidate machinery, built from what the documents already say.

    Distinct and null counts come from `cardinality`, uniqueness from the key
    roles, and the row count is the largest `distinct + nulls` any column
    reports, which is exact for a column with no duplicates and a floor
    otherwise. Ranges are unknown, so the range filters fail open, as they
    do for strings inside Link. `source` gives the schema and table the
    inclusion statements name.
    """
    out: dict[str, TableStats] = {}
    for t in sl.get("tables", []):
        if t.get("modeling") == "inventory_only":
            continue
        schema, _, bare = (t.get("source") or t["name"].split(SEP)[-1]).rpartition(".")
        cols: dict[str, ColumnStats] = {}
        row_count = 0
        for c in t.get("columns", []):
            card = c.get("cardinality") or {}
            distinct = int(card.get("distinct") or 0)
            nulls = int(card.get("null_count") or 0)
            row_count = max(row_count, distinct + nulls)
            cols[c["name"]] = ColumnStats(
                name=c["name"], sql_type=c.get("sql_type", ""), row_count=0, n_null=nulls,
                n_distinct=distinct,
                is_unique=c.get("entity_role") in ("primary_key", "unique_key"))
        for cs in cols.values():
            cs.row_count = row_count
        meta = TableMeta(schema=schema or "main", name=bare,
                         columns=tuple(ColumnMeta(c.name, c.sql_type) for c in cols.values()))
        out[t["name"]] = TableStats(table=meta, row_count=row_count, columns=cols)
    return out


def _prefer_certified(cands: list[FKCandidate], tables: dict) -> list[FKCandidate]:
    """Among a child's equally-named parents, a certified table beats an inferred one.

    The platform team's conformed dimension and a staging copy of it hold
    the same keys and score the same on names; certification is the one
    signal that says which one the organisation stands behind.
    """
    by_child: dict[tuple, list[FKCandidate]] = {}
    for c in cands:
        by_child.setdefault((c.child_table, c.child_column), []).append(c)
    out: list[FKCandidate] = []
    for group in by_child.values():
        top = max(c.name_score for c in group)
        tied = [c for c in group if c.name_score == top]
        certified = [c for c in tied
                     if (tables.get(c.parent_table) or {}).get("lifecycle") == "certified"]
        if len(tied) > 1 and certified:
            drop = {c.key() for c in tied if c not in certified}
            group = [c for c in group if c.key() not in drop]
        out.extend(group)
    return out


def _edge(c: FKCandidate, confidence: float, provenance: list[dict]) -> dict:
    return {
        "name": f"{c.child_table}.{c.child_column}_to_{c.parent_table}".replace(SEP, "_"),
        "from": {"table": c.child_table, "columns": [c.child_column]},
        "to": {"table": c.parent_table, "columns": [c.parent_column]},
        "cardinality": "many_to_one", "join_type": "left", "fanout_risk": False,
        "lifecycle": "inferred", "confidence": round(confidence, 2),
        "provenance": provenance,
    }


def _names_only(rs: ResolvedSet, stats: dict, children: list[tuple[str, str]]) -> list[dict]:
    """Edges proposed from names and distinct counts alone, when no connection can test them."""
    sl = rs.doc["semantic_layer"]
    parents, _ = select_parents_and_children(stats, rs.doc)
    index = ParentIndex(parents, stats)
    edges = []
    for ctn, ccn in children:
        best = None
        for ptn, pcn in index.ranked(ccn, ctn):
            if _owner(ptn) == _owner(ctn):
                continue
            if stats[ptn].columns[pcn].n_distinct < stats[ctn].columns[ccn].n_distinct:
                continue
            best = (ptn, pcn)
            break
        if best is None:
            continue
        ptn, pcn = best
        score = name_similarity(ccn, ctn, pcn, ptn)
        if score < AUTO_NAME_THRESHOLD:
            continue
        c = FKCandidate(ctn, ccn, ptn, pcn, 0.0, stats[ctn].columns[ccn].n_distinct,
                        stats[ptn].columns[pcn].n_distinct, score)
        edges.append(_edge(c, NAMES_ONLY_CONFIDENCE, [
            {"signal": "naming", "detail": f"name score {score}; NOT tested: no connection "
                                          f"could reach both documents' warehouses"}]))
    _ = sl
    return edges


def _load_members(member_paths: list[str | Path], hub_name: str,
                  out_dir: Path) -> tuple[dict[str, dict], list[dict]]:
    """Member documents by name, and the hub's `members` entries with paths relative to it."""
    members: dict[str, dict] = {}
    entries = []
    for mp in member_paths:
        doc = load_yaml(Path(mp).read_text())
        name = doc["semantic_layer"]["name"]
        if name in members or name == hub_name:
            raise ValueError(f"two documents are called '{name}'; names must be unique in a set")
        members[name] = doc
        try:
            rel = Path(mp).resolve().relative_to(out_dir)
        except ValueError:
            rel = Path(mp).resolve()
        entries.append({"name": name, "path": str(rel)})
    return members, entries


def _tested_edges(source, rs: ResolvedSet, stats: dict, children: list[tuple[str, str]],
                  tables: dict, query_workers: int, result: StitchResult) -> list[dict]:
    """Edges from inclusion tests through `source`: Link's own pipeline, across documents."""
    cands = generate_candidates(
        source, stats, rs.doc, query_workers=query_workers,
        only_children=set(children),
        parent_ok=lambda ctn, ptn: _owner(ctn) != _owner(ptn))
    result.tested = len(cands)
    cands = _resolve_directions(cands, stats)
    cands = _prefer_certified(cands, tables)
    pk_by_table = {n: t.get("primary_key") or [] for n, t in tables.items()}
    accepted, review, _killed, _ = _decide_candidates(cands, {}, None, lineage=None,
                                                      pk_by_table=pk_by_table)
    edges = []
    for c, _v in accepted:
        edges.append(_edge(c, min(0.9, 0.6 + 0.2 * c.name_score), [
            {"signal": "inclusion_dependency",
             "detail": f"inclusion {c.inclusion_ratio} over {c.child_distinct} distinct "
                       f"child values; name score {c.name_score}"}]))
    for c, _v in review:
        edges.append(_edge(c, REVIEW_CONFIDENCE, [
            {"signal": "inclusion_dependency",
             "detail": f"inclusion {c.inclusion_ratio} over {c.child_distinct} distinct "
                       f"child values; name score {c.name_score}: statistics only, "
                       f"review before relying on it"}]))
    result.accepted, result.review = len(accepted), len(review)
    return edges


def _single_keys(sl: dict, stats: dict) -> dict[str, str]:
    """Table -> its entity key, for every modelled table that has a single one.

    The primary key when it is one column. For an SCD2 table the primary key
    is the natural key plus the validity start (`ticket_id, valid_from`), and
    the entity key is the natural key: a copy of the dimension holds tickets,
    not ticket versions.
    """
    out: dict[str, str] = {}
    for t in sl.get("tables", []):
        if t["name"] not in stats:
            continue
        pk = list(t.get("primary_key") or [c["name"] for c in t.get("columns", [])
                                           if c.get("entity_role") == "primary_key"])
        scd = t.get("scd") or {}
        if scd.get("type") == 2 and len(pk) == 2:
            pk = [c for c in pk if c not in (scd.get("valid_from"), scd.get("valid_to"))]
        if len(pk) == 1 and pk[0] in stats[t["name"]].columns:
            out[t["name"]] = pk[0]
    return out


# Key names that name no entity: a surrogate every fact in a warehouse may
# carry under the same name. Two tables sharing `event_id` are not thereby
# the same entity; on a 5,000-table estate 460 facts had one, and pairing
# them gave 97,835 candidates, all wrong.
GENERIC_KEYS = frozenset({
    "id", "pk", "sk", "key", "row_id", "rowid", "record_id", "event_id", "seq", "sequence",
    "surrogate_key", "hash_key", "uuid", "guid",
})


def copy_pairs(sl: dict, stats: dict) -> list[tuple[str, str, str, str, float]]:
    """(table_a, key_a, table_b, key_b, name_score) for key columns that could be one entity.

    Two tables in different members, each with a single-column key of the
    same type family that names an entity (not `id` or `event_id`), whose key
    columns share a name or score as Link's name test would. Each unordered
    pair once; the test decides direction. A table keeps at most
    MAX_QUERIES_PER_CHILD partners per other member, best-named first, so
    the work is bounded the way Link's is.
    """
    keys = {t: k for t, k in _single_keys(sl, stats).items() if k.lower() not in GENERIC_KEYS}
    items = sorted(keys.items())

    def score_of(ta: str, ka: str, tb: str, kb: str) -> float:
        score = max(name_similarity(ka, ta, kb, tb), name_similarity(kb, tb, ka, ta))
        return max(score, AUTO_NAME_THRESHOLD) if ka.lower() == kb.lower() else score

    scored: dict[tuple[str, str], list[tuple[float, str]]] = {}
    for i, (ta, ka) in enumerate(items):
        fa = _type_family(stats[ta].columns[ka].sql_type)
        for tb, kb in items[i + 1:]:
            if _owner(ta) == _owner(tb) or _type_family(stats[tb].columns[kb].sql_type) != fa:
                continue
            score = score_of(ta, ka, tb, kb)
            if score >= AUTO_NAME_THRESHOLD:
                scored.setdefault((ta, _owner(tb)), []).append((score, tb))
                scored.setdefault((tb, _owner(ta)), []).append((score, ta))
    kept: set[tuple[str, str]] = set()
    for (t, _), lst in scored.items():
        lst.sort(key=lambda x: (-x[0], x[1]))
        for _, other in lst[:MAX_QUERIES_PER_CHILD]:
            kept.add((min(t, other), max(t, other)))
    return [(ta, keys[ta], tb, keys[tb], score_of(ta, keys[ta], tb, keys[tb]))
            for ta, tb in sorted(kept)]


def _overlaps(source, stats: dict, pairs: list[tuple[str, str, str, str, float]],
              query_workers: int) -> dict[tuple[str, str], float]:
    """Inclusion ratio of each table's key in its partner's, both directions.

    One statement per table with all its partners, run through Link's own
    fan-out (`_inclusion_many`), which gives each worker thread its own
    executor: a connection shared across threads interleaves result rows.
    """
    by_table: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for ta, ka, tb, kb, _ in pairs:
        by_table.setdefault((ta, ka), []).append((tb, kb))
        by_table.setdefault((tb, kb), []).append((ta, ka))
    plan = [(ctn, ccn, ptn, pcn) for (ctn, ccn), partners in by_table.items()
            for ptn, pcn in partners]
    ratios = _inclusion_many(source, stats, plan, query_workers)
    return {(ctn, ptn): r for (ctn, _, ptn, _), r in zip(plan, ratios, strict=True)}


def _canonical(ta: str, tb: str, a_in_b: float, b_in_a: float, tables: dict) -> str | None:
    """Which of two copies is the system of record: a certified one, else the key superset."""
    cert = [t for t in (ta, tb) if (tables.get(t) or {}).get("lifecycle") == "certified"]
    if len(cert) == 1:
        return cert[0]
    if a_in_b >= MIN_INCLUSION and b_in_a < MIN_INCLUSION:
        return tb
    if b_in_a >= MIN_INCLUSION and a_in_b < MIN_INCLUSION:
        return ta
    return None


def _copy_edge(pair: tuple[str, str, str, str], tables: dict, overlap: dict | None,
               confidence: float, provenance: list[dict], canonical: str | None) -> dict:
    """The edge for (copy, canonical table, copy key, canonical key)."""
    copy, canon, kcopy, kcanon = pair
    cols_copy = [c["name"] for c in tables[copy].get("columns", [])]
    cols_canon = {c["name"].lower() for c in tables[canon].get("columns", [])}
    edge = {
        "name": f"{copy}_same_entity_as_{canon}".replace(SEP, "_"),
        "from": {"table": copy, "columns": [kcopy]},
        "to": {"table": canon, "columns": [kcanon]},
        "cardinality": "one_to_one", "join_type": "left", "fanout_risk": False,
        "same_entity": True,
        "shared_columns": [c for c in cols_copy if c.lower() in cols_canon],
        "extends_with": [c for c in cols_copy if c.lower() not in cols_canon],
        "lifecycle": "inferred", "confidence": round(confidence, 2),
        "provenance": provenance,
    }
    if canonical:
        edge["canonical"] = canonical
    if overlap:
        edge["key_overlap"] = overlap
    return edge


def _bare(table: str) -> str:
    return table.rsplit(SEP, 1)[-1].lower()


def _best_partner(edges: list[dict]) -> list[dict]:
    """One same_entity edge per copy per other member.

    Best-named first; among equal names a partner in the same naming layer
    (a published `dim_ticket` for a published `dim_ticket`, not the
    `int__ticket` that holds the same keys), then the one whose bare name
    matches, then the fuller one.
    """
    by: dict[tuple[str, str], dict] = {}
    for e in edges:
        a, b = e["from"]["table"], e["to"]["table"]
        k = (a, _owner(b))
        ov = e.get("key_overlap") or {}
        rank = (e.get("canonical") is not None, e["_score"], same_naming_layer(a, b),
                _bare(a) == _bare(b), ov.get("to_in_from", 0.0))
        if k not in by or rank > by[k]["_rank"]:
            e["_rank"] = rank
            by[k] = e
    out = []
    for e in by.values():
        e.pop("_rank", None), e.pop("_score", None)
        out.append(e)
    return out


def copy_edges(source, sl: dict, stats: dict, tables: dict, query_workers: int) -> list[dict]:
    """same_entity edges between members, tested through `source` (names only when None)."""
    pairs = copy_pairs(sl, stats)
    if not pairs:
        return []
    edges: list[dict] = []
    if source is None:
        for ta, ka, tb, kb, score in pairs:
            da, db = stats[ta].columns[ka].n_distinct, stats[tb].columns[kb].n_distinct
            pair = (ta, tb, ka, kb) if da <= db else (tb, ta, kb, ka)
            e = _copy_edge(pair, tables, None, NAMES_ONLY_CONFIDENCE, [
                {"signal": "naming", "detail": f"same key name, {da} and {db} distinct values; "
                                              f"NOT tested: no connection could reach both "
                                              f"documents' warehouses"}], None)
            e["_score"] = score
            edges.append(e)
        return _best_partner(edges)
    ratios = _overlaps(source, stats, pairs, query_workers)
    for ta, ka, tb, kb, score in pairs:
        a_in_b, b_in_a = ratios.get((ta, tb), 0.0), ratios.get((tb, ta), 0.0)
        if max(a_in_b, b_in_a) < MIN_INCLUSION:
            continue
        canon = _canonical(ta, tb, a_in_b, b_in_a, tables)
        if canon == ta:
            copy, canon_t, kc, kn, c_in_n, n_in_c = tb, ta, kb, ka, b_in_a, a_in_b
        else:
            copy, canon_t, kc, kn, c_in_n, n_in_c = ta, tb, ka, kb, a_in_b, b_in_a
        overlap = {"from_in_to": round(c_in_n, 3), "to_in_from": round(n_in_c, 3)}
        detail = (f"{copy} keys in {canon_t}: {overlap['from_in_to']}; the reverse "
                  f"{overlap['to_in_from']}; name score {score}")
        if canon is None:
            e = _copy_edge((copy, canon_t, kc, kn), tables, overlap, REVIEW_CONFIDENCE, [
                {"signal": "inclusion_dependency",
                 "detail": detail + ": the same keys and neither is certified, so no "
                                    "canonical copy is named; review"}], None)
        else:
            why = ("certified" if (tables.get(canon_t) or {}).get("lifecycle") == "certified"
                   else "holds every key of the other")
            e = _copy_edge((copy, canon_t, kc, kn), tables, overlap, COPY_CONFIDENCE, [
                {"signal": "inclusion_dependency",
                 "detail": detail + f"; canonical {canon_t} ({why})"}], canon_t)
        e["_score"] = score
        edges.append(e)
    return _best_partner(edges)



def stitch(member_paths: list[str | Path], hub_name: str, out_path: str | Path,
           source=None, query_workers: int = 4, via_label: str | None = None) -> StitchResult:
    """Build the hub for these members; `source` is the executor for inclusion tests (or None)."""
    members, entries = _load_members(member_paths, hub_name, Path(out_path).resolve().parent)
    hub = {"semantic_layer": {
        "spec_version": "0.6.0", "name": hub_name,
        "generated_by": {"engine": __version__, "model": "none/heuristic"},
        "members": entries,
    }}
    rs = resolve(hub, members)
    sl = rs.doc["semantic_layer"]
    stats = stats_from_documents(sl)
    children = unresolved_references(sl, stats)
    tables = {t["name"]: t for t in sl.get("tables", [])}
    result = StitchResult(hub=hub)
    if source is None:
        edges = _names_only(rs, stats, children)
        result.mode = "names_only"
    else:
        edges = _tested_edges(source, rs, stats, children, tables, query_workers, result)
    found = {(e["from"]["table"], e["from"]["columns"][0]) for e in edges}
    same = copy_edges(source, sl, stats, tables, query_workers)
    result.copies = len(same)
    edges = edges + same
    result.unresolved = [
        {"table": ctn, "column": ccn,
         "note": f"no parent in any other member "
                 f"({stats[ctn].columns[ccn].n_distinct} distinct values)"}
        for ctn, ccn in children if (ctn, ccn) not in found]
    if edges:
        hub["semantic_layer"]["relationships"] = edges
    if result.unresolved:
        hub["semantic_layer"]["unresolved"] = result.unresolved
    via = f" via {via_label}" if via_label else ""
    logger.info("stitch: %d unresolved references, %d tested, %d accepted, %d for review, "
                "%d unresolved, %d same-entity copies (%s%s)", len(children), result.tested,
                result.accepted, result.review, len(result.unresolved), result.copies,
                result.mode, via)
    return result
