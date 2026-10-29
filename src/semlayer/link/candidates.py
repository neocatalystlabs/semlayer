# SPDX-License-Identifier: FSL-1.1-ALv2
"""FK candidate generation: pruned inclusion-dependency testing (Stage 2).

The IND explosion is tamed by pruning BEFORE any inclusion query runs
(feasibility G): parents must be unique id-like columns, children must be
id-role or type-compatible non-unique columns, and pairs must be
type-compatible. Every candidate carries its statistical evidence; the
CORROBORATION RULE (adversarial B2) decides auto-include vs LLM-gate:
statistics alone NEVER auto-include an FK.
"""

from __future__ import annotations

import logging
import re
import threading
from bisect import bisect_left
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import cache

from semlayer.profile.typing_rules import normalise_name, strip_ordinal

logger = logging.getLogger(__name__)

MIN_INCLUSION = 0.95          # candidate threshold
# Candidate parent columns considered warehouse-wide. This caps a SORT, not
# queries -- inclusion tests stay bounded by MAX_QUERIES_PER_CHILD below -- so
# the cost of raising it is client CPU, not warehouse time.
#
# 200 was silently wrong above ~100 tables. Measured parents per table across
# the fixture corpus: 1.46 (collision_heavy), 1.53 (dbt_synthea), 1.72
# (adventureworks_dw, messy_mart), 2.58 (tpcds_clean) -- so 200 is reached at
# 77-137 tables, an order of magnitude below the 2,000-table line we document.
# Worse, it degraded in two stages: `named` parents are listed first, so the cap
# cut the `shaped` tail first and STATISTICS-ONLY FK discovery disappeared
# entirely, then named parents started dropping alphabetically by table.
#
# 5,000 covers ~2,900 tables at the median rate. The per-child sort this
# caps was the dominant cost of Link at scale until ParentIndex (below)
# replaced it: at 5,000 tables, Link was 917 s on a 20-core machine with the
# sort on one core, 370 s with the index, 111 s once each child's tests also
# went out as one statement. The cap now bounds an index, not a sort, and a
# strict no-op on every current fixture, whose largest parent list is 177.
#
# 2026-10-28: the cap applies to the key-SHAPED (speculative) parents only.
# NAMED parents -- unique columns the naming vocabulary already proposes --
# are never truncated. A 50,000-table synthetic warehouse had 7,952 of them;
# the cap kept the first 5,000 in table order, which dropped the staging
# dimensions, and the 8,820 staging foreign keys were then asserted against
# the core dimension holding the same key values (gold recall 0.385). With
# the index the parent count no longer drives a per-child cost; the
# zero-score tail, which did, is bounded separately (MAX_UNSCORED_SCAN).
MAX_PARENTS = 5_000
MAX_QUERIES_PER_CHILD = 40    # cap inclusion tests per child column
# Parents with NO name evidence for a child (the zero-score tail of its
# ranking) can only reach the review queue, never the output on their own.
# They used to be walked in full for every child -- children x parents --
# which was the whole of Link's superlinear term once the sort was gone (9x
# the time for 3x the tables between 15,000 and 50,000). Now they are tried
# from the narrowest key domain that can contain the child's values upward
# -- a real parent's distinct count sits just above its child's -- and the
# walk stops at this many examined.
MAX_UNSCORED_SCAN = 10 * MAX_QUERIES_PER_CHILD
MIN_PARENT_DISTINCT = 2       # a one-row table makes EVERY column unique
MAX_CHILD_DISTINCT = 100_000  # above this a column is free text, not a key reference

# Semantic types that are never a key REFERENCE, however well their values
# line up with a parent column. `ord_rtn.rtn_dt` is contained in
# `date_dim.cal_dt` with 100% inclusion and an exactly matching token, but it
# is a date that happens to be in the calendar, not a foreign key -- and
# asserting it cost messy_mart's FK precision floor (0.95 -> 0.90). Names are
# the same shape of mistake: `etl_log.job_nm -> job_ctrl.job_nm` scores 1.20
# on naming and is still not a key.
_NON_REFERENCE_TYPES = frozenset({
    "date", "timestamp_event", "name", "free_text", "url", "json_object",
    "pii_name", "pii_email", "pii_phone", "pii_address", "pii_national_id",
})

