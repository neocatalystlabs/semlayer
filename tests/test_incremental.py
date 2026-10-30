"""Partition-wise merging, and the change token that decides what to re-read.

Pure merge tests need no warehouse. The partition-enumeration and
merged-equals-whole tests need real partitions, so they use pgserver -- the
same no-credentials route as test_postgres.py.
"""

from __future__ import annotations

import pytest

from semlayer.incremental import (
    DistinctBounds,
    merge_column_stats,
    merge_table_stats,
    partitions_to_profile,
)
from semlayer.profile.stats import ColumnStats, TableStats
from semlayer.source import Partition, TableMeta


def col(**kw):
    base = dict(name="c", sql_type="INTEGER", row_count=10, n_null=0,
                n_distinct=10, is_unique=True)
    base.update(kw)
    return ColumnStats(**base)


# --- what merges exactly --------------------------------------------------

def test_counts_and_nulls_sum():
    m, _ = merge_column_stats([col(row_count=10, n_null=2), col(row_count=5, n_null=1)])
    assert (m.row_count, m.n_null) == (15, 3)


def test_numeric_min_max_are_compared_as_numbers_not_strings():
    """min_val/max_val are stored as STRINGS, so this is a real trap.

    Lexically "9" > "10", so a naive merge reports a maximum of 9 for a column
    that reaches 10 -- and every downstream range check inherits the error.
    """
    a = col(sql_type="INTEGER", min_val="9", max_val="9")
    b = col(sql_type="INTEGER", min_val="10", max_val="10")
    m, _ = merge_column_stats([a, b])
    assert (m.min_val, m.max_val) == ("9", "10")


def test_text_min_max_stay_lexical():
    a = col(sql_type="VARCHAR", min_val="apple", max_val="apple")
    b = col(sql_type="VARCHAR", min_val="banana", max_val="banana")
    m, _ = merge_column_stats([a, b])
    assert (m.min_val, m.max_val) == ("apple", "banana")


def test_iso_dates_need_no_cast_because_lexical_order_is_chronological():
    a = col(sql_type="DATE", min_val="2024-01-15", max_val="2024-01-15")
    b = col(sql_type="DATE", min_val="2024-11-02", max_val="2024-11-02")
    m, _ = merge_column_stats([a, b])
    assert (m.min_val, m.max_val) == ("2024-01-15", "2024-11-02")


def test_unparseable_numeric_falls_back_to_lexical_rather_than_raising():
    a = col(sql_type="INTEGER", min_val="n/a", max_val="n/a")
    b = col(sql_type="INTEGER", min_val="zzz", max_val="zzz")
    m, _ = merge_column_stats([a, b])
    assert m.max_val == "zzz"


def test_avg_len_is_weighted_by_non_null_rows():
    a = col(sql_type="VARCHAR", row_count=100, n_null=0, avg_len=10.0)
    b = col(sql_type="VARCHAR", row_count=300, n_null=0, avg_len=2.0)
    m, _ = merge_column_stats([a, b])
    assert m.avg_len == pytest.approx((10.0 * 100 + 2.0 * 300) / 400)


def test_all_null_column_merges_to_no_min_max():
    m, _ = merge_column_stats([col(n_null=10, min_val=None, max_val=None),
                               col(n_null=10, min_val=None, max_val=None)])
    assert m.min_val is None and m.max_val is None


# --- what does not merge, and must not pretend to -------------------------

def test_distinct_bounds_bracket_the_truth():
    """A value can appear in every partition, so only bounds are knowable."""
    m, b = merge_column_stats([col(n_distinct=3), col(n_distinct=5)])
    assert (b.lower, b.upper) == (5, 8)
    assert not b.exact
    assert m.n_distinct == b.lower, "the LOWER bound, deliberately"


def test_single_partition_distinct_is_exact():
    _, b = merge_column_stats([col(n_distinct=7)])
    assert b.exact and b.lower == b.upper == 7


def test_uniqueness_is_never_asserted_from_inexact_bounds():
    """Understating distinctness costs recall; overstating asserts a false key.

    A genuine key spread over partitions has lower bound = rows-per-partition,
    far below the row count, so merging alone CANNOT find keys. That is the
    conservative direction, and it is why the sketch is required rather than
    merely an optimisation.
    """
    parts = [col(row_count=100, n_null=0, n_distinct=100, is_unique=True) for _ in range(3)]
    m, b = merge_column_stats(parts)
    assert m.row_count == 300
    assert (b.lower, b.upper) == (100, 300)
    assert m.is_unique is False, "not provable from parts, so not claimed"


def test_uniqueness_is_asserted_when_it_is_provable():
    m, b = merge_column_stats([col(row_count=10, n_null=0, n_distinct=10)])
    assert b.exact and m.is_unique is True


