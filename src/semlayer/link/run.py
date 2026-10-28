# SPDX-License-Identifier: FSL-1.1-ALv2
"""Link stage runner: profile document -> +FKs, +relationships, +table types.

Decision policy per candidate (the confidently-wrong defense, adversarial B2):
- AUTO-INCLUDE requires statistics (inclusion >= 0.95) AND corroboration:
  strong naming agreement (>= 0.6) AND, when an LLM is present, a plausible
  verdict. Statistics alone NEVER auto-include.
- Statistically-strong candidates with weak naming are the trap shape: they
  go to the LLM; survivors land in the REVIEW QUEUE (lifecycle inferred,
  low confidence), never auto-included.
- LLM-implausible kills the candidate; the kill is recorded (provenance of
  absence matters for the audit sample).
"""

from __future__ import annotations

import logging

from semlayer.lineage import same_naming_layer
from semlayer.link.candidates import (
    FKCandidate,
    _inclusion,
    _key_capable,
    _name_tokens,
    _type_family,
    generate_candidates,
)
from semlayer.link.table_type import classify_table
from semlayer.profile.typing_rules import normalise_name

logger = logging.getLogger(__name__)

AUTO_NAME_THRESHOLD = 0.6
REVIEW_CONFIDENCE = 0.45

_REC_HINT = ("mgr", "manager", "parent", "sup", "boss", "report", "referrer", "ref_by")


def _affinity(tn: str, cn: str) -> int:
    """Count name tokens `cn` shares with its own table `tn` (self-affinity)."""
    return len(_name_tokens(cn, tn) & _name_tokens(tn, tn))


def _resolve_directions(candidates: list[FKCandidate], stats_by_table: dict) -> list[FKCandidate]:
    """Flip symmetric unique<->unique candidates so the true entity-table PK is the parent.

    If both A.x->B.y and B.y->A.x are candidates with equal distinct counts,
    naming affinity decides which side is the entity table (the parent).
    """
    fixed = []
    for c in candidates:
        both_unique = (stats_by_table[c.child_table].columns[c.child_column].is_unique
                       and stats_by_table[c.parent_table].columns[c.parent_column].is_unique)
        inverted = (both_unique
                    and c.child_distinct == c.parent_distinct
                    and _affinity(c.child_table, c.child_column)
                    > _affinity(c.parent_table, c.parent_column))
        if inverted:
            c = FKCandidate(c.parent_table, c.parent_column, c.child_table, c.child_column,
                            c.inclusion_ratio, c.parent_distinct, c.child_distinct, c.name_score)
        fixed.append(c)
    return fixed