# Suffixes the naming vocabulary recognises as key-like. Module level because
# the parent predicates below need it; it used to be local to the generator.
_KEYISH_RE = re.compile(r"(_id|_key|_sk|_cd|_code|_no|_nbr|_number)$|^id$")


def _key_capable(sql_type: str) -> bool:
    """Could a column of this type be a key at all?

    Integers, strings and dates can. Continuous numerics cannot in practice: a
    unique DOUBLE or DECIMAL is a measure that happens not to repeat, not an
    identifier, and admitting them as parents buys nothing but inclusion
    queries against every amount column in the warehouse.
    """
    t = sql_type.upper()
    if any(k in t for k in ("DECIMAL", "NUMERIC", "DOUBLE", "FLOAT", "REAL")):
        return False
    return any(k in t for k in ("INT", "CHAR", "TEXT", "STRING", "DATE", "TIME"))


@dataclass
class FKCandidate:
    """A statistically-supported candidate foreign key, pending corroboration."""

    child_table: str
    child_column: str
    parent_table: str
    parent_column: str
    inclusion_ratio: float
    child_distinct: int
    parent_distinct: int
    name_score: float          # 0-1: naming agreement signal

    def key(self) -> tuple:
        """Identity tuple used to correlate this candidate across LLM verdicts and audit records."""
        return (self.child_table, self.child_column, self.parent_table, self.parent_column)


_STOP = {"id", "key", "sk", "pk", "cd", "code", "no", "nbr", "number"}

# warehouse-abbreviation expansion (cust_id and customer_id must agree)
_ABBREV = {
    "cust": "customer", "ord": "order", "prod": "product", "whs": "warehouse",
    "dt": "date", "chnl": "channel", "cmpgn": "campaign", "mth": "month",
    "rgn": "region", "cat": "category", "addr": "address", "sts": "status",
    "qty": "quantity", "amt": "amount", "dept": "department", "emp": "employee",
    "mgr": "manager", "acct": "account", "sub": "subscription", "evt": "event",
    "rtn": "return", "agg": "aggregate", "sls": "sales", "mstr": "master",
}


ABBREV = _ABBREV  # shared warehouse-abbreviation table (search uses it too)


# Per-name helpers are cached. A warehouse has thousands of distinct column
# and table names but the ranking scores children x parents PAIRS, and the
# first version re-split, re-lowercased and re-expanded both names for every
# pair: 2.5 million tokenisations for ~2,300 distinct names on a 300-table
# warehouse, 55 s of a 100 s Link. Every function below is pure in its string
# arguments, so the cache changes nothing but the clock.
@cache
def _name_tokens(col: str, table: str) -> frozenset[str]:
    raw = set(re.split(r"[_\W]+", col.lower())) - _STOP - {""}
    toks = {_ABBREV.get(t, t).rstrip("s") for t in raw}
    if not toks:
        toks = {_ABBREV.get(table.lower(), table.lower()).rstrip("s")}
    return frozenset(toks)


@cache
def _table_initials(table: str) -> str:
    return "".join(w[0] for w in re.split(r"[_\W]+", table.lower()) if w)


@cache
def _norm(name: str) -> str:
    return normalise_name(name)


@cache
def _norm_child(col: str) -> str:
    return strip_ordinal(normalise_name(col))


_PARENT_ALIAS_RE = re.compile(r"^[a-z]{1,3}_(.{6,})$")


@cache
def _parent_core(parent_col: str) -> tuple[str, bool]:
    """The parent column with a 1-3 letter alias prefix stripped, and whether one was.

    TPC-DS `d_date_sk` -> ("date_sk", True); `customer_id` -> ("customer_id", False).
    """
    low = _norm(parent_col).lower()
    m = _PARENT_ALIAS_RE.match(low)
    return (m.group(1), True) if m else (low, False)


@cache
def _ptoks(core: str) -> tuple[str, ...]:
    return tuple(_ABBREV.get(t, t) for t in core.split("_") if t and t not in _STOP)