def test_nulls_block_uniqueness_even_when_bounds_are_exact():
    m, _ = merge_column_stats([col(row_count=10, n_null=1, n_distinct=10)])
    assert m.is_unique is False


def test_merge_rejects_zero_partitions():
    with pytest.raises(ValueError):
        merge_column_stats([])
    with pytest.raises(ValueError):
        merge_table_stats([])


def test_table_merge_covers_every_column():
    meta = TableMeta("s", "t", ())
    a = TableStats(meta, 10, {"x": col(row_count=10), "y": col(row_count=10, n_distinct=2)})
    b = TableStats(meta, 5, {"x": col(row_count=5), "y": col(row_count=5, n_distinct=2)})
    m, bounds = merge_table_stats([a, b])
    assert m.row_count == 15
    assert set(bounds) == {"x", "y"}
    assert bounds["y"] == DistinctBounds(lower=2, upper=4)


# --- the change token -----------------------------------------------------

def part(name, token="1:0:0"):
    return Partition("w", name, "w", "evt", change_token=token)


def test_unknown_partition_is_always_reprofiled():
    stale, reuse = partitions_to_profile((part("p1"),), {})
    assert [p.name for p in stale] == ["p1"] and not reuse


def test_unchanged_token_is_reused():
    stale, reuse = partitions_to_profile((part("p1", "7:0:0"),), {"w.p1": "7:0:0"})
    assert not stale and [p.name for p in reuse] == ["p1"]


def test_changed_token_forces_a_reprofile():
    stale, _ = partitions_to_profile((part("p1", "7:1:0"),), {"w.p1": "7:0:0"})
    assert [p.name for p in stale] == ["p1"]


def test_a_DECREASED_token_still_forces_a_reprofile():
    """pg_stat_reset() makes the counters go backwards.

    Comparing as an ordering would read that as "no new activity" and keep
    stale state forever. Tokens are compared for equality only.
    """
    stale, reuse = partitions_to_profile((part("p1", "1:0:0"),), {"w.p1": "9000:5:2"})
    assert [p.name for p in stale] == ["p1"] and not reuse


def test_an_empty_token_is_treated_as_unknown_not_as_unchanged():
    """A source that cannot tell us must not be read as 'nothing changed'."""
    stale, reuse = partitions_to_profile((part("p1", ""),), {"w.p1": ""})
    assert [p.name for p in stale] == ["p1"] and not reuse


# --- against real partitions ---------------------------------------------

pgserver = pytest.importorskip("pgserver")
pytest.importorskip("psycopg")

PART_DDL = """
DROP SCHEMA IF EXISTS w CASCADE;
CREATE SCHEMA w;
CREATE TABLE w.evt (evt_id BIGINT, cust_id INT, amt NUMERIC(10,2),
                    sts_cd TEXT, evt_dt DATE NOT NULL)
  PARTITION BY RANGE (evt_dt);
CREATE TABLE w.evt_2024_01 PARTITION OF w.evt
  FOR VALUES FROM ('2024-01-01') TO ('2024-02-01');
CREATE TABLE w.evt_2024_02 PARTITION OF w.evt
  FOR VALUES FROM ('2024-02-01') TO ('2024-03-01');
CREATE TABLE w.evt_2024_03 PARTITION OF w.evt
  FOR VALUES FROM ('2024-03-01') TO ('2024-04-01');
"""


@pytest.fixture(scope="module")
def part_src(tmp_path_factory):
    import psycopg

    from semlayer.pipeline import open_source_from_uri

    srv = pgserver.get_server(str(tmp_path_factory.mktemp("pgpart")))
    uri = srv.get_uri()
    with psycopg.connect(uri, autocommit=True) as c:
        c.execute(PART_DDL)
        with c.cursor() as cur:
            cur.executemany(
                "INSERT INTO w.evt VALUES (%s,%s,%s,%s,%s)",
                [(i, i % 500, 10.0 + (i % 97), "AX"[i % 2], f"2024-0{(i % 3) + 1}-15")
                 for i in range(30000)])
        c.execute("ANALYZE")
    src = open_source_from_uri(uri)
    yield src
    src.close()


def test_enumerates_partitions_with_bounds_and_row_estimates(part_src):
    parts = part_src.list_partitions()
    assert ("w", "evt") in parts, "the parent, not the children, is the key"
    got = parts[("w", "evt")]
    assert [p.name for p in got] == ["evt_2024_01", "evt_2024_02", "evt_2024_03"]
    assert all(p.est_rows > 0 for p in got), "reltuples, free from the catalog"
    assert all("FOR VALUES FROM" in p.bound for p in got)
    assert all(p.change_token for p in got)


