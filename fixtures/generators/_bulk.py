"""Fast bulk insert for fixture generators.

DuckDB's executemany is ~2ms/row (row-by-row prepared statements); CSV + COPY
is ~1000x faster. All generators MUST use bulk_insert for row loads.

None -> NULL (empty CSV field), dates/datetimes serialize as ISO strings,
which DuckDB's CSV reader parses against the declared column types.
"""

from __future__ import annotations

import csv
import os
import tempfile


def bulk_insert(con, table: str, rows: list[tuple]) -> None:
    if not rows:
        return
    fd, path = tempfile.mkstemp(suffix=".csv")
    try:
        with os.fdopen(fd, "w", newline="") as f:
            csv.writer(f).writerows(rows)
        con.execute(f"COPY {table} FROM '{path}' (HEADER false, NULLSTR '')")
    finally:
        os.unlink(path)


def bulk_insert_sql(con, insert_sql: str, rows: list[tuple]) -> None:
    """Drop-in replacement for con.executemany(insert_sql, rows): parses the
    table name out of 'INSERT INTO <table> VALUES ...' and COPYs."""
    m = insert_sql.split()
    assert m[0].upper() == "INSERT" and m[1].upper() == "INTO", insert_sql
    bulk_insert(con, m[2], rows)
