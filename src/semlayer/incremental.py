"""Partition-wise profiling: merge per-partition statistics, re-profile only what changed.

The measured case for this. One pass over a 45TB Oracle warehouse costs days
(prd/scan-cost-and-connectors.md), and approximating the distinct counts only
takes ~18% off, because the null pass is irreducible and already 43% of the
expensive query. What takes ~100% off is not re-reading data that has not
changed -- and in a warehouse the large tables are partitioned, usually by
date, so almost all of the bytes are history that will never change again.

What merges EXACTLY, and what does not:

  row_count     sum                                  exact
  n_null        sum                                  exact
  min / max     min of mins, max of maxes            exact (see the cast trap)
  avg_len       weighted by non-null count            exact
  top_values    merge of value -> count maps          NOT exact: each partition
                                                      keeps only its top TOP_K
  n_distinct    NOT SUMMABLE AT ALL                   bounds only
  is_unique     not derivable from parts              needs a global check

`n_distinct` is the whole difficulty. A value can appear in every partition, so
the true count is bounded but not determined:

    max(d_i)  <=  D  <=  sum(d_i)

Both bounds are exact and cheap. This module reports them and sets the merged
`n_distinct` to the LOWER bound deliberately: understating distinctness makes a
key look non-unique, which costs recall, while overstating it would make a
non-key look unique and assert a false primary key. Failing toward "not a key"
is the safe direction. A mergeable HLL sketch narrows the gap to ~0.05%
(measured) and is the next slice; this module is written so the sketch slots
into `resolve` without the merge changing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import pathlib
from dataclasses import dataclass, field, replace
from typing import Protocol, runtime_checkable

from semlayer.profile.stats import ColumnStats, TableStats
from semlayer.source import Partition, TableMeta

logger = logging.getLogger(__name__)

# sql_type families whose min/max must be compared as NUMBERS. ColumnStats
# keeps min_val/max_val as strings, so a lexical merge would decide "9" > "10"
# and report a maximum of 9 for a column reaching 10. Dates and timestamps are
# ISO-formatted, where lexical order IS chronological, so they need no cast.
_NUMERIC = ("INT", "DEC", "NUM", "FLOAT", "DOUBLE", "REAL", "BIGINT", "SMALLINT")


def _is_numeric(sql_type: str) -> bool:
    t = (sql_type or "").upper()
    return any(k in t for k in _NUMERIC)


def _extreme(vals: list[str | None], sql_type: str, want_max: bool) -> str | None:
    """Min or max across partitions, comparing numerically where that is right."""
    present = [v for v in vals if v is not None]
    if not present:
        return None
    if _is_numeric(sql_type):
        try:
            # Key ONLY on the numeric value. Keying on the (value, string)
            # tuple broke ties by string order, so min('0', '0.0') -> '0'.
            # Both answers were CORRECT -- '0' and '0.0' are the same number,
            # so either is a valid extreme -- which is why this is stability,
            # not a bug fix. The actual defect was elsewhere: a caller compared
            # this output to a parent's string by equality and read '0' != '0.0'
            # as a range violation between two equal numbers. Comparing
            # numerically is that caller's job; returning a stable
            # representation just removes the temptation.
            return (max(present, key=float) if want_max
                    else min(present, key=float))
        except (TypeError, ValueError):
            pass          # not actually parseable; fall through to lexical
    return max(present) if want_max else min(present)


@dataclass(frozen=True)
class DistinctBounds:
    """What partition-wise merging can honestly say about a distinct count."""

    lower: int                # max over partitions
    upper: int                # sum over partitions

    @property
    def exact(self) -> bool:
        """True when the bounds coincide, so no sketch or global pass is needed."""
        return self.lower == self.upper


def merge_column_stats(parts: list[ColumnStats]) -> tuple[ColumnStats, DistinctBounds]:
    """Merge one column's stats across partitions, plus honest distinct bounds."""
    if not parts:
        raise ValueError("cannot merge zero partitions")
    first = parts[0]
    rows = sum(p.row_count for p in parts)
    nulls = sum(p.n_null for p in parts)
    bounds = DistinctBounds(lower=max(p.n_distinct for p in parts),
                            upper=sum(p.n_distinct for p in parts))

    weighted = [(p.avg_len, p.row_count - p.n_null) for p in parts if p.avg_len is not None]
    avg_len = (sum(a * n for a, n in weighted) / sum(n for _, n in weighted)
               if weighted and sum(n for _, n in weighted) else None)

    merged_top: dict[str, int] = {}
    for p in parts:
        for v, n in p.top_values:
            merged_top[v] = merged_top.get(v, 0) + n
    top = sorted(merged_top.items(), key=lambda kv: (-kv[1], kv[0]))[:len(first.top_values) or 0]

    # A partition's top_values is COMPLETE when it lists every distinct value it
    # has, which `profile_table` gives us for low-cardinality columns. When that
    # holds for every partition, the union of those value sets is the exact
    # global distinct count -- no sketch and no extra scan. This matters: without
    # it a column with one value per partition and six overall is reported as
    # having ONE, an 83% error on a published number, because the lower bound is
    # all arithmetic can offer.
    if all(len(p.top_values) == p.n_distinct for p in parts):
        exact = len(merged_top)
        bounds = DistinctBounds(lower=exact, upper=exact)

    # is_unique only when provable: the bounds must coincide, the count must
    # equal every row, and no row may be null. Anything less stays False.
    unique = bool(bounds.exact and nulls == 0 and rows > 0 and bounds.lower == rows)

    return replace(
        first,
        row_count=rows,
        n_null=nulls,
        n_distinct=bounds.lower,
        is_unique=unique,
        min_val=_extreme([p.min_val for p in parts], first.sql_type, want_max=False),
        max_val=_extreme([p.max_val for p in parts], first.sql_type, want_max=True),
        avg_len=avg_len,
        top_values=top,
    ), bounds