def test_merged_partition_stats_equal_a_full_table_profile(part_src):
    """The claim the whole design rests on, checked field by field."""
    from semlayer.profile.stats import profile_table

    parent = next(t for t in part_src.list_tables() if t.qualified == "w.evt")
    parts = part_src.list_partitions()[("w", "evt")]
    full = profile_table(part_src, parent)
    per = [profile_table(part_src, TableMeta(p.schema, p.name, parent.columns))
           for p in parts]
    merged, bounds = merge_table_stats(per)

    assert merged.row_count == full.row_count
    for name, fc in full.columns.items():
        mc = merged.columns[name]
        assert mc.n_null == fc.n_null, name
        assert mc.min_val == fc.min_val, f"{name} min"
        assert mc.max_val == fc.max_val, f"{name} max"
        # and the bounds must bracket the true distinct count
        assert bounds[name].lower <= fc.n_distinct <= bounds[name].upper, name


def test_a_backfill_changes_the_token_though_the_row_count_does_not(part_src):
    """The silent-failure mode this design has to survive.

    An in-place correction to an old partition leaves the row count identical.
    Verified: an UPDATE of ~10,000 rows moves n_tup_upd by ~10,000 and leaves
    reltuples and count(*) untouched, so a count-based token would reuse a
    stale sketch forever.
    """
    import psycopg

    before = {p.qualified: p.change_token
              for p in part_src.list_partitions()[("w", "evt")]}
    rows_before = part_src.query("SELECT count(*) FROM w.evt_2024_01")[0][0]

    with psycopg.connect(part_src.con.info.dsn, autocommit=True) as c:
        n = c.execute("UPDATE w.evt SET amt = amt + 1 WHERE evt_dt = '2024-01-15'").rowcount
    assert n > 0, "the backfill must actually touch rows"

    after = {p.qualified: p.change_token
             for p in part_src.list_partitions()[("w", "evt")]}
    rows_after = part_src.query("SELECT count(*) FROM w.evt_2024_01")[0][0]

    assert rows_after == rows_before, "an in-place update does not change the count"
    assert after["w.evt_2024_01"] != before["w.evt_2024_01"], \
        "the token MUST move, or backfills are invisible"
    assert after["w.evt_2024_02"] == before["w.evt_2024_02"], \
        "untouched partitions must stay reusable"

    stale, reuse = partitions_to_profile(
        part_src.list_partitions()[("w", "evt")], before)
    assert [p.name for p in stale] == ["evt_2024_01"]
    assert len(reuse) == 2, "only the backfilled partition is re-read"


# --- state is a CACHE: the two-way-door guard -----------------------------
#
# These are the analogue of MinimalSource in test_lineage.py. They assert the
# property that makes the state file reversible -- it can be moved, reformatted
# or thrown away without changing any answer. If one of these starts failing,
# something un-derivable has been written into state and the door has shut.

def _profile_all(src, store, source_id="t"):
    from semlayer.incremental import profile_partitioned
    parent = next(t for t in src.list_tables() if t.qualified == "w.evt")
    parts = src.list_partitions()[("w", "evt")]
    return profile_partitioned(src, parent, parts, store, source_id)


def _shape(res):
    """Everything the profile claims, so two runs can be compared exactly."""
    return {
        "rows": res.stats.row_count,
        "cols": {n: (c.row_count, c.n_null, c.n_distinct, c.is_unique,
                     c.min_val, c.max_val, c.avg_len, tuple(c.top_values))
                 for n, c in res.stats.columns.items()},
        "bounds": {n: (b.lower, b.upper) for n, b in res.bounds.items()},
    }


def test_state_changes_nothing_about_the_answer(part_src, tmp_path):
    """Cold store, warm store, and no store at all must agree exactly."""
    from semlayer.incremental import LocalStateStore, NullStateStore

    scratch = LocalStateStore(tmp_path / "layer.state.json")
    cold = _profile_all(part_src, scratch)        # populates
    warm = _profile_all(part_src, scratch)        # serves from state
    none = _profile_all(part_src, NullStateStore())

    assert _shape(cold) == _shape(warm) == _shape(none)
    assert cold.reused == [] and len(cold.profiled) == 3, "cold store reads everything"
    assert warm.profiled == [] and len(warm.reused) == 3, "warm store reads nothing"
    assert none.reused == [], "the null store never reuses"


def test_deleting_the_state_file_costs_time_and_nothing_else(part_src, tmp_path):
    from semlayer.incremental import LocalStateStore

    f = tmp_path / "layer.state.json"
    before = _profile_all(part_src, LocalStateStore(f))
    LocalStateStore(f).flush()
    assert f.exists()
    f.unlink()
    after = _profile_all(part_src, LocalStateStore(f))
    assert _shape(before) == _shape(after)