def name_similarity(child_col: str, child_table: str, parent_col: str, parent_table: str) -> float:
    """Score naming agreement between a candidate child and parent column.

    >1.0 = exact column-name agreement; ~1.0 strong token agreement;
    0 = nothing in common (the trap shape: shoe_size -> dept_id).

    Names are FOLDED first. Every rule below splits on non-word characters, so a
    PascalCase name was a single token and the token logic could not run at
    all. On AdventureWorksDW that made `DueDateKey -> DimDate.DateKey` score
    0.95 and `OrderDateKey` and `ShipDateKey` score 0.35: `p_core` became
    "datekey", the child ended with it leaving rest="due", which has no
    trailing underscore and so fell into the fused-initials branch meant for
    `ws_bill_hdemo_sk`. That branch asks whether "due"+"datekey" starts with
    DimDate's initial "d" -- true by accident of the word "due", false for
    "order" and "ship". One false positive and two false negatives from one
    mechanism, and the two role-playing date keys went to review unasserted.
    """
    child_col = _norm_child(child_col)
    raw_parent_col = parent_col
    parent_col = _norm(parent_col)
    child_table = _norm(child_table)
    parent_table = _norm(parent_table)
    if child_col.lower() == parent_col.lower():
        return 1.2  # exact-name match outranks any token-subset tie
    # prefix-coded schemas (TPC-DS: d_date_sk in date_dim, cc_closed_date_sk in
    # call_center): strip the parent's short alias prefix; a child column
    # ENDING with the parent's core name is a strong reference signal.
    child_l = child_col.lower()
    p_core, m = _parent_core(raw_parent_col)
    if len(p_core) >= 6 and child_l.endswith(p_core):
        rest = child_l[: -len(p_core)]
        if rest == "" or rest.endswith("_"):
            return 0.9  # clean token boundary: cc_closed_[date_sk]
        # fused-initials disambiguation: ws_bill_h[demo_sk] -> the 'h' fuses
        # with the core; it must match THIS parent's initials (hd_demo_sk in
        # household_demographics), else it points at a sibling table
        fused = rest.split("_")[-1] + p_core.split("_")[0]
        ini = _table_initials(parent_table)
        alias = parent_col.lower().split("_")[0] if m else ini
        return 0.95 if fused.startswith(ini) or fused.startswith(alias) else 0.35
    # token-level suffix match with abbreviation normalization:
    # cs_bill_addr_sk vs ca_address_sk (core address_sk) -> [address] tail match
    ptoks = _ptoks(p_core)
    ctoks = _ptoks(child_l)
    if ptoks and len(ctoks) >= len(ptoks) and ctoks[-len(ptoks):] == ptoks:
        return 0.9
    ct = _name_tokens(child_col, child_table)
    # tokens inherited from the child's own table name carry no reference
    # signal (dly_sls_agg.agg_dt_key: 'agg' is table identity, 'dt' points out)
    table_toks = _name_tokens(child_table, child_table)
    stripped = ct - table_toks
    if stripped:
        ct = stripped
    pt = _name_tokens(parent_col, parent_table) | _name_tokens(parent_table, parent_table)
    if not ct:
        return 0.0
    return round(len(ct & pt) / len(ct), 3)


@cache
def _type_family(sql_type: str) -> str:
    t = sql_type.upper()
    if any(k in t for k in ("INT", "DECIMAL", "NUMERIC", "DOUBLE", "FLOAT", "HUGEINT")):
        return "num"
    if any(k in t for k in ("CHAR", "TEXT", "STRING")):
        return "str"
    if "DATE" in t or "TIME" in t:
        return "temporal"
    return "other"


# Range pruning. Profile already holds min_val/max_val, so a child whose value
# range escapes its parent's cannot be FULLY contained -- without issuing a
# query. Two things make this subtler than it looks, both of them measured:
#
# 1. It is NOT a sound hard prune in general. MIN_INCLUSION is 0.95, so a real
#    key with a few out-of-range orphans is still a legitimate candidate. On
#    dbt_synthea 36.2% of 24,827 tests violate the range check, and discarding
#    all of them would drop keys sitting at incl=0.96.
# 2. It IS sound for SMALL children. inclusion = 1 - misses/distinct >= 0.95
#    requires misses <= 0.05 * distinct, so for distinct <= 19 it requires
#    ZERO misses -- and a range violation guarantees at least one. Measured:
#    18.6% of tests are soundly prunable this way with 0 candidates lost; the
#    other 17.7% are demoted instead, tested only if budget remains.
#
# Comparison is NUMERIC per type family, never lexical: min_val/max_val are
# stored as strings, and comparing '0' to '0.0' by equality once manufactured a
# violation between two equal numbers. Anything unparseable or absent FAILS
# OPEN -- no prune, no demotion -- because absence of evidence is not evidence
# of a violation.
#
# Staleness: min/max come from the last profile, so DML can invalidate them.
# That makes them one of the things targeted re-profiling must refresh; a stale
# bound can only cost a candidate, never admit a wrong one, because the test
# still runs for everything not hard-pruned.
RANGE_SAFE_DISTINCT = int(1 / (1 - MIN_INCLUSION))   # 19 at 0.95: zero misses allowed