def _best_parent_per_child(
    candidates: list[FKCandidate], pk_by_table: dict[str, list[str]] | None = None,
    lineage=None,
) -> tuple[dict[tuple, FKCandidate], set[tuple]]:
    """Pick one parent per child column, and say which picks the ETL decided.

    Ranking was `(name_score, inclusion_ratio)` with replacement on STRICTLY
    greater -- so when two candidate parents tied on both, the winner was
    whichever came first out of a dict. That is not a judgement, and it was
    wrong in a measurable way: `person` and `int__person` in dbt_synthea hold
    identical key sets, so uniqueness, inclusion and naming all tie, and 27 of
    27 person_id edges went to the staging step rather than the published table
    the project's own tests declare (docs/calibration.md, 2026-10-12).

    **The pk guard applies only when an ETL manifest was supplied. The layer
    rule applies always**, reading the stage from the manifest when there is
    one and from the table name when there is not (2026-10-16). The guarantee
    that replaced "no manifest means no behaviour change" is narrower and is
    what messy_mart actually relies on: where no name carries a stage prefix
    every candidate sits in the same "" layer, the term ties, and the ranking
    is exactly what it always was. Both properties are tested by name in
    tests/test_lineage.py.

    1. **The ETL builds one candidate from the other.** Among otherwise-tied
       candidates, the DOWNSTREAM table is the published one and the upstream
       is a staging step on the way to it. `lineage.prefer` returns None for
       unrelated tables, so a manifest only breaks ties it actually knows
       about.

    2. **A guard on rule 1: prefer the candidate whose column is the parent's
       primary key.** Needed because "downstream" is not "the published entity
       table". A first version applied rule 1 alone and moved 26 person_id
       edges from `int__person` to `observation_period` -- a FACT table
       downstream of the dimension, whose own key is `observation_period_id`.

    3. **Co-location in the ETL outranks name similarity.** When several
       parents fully contain the child's values, the one the declaring model
       meant is overwhelmingly the one in its OWN stage of the pipeline. This
       sits FIRST in the tuple, ahead of `name_score`, because the competing
       parents here are near-identical by name -- `stg_synthea__encounters` and
       `int__encounters` score 1.2 and 1.2 -- so any ranking that defers to
       naming cannot separate them. Inert without a manifest and inert for
       tables whose layer is unknown, so the only runs affected are ones that
       supplied an ETL that states its own layout.

       Measured as a within-harness comparison on dbt_synthea (same selection
       code, layer rule on vs off): 71 gold edges gained, 7 lost, and wrong
       parents on gold-mentioned columns fell from 77 to 13. It is NOT
       `prefer()`: downstream is right for person/int__person and wrong for the
       encounters pairs, where gold is the upstream staging table.

    The pk guard is NOT an independent signal and must not be promoted to one.
    Measured alone it changed no recall at all (26.0% either way on
    dbt_synthea), and applied without rule 1 it BREAKS messy_mart: the right
    parent for `prod_id` is `prod_ref`, but `sku_xref.prod_id` is both
    sku_xref's primary key AND itself a foreign key to prod_ref, so preferring
    a primary key picks the cross-reference table and FK precision falls 0.973
    -> 0.892 against a 0.95 floor. A column being a key where it is stored
    says nothing about whether the entity originates there.
    """
    pks = pk_by_table or {}

    def _is_pk(c: FKCandidate) -> bool:
        return c.parent_column in (pks.get(c.parent_table) or ())

    def _same_layer(c: FKCandidate) -> bool:
        """Co-location in the ETL: from the manifest, else from the names.

        The manifest is better evidence and wins wherever it speaks. Where it
        does not -- no manifest at all, or tables it does not know -- the names
        still carry the convention, so the signal degrades rather than
        vanishing. Measured on dbt_synthea with no manifest: 57 of 62 wrong
        parents are wrong only because this tiebreak was inert.
        """
        # Naming is the fallback for NO manifest, not a per-table top-up. A
        # first version consulted names whenever the manifest did not know a
        # table, which changed the manifest path's answers in four unit tests:
        # a name-derived layer is weaker evidence and must not interleave with
        # a manifest's own judgement.
        if lineage:
            return lineage.same_layer(c.parent_table, c.child_table)
        return same_naming_layer(c.parent_table, c.child_table)

    best: dict[tuple, FKCandidate] = {}
    lineage_wins: set[tuple] = set()
    for c in candidates:
        key = (c.child_table, c.child_column)
        prev = best.get(key)
        if prev is None:
            best[key] = c
            continue
        # The pk guard is inert without a manifest: it exists only to stop the
        # lineage rule below walking downstream onto a fact table, and on its
        # own it is actively wrong (see the docstring).
        # The pk guard stays gated on having a manifest. Ungating it was tried
        # and reverted: messy_mart fell to 0.892 against its 0.95 floor, with
        # sku_xref beating prod_ref for prod_id -- the exact failure the
        # docstring describes. The reason the reasoning was wrong is worth
        # keeping: "the guard is safe once the layer rule is active" assumes
        # the layer rule DECIDES, and in messy_mart prod_ref and sku_xref are
        # both unprefixed, so the layer term ties and the guard acts alone.
        score = (_same_layer(c), c.name_score, c.inclusion_ratio,
                 bool(lineage) and _is_pk(c))
        prev_score = (_same_layer(prev), prev.name_score, prev.inclusion_ratio,
                      bool(lineage) and _is_pk(prev))
        if score > prev_score:
            best[key] = c
            lineage_wins.discard(key)
        elif score == prev_score and lineage:
            winner = lineage.prefer(c.parent_table, prev.parent_table)
            if winner == c.parent_table and winner != prev.parent_table:
                best[key] = c
                lineage_wins.add(key)
            elif winner == prev.parent_table and winner != c.parent_table:
                lineage_wins.add(key)
    return best, {best[k].key() for k in lineage_wins}