def test_state_survives_a_round_trip_through_the_file(part_src, tmp_path):
    from semlayer.incremental import LocalStateStore

    f = tmp_path / "layer.state.json"
    s1 = LocalStateStore(f)
    first = _profile_all(part_src, s1)
    s1.flush()
    reopened = _profile_all(part_src, LocalStateStore(f))   # fresh object, same file
    assert _shape(first) == _shape(reopened)
    assert reopened.profiled == [], "the file, not memory, served it"


def test_an_unknown_state_version_is_ignored_rather_than_fatal(part_src, tmp_path):
    """So the format can change unilaterally; old state is just discarded."""
    import json

    from semlayer.incremental import LocalStateStore

    f = tmp_path / "layer.state.json"
    s = LocalStateStore(f)
    _profile_all(part_src, s)
    s.flush()
    doc = json.loads(f.read_text())
    doc["state_version"] = 999999
    f.write_text(json.dumps(doc))

    res = _profile_all(part_src, LocalStateStore(f))
    assert len(res.profiled) == 3, "re-profiled, not crashed"


def test_corrupt_state_file_is_ignored_rather_than_fatal(part_src, tmp_path):
    from semlayer.incremental import LocalStateStore

    f = tmp_path / "layer.state.json"
    f.write_text("{ this is not json")
    res = _profile_all(part_src, LocalStateStore(f))
    assert len(res.profiled) == 3


def test_a_backfill_invalidates_only_the_touched_partition(part_src, tmp_path):
    """The payoff: re-read one partition of three, and still be exactly right."""
    import psycopg

    from semlayer.incremental import LocalStateStore, NullStateStore

    store = LocalStateStore(tmp_path / "layer.state.json")
    _profile_all(part_src, store)

    with psycopg.connect(part_src.con.info.dsn, autocommit=True) as c:
        n = c.execute("UPDATE w.evt SET amt = amt + 5 WHERE evt_dt = '2024-01-15'").rowcount
    assert n > 0

    after = _profile_all(part_src, store)
    assert after.profiled == ["evt_2024_01"], "only the backfilled partition"
    assert len(after.reused) == 2
    # and the incremental answer must equal a from-scratch one
    assert _shape(after) == _shape(_profile_all(part_src, NullStateStore()))


def test_state_keys_are_location_independent():
    """Keyed by content, so the same key resolves in a file, a row or a blob."""
    from semlayer.incremental import StateKey

    a = StateKey("src-a", "w", "evt", "evt_2024_01")
    b = StateKey("src-a", "w", "evt", "evt_2024_01")
    c = StateKey("src-b", "w", "evt", "evt_2024_01")
    assert a.path() == b.path() and a.path() != c.path()
    assert a.path().isalnum(), "filesystem- and URL-safe"


def test_producer_is_recorded_so_sketches_cannot_be_merged_across_engines(part_src, tmp_path):
    """Snowflake, BigQuery and DataSketches HLL blobs are not mutually readable.

    Merging across producers would be silent garbage, so the producer is stored
    now, before any sketch exists to get it wrong.
    """
    from semlayer.incremental import LocalStateStore, PartitionState, StateKey

    f = tmp_path / "layer.state.json"
    s = LocalStateStore(f)
    _profile_all(part_src, s)
    key = StateKey("t", "w", "evt", "evt_2024_01")
    assert s.load(key).producer == "PostgresSource"
    assert PartitionState.from_json({"state_version": 1}) is None, "malformed -> None"


# --- sketches: the piece that makes key detection possible at all ----------

def _duck_partitioned(n_parts=6, rows=10000):
    """Six tables presented as partitions of one logical table.

    Partitions need not be engine-native: Hive-partitioned parquet and a set of
    monthly tables are the same shape, and using DuckDB here keeps the sketch
    path testable without a cloud warehouse.
    """
    import duckdb

    from semlayer.source import DuckDBSource, Partition

    con = duckdb.connect()
    try:
        con.execute("LOAD datasketches")
    except Exception:
        pytest.skip("datasketches extension unavailable")
    for m in range(n_parts):
        con.execute(f"""CREATE TABLE evt_{m} AS SELECT
            (range * {n_parts} + {m}) AS evt_id,
            (range % 500) AS cust_id,
            'M{m}' AS part_cd
          FROM range({rows})""")

    class Partitioned(DuckDBSource):
        def list_partitions(self):
            return {("main", "evt"): tuple(
                Partition("main", f"evt_{m}", "main", "evt",
                          change_token=f"tok{m}", est_rows=rows)
                for m in range(n_parts))}

    src = Partitioned(con)
    cols = next(t for t in src.list_tables() if t.name == "evt_0").columns
    return src, TableMeta("main", "evt", cols), src.list_partitions()[("main", "evt")]


def _run(src, logical, parts, store=None):
    from semlayer.incremental import NullStateStore, profile_partitioned
    return profile_partitioned(src, logical, parts, store or NullStateStore(), "t")