def _as_num(v):
    """Numeric value of a stored bound, or None if it is absent/unparseable."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def range_escapes(cstat, pstat) -> bool:
    """True only when the child range PROVABLY escapes the parent's.

    Numeric families only. Strings would need collation-aware comparison to be
    sound, and temporal bounds are stored in formats whose ordering we have not
    verified across engines -- both fail open rather than guess.
    """
    if _type_family(cstat.sql_type) != "num" or _type_family(pstat.sql_type) != "num":
        return False
    cl, ch = _as_num(cstat.min_val), _as_num(cstat.max_val)
    pl, ph = _as_num(pstat.min_val), _as_num(pstat.max_val)
    if None in (cl, ch, pl, ph):
        return False          # fail open
    return cl < pl or ch > ph


def range_prunable(cstat, pstat) -> bool:
    """A range escape DISPROVES candidacy only when no miss is affordable."""
    return cstat.n_distinct <= RANGE_SAFE_DISTINCT and range_escapes(cstat, pstat)


def _named_parent(stats_by_table, has_validity, tn: str, cn: str, c: dict) -> bool:
    """A parent the naming vocabulary already proposes."""
    st = stats_by_table[tn].columns[cn]
    if c.get("entity_role") in ("primary_key", "unique_key") and st.is_unique:
        return True
    if not _KEYISH_RE.search(cn.lower()):
        return False
    if st.is_unique:
        return True
    # SCD parent: a non-unique business key with a bounded number of versions
    # per entity, on a table carrying validity columns
    return bool(has_validity(tn) and st.n_distinct >= 20
                and st.row_count <= 6 * max(1, st.n_distinct))


def _shaped_parent(stats_by_table, tn: str, cn: str) -> bool:
    """Key-SHAPED whatever it is called: unique, populated, key-capable.

    The name gate here was why a textbook foreign key could be invisible.
    `Rndrng_NPI` on CMS's Medicare files is the National Provider Identifier --
    unique across the summary table, non-unique across the detail, 100%
    inclusion -- and it ends in `_npi`, so it matched no vocabulary, never
    became a candidate, and Link returned ZERO relationships on a two-table
    warehouse. The uniqueness and inclusion statistics were never consulted.

    Safe to widen because naming remains the CORROBORATION test downstream:
    inclusion alone never auto-includes an FK, so a statistically-included
    pair whose names disagree lands in the review queue, not in the output.
    """
    st = stats_by_table[tn].columns[cn]
    return (st.is_unique and st.n_distinct >= MIN_PARENT_DISTINCT
            and _key_capable(st.sql_type))


class ParentIndex:
    """Rank candidate parents for a child without scoring every one of them.

    `sorted(parents, key=name_similarity, reverse=True)` scored children x
    parents pairs: 644,330 scores on a 300-table warehouse, and the product
    grows ~36x between 300 and 2,000 tables while the inclusion queries grow
    ~6x. It ran on one core for most of Link's wall clock at 2,000 tables.

    The ranking is reproduced EXACTLY from a block. Every branch of
    `name_similarity` that returns more than zero needs one of four things
    to hold between child and parent, and each is an index lookup:

      1.2   exact column name           -> parents keyed by normalised column
      0.9+  child ends with parent core -> parents keyed by the core's last six
            (core is >= 6 chars)           characters, probed with the child's
      0.9   child token tail == ptoks    -> parents keyed by the core's last token
      >0    shared name token            -> parents keyed by every token of
                                            column and table

    Anything outside the union scores zero. `scored()` builds the positive
    part of that order directly: positives sorted by score then position,
    restricted to the child's type family, which `_testable` would drop
    anyway. tests/test_candidate_blocking.py checks it against the brute-force
    sort on every fixture.

    The zero-scored rest is no longer walked in full. `unscored()` yields the
    same-family parents whose distinct count can contain the child's, the
    narrowest first (a real parent's key domain sits just above its child's:
    claims.PATIENTID and patients.Id hold the same 1,171 values, while the
    encounter and observation ids above them hold millions), position
    breaking ties. Parents too narrow for the child are skipped by bisection,
    not examined. The caller bounds how far it reads (MAX_UNSCORED_SCAN).
    """

    def __init__(self, parents: list[tuple[str, str]], stats_by_table) -> None:
        self.parents = parents
        self.family = [_type_family(stats_by_table[tn].columns[cn].sql_type)
                       for tn, cn in parents]
        self.distinct = [stats_by_table[tn].columns[cn].n_distinct for tn, cn in parents]
        self.by_family: dict[str, list[int]] = {}
        self.by_exact: dict[str, list[int]] = {}
        self.by_tail6: dict[str, list[int]] = {}
        self.by_lasttok: dict[str, list[int]] = {}
        self.by_tok: dict[str, list[int]] = {}
        for i, (ptn, pcn) in enumerate(parents):
            self.by_family.setdefault(self.family[i], []).append(i)
            self.by_exact.setdefault(_norm(pcn).lower(), []).append(i)
            core, _ = _parent_core(pcn)
            if len(core) >= 6:
                self.by_tail6.setdefault(core[-6:], []).append(i)
            ptoks = _ptoks(core)
            if ptoks:
                self.by_lasttok.setdefault(ptoks[-1], []).append(i)
            for t in _name_tokens(pcn, ptn) | _name_tokens(ptn, ptn):
                self.by_tok.setdefault(t, []).append(i)
        self.by_family_narrow: dict[str, list[int]] = {
            fam: sorted(idx, key=lambda i: (self.distinct[i], i))
            for fam, idx in self.by_family.items()}
        self.by_family_widths: dict[str, list[int]] = {
            fam: [self.distinct[i] for i in idx] for fam, idx in self.by_family_narrow.items()}

    def block(self, ccn: str, ctn: str) -> set[int]:
        """Indices of every parent that can score above zero for this child."""
        child_col = _norm_child(ccn)
        child_l = child_col.lower()
        out: set[int] = set(self.by_exact.get(child_l, ()))
        if len(child_l) >= 6:
            out.update(self.by_tail6.get(child_l[-6:], ()))
        ctoks = _ptoks(child_l)
        if ctoks:
            out.update(self.by_lasttok.get(ctoks[-1], ()))
        for t in _name_tokens(child_col, _norm(ctn)):
            out.update(self.by_tok.get(t, ()))
        return out

    def scored(self, ccn: str, ctn: str, family: str | None = None) -> list[int]:
        """Indices of the parents with name evidence, by similarity then position.

        With `family`, only parents of that type family.
        """
        out = []
        for i in self.block(ccn, ctn):
            if family is not None and self.family[i] != family:
                continue
            ptn, pcn = self.parents[i]
            sim = name_similarity(ccn, ctn, pcn, ptn)
            if sim > 0:
                out.append((-sim, i))
        out.sort()
        return [i for _, i in out]

    def unscored(self, family: str, min_distinct: int, skip: set[int]) -> Iterator[int]:
        """Same-family parents outside `skip` that can contain `min_distinct` values.

        Narrowest key domain first, position breaking ties; parents too
        narrow for the child are never examined.
        """
        order = self.by_family_narrow.get(family, [])
        start = bisect_left(self.by_family_widths.get(family, []), min_distinct)
        for i in order[start:]:
            if i not in skip:
                yield i

    def ranked(self, ccn: str, ctn: str, family: str | None = None) -> list[tuple[str, str]]:
        """The parents with name evidence, as pairs: the brute-force order's positive part."""
        return [self.parents[i] for i in self.scored(ccn, ctn, family)]


