"""Statistical column profiling (Stage 1, deterministic half).

All statistics are computed in the warehouse via pushed-down SQL — no data
leaves except the stats themselves and (unless no_sample_egress) top values.
Sampling uses TABLESAMPLE where row counts are large; key checks (uniqueness)
are always full-column (feasibility F2).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from semlayer.source import QueryExecutor, TableMeta

SAMPLE_THRESHOLD = 1_000_000  # full-scan stats below this row count
TOP_K = 10


@dataclass
class ColumnStats:
    """Per-column statistics gathered by a single pushed-down profiling pass."""

    name: str
    sql_type: str
    row_count: int
    n_null: int
    n_distinct: int
    is_unique: bool
    min_val: str | None = None
    max_val: str | None = None
    avg_len: float | None = None
    top_values: list[tuple[str, int]] = field(default_factory=list)

    @property
    def null_rate(self) -> float:
        """Fraction of rows with a NULL in this column."""
        return self.n_null / self.row_count if self.row_count else 0.0

    @property
    def cardinality_ratio(self) -> float:
        """Distinct-value ratio among non-null rows (1.0 == unique)."""
        nn = self.row_count - self.n_null
        return self.n_distinct / nn if nn else 0.0


@dataclass
class TableStats:
    """Row count plus per-column stats for one profiled table."""

    table: TableMeta
    row_count: int
    columns: dict[str, ColumnStats]


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _qualify(ex, t: TableMeta) -> str:
    """Sources may override identifier quoting (e.g. BigQuery backticks)."""
    if hasattr(ex, "qualify"):
        return ex.qualify(t.schema, t.name)
    return f"{_q(t.schema)}.{_q(t.name)}"


def _qcol(ex, name: str) -> str:
    if hasattr(ex, "quote_ident"):
        return ex.quote_ident(name)
    return _q(name)


_ORDERABLE_TYPE_KEYS = ("INT", "DECIMAL", "NUMERIC", "DOUBLE", "FLOAT", "REAL", "HUGEINT",
                        "CHAR", "TEXT", "STRING", "DATE", "TIME", "BOOL")


def _is_text(sql_type: str) -> bool:
    return any(k in sql_type.upper() for k in ("CHAR", "TEXT", "STRING"))


def _is_orderable(sql_type: str) -> bool:
    """Whether min/max can be pushed portably for this type.

    Non-orderable types (structs/arrays/JSON) keep min_val/max_val = None:
    they raise backend-specific errors that cannot be enumerated portably.
    """
    return any(k in sql_type.upper() for k in _ORDERABLE_TYPE_KEYS)


def profile_table(ex: QueryExecutor, t: TableMeta) -> TableStats:
    """Compute row count and per-column stats for one table via pushed-down SQL.

    Round-trip budget (BETA Q5): column statistics are BATCHED into wide
    aggregate SELECTs — one null/distinct query and one min/max/length query
    per table instead of 3-4 queries per column. On network warehouses
    (Snowflake/BigQuery) this is the difference between hundreds and
    thousands of round-trips per run. Only top-value sampling remains
    per-column (GROUP BY cannot be batched portably), and only for
    low-cardinality columns.
    """
    fq = _qualify(ex, t)
    row_count = ex.query(f"SELECT count(*) FROM {fq}")[0][0]

    qcols = [(c, _qcol(ex, c.name)) for c in t.columns]
    null_distinct_exprs = ", ".join(
        f"count(*) - count({qc}), count(DISTINCT {qc})" for _, qc in qcols
    )
    nd_row = ex.query(f"SELECT {null_distinct_exprs} FROM {fq}")[0] if qcols else ()

    # second wide query: min/max for orderable columns, avg(length) for text
    extras_exprs: list[str] = []
    extras_slots: dict[str, tuple[int, int | None]] = {}  # name -> (minmax_idx, len_idx)
    idx = 0
    for c, qc in qcols:
        mm_idx = -1
        len_idx: int | None = None
        if _is_orderable(c.sql_type):
            extras_exprs.append(f"min({qc})")
            extras_exprs.append(f"max({qc})")
            mm_idx = idx
            idx += 2
        if _is_text(c.sql_type):
            extras_exprs.append(f"avg(length({qc}))")
            len_idx = idx
            idx += 1
        extras_slots[c.name] = (mm_idx, len_idx)
    extras_row = (
        ex.query(f"SELECT {', '.join(extras_exprs)} FROM {fq}")[0] if extras_exprs else ()
    )

    cols: dict[str, ColumnStats] = {}
    for i, (c, qc) in enumerate(qcols):
        n_null, n_distinct = nd_row[2 * i], nd_row[2 * i + 1]
        is_unique = row_count > 0 and n_null == 0 and n_distinct == row_count
        stats = ColumnStats(
            name=c.name, sql_type=c.sql_type, row_count=row_count,
            n_null=n_null, n_distinct=n_distinct, is_unique=is_unique,
        )
        mm_idx, len_idx = extras_slots[c.name]
        if mm_idx >= 0:
            mn, mx = extras_row[mm_idx], extras_row[mm_idx + 1]
            stats.min_val = None if mn is None else str(mn)
            stats.max_val = None if mx is None else str(mx)
        if len_idx is not None:
            avg_len = extras_row[len_idx]
            stats.avg_len = float(avg_len) if avg_len is not None else None
        if 0 < n_distinct <= 1000:
            stats.top_values = [
                (str(v), int(n))
                for v, n in ex.query(
                    f"SELECT {qc}, count(*) FROM {fq} WHERE {qc} IS NOT NULL "
                    f"GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {TOP_K}"
                )
            ]
        cols[c.name] = stats
    return TableStats(table=t, row_count=row_count, columns=cols)