def test_merging_alone_cannot_nominate_a_key_but_an_exact_check_recovers_it():
    """Why sketches matter, and why they are not the only answer.

    MERGING bounds a key at [rows-per-partition .. sum], so `needs_exact` stays
    empty: the sketch is what nominates. But a column locally distinct in every
    partition is nominated for free, and one exact count then proves it -- which
    is how PostgreSQL, with no core HLL, still gets its keys.
    """
    src, logical, parts = _duck_partitioned()
    src._sketches = False
    r = _run(src, logical, parts)
    assert r.stats.row_count == 60000
    assert r.needs_exact == [], "no sketch, so nothing was nominated BY a sketch"
    assert r.sketched == []
    # the free filter nominated it anyway, and the exact check settled it
    assert "evt_id" in r.resolved_exactly
    assert r.bounds["evt_id"] == DistinctBounds(lower=60000, upper=60000)
    assert r.stats.columns["evt_id"].is_unique is True
    # and a column that repeats inside a partition was never checked
    assert "cust_id" not in r.resolved_exactly
    assert r.bounds["cust_id"] == DistinctBounds(lower=500, upper=3000)


def test_sketches_narrow_the_bounds_and_nominate_the_key():
    src, logical, parts = _duck_partitioned()
    r = _run(src, logical, parts)
    b = r.bounds["evt_id"]
    assert b.lower > 55000, f"sketch should narrow the lower bound, got {b}"
    assert r.needs_exact == ["evt_id"]
    assert r.sketched == ["evt_id"], "only locally-distinct columns are sketched"


def test_only_locally_distinct_columns_are_sketched():
    """A globally unique column must be unique in EVERY partition.

    So a column that already repeats inside one partition can never be a key,
    needs no sketch, and must not cost state.
    """
    src, logical, parts = _duck_partitioned()
    r = _run(src, logical, parts)
    assert "cust_id" not in r.sketched
    assert "part_cd" not in r.sketched


def test_the_estimate_is_clamped_inside_the_proven_bounds():
    """sum(d_i) is a PROVEN upper bound; an estimate above it is incoherent.

    Unclamped this reported 61,715 distinct values for a column that cannot
    have more than 60,000.
    """
    src, logical, parts = _duck_partitioned()
    r = _run(src, logical, parts)
    for name, b in r.bounds.items():
        est = r.stats.columns[name].n_distinct
        assert b.lower <= est <= b.upper, f"{name}: {est} outside {b}"
        assert est <= 60000


def test_low_cardinality_columns_are_exact_without_any_sketch():
    """When every partition lists all its values, the union is the exact count.

    Without this, a column with one value per partition and six overall was
    reported as having ONE -- an 83% error on a published number.
    """
    src, logical, parts = _duck_partitioned()
    src._sketches = False
    r = _run(src, logical, parts)
    assert r.bounds["part_cd"] == DistinctBounds(lower=6, upper=6)
    assert r.bounds["part_cd"].exact
    assert r.stats.columns["part_cd"].n_distinct == 6


def test_a_union_across_producers_is_refused():
    """Snowflake, BigQuery and DataSketches blobs are not mutually readable.

    A mixed union would be silent garbage, so the whole table falls back to
    arithmetic bounds rather than producing a confident wrong number.
    """
    from semlayer.incremental import LocalStateStore, PartitionState, StateKey

    src, logical, parts = _duck_partitioned()
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        store = LocalStateStore(f"{d}/s.json")
        _run(src, logical, parts, store)
        # forge one entry as if another engine had written it
        k = StateKey("t", "main", "evt", "evt_0")
        st = store.load(k)
        store.save(k, PartitionState(change_token=st.change_token, row_count=st.row_count,
                                     columns=st.columns, producer="snowflake-hll",
                                     sketches=st.sketches))
        r = _run(src, logical, parts, store)
    assert r.sketched == [], "a mixed-producer union must not be attempted"
    # the exact check is independent of sketches, so the key is still proven --
    # a refused union degrades the ESTIMATE, never the correctness
    assert "evt_id" in r.resolved_exactly
    assert r.bounds["cust_id"] == DistinctBounds(lower=500, upper=3000)


def test_a_missing_sketch_for_one_partition_leaves_bounds_as_merged():
    from semlayer.incremental import LocalStateStore, PartitionState, StateKey

    src, logical, parts = _duck_partitioned()
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        store = LocalStateStore(f"{d}/s.json")
        _run(src, logical, parts, store)
        k = StateKey("t", "main", "evt", "evt_3")
        st = store.load(k)
        store.save(k, PartitionState(change_token=st.change_token, row_count=st.row_count,
                                     columns=st.columns, producer=st.producer, sketches={}))
        r = _run(src, logical, parts, store)
    assert r.sketched == [], "an incomplete set of sketches must not be unioned"


