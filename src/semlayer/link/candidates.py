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

MIN_INCLUSION = 0.95          # candidate threshold
MAX_PARENTS = 200             # safety cap per warehouse
MAX_QUERIES_PER_CHILD = 40    # cap inclusion tests per child column


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
    """
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


def generate_candidates(ex, stats_by_table: dict, doc: dict) -> list[FKCandidate]:
    """stats_by_table: {table_name: TableStats}; doc: profile-stage document."""
    cols = {
        (t["name"], c["name"]): c
        for t in doc["semantic_layer"]["tables"] for c in t["columns"]
    }
    _KEYISH_RE = re.compile(r"(_id|_key|_sk|_cd|_code|_no|_nbr|_number)$|^id$")

    def _has_validity_cols(tn: str) -> bool:
        names = {c.name.lower() for c in stats_by_table[tn].table.columns}
        return bool({"eff_start_dt", "eff_end_dt"} & names or {"valid_from", "valid_to"} & names)

    # parents: unique id-like columns, PLUS natural keys of SCD2 tables
    # (non-unique, few versions per key, table has validity columns)
    parents = [
        (tn, cn) for (tn, cn), c in cols.items()
        if (c.get("entity_role") in ("primary_key", "unique_key")
            and stats_by_table[tn].columns[cn].is_unique)
        or (_KEYISH_RE.search(cn.lower()) and stats_by_table[tn].columns[cn].is_unique)
        or (_KEYISH_RE.search(cn.lower()) and _has_validity_cols(tn)
            and not stats_by_table[tn].columns[cn].is_unique
            and stats_by_table[tn].columns[cn].n_distinct >= 20
            and stats_by_table[tn].columns[cn].row_count
            <= 6 * max(1, stats_by_table[tn].columns[cn].n_distinct))
    ][:MAX_PARENTS]
    # children: fk/identifier roles, numeric dimensions, or ANY key/code-suffixed column
    children = [
        (tn, cn) for (tn, cn), c in cols.items()
        if c.get("entity_role") == "foreign_key"
        or (c.get("semantic_type") == "identifier" and not stats_by_table[tn].columns[cn].is_unique)
        or (c.get("entity_role") == "dimension" and _type_family(c["sql_type"]) == "num"
            and 1 < stats_by_table[tn].columns[cn].n_distinct <= 100_000)
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
        for ptn, pcn in ranked:
            if ptn == ctn:
                continue  # same-table parents are the recursive/self-FK path, not this one
            pstat = stats_by_table[ptn].columns[pcn]
            if _type_family(cstat.sql_type) != _type_family(pstat.sql_type):
                continue
            if cstat.n_distinct > pstat.n_distinct:
                continue  # child domain can't exceed parent
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
    misses = ex.query(
        f"SELECT count(*) FROM (SELECT DISTINCT {qc} AS v FROM {cq} WHERE {qc} IS NOT NULL "
        f"EXCEPT SELECT {qp} FROM {pq}) t"
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