def _testable(index: ParentIndex, ccn: str, ctn: str, cstat, stats_by_table,
              budget: int = MAX_QUERIES_PER_CHILD) -> list[tuple[str, str]]:
    """Up to `budget` parents worth an inclusion query for this child, in the order to try.

    Filters that DISPROVE candidacy drop the pair; the range check that merely
    makes it unlikely demotes it to the tail, where it is tested only if the
    budget outlasts the pairs with no evidence against them. Partitioning
    rather than re-sorting keeps the name-similarity order inside each half.

    Every parent with name evidence is considered. Parents with none come
    from `index.unscored()` and are read only until the budget is full or
    MAX_UNSCORED_SCAN of them have been examined: on their own they can only
    reach the review queue, and reading them all was children x parents.
    """
    head: list[int] = []
    tail: list[int] = []
    family = _type_family(cstat.sql_type)

    def consider(i: int) -> bool:
        ptn, pcn = index.parents[i]
        if ptn == ctn:
            return False  # same-table parents are the recursive/self-FK path, not this one
        pstat = stats_by_table[ptn].columns[pcn]
        if family != _type_family(pstat.sql_type):
            return False
        if cstat.n_distinct > pstat.n_distinct:
            return False  # child domain can't exceed parent
        if range_prunable(cstat, pstat):
            return False  # zero misses affordable and a range escape guarantees one
        (tail if range_escapes(cstat, pstat) else head).append(i)
        return True

    scored = index.scored(ccn, ctn, family)
    for i in scored:
        consider(i)
    # Parents with name evidence are never displaced: the unscored ones get
    # only the room they leave, so nothing is truncated below.
    room = budget - len(head) - len(tail)
    examined = 0
    picked: list[int] = []
    for i in index.unscored(family, cstat.n_distinct, set(scored)):
        if room <= 0 or examined >= MAX_UNSCORED_SCAN:
            break
        examined += 1
        if consider(i):
            picked.append(i)
            room -= 1
    # The unscored ones were READ narrowest-first but are EMITTED in position
    # order, as the full walk emitted them: wherever the set is unchanged the
    # plan, the candidate list and every LLM prompt built from it are
    # byte-identical, so recorded cassettes still replay.
    n_scored_head = len(head) - sum(1 for i in picked if i in head)
    picked_set = set(picked)
    head = head[:n_scored_head] + sorted(i for i in head if i in picked_set)
    n_scored_tail = len(tail) - sum(1 for i in picked if i in tail)
    tail = tail[:n_scored_tail] + sorted(i for i in tail if i in picked_set)
    # The budget applies to the named parents too, as it always has: a child
    # whose name scores hundreds of parents still gets `budget` tests. Dropping
    # this slice once planned 1,485,245 statements on a 15,000-table warehouse
    # where 528,060 was the figure.
    return [index.parents[i] for i in (head + tail)[:budget]]