def _decide_candidates(
    candidates: list[FKCandidate], verdicts: dict, llm, lineage=None,
    pk_by_table: dict[str, list[str]] | None = None,
) -> tuple[list[tuple[FKCandidate, dict | None]], list[tuple[FKCandidate, dict | None]],
           list[tuple[FKCandidate, dict | None]], set[tuple]]:
    """Bucket candidates into (accepted, review, killed) per the corroboration rule.

    Direction resolution for symmetric unique<->unique pairs (1:1/xref):
    for each child column, only its single best-scored candidate parent
    survives to the accept/review/kill decision (best_for_child).
    """
    best_for_child, lineage_wins = _best_parent_per_child(
        candidates, pk_by_table, lineage)
    surviving = _drop_alternate_key_paths(best_for_child.values())

    accepted, review, killed = [], [], []
    for c in surviving:
        v = verdicts.get(c.key())
        llm_plausible = (v or {}).get("verdict") == "plausible"
        llm_implausible = (v or {}).get("verdict") == "implausible"
        if llm_implausible:
            killed.append((c, v))
            continue
        corroborated = c.name_score >= AUTO_NAME_THRESHOLD
        if corroborated and (llm is None or llm_plausible):
            accepted.append((c, v))
        else:
            review.append((c, v))
    return accepted, review, killed, lineage_wins


def _drop_alternate_key_paths(candidates) -> list[FKCandidate]:
    """Within one (child table, parent table) pair, keep ONE parent column.

    Two candidates between the same pair of tables mean one of two very
    different things:

    - **Role-playing**: different child columns referencing the SAME parent
      column. `cs_sold_date_sk` and `cs_ship_date_sk` both point at
      `date_dim.d_date_sk`, and both are real -- order date and ship date are
      different questions. These must ALL survive.
    - **Alternate keys**: child columns referencing DIFFERENT parent columns.
      `ord_hdr.date_key -> date_dim.date_key` is the surrogate path and
      `ord_hdr.ord_dt -> date_dim.cal_dt` is the same relationship through the
      natural key. Both have 100% inclusion and a unique parent, so statistics
      cannot separate them -- but asserting both hands the compiler two
      equally-short paths between the tables, and it refuses to group through
      the ambiguity. The redundant path costs a capability.

    So: pick the single best-attested parent column for the pair, and keep
    every candidate that references it. Role-playing survives because its
    candidates all share that column; the natural-key alternate is dropped
    because it references a different one.

    Needed only because the candidate gate now admits statistically-shaped
    parents: natural-key paths were previously invisible, so the ambiguity
    could not arise.
    """
    by_pair: dict[tuple, list[FKCandidate]] = {}
    for c in candidates:
        by_pair.setdefault((c.child_table, c.parent_table), []).append(c)
    out: list[FKCandidate] = []
    for group in by_pair.values():
        best_col = max(group, key=lambda c: (c.name_score, c.inclusion_ratio)).parent_column
        out.extend(c for c in group if c.parent_column == best_col)
    return out


