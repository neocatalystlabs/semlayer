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

import re
from dataclasses import dataclass

from semlayer.profile.typing_rules import normalise_name, strip_ordinal

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
# 5,000 covers ~2,900 tables at the median rate. Cost of the larger per-child
# sort, measured: 3,440 parents x 25,000 children = ~24 min of Python against a
# run already measured in hours. A strict no-op on every current fixture, whose
# largest parent list is 177. Blocking (index parents by name core and type
# family, ~4 tests per child) removes that sort cost and is the follow-up -- a
# performance fix, not a correctness one.
MAX_PARENTS = 5_000
MAX_QUERIES_PER_CHILD = 40    # cap inclusion tests per child column
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


def _name_tokens(col: str, table: str) -> set[str]:
    raw = set(re.split(r"[_\W]+", col.lower())) - _STOP - {""}
    toks = {_ABBREV.get(t, t).rstrip("s") for t in raw}
    if not toks:
        toks = {_ABBREV.get(table.lower(), table.lower()).rstrip("s")}
    return toks


def _table_initials(table: str) -> str:
    return "".join(w[0] for w in re.split(r"[_\W]+", table.lower()) if w)


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
    child_col = strip_ordinal(normalise_name(child_col))
    parent_col = normalise_name(parent_col)
    child_table = normalise_name(child_table)
    parent_table = normalise_name(parent_table)
    if child_col.lower() == parent_col.lower():
        return 1.2  # exact-name match outranks any token-subset tie
    # prefix-coded schemas (TPC-DS: d_date_sk in date_dim, cc_closed_date_sk in
    # call_center): strip the parent's short alias prefix; a child column
    # ENDING with the parent's core name is a strong reference signal.
    child_l = child_col.lower()
    m = re.match(r"^[a-z]{1,3}_(.{6,})$", parent_col.lower())
    p_core = m.group(1) if m else parent_col.lower()
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
    ptoks = [_ABBREV.get(t, t) for t in p_core.split("_") if t and t not in _STOP]
    ctoks = [_ABBREV.get(t, t) for t in child_l.split("_") if t and t not in _STOP]
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


def _testable(ranked, ctn: str, cstat, stats_by_table) -> list[tuple[str, str]]:
    """Parents worth an inclusion query for this child, in the order to try.

    Filters that DISPROVE candidacy drop the pair; the range check that merely
    makes it unlikely demotes it to the tail, where it is tested only if the
    budget outlasts the pairs with no evidence against them. Partitioning
    rather than re-sorting keeps the name-similarity order inside each half.
    """
    head: list[tuple[str, str]] = []
    tail: list[tuple[str, str]] = []
    for ptn, pcn in ranked:
        if ptn == ctn:
            continue  # same-table parents are the recursive/self-FK path, not this one
        pstat = stats_by_table[ptn].columns[pcn]
        if _type_family(cstat.sql_type) != _type_family(pstat.sql_type):
            continue
        if cstat.n_distinct > pstat.n_distinct:
            continue  # child domain can't exceed parent
        if range_prunable(cstat, pstat):
            continue  # zero misses affordable and a range escape guarantees one
        (tail if range_escapes(cstat, pstat) else head).append((ptn, pcn))
    return head + tail


def generate_candidates(ex, stats_by_table: dict, doc: dict) -> list[FKCandidate]:
    """stats_by_table: {table_name: TableStats}; doc: profile-stage document."""
    cols = {
        (t["name"], c["name"]): c
        for t in doc["semantic_layer"]["tables"] for c in t["columns"]
    }

    def _has_validity_cols(tn: str) -> bool:
        names = {c.name.lower() for c in stats_by_table[tn].table.columns}
        return bool({"eff_start_dt", "eff_end_dt"} & names or {"valid_from", "valid_to"} & names)

    # parents: unique id-like columns, PLUS natural keys of SCD2 tables
    # (non-unique, few versions per key, table has validity columns)
    # Named candidates FIRST, so MAX_PARENTS truncates the speculative tail
    # rather than an obvious key. The cap bites only on wide warehouses, and
    # dict order is not a defensible thing to drop a real key by.
    named = [(tn, cn) for (tn, cn), c in cols.items()
             if _named_parent(stats_by_table, _has_validity_cols, tn, cn, c)]
    seen = set(named)
    shaped = [(tn, cn) for (tn, cn) in cols
              if (tn, cn) not in seen and _shaped_parent(stats_by_table, tn, cn)]
    parents = (named + shaped)[:MAX_PARENTS]
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

    out: list[FKCandidate] = []
    for ctn, ccn in children:
        cstat = stats_by_table[ctn].columns[ccn]
        # test the most name-plausible parents FIRST so the query cap
        # never starves an obvious match (order_items.order_id -> orders)
        ranked = sorted(
            parents,
            key=lambda p: name_similarity(ccn, ctn, p[1], p[0]),
            reverse=True,
        )
        tested = 0
        for ptn, pcn in _testable(ranked, ctn, cstat, stats_by_table):
            pstat = stats_by_table[ptn].columns[pcn]
            if tested >= MAX_QUERIES_PER_CHILD:
                break
            tested += 1
            ratio = _inclusion(ex, stats_by_table, ctn, ccn, ptn, pcn)
            if ratio >= MIN_INCLUSION:
                out.append(FKCandidate(
                    ctn, ccn, ptn, pcn, round(ratio, 4),
                    cstat.n_distinct, pstat.n_distinct,
                    name_similarity(ccn, ctn, pcn, ptn),
                ))
    return out


def _inclusion(ex, stats_by_table, ctn, ccn, ptn, pcn) -> float:
    cq = _qual(ex, stats_by_table, ctn)
    pq = _qual(ex, stats_by_table, ptn)
    qc, qp = _qcol(ex, ccn), _qcol(ex, pcn)
    # NOT EXISTS rather than EXCEPT, and the reason is a bug this shape had.
    # Bare `EXCEPT` is a syntax error on BigQuery, which requires
    # `EXCEPT DISTINCT` -- so foreign-key discovery did not run on BigQuery at
    # all, undetected, because the portability test checked a hand-curated list
    # of shapes that never included this one. It is the most-issued query in the
    # system (24,827 on a 116-table warehouse).
    #
    # `EXCEPT DISTINCT` would also parse everywhere, but NOT EXISTS avoids the
    # set-operator family entirely -- Oracle before 21c has MINUS and no EXCEPT
    # at all -- and that family is exactly where the bug came from. Verified
    # identical results against the EXCEPT form, and parses in oracle, postgres,
    # snowflake, bigquery and duckdb.
    misses = ex.query(
        f"SELECT count(*) FROM (SELECT DISTINCT {qc} AS v FROM {cq} "
        f"WHERE {qc} IS NOT NULL) d "
        f"WHERE NOT EXISTS (SELECT 1 FROM {pq} p WHERE p.{qp} = d.v)"
    )[0][0]
    distinct = stats_by_table[ctn].columns[ccn].n_distinct
    return 1.0 - (misses / distinct) if distinct else 0.0


def _qual(ex, stats_by_table, table: str) -> str:
    t = stats_by_table[table].table
    if hasattr(ex, "qualify"):
        return ex.qualify(t.schema, t.name)
    return f'"{t.schema}"."{t.name}"'


def _qcol(ex, name: str) -> str:
    if hasattr(ex, "quote_ident"):
        return ex.quote_ident(name)
    return f'"{name}"'