def select_parents_and_children(stats_by_table: dict, doc: dict
                                ) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """The candidate parent columns (capped, named first) and child columns.

    Split out of `generate_candidates` so the ranking can be tested against
    the brute-force sort on real fixtures without issuing a query.
    """
    # Inventory-only tables (raw shards, backups: semlayer.tier) are neither
    # children nor parents. As parents they were the wrong-copy problem: every
    # copy of an entity holds the same key values, and on a 2,000-table
    # synthetic estate 69% of statistical foreign keys pointed at a raw or
    # scratch copy instead of the dimension. As children they were 70% of the
    # query budget spent on tables nobody models against.
    cols = {
        (t["name"], c["name"]): c
        for t in doc["semantic_layer"]["tables"] if t.get("modeling") != "inventory_only"
        for c in t["columns"]
    }

    def _has_validity_cols(tn: str) -> bool:
        names = {c.name.lower() for c in stats_by_table[tn].table.columns}
        return bool({"eff_start_dt", "eff_end_dt"} & names or {"valid_from", "valid_to"} & names)

    # parents: unique id-like columns, PLUS natural keys of SCD2 tables
    # (non-unique, few versions per key, table has validity columns)
    # Named candidates are never truncated; MAX_PARENTS caps the speculative
    # shaped list only. Dict order is not a defensible thing to drop a real
    # key by, and at 50,000 tables it dropped every staging dimension.
    named = [(tn, cn) for (tn, cn), c in cols.items()
             if _named_parent(stats_by_table, _has_validity_cols, tn, cn, c)]
    seen = set(named)
    shaped = [(tn, cn) for (tn, cn) in cols
              if (tn, cn) not in seen and _shaped_parent(stats_by_table, tn, cn)]
    parents = named + shaped[:MAX_PARENTS]
    # children: fk/identifier roles, numeric dimensions, or ANY key/code-suffixed column
    children = [
        (tn, cn) for (tn, cn), c in cols.items()
        if c.get("entity_role") == "foreign_key"
        or (c.get("semantic_type") == "identifier" and not stats_by_table[tn].columns[cn].is_unique)
        # Any non-unique dimension of a key-capable type, not just numeric
        # ones. The `num`-only restriction was the second half of why CMS's
        # Rndrng_NPI was invisible: widening parents alone did nothing,
        # because the detail side is a VARCHAR typed enum/dimension and so
        # failed every child test too.
        or (c.get("entity_role") == "dimension" and _key_capable(c["sql_type"])
            and c.get("semantic_type") not in _NON_REFERENCE_TYPES
            and 1 < stats_by_table[tn].columns[cn].n_distinct <= MAX_CHILD_DISTINCT
            and not stats_by_table[tn].columns[cn].is_unique)
        or (_KEYISH_RE.search(cn.lower()) and not stats_by_table[tn].columns[cn].is_unique)
        # unique keyish columns that are NOT the table's own PK can still be
        # FKs (1:1 links, one-row-per-parent xref tables)
        or (_KEYISH_RE.search(cn.lower()) and stats_by_table[tn].columns[cn].is_unique
            and cn not in set(next((t.get("primary_key") or [] for t in
                doc["semantic_layer"]["tables"] if t["name"] == tn), [])))
    ]
    children = sorted(set(children))
    return parents, children