def _apply_accepted(accepted: list, review: list, cols: dict,
                    lineage_wins: set | frozenset = frozenset()) -> None:
    """Write foreign_key/confidence/provenance onto accepted columns; provenance-only for review."""
    for c, v in accepted:
        col = cols[(c.child_table, c.child_column)]
        col["entity_role"] = "foreign_key"
        col["foreign_key"] = {
            "references": f"{c.parent_table}.{c.parent_column}",
            "relationship": "many_to_one",
        }
        col["confidence"] = round(min(0.9, 0.6 + 0.2 * c.name_score + (0.1 if v else 0.0)), 2)
        col.setdefault("provenance", []).append({
            "signal": "inclusion_dependency",
            "detail": f"ratio {c.inclusion_ratio}, name score {c.name_score}"
                      + (f", llm: {v['reason']}" if v else ""),
        })
        if c.key() in lineage_wins:
            col.setdefault("provenance", []).append({
                "signal": "etl_lineage",
                "detail": f"parent {c.parent_table} chosen over a statistically "
                          f"identical candidate: the ETL builds it downstream",
            })
    for c, v in review:
        col = cols[(c.child_table, c.child_column)]
        col.setdefault("provenance", []).append({
            "signal": "inclusion_dependency",
            "detail": f"REVIEW-QUEUED candidate -> {c.parent_table}.{c.parent_column} "
                      f"(ratio {c.inclusion_ratio}, name score {c.name_score}"
                      + (f", llm: {v['reason']}" if v else ", no llm") + ")",
        })


def _emit_relationships(accepted: list, cols: dict, doc: dict,
                        declared_edges: list | None = None) -> tuple[dict, dict]:
    """Build the relationships block from accepted FKs; return fk_out/fk_in fan counts.

    Declared edges are emitted FIRST and at higher confidence than anything we
    discover. A catalog FK is a human saying which tables join, which beats our
    naming heuristics -- the same argument that made native comments outrank a
    generated description. It is still a CLAIM: Snowflake and BigQuery never
    enforce, and Postgres allows NOT VALID, so `enforced` rides in the
    provenance instead of being flattened away.
    """
    rels = []
    fk_out: dict[str, int] = {}
    fk_in: dict[str, int] = {}
    for child, ccols, parent, pcols, enforced in (declared_edges or []):
        rels.append({
            "name": f"{child}_to_{parent}",
            "from": {"table": child, "columns": ccols},
            "to": {"table": parent, "columns": pcols},
            "cardinality": "many_to_one",
            "join_type": "left",
            "fanout_risk": False,
            "lifecycle": "inferred",
            "confidence": 0.95 if enforced else 0.85,
            "provenance": [{
                "signal": "catalog_prior",
                "detail": ("FOREIGN KEY declared in the warehouse catalog"
                           + ("" if enforced else "; NOT enforced by the engine")),
            }],
        })
        fk_out[child] = fk_out.get(child, 0) + 1
        fk_in[parent] = fk_in.get(parent, 0) + 1
        if len(ccols) == 1:
            col = cols.get((child, ccols[0]))
            if col is not None:
                col["entity_role"] = "foreign_key"
                col["foreign_key"] = {"references": f"{parent}.{pcols[0]}",
                                      "relationship": "many_to_one"}
                col["confidence"] = 0.95 if enforced else 0.85
                col.setdefault("provenance", []).append({
                    "signal": "catalog_prior",
                    "detail": "FOREIGN KEY declared in the warehouse catalog",
                })
    for c, _v in accepted:
        rels.append({
            "name": f"{c.child_table}_to_{c.parent_table}",
            "from": {"table": c.child_table, "columns": [c.child_column]},
            "to": {"table": c.parent_table, "columns": [c.parent_column]},
            "cardinality": "many_to_one",
            "join_type": "left",
            "fanout_risk": False,
            "lifecycle": "inferred",
            "confidence": cols[(c.child_table, c.child_column)]["confidence"],
        })
        fk_out[c.child_table] = fk_out.get(c.child_table, 0) + 1
        fk_in[c.parent_table] = fk_in.get(c.parent_table, 0) + 1
    # reverse direction of each accepted many_to_one is a fan-out for the parent side
    for r in rels:
        r["fanout_risk"] = True  # traversing one-to-many from parent side multiplies rows
    if rels:
        doc["semantic_layer"]["relationships"] = rels
    return fk_out, fk_in