def test_sketches_round_trip_through_the_state_file():
    from semlayer.incremental import LocalStateStore

    src, logical, parts = _duck_partitioned()
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        f = f"{d}/s.json"
        s1 = LocalStateStore(f)
        first = _run(src, logical, parts, s1)
        s1.flush()
        again = _run(src, logical, parts, LocalStateStore(f))
    assert again.profiled == [], "served from the file"
    assert again.sketched == first.sketched
    assert again.bounds["evt_id"] == first.bounds["evt_id"]
    assert again.needs_exact == first.needs_exact


def test_a_source_without_sketch_support_still_profiles_partitions():
    """Postgres has no core HLL; bounds-only must remain a working mode."""
    src, logical, parts = _duck_partitioned()
    for attr in ("sketch_producer", "build_distinct_sketch", "estimate_sketch_union"):
        setattr(type(src), attr, None)
    try:
        r = _run(src, logical, parts)
    finally:
        for attr in ("sketch_producer", "build_distinct_sketch", "estimate_sketch_union"):
            delattr(type(src), attr)
    assert r.stats.row_count == 60000
    assert r.sketched == []


# --- the cardinality block in layer.yaml ----------------------------------

def test_cardinality_reports_the_interval_and_how_it_was_obtained():
    from semlayer.incremental import apply_cardinality

    src, logical, parts = _duck_partitioned()
    res = _run(src, logical, parts)
    table = {"name": "evt", "columns": [{"name": n} for n in
                                        ("evt_id", "cust_id", "part_cd")]}
    apply_cardinality(table, res)
    by = {c["name"]: c["cardinality"] for c in table["columns"]}

    # the key ends up exact -- the sketch narrowed it, then one count PROVED it,
    # and claiming "exact" is honest only because a count actually ran
    assert by["evt_id"]["method"] == "exact"
    assert by["evt_id"]["lower_bound"] == by["evt_id"]["upper_bound"] == 60000

    # low cardinality came out exact from complete value maps, with no sketch
    assert by["part_cd"]["method"] == "exact"
    assert (by["part_cd"]["lower_bound"], by["part_cd"]["upper_bound"]) == (6, 6)

    # and the honest remaining case: an interval nothing narrowed
    assert by["cust_id"]["method"] == "bounds_only"
    assert by["cust_id"]["upper_bound"] > by["cust_id"]["lower_bound"]

    for c in by.values():
        assert c["lower_bound"] <= c["distinct"] <= c["upper_bound"]
        assert c["partitions_covered"] == 6


def test_cardinality_blocks_validate_against_the_spec():
    """The field is declared, not just tolerated by a permissive schema."""
    import jsonschema

    from semlayer.incremental import apply_cardinality
    from semlayer.validate import _load_schema

    src, logical, parts = _duck_partitioned()
    res = _run(src, logical, parts)
    table = {"name": "evt", "columns": [{"name": n} for n in
                                        ("evt_id", "cust_id", "part_cd")]}
    apply_cardinality(table, res)
    sub = _load_schema()["$defs"]["cardinality"]
    for c in table["columns"]:
        jsonschema.validate(c["cardinality"], sub)


def test_a_full_table_profile_claims_exact():
    """No partitions involved means a real COUNT(DISTINCT), so say so."""
    import duckdb

    from semlayer.profile.run import profile_with_stats
    from semlayer.source import DuckDBSource

    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT range AS k, range % 3 AS g FROM range(30)")
    doc, _ = profile_with_stats(DuckDBSource(con), llm=None)
    cols = {c["name"]: c["cardinality"] for c in doc["semantic_layer"]["tables"][0]["columns"]}
    assert cols["k"]["method"] == "exact"
    assert cols["k"]["lower_bound"] == cols["k"]["upper_bound"] == 30
    assert cols["g"]["distinct"] == 3
    con.close()


# --- reachable from the product, not just from these tests ----------------
#
# The machinery above was built and tested for four slices while nothing in
# pipeline.py or cli.py called it, so no user could reach any of it. These
# assert the wiring itself.

def test_state_is_off_by_default_and_changes_nothing(part_src):
    """No --state must mean byte-for-byte the previous behaviour."""
    from semlayer.pipeline import infer_with_report

    doc, rep = infer_with_report(part_src, llm=None)
    assert "partitions" not in rep, "no state, no partition-wise profiling"
    names = {t["name"] for t in doc["semantic_layer"]["tables"]}
    assert "evt_2024_01" in names, "partitions are profiled as ordinary tables"