def generate_candidates(ex, stats_by_table: dict, doc: dict,
                        query_workers: int = 1) -> list[FKCandidate]:
    """stats_by_table: {table_name: TableStats}; doc: profile-stage document.

    `query_workers`: how many inclusion queries are in flight at once. The
    set of queries is fixed before any of them runs -- the cap counts tests
    issued, not matches found, and nothing in the loop feeds back into which
    parents are tested -- so the candidates come out identical at any count.
    These queries run in the customer's warehouse and contend on their
    compute, which is why the default is small (see docs/scale.md).
    """
    parents, children = select_parents_and_children(stats_by_table, doc)
    plan: list[tuple[str, str, str, str]] = []
    index = ParentIndex(parents, stats_by_table)
    for ctn, ccn in children:
        cstat = stats_by_table[ctn].columns[ccn]
        # test the most name-plausible parents FIRST so the query cap
        # never starves an obvious match (order_items.order_id -> orders).
        # Restricting to the child's type family drops nothing _testable
        # would keep; the zero-scored tail is bounded inside it.
        testable = _testable(index, ccn, ctn, cstat, stats_by_table)
        plan.extend((ctn, ccn, ptn, pcn) for ptn, pcn in testable)

    ratios = _inclusion_many(ex, stats_by_table, plan, query_workers)
    assert len(ratios) == len(plan)

    out: list[FKCandidate] = []
    for (ctn, ccn, ptn, pcn), ratio in zip(plan, ratios, strict=True):
        if ratio >= MIN_INCLUSION:
            out.append(FKCandidate(
                ctn, ccn, ptn, pcn, round(ratio, 4),
                stats_by_table[ctn].columns[ccn].n_distinct,
                stats_by_table[ptn].columns[pcn].n_distinct,
                name_similarity(ccn, ctn, pcn, ptn),
            ))
    return out


def _group_plan(plan: list[tuple[str, str, str, str]]
                ) -> list[tuple[str, str, list[tuple[str, str]]]]:
    """The plan as (child_table, child_column, [parents]) groups, in plan order.

    The plan is built one child at a time, so a child's tests are contiguous;
    grouping keeps that order and so does flattening the results back.
    """
    groups: list[tuple[str, str, list[tuple[str, str]]]] = []
    for ctn, ccn, ptn, pcn in plan:
        if groups and groups[-1][0] == ctn and groups[-1][1] == ccn:
            groups[-1][2].append((ptn, pcn))
        else:
            groups.append((ctn, ccn, [(ptn, pcn)]))
    return groups