def _detect_self_ref_hierarchy(t: dict, stats_by_table: dict, source) -> list[dict]:
    """Detect a column referencing the table's own PK (org-chart style).

    Requires a SEMANTIC hint in the name — numeric ⊆-coincidences against
    the PK are rampant (TPC-DS i_class_id ⊆ i_item_sk), so inclusion alone
    never creates a recursive hierarchy.
    """
    tn = t["name"]
    pk = (t.get("primary_key") or [None])[0]
    if not pk:
        return []
    # Folded, not lowercased. `ParentAccountKey` lowercases to
    # "parentaccountkey", which does not END with "_key", so every
    # self-referencing key on a PascalCase warehouse was invisible here --
    # AdventureWorksDW declares four (Account, DepartmentGroup, Employee,
    # Organization) and produced zero recursive hierarchies. candidates.py
    # skips same-table parents on the grounds that this path handles them, so
    # they fell between the two mechanisms and were represented nowhere.
    #
    # Measured after the fold: THREE of the four, not four. DimDepartmentGroup
    # is 7 rows where every child points at the single root, so
    # ParentDepartmentGroupKey has n_distinct = 1 and the guard below drops it.
    # The guard is right to exist -- numeric coincidences against a PK are
    # rampant -- but n_distinct < 2 is also the signature of a legitimate
    # two-level org chart, so it costs a real edge. Raising it needs a
    # different discriminator than cardinality, not a lower threshold.
    keyish = [c for c in t["columns"]
              if c["name"] != pk
              and normalise_name(c["name"]).endswith(("_id", "_key", "_sk"))]
    hierarchies = []
    for c in keyish:
        if not any(h in normalise_name(c["name"]) for h in _REC_HINT):
            continue
        cs = stats_by_table[tn].columns[c["name"]]
        ps = stats_by_table[tn].columns[pk]
        if cs.is_unique or cs.n_distinct < 2:
            continue
        if _type_family(cs.sql_type) != _type_family(ps.sql_type):
            continue
        if _inclusion(source, stats_by_table, tn, c["name"], tn, pk) >= 0.95:
            hierarchies.append({
                "name": f"{tn}_{c['name']}_hierarchy", "kind": "recursive",
                "dimension_table": tn,
                "recursive": {"parent_column": c["name"], "child_column": pk},
                "lifecycle": "inferred", "confidence": 0.7,
                "provenance": [{"signal": "inclusion_dependency",
                                "detail": f"{c['name']} values ⊆ {pk} (self-reference)"}],
            })
    return hierarchies


def _detect_bom_hierarchy(t: dict) -> list[dict]:
    """Detect parent_X / child_X column pairs (bill-of-materials style)."""
    tn = t["name"]
    by_suffix: dict[str, dict] = {}
    for c in t["columns"]:
        m = c["name"].lower()
        if m.startswith("parent_"):
            by_suffix.setdefault(m[7:], {})["parent"] = c["name"]
        elif m.startswith("child_"):
            by_suffix.setdefault(m[6:], {})["child"] = c["name"]
    hierarchies = []
    for suffix, pair in by_suffix.items():
        if "parent" in pair and "child" in pair:
            hierarchies.append({
                "name": f"{tn}_{suffix}_hierarchy", "kind": "recursive",
                "dimension_table": tn,
                "recursive": {"parent_column": pair["parent"], "child_column": pair["child"]},
                "lifecycle": "inferred", "confidence": 0.75,
                "provenance": [{"signal": "naming",
                                "detail": f"parent_/child_ column pair over '{suffix}'"}],
            })
    return hierarchies


def _detect_recursive_hierarchies(doc: dict, stats_by_table: dict, source) -> list[dict]:
    """Detect self-referential (org-chart) and parent_/child_ (BOM) recursive structures."""
    hierarchies = []
    for t in _modelled(doc):
        hierarchies.extend(_detect_self_ref_hierarchy(t, stats_by_table, source))
        hierarchies.extend(_detect_bom_hierarchy(t))
    return hierarchies