def test_with_state_the_parent_is_profiled_and_children_are_not(part_src, tmp_path):
    """Otherwise the same rows are profiled twice and the layer gains 120
    monthly 'tables' nobody models against."""
    from semlayer.incremental import LocalStateStore
    from semlayer.pipeline import infer_with_report

    doc, rep = infer_with_report(part_src, llm=None,
                                 state=LocalStateStore(tmp_path / "s.json"))
    names = {t["name"] for t in doc["semantic_layer"]["tables"]}
    assert "evt" in names
    assert not [n for n in names if "2024" in n], "children must be absorbed"
    assert rep["partitions"] == {"profiled": 3, "reused": 0}


def test_a_second_run_reads_no_partitions(part_src, tmp_path):
    """The acceptance test for the whole feature."""
    from semlayer.incremental import LocalStateStore
    from semlayer.pipeline import infer_with_report

    f = tmp_path / "s.json"
    s1 = LocalStateStore(f)
    infer_with_report(part_src, llm=None, state=s1)
    s1.flush()
    _, rep = infer_with_report(part_src, llm=None, state=LocalStateStore(f))
    assert rep["partitions"] == {"profiled": 0, "reused": 3}


def test_the_run_report_says_what_was_skipped(part_src, tmp_path):
    """A user who passes --state must be able to see whether it helped."""
    from semlayer.incremental import LocalStateStore
    from semlayer.pipeline import infer_with_report

    _, rep = infer_with_report(part_src, llm=None,
                               state=LocalStateStore(tmp_path / "s.json"))
    assert set(rep["partitions"]) == {"profiled", "reused"}


def test_cardinality_in_the_document_reports_the_merged_method(part_src, tmp_path):
    """Postgres has no core HLL, so a key column honestly reads bounds_only."""
    from semlayer.incremental import LocalStateStore
    from semlayer.pipeline import infer_with_report

    doc, _ = infer_with_report(part_src, llm=None,
                               state=LocalStateStore(tmp_path / "s.json"))
    evt = next(t for t in doc["semantic_layer"]["tables"] if t["name"] == "evt")
    card = {c["name"]: c["cardinality"] for c in evt["columns"]}
    assert card["evt_id"]["method"] == "exact", "proven by a count, not estimated"
    assert card["evt_id"]["partitions_covered"] == 3
    for c in card.values():
        assert c["lower_bound"] <= c["distinct"] <= c["upper_bound"]


def test_source_identity_is_stable_and_carries_no_credentials(part_src):
    """It keys the state file, so two warehouses must not collide -- and the
    file is something a user may commit."""
    from semlayer.source import source_identity

    sid = source_identity(part_src)
    assert sid == source_identity(part_src), "stable across calls"
    assert sid.startswith("postgres:")
    assert "password" not in sid.lower()
    dsn = part_src.con.info.dsn
    for secret in ("postgres:@", "@"):
        if secret in dsn:
            break
    assert part_src.con.info.dbname in sid


def test_an_unidentifiable_source_falls_back_rather_than_crashing():
    from semlayer.source import source_identity

    class Mute:
        pass

    class Angry:
        def source_id(self):
            raise RuntimeError("no")

    assert source_identity(Mute()) == "Mute"
    assert source_identity(Angry()) == "Angry"


# --- exact uniqueness, scoped to the candidates ---------------------------
#
# The last A1 piece. On an engine with mergeable sketches this is an
# optimisation; on PostgreSQL, which has no core HLL, bounds never narrow and
# `--state` would silently lose every primary key without it.

def test_only_locally_distinct_columns_are_nominated():
    """The free, sound filter: a global key must be unique in EVERY partition.

    A column that repeats inside any one partition is eliminated with no query
    at all -- a necessary condition, so it can never discard a real key.
    """
    src, logical, parts = _duck_partitioned()
    res = _run(src, logical, parts)
    # asserted through the public surface: by the time `uniqueness_candidates`
    # could be called again, resolution has already made those bounds exact.
    assert res.resolved_exactly == ["evt_id"]
    assert "cust_id" not in res.resolved_exactly, "repeats within a partition"
    assert "part_cd" not in res.resolved_exactly, "already exact; nothing to learn"


def test_a_real_key_is_proven_and_becomes_exact(part_src, tmp_path):
    """Postgres: bounds alone read a genuine key as [10,000..60,000]."""
    from semlayer.incremental import LocalStateStore
    from semlayer.pipeline import infer_with_report

    doc, _ = infer_with_report(part_src, llm=None,
                               state=LocalStateStore(tmp_path / "s.json"))
    evt = next(t for t in doc["semantic_layer"]["tables"] if t["name"] == "evt")
    card = {c["name"]: c["cardinality"] for c in evt["columns"]}
    assert card["evt_id"]["method"] == "exact"
    assert card["evt_id"]["lower_bound"] == card["evt_id"]["upper_bound"]
    assert evt["primary_key"] == ["evt_id"], "the whole point: keys work again"
    # and the eliminated columns were never queried
    assert card["cust_id"]["method"] == "bounds_only"