def merge_table_stats(parts: list[TableStats]) -> tuple[TableStats, dict[str, DistinctBounds]]:
    """Merge per-partition TableStats into one, reporting distinct bounds per column.

    Columns whose bounds do not coincide are the ONLY work left: either a
    sketch union or one global distinct pass over that column. Everything else
    is exact.
    """
    if not parts:
        raise ValueError("cannot merge zero partitions")
    cols: dict[str, ColumnStats] = {}
    bounds: dict[str, DistinctBounds] = {}
    for name in parts[0].columns:
        per = [p.columns[name] for p in parts if name in p.columns]
        cols[name], bounds[name] = merge_column_stats(per)
    return TableStats(table=parts[0].table,
                      row_count=sum(p.row_count for p in parts),
                      columns=cols), bounds


def partitions_to_profile(
    current: tuple[Partition, ...], known: dict[str, str],
) -> tuple[list[Partition], list[Partition]]:
    """Split partitions into (must re-profile, reusable), by change token.

    `known` maps a partition's qualified name to the token recorded when it was
    last profiled. Tokens are compared for EQUALITY ONLY -- never ordering.
    Postgres' `n_tup_*` counters are cumulative since the last stats reset, so
    `pg_stat_reset()` makes them go backwards; a decrease is still a change and
    must still force a re-profile. An unknown partition is always re-profiled,
    and an empty token means "the source could not tell us", which also forces
    one rather than silently trusting stale state.
    """
    stale, reusable = [], []
    for p in current:
        prev = known.get(p.qualified)
        if prev is None or not p.change_token or prev != p.change_token:
            stale.append(p)
        else:
            reusable.append(p)
    return stale, reusable


# --- state: a CACHE, never a source of truth -------------------------------
#
# The whole design stays a two-way door on one rule: everything stored here
# must be RE-DERIVABLE from the warehouse. Delete it, corrupt it, move it to a
# different machine or a different backend, and inference is slower, never
# wrong. That is what lets us change the format, migrate the sidecar into the
# hosted control plane, or abandon it, with nothing lost.
#
# The rule to enforce in review: if something un-derivable ever lands in here
# -- a human's review decision, an accepted-FK override, a renamed metric --
# state becomes load-bearing and this stops being reversible.
#
# So: sketch bytes and raw statistics, yes. Anything a person decided, no.