def _inclusion_many(ex, stats_by_table, plan: list[tuple[str, str, str, str]],
                    workers: int) -> list[float]:
    """One inclusion ratio per planned test, in plan order.

    The unit of work is a CHILD COLUMN with all its candidate parents, tested
    in one statement (`_inclusion_batch`), not one statement per pair: the
    tests are small and the cost of each was the round trip, not the work.
    Each worker thread gets its own executor from `ex.for_worker()` -- a
    source without one runs serially, because a connection shared across
    threads is not something to assume safe. Results are applied in plan
    order by the caller, so the fan-out cannot change which candidates
    survive. A worker that raises fails the run.
    """
    groups = _group_plan(plan)
    for_worker = getattr(ex, "for_worker", None)
    if workers <= 1 or len(groups) <= 1 or for_worker is None:
        if workers > 1 and for_worker is None:
            logger.info("source %s has no per-worker executor; inclusion tests run serially",
                        type(ex).__name__)
        return [r for g in groups for r in _inclusion_batch(ex, stats_by_table, *g)]

    local = threading.local()
    made: list = []
    lock = threading.Lock()

    def init() -> None:
        local.ex = for_worker()
        with lock:
            made.append(local.ex)

    def one(group) -> list[float]:
        return _inclusion_batch(local.ex, stats_by_table, *group)

    try:
        with ThreadPoolExecutor(max_workers=min(workers, len(groups)), initializer=init) as pool:
            return [r for rs in pool.map(one, groups) for r in rs]
    finally:
        for w in made:
            close = getattr(w, "close", None)
            if close is not None and w is not ex:
                close()


# Parents tested per statement. Equal to MAX_QUERIES_PER_CHILD, so a child is
# normally one statement; the chunking exists so a larger cap cannot build an
# arbitrarily wide query.
INCLUSION_BATCH = 40


def _inclusion_batch(ex, stats_by_table, ctn: str, ccn: str,
                     parents: list[tuple[str, str]]) -> list[float]:
    """Inclusion ratios of one child column against each parent, in order.

    One statement per up-to-INCLUSION_BATCH parents: the child's distinct
    values are computed once, and each parent contributes a
    `sum(CASE WHEN NOT EXISTS (...) THEN 1 ELSE 0 END)` column counting the
    values it does not contain. That is the SAME predicate `_inclusion` runs
    per pair -- `NOT EXISTS` rather than `EXCEPT`, for the BigQuery reason
    recorded there -- so the ratios are identical and only the round trips
    change: at 300 tables 24,847 statements became 637. `sum(CASE ...)` is
    the portable aggregate (no `FILTER`), the statement has a FROM clause
    (Oracle), and the shape parses in duckdb, postgres, snowflake, bigquery
    and oracle.
    """
    if not parents:
        return []
    cq = _qual(ex, stats_by_table, ctn)
    qc = _qcol(ex, ccn)
    distinct = stats_by_table[ctn].columns[ccn].n_distinct
    out: list[float] = []
    for i in range(0, len(parents), INCLUSION_BATCH):
        chunk = parents[i:i + INCLUSION_BATCH]
        cols = ", ".join(
            f"sum(CASE WHEN NOT EXISTS (SELECT 1 FROM {_qual(ex, stats_by_table, ptn)} p "
            f"WHERE p.{_qcol(ex, pcn)} = d.v) THEN 1 ELSE 0 END) AS m{j}"
            for j, (ptn, pcn) in enumerate(chunk))
        row = ex.query(
            f"SELECT {cols} FROM (SELECT DISTINCT {qc} AS v FROM {cq} "
            f"WHERE {qc} IS NOT NULL) d"
        )[0]
        # an empty child gives NULL sums: no distinct values, no misses
        out.extend(1.0 - ((m or 0) / distinct) if distinct else 0.0 for m in row)
    return out


def _inclusion(ex, stats_by_table, ctn, ccn, ptn, pcn) -> float:
    """One pair; the batched form above is what the pipeline issues."""
    return _inclusion_batch(ex, stats_by_table, ctn, ccn, [(ptn, pcn)])[0]


def _qual(ex, stats_by_table, table: str) -> str:
    t = stats_by_table[table].table
    if hasattr(ex, "qualify"):
        return ex.qualify(t.schema, t.name)
    return f'"{t.schema}"."{t.name}"'


def _qcol(ex, name: str) -> str:
    if hasattr(ex, "quote_ident"):
        return ex.quote_ident(name)
    return f'"{name}"'