def test_overlapping_partitions_are_correctly_DISPROVED():
    """The case merging cannot resolve, and the reason bounds are not enough.

    Each partition is internally unique, so the free filter nominates the
    column -- but the partitions overlap, so it is not a global key. Only an
    exact count can tell these apart, and getting it wrong would assert a false
    primary key.
    """
    import duckdb

    from semlayer.incremental import NullStateStore, profile_partitioned
    from semlayer.source import DuckDBSource, Partition

    con = duckdb.connect()
    for m in range(3):
        # each table holds 0..999 -- internally unique, wholly overlapping
        con.execute(f"CREATE TABLE ov_{m} AS SELECT range AS k FROM range(1000)")

    class P(DuckDBSource):
        def list_partitions(self):
            return {("main", "ov"): tuple(
                Partition("main", f"ov_{m}", "main", "ov", change_token=f"t{m}")
                for m in range(3))}

    src = P(con)
    cols = next(t for t in src.list_tables() if t.name == "ov_0").columns
    res = profile_partitioned(src, TableMeta("main", "ov", cols),
                              src.list_partitions()[("main", "ov")],
                              NullStateStore(), "t")
    assert res.stats.row_count == 3000
    assert "k" in res.resolved_exactly, "it was nominated and then checked"
    assert res.stats.columns["k"].n_distinct == 1000
    assert res.stats.columns["k"].is_unique is False, "3,000 rows, 1,000 values"
    assert res.bounds["k"].exact
    con.close()


def test_the_number_of_exact_checks_is_capped():
    """Scoping the work must not become doing more of it."""
    from semlayer.incremental import MAX_UNIQUENESS_CHECKS, uniqueness_candidates

    src, logical, parts = _duck_partitioned()
    res = _run(src, logical, parts)
    per = [__import__("semlayer.profile.stats", fromlist=["x"]).profile_table(
        src, TableMeta(p.schema, p.name, logical.columns)) for p in parts]
    assert len(uniqueness_candidates(per, res)) <= MAX_UNIQUENESS_CHECKS


def test_a_failing_uniqueness_check_degrades_rather_than_aborting():
    from semlayer.incremental import DistinctBounds, IncrementalResult, resolve_uniqueness
    from semlayer.profile.stats import ColumnStats

    class Angry:
        def query(self, sql):
            raise RuntimeError("statement timeout")
        def qualify(self, s, n):
            return f"{s}.{n}"

    cs = ColumnStats(name="k", sql_type="INTEGER", row_count=10, n_null=0,
                     n_distinct=10, is_unique=True)
    res = IncrementalResult(stats=TableStats(TableMeta("s", "t", ()), 10, {"k": cs}),
                            bounds={"k": DistinctBounds(5, 10)},
                            profiled=["p"], reused=[])
    got = resolve_uniqueness(Angry(), (Partition("s", "p", "s", "t"),), res,
                             [TableStats(TableMeta("s", "p", ()), 10, {"k": cs})])
    assert got == [], "no claim is better than a wrong one"
    assert res.bounds["k"] == DistinctBounds(5, 10), "bounds left as merged"


# --- the scale line -------------------------------------------------------

def test_scale_warning_fires_only_past_the_tested_line():
    """A warning, never a refusal: a user who knows their warehouse may be
    right to proceed. What we must not do is hand them a silently-truncated
    layer and let them believe it is complete."""
    from semlayer.profile.run import SCALE_COLUMNS, SCALE_TABLES, _scale_warning

    assert _scale_warning(SCALE_TABLES, SCALE_COLUMNS) is None, "at the line is fine"
    assert _scale_warning(10, 10) is None
    over_t = _scale_warning(SCALE_TABLES + 1, 10)
    over_c = _scale_warning(10, SCALE_COLUMNS + 1)
    assert over_t and "tables" in over_t and "--schema" in over_t and "stitch" in over_t
    assert over_c and "columns" in over_c
    both = _scale_warning(SCALE_TABLES * 3, SCALE_COLUMNS * 3)
    assert "tables" in both and "columns" in both


def test_the_scale_warning_never_enters_the_document(part_src, tmp_path):
    """It lives in the run report. The document root is additionalProperties:
    false, so writing it into the doc broke spec conformance for 11 tests."""
    from semlayer.pipeline import infer_with_report
    from semlayer.validate import validate_document

    doc, rep = infer_with_report(part_src, llm=None)
    assert not [k for k in doc if k.startswith("_")], "no private keys survive"
    assert validate_document(doc).ok
    assert "scale_warning" not in rep, "this warehouse is small"
    assert rep["counts"]["tables"] and rep["counts"]["columns"]