def _modelled(doc: dict) -> list[dict]:
    """The tables Link works on: everything not set aside as inventory-only.

    An inventory-only table (semlayer.tier: raw and scratch tiers) keeps its
    shape and nothing else -- no table type, no composite-key probing, no
    hierarchies, no candidacy on either side of a foreign key.
    """
    return [t for t in doc["semantic_layer"]["tables"] if t.get("modeling") != "inventory_only"]


def _classify_tables(doc: dict, fk_out: dict, fk_in: dict) -> None:
    """Assign table_type + confidence/provenance to every modelled table in place."""
    for t in _modelled(doc):
        ttype, conf, detail = classify_table(t, fk_out.get(t["name"], 0), fk_in.get(t["name"], 0))
        t["table_type"] = ttype
        # confidence in the LABEL, not a quality score -- see docs/calibration.md
        t["confidence"] = conf
        t.setdefault("provenance", []).append(
            {"signal": "statistic", "detail": f"table_type: {detail}"}
        )


# ------------------------------------------------- composite primary keys
# PK detection picked the best single unique identifier column, so a table
# whose key is a COLUMN PAIR got no primary_key at all. That is not an edge
# case: it is the shape of every line-item fact in the commonest warehouse
# benchmark there is. tpcds_clean's store_sales is keyed on
# (ss_item_sk, ss_ticket_number) -- 180 distinct items and 2,400 distinct
# tickets over 28,810 rows, so neither column is remotely unique alone -- and
# the same holds for catalog_sales, web_sales and inventory. Gold declares 9
# composite PKs; we detected none.
_MAX_PK_CANDIDATES = 10   # 10 columns = 45 pairs, one cheap COUNT each
# Gated on STATISTICAL shape, not on a name-derived role. Filtering by
# entity_role excluded tpcds_clean's `ss_ticket_number`, which is typed
# quantity/measure because it ends in `_number` -- and the gold pair
# (ss_item_sk, ss_ticket_number) is unique over all 28,810 rows. Gold itself
# calls that column quantity/primary_key. Gating on the classification while
# trying to establish what the classification was guessing at is the same
# mistake that made key discovery name-first.
_NEVER_PK_TYPES = frozenset({"monetary_value", "percentage", "rate", "free_text",
                             "url", "json_object", "name", "flag"})


def _composite_pk_candidates(t: dict, ts) -> list[str]:
    """Columns that could participate in a compound key.

    Identifier-shaped and individually non-unique. Ordered by cardinality
    DESCENDING because a high-cardinality column does most of the work of
    making a pair unique, and the cap should keep those rather than a dozen
    low-cardinality flags.
    """
    out = []
    for c in t["columns"]:
        st = ts.columns.get(c["name"])
        # A key member is never null and never unique alone (or it would be
        # the whole key).
        if st is None or st.is_unique or st.n_null or st.n_distinct < 2:
            continue
        if (c.get("semantic_type") or "") in _NEVER_PK_TYPES:
            continue
        if (c.get("semantic_type") or "").startswith("pii_"):
            continue
        if not _key_capable(st.sql_type):
            continue
        out.append(c["name"])
    return sorted(out, key=lambda n: -ts.columns[n].n_distinct)[:_MAX_PK_CANDIDATES]


def _is_unique_tuple(source, tq: str, cols: tuple[str, ...], rows: int) -> bool:
    """Is this column tuple jointly unique over every row?"""
    sel = ", ".join(f'"{c}"' for c in cols)
    got = source.query(
        f"SELECT count(*) FROM (SELECT DISTINCT {sel} FROM {tq})")[0][0]
    return int(got or 0) == rows