STATE_VERSION = 1


@dataclass(frozen=True)
class StateKey:
    """Identifies one partition's cached statistics, independent of WHERE stored.

    `source_id` is supplied by the caller rather than invented here, and must
    be stable and free of credentials -- it ends up in a file a user may commit.
    """

    source_id: str
    schema: str
    table: str
    partition: str

    def path(self) -> str:
        """A stable, filesystem- and URL-safe identity for this entry."""
        raw = "/".join((self.source_id, self.schema, self.table, self.partition))
        return hashlib.sha256(raw.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class PartitionState:
    """Cached, re-derivable statistics for one partition.

    `producer` records which engine computed this. It matters for sketches:
    Snowflake's HLL_ACCUMULATE, BigQuery's HLL_COUNT.INIT and Apache
    DataSketches blobs are not mutually readable, so merging across producers
    would yield silent garbage rather than an error. A mismatch must force a
    re-profile -- which is harmless, because of the cache rule above.
    """

    change_token: str
    row_count: int
    columns: dict[str, dict]          # column name -> plain-JSON ColumnStats
    producer: str = ""
    sketches: dict[str, str] = field(default_factory=dict)   # column -> base64

    def to_json(self) -> dict:
        """Plain JSON, with the version stamped on every entry."""
        return {"state_version": STATE_VERSION, "change_token": self.change_token,
                "row_count": self.row_count, "columns": self.columns,
                "producer": self.producer, "sketches": self.sketches}

    @classmethod
    def from_json(cls, d: dict) -> PartitionState | None:
        """Parse one entry, or None if it is from a version we do not know.

        Unknown version means IGNORE and re-profile -- never raise. That is
        what lets the format change unilaterally.
        """
        if not isinstance(d, dict) or d.get("state_version") != STATE_VERSION:
            return None
        try:
            return cls(change_token=str(d["change_token"]), row_count=int(d["row_count"]),
                       columns=dict(d["columns"]), producer=str(d.get("producer", "")),
                       sketches=dict(d.get("sketches") or {}))
        except (KeyError, TypeError, ValueError):
            return None


def stats_to_json(cs: ColumnStats) -> dict:
    """ColumnStats -> plain JSON. top_values' tuples become lists."""
    return {"name": cs.name, "sql_type": cs.sql_type, "row_count": cs.row_count,
            "n_null": cs.n_null, "n_distinct": cs.n_distinct,
            "is_unique": cs.is_unique, "min_val": cs.min_val, "max_val": cs.max_val,
            "avg_len": cs.avg_len, "top_values": [[v, n] for v, n in cs.top_values]}


def stats_from_json(d: dict) -> ColumnStats:
    """Inverse of stats_to_json."""
    return ColumnStats(
        name=d["name"], sql_type=d["sql_type"], row_count=int(d["row_count"]),
        n_null=int(d["n_null"]), n_distinct=int(d["n_distinct"]),
        is_unique=bool(d["is_unique"]), min_val=d.get("min_val"),
        max_val=d.get("max_val"), avg_len=d.get("avg_len"),
        top_values=[(v, int(n)) for v, n in (d.get("top_values") or [])])


@runtime_checkable
class StateStore(Protocol):
    """Where cached partition statistics live. Two methods, so any backend fits."""

    def load(self, key: StateKey) -> PartitionState | None:
        """Cached state for this partition, or None to re-profile."""
        ...

    def save(self, key: StateKey, state: PartitionState) -> None:
        """Record state for this partition."""
        ...


class NullStateStore:
    """Remembers nothing: every run is a full profile. The default.

    Its existence is the point -- the engine must be fully functional with no
    state at all, or the hosted control plane becomes mandatory and the
    standalone-engine stance in CLAUDE.md is dead.
    """

    def load(self, key: StateKey) -> PartitionState | None:
        """Always None."""
        return None

    def save(self, key: StateKey, state: PartitionState) -> None:
        """Discard."""
        return None


class LocalStateStore:
    """A JSON sidecar beside layer.yaml. The free CLI's backend.

    Held in memory and written once by `flush`, because a CLI run saves every
    partition and rewriting the file each time would be quadratic. Scaling
    limit, stated rather than discovered: one file for a warehouse with
    hundreds of thousands of partitions is the wrong shape, and that is when
    the remote store earns its place.
    """

    def __init__(self, path: str | pathlib.Path):
        self.path = pathlib.Path(path)
        self._entries: dict[str, dict] = {}
        self._load_file()

    def _load_file(self) -> None:
        try:
            doc = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return                      # absent or unreadable: start empty
        if not isinstance(doc, dict) or doc.get("state_version") != STATE_VERSION:
            return                      # a format we do not know: ignore it all
        entries = doc.get("entries")
        if isinstance(entries, dict):
            self._entries = entries

    def load(self, key: StateKey) -> PartitionState | None:
        """Cached state, or None if absent or from an unknown version."""
        raw = self._entries.get(key.path())
        return PartitionState.from_json(raw) if raw else None

    def save(self, key: StateKey, state: PartitionState) -> None:
        """Buffer in memory; `flush` writes the file."""
        self._entries[key.path()] = state.to_json()

    def flush(self) -> None:
        """Write the sidecar atomically, so an interrupted run cannot corrupt it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(
            {"state_version": STATE_VERSION, "entries": self._entries}, indent=1))
        tmp.replace(self.path)


@dataclass
class IncrementalResult:
    """What a partition-wise profile produced, and how much it had to read."""

    stats: TableStats
    bounds: dict[str, DistinctBounds]
    profiled: list[str]        # partitions actually read
    reused: list[str]          # partitions served from state
    sketched: list[str] = field(default_factory=list)      # columns a union resolved
    needs_exact: list[str] = field(default_factory=list)   # columns that may be keys
    resolved_exactly: list[str] = field(default_factory=list)  # columns proven by a count


def _locally_distinct(cs: ColumnStats) -> bool:
    """Is every non-null value in this partition distinct?

    The filter that keeps state small. A GLOBALLY unique column must be unique
    within every partition, so a column that already repeats inside one
    partition can never be a key and needs no sketch for key detection.
    """
    non_null = cs.row_count - cs.n_null
    return non_null > 0 and cs.n_distinct == non_null


def _resolve_with_sketches(
    source, res: IncrementalResult, states: dict[str, PartitionState],
    producer: str,
) -> None:
    """Narrow distinct bounds by unioning per-partition sketches, in place.

    A sketch never proves uniqueness -- it says whether the row count falls
    inside the confidence interval, i.e. whether an exact check is worth
    running. Columns where it does land in `needs_exact`.

    Unions are refused across producers: Snowflake, BigQuery and DataSketches
    blobs are not mutually readable, and a mixed union would be silent garbage.
    """
    union = getattr(source, "estimate_sketch_union", None)
    if union is None or not producer:
        return
    by_col: dict[str, list[str]] = {}
    for st in states.values():
        if st.producer and st.producer != producer:
            return          # mixed producers: refuse the whole table, re-profile later
        for col, blob in st.sketches.items():
            by_col.setdefault(col, []).append(blob)
    for col, blobs in by_col.items():
        if col not in res.stats.columns or len(blobs) != len(states):
            continue        # a partition contributed no sketch: bounds stay as merged
        if res.bounds[col].exact:
            continue        # already known exactly; a sketch can only make it worse
        got = union(blobs)
        if got is not None:
            _narrow_one(res, col, got)


def _narrow_one(res: IncrementalResult, col: str, got: tuple[int, int, int]) -> None:
    """Apply one sketch union to a column's bounds, in place."""
    est, lo, hi = got
    prev = res.bounds[col]
    # Intersect with the count-derived bounds: a sketch can only narrow what
    # arithmetic already proved, never widen past it.
    lo, hi = max(lo, prev.lower), min(hi, prev.upper)
    if lo > hi:
        return              # sketch and arithmetic disagree: keep the proven bounds
    # The point estimate must sit inside them too. Unclamped, a sketch reported
    # 61,715 distinct values for a column that provably has at most 60,000 (the
    # sum of its partitions' counts) -- incoherent on its face.
    est = min(max(est, lo), hi)
    res.bounds[col] = DistinctBounds(lower=lo, upper=hi)
    res.stats.columns[col] = replace(res.stats.columns[col], n_distinct=est)
    res.sketched.append(col)
    # A sketch never PROVES uniqueness. If the row count falls inside the
    # interval the column may be a key and an exact check is worth running.
    if lo <= res.stats.row_count <= hi:
        res.needs_exact.append(col)


def profile_partitioned(
    source, table: TableMeta, partitions: tuple[Partition, ...],
    store: StateStore, source_id: str,
) -> IncrementalResult:
    """Profile a partitioned table, reading only partitions whose token moved.

    Correctness rests on the token, not on this function: see
    `partitions_to_profile`. A partition with no usable cached state is simply
    profiled, so a cold store, an unknown format and a changed producer all
    converge on "do the work".
    """
    from semlayer.profile.stats import profile_table

    known: dict[str, str] = {}
    cached: dict[str, PartitionState] = {}
    for p in partitions:
        st = store.load(StateKey(source_id, p.parent_schema, p.parent_table, p.name))
        if st is not None:
            cached[p.name] = st
            known[p.qualified] = st.change_token
    stale, reusable = partitions_to_profile(partitions, known)

    per: list[TableStats] = []
    for p in reusable:
        st = cached[p.name]
        per.append(TableStats(
            table=TableMeta(p.schema, p.name, table.columns),
            row_count=st.row_count,
            columns={n: stats_from_json(d) for n, d in st.columns.items()}))
    producer = ""
    get_producer = getattr(source, "sketch_producer", None)
    if get_producer is not None:
        producer = get_producer() or ""
    build = getattr(source, "build_distinct_sketch", None)
    qualify = getattr(source, "qualify", lambda sch, n: f'"{sch}"."{n}"')

    for p in stale:
        meta = TableMeta(p.schema, p.name, table.columns)
        ts = profile_table(source, meta)
        per.append(ts)
        sketches: dict[str, str] = {}
        if build is not None and producer:
            fq = qualify(p.schema, p.name)
            for name, cs in ts.columns.items():
                if _locally_distinct(cs):
                    blob = build(fq, name)
                    if blob:
                        sketches[name] = blob
        st = PartitionState(change_token=p.change_token, row_count=ts.row_count,
                            columns={n: stats_to_json(c) for n, c in ts.columns.items()},
                            producer=producer or type(source).__name__,
                            sketches=sketches)
        cached[p.name] = st
        store.save(StateKey(source_id, p.parent_schema, p.parent_table, p.name), st)

    merged, bounds = merge_table_stats(per)
    res = IncrementalResult(
        stats=TableStats(table=table, row_count=merged.row_count, columns=merged.columns),
        bounds=bounds,
        profiled=[p.name for p in stale], reused=[p.name for p in reusable])
    _resolve_with_sketches(source, res, cached, producer)
    res.resolved_exactly = resolve_uniqueness(source, partitions, res, per)
    return res


def apply_cardinality(table: dict, res: IncrementalResult) -> None:
    """Rewrite a table's `cardinality` blocks from a partition-wise profile.

    A whole-table profile counts exactly and says so. A partition-wise one
    usually cannot, and this is where that honesty has to survive into the
    published document: the interval is what we know, `distinct` is a point
    inside it, and `method` says what produced it.

    `exact` is still claimed where it is earned -- when the merged bounds
    coincide, which happens for any low-cardinality column whose partitions
    each listed all their values.
    """
    sketched = set(res.sketched)
    for col in table.get("columns", []):
        name = col.get("name")
        b = res.bounds.get(name)
        if b is None or name not in res.stats.columns:
            continue
        cs = res.stats.columns[name]
        if b.exact:
            method = "exact"
        elif name in sketched:
            method = "hll_sketch"
        else:
            method = "bounds_only"
        col["cardinality"] = {
            "distinct": cs.n_distinct,
            "lower_bound": b.lower,
            "upper_bound": b.upper,
            "method": method,
            "null_count": cs.n_null,
            # No `as_of`. It was `prev.get("as_of") or _now()`, which made the
            # document non-deterministic on full builds AND reported the wrong
            # thing here: `prev` is the PREVIOUS DOCUMENT, so once set the
            # timestamp was kept whether or not this column had just been
            # re-profiled. It recorded when a statistic was FIRST measured and
            # never when it was last measured -- the opposite of staleness.
            #
            # Proper staleness reporting belongs on PartitionState next to
            # `change_token`, which is what actually knows a partition moved,
            # and should surface in the document only for columns whose numbers
            # were reused. Deliberately deferred; see docs/calibration.md.
            "partitions_covered": len(res.profiled) + len(res.reused),
        }


# One exact check is the only thing that proves a key. Capped so a pathological
# table cannot turn "scope the work" into "do more work": a warehouse with 40
# locally-distinct columns in one table is telling us something is wrong with
# the shape, not that it has 40 keys.
MAX_UNIQUENESS_CHECKS = 12


def uniqueness_candidates(per_partition: list[TableStats], res: IncrementalResult) -> list[str]:
    """Columns that could still be a global key, cheapest nomination first.

    The free and sound filter: a globally unique column must be unique within
    EVERY partition, so a column that repeats inside any one of them is
    eliminated with no query at all. That is a necessary condition, not a
    sufficient one -- partitions can each be internally unique and still
    overlap each other, which is exactly the case merging cannot resolve.

    Columns already proven exact are skipped: there is nothing left to learn.
    """
    if not per_partition:
        return []
    out = []
    for name, cs in res.stats.columns.items():
        if res.bounds[name].exact:
            continue                      # already known, including is_unique
        per = [p.columns[name] for p in per_partition if name in p.columns]
        if len(per) != len(per_partition) or not per:
            continue
        if all(_locally_distinct(c) for c in per) and cs.n_null == 0:
            out.append(name)
    # Narrowest interval first: the sketch, where there was one, has already
    # said which of these is most likely to be a key.
    out.sort(key=lambda n: res.bounds[n].upper - res.bounds[n].lower)
    return out[:MAX_UNIQUENESS_CHECKS]


def resolve_uniqueness(source, partitions: tuple[Partition, ...],
                       res: IncrementalResult, per_partition: list[TableStats]) -> list[str]:
    """Prove or disprove uniqueness for the key candidates, exactly.

    This is the piece that makes partition-wise profiling usable for keys at
    all. Merging bounds a distinct count at `max(d_i) <= D <= sum(d_i)`, so a
    real key spread over six partitions reads as `[10,000 .. 60,000]` and
    `is_unique` stays False. On an engine with mergeable sketches that interval
    narrows enough to nominate; on PostgreSQL, which has no core HLL, it does
    not narrow at all -- so without this, `--state` on Postgres silently loses
    every primary key. It is a correctness requirement there, not an
    optimisation.

    Costs one query per candidate, against perhaps ten of ninety columns,
    instead of an exact COUNT(DISTINCT) for all ninety.
    """
    cands = uniqueness_candidates(per_partition, res)
    if not cands:
        return []
    qualify = getattr(source, "qualify", lambda sch, n: f'"{sch}"."{n}"')

    def q(ident: str) -> str:
        return '"' + ident.replace('"', '""') + '"'

    resolved = []
    for col in cands:
        # UNION ALL over the partitions rather than the parent relation: a
        # partition set is not always a queryable table (Hive-partitioned
        # parquet, or a set of monthly tables), and this is one pass either way.
        legs = " UNION ALL ".join(
            f"SELECT {q(col)} AS v FROM {qualify(p.schema, p.name)}" for p in partitions)
        try:
            row = source.query(
                f"SELECT count(*), count(DISTINCT v) FROM ({legs}) t")[0]
        except Exception as e:
            logger.warning("uniqueness check failed for %s: %s", col, e)
            continue
        rows, distinct = int(row[0]), int(row[1])
        res.bounds[col] = DistinctBounds(lower=distinct, upper=distinct)
        res.stats.columns[col] = replace(
            res.stats.columns[col], n_distinct=distinct,
            is_unique=bool(rows and distinct == rows
                           and res.stats.columns[col].n_null == 0))
        resolved.append(col)
    return resolved