def detect_composite_pk(source, t: dict, ts) -> list[str] | None:
    """Smallest jointly-unique tuple of identifier columns, or None.

    Pairs before triples, and the cardinality product must be able to reach
    the row count before any query runs -- 180 items x 2,400 tickets = 432,000
    possible combinations over 28,810 rows, so the pair is worth testing,
    while two 4-value flags never are.
    """
    import itertools

    rows = ts.row_count
    if not rows or t.get("primary_key"):
        return None
    cands = _composite_pk_candidates(t, ts)
    if len(cands) < 2:
        return None
    qualify = getattr(source, "qualify", lambda s, n: f'"{s}"."{n}"')
    tq = qualify(ts.table.schema, ts.table.name)
    for size in (2, 3):
        if len(cands) < size:
            break
        # Smallest cardinality product first: a MINIMAL key is tight, its
        # possible combinations barely covering the row count. Taking the
        # first unique tuple found instead picked
        # (cr_returned_time_sk, cr_returned_date_sk) on catalog_returns -- also
        # unique, and a coincidence of two timestamps rather than the key.
        combos = []
        for combo in itertools.combinations(cands, size):
            product = 1
            for c in combo:
                product *= ts.columns[c].n_distinct
            if product >= rows:
                combos.append((product, combo))
        for _product, combo in sorted(combos):
            if _is_unique_tuple(source, tq, combo, rows):
                return list(combo)
    return None


def _table_index(doc: dict) -> dict[tuple[str, str], dict]:
    """(schema, table) -> the doc's table entry, from its `source` field."""
    out = {}
    for t in doc["semantic_layer"]["tables"]:
        schema, _, name = (t.get("source") or "").rpartition(".")
        out[(schema, name or t["name"])] = t
    return out


def _apply_declared_pks(doc: dict, declared) -> int:
    """Set primary_key from the catalog's own declaration. Rung 1, and free.

    Runs BEFORE composite-key probing, which is the point: `detect_composite_pk`
    returns early when a primary key is already set, so every declared key
    removes up to 45 DISTINCT-over-a-tuple scans from that table.
    """
    idx = _table_index(doc)
    n = 0
    for (schema, table), cols in declared.primary_keys.items():
        t = idx.get((schema, table))
        if t is None:
            continue
        want = list(cols)
        cur = list(t.get("primary_key") or [])
        if cur == want:
            # CORROBORATION, not a no-op. Profile already picks a single-column
            # key from uniqueness, so without this branch a declaration that
            # agrees would vanish -- and two independent signals agreeing is
            # exactly what we claim to record.
            detail = (f"PRIMARY KEY ({', '.join(want)}) declared in the warehouse "
                      f"catalog; agrees with the inferred key")
        elif cur:
            # A declared key is checked by the engine; our inference is not.
            detail = (f"PRIMARY KEY ({', '.join(want)}) declared in the warehouse "
                      f"catalog; REPLACES the inferred key ({', '.join(cur)})")
            t["primary_key"] = want
        else:
            detail = (f"PRIMARY KEY ({', '.join(want)}) declared in the warehouse "
                      f"catalog")
            t["primary_key"] = want
        t.setdefault("provenance", []).append(
            {"signal": "catalog_prior", "detail": detail})
        n += 1
    return n


def _declared_fk_edges(doc: dict, declared) -> list[tuple]:
    """Resolve declared FKs to doc table names, dropping any we cannot place."""
    idx = _table_index(doc)
    out = []
    for f in declared.foreign_keys:
        child = idx.get((f.child_schema, f.child_table))
        parent = idx.get((f.parent_schema, f.parent_table))
        if child is None or parent is None:
            continue          # references something outside the inferred scope
        if "inventory_only" in (child.get("modeling"), parent.get("modeling")):
            continue          # SPEC 2.13: no relationships to or from an inventory-only table
        if not f.child_columns or len(f.child_columns) != len(f.parent_columns):
            continue
        out.append((child["name"], list(f.child_columns),
                    parent["name"], list(f.parent_columns), f.enforced))
    return out


def _apply_composite_pks(source, doc: dict, stats_by_table: dict) -> None:
    """Set primary_key on tables whose key is a tuple, and mark the members."""
    for t in _modelled(doc):
        ts = stats_by_table.get(t["name"])
        if ts is None:
            continue
        pk = detect_composite_pk(source, t, ts)
        if not pk:
            continue
        t["primary_key"] = pk
        t.setdefault("provenance", []).append({
            "signal": "statistic",
            "detail": f"compound key: ({', '.join(pk)}) is unique over all "
                      f"{ts.row_count} rows; no member is unique alone",
        })

def link_source(source, doc: dict, stats_by_table: dict, llm=None, lineage=None,  # noqa: PLR0913, PLR0917
                use_declared: bool = True, query_workers: int = 1,
                llm_workers: int = 1) -> dict:
    """Run the Link stage over a profiled document.

    Mutates and returns doc: adds foreign_key blocks, relationships,
    table_type. Also returns audit info in doc['_link_audit'] (stripped
    before serialization; used by eval).

    `query_workers`: concurrent inclusion queries (see candidates.py);
    `llm_workers`: concurrent validation chunks (see validate_llm.py). The
    output is identical at any count of either; only wall-clock changes.
    """
    # Rung 1 of the read-before-scan ladder: the catalog's own declarations,
    # one query and no table touched. Optional on the source, like comments.
    # `use_declared=False` measures DISCOVERY rather than the product. Seven of
    # nine fixtures declare their primary keys -- written by our own generators
    # -- so reading them would score the gold's PK assertions without inferring
    # anything, re-baselining the floors upward and masking any future
    # regression in discovery behind a declaration. The eval harness therefore
    # runs discovery-only and we publish both numbers, exactly as we already do
    # for --no-llm and --no-sample-egress. Same reasoning as withholding
    # AdventureWorks' declared keys (fixtures/external/registry.yaml).
    declared = None
    getter = getattr(source, "list_constraints", None) if use_declared else None
    if getter is not None:
        try:
            declared = getter()
        except Exception as e:      # a catalog we cannot read must not stop inference
            logger.warning("declared constraints unavailable: %s", e)
    declared_edges: list = []
    if declared:
        n_pk = _apply_declared_pks(doc, declared)
        declared_edges = _declared_fk_edges(doc, declared)
        logger.info("declared constraints: %d primary keys, %d foreign keys",
                    n_pk, len(declared_edges))

    # BEFORE candidate generation, which reads primary_key to decide parents.
    _apply_composite_pks(source, doc, stats_by_table)
    candidates = generate_candidates(source, stats_by_table, doc, query_workers=query_workers)
    # A declared FK settles that column; rediscovering it wastes an inclusion
    # check and risks asserting a second, different parent for the same column.
    if declared_edges:
        taken = {(child, c) for child, ccols, _p, _pc, _e in declared_edges for c in ccols}
        candidates = [c for c in candidates
                      if (c.child_table, c.child_column) not in taken]
    candidates = _resolve_directions(candidates, stats_by_table)

    verdicts = {}
    if llm is not None and candidates:
        from semlayer.link.validate_llm import validate_candidates
        verdicts = validate_candidates(llm, candidates, workers=max(1, llm_workers))

    cols = {
        (t["name"], c["name"]): c
        for t in doc["semantic_layer"]["tables"] for c in t["columns"]
    }
    pk_by_table = {t["name"]: t.get("primary_key") or []
                   for t in doc["semantic_layer"]["tables"]}
    accepted, review, killed, lineage_wins = _decide_candidates(
        candidates, verdicts, llm, lineage=lineage, pk_by_table=pk_by_table)
    _apply_accepted(accepted, review, cols, lineage_wins)
    fk_out, fk_in = _emit_relationships(accepted, cols, doc, declared_edges)

    hierarchies = _detect_recursive_hierarchies(doc, stats_by_table, source)
    if hierarchies:
        doc["semantic_layer"]["hierarchies"] = hierarchies

    _classify_tables(doc, fk_out, fk_in)

    doc["_link_audit"] = {
        "n_candidates": len(candidates),
        "accepted": [(c.key(), c.inclusion_ratio, c.name_score) for c, _ in accepted],
        "review": [(c.key(), c.inclusion_ratio, c.name_score) for c, _ in review],
        "killed": [(c.key(), (v or {}).get("reason")) for c, v in killed],
        "lineage_decided": sorted(lineage_wins),
        "declared_edges": [(c, tuple(cc), p, tuple(pc)) for c, cc, p, pc, _ in declared_edges],
    }
    return doc
