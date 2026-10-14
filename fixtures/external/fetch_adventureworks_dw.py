"""AdventureWorksDW: Microsoft's sample data warehouse, as a typed DuckDB file.

Track P. MIT licence, read from the samples repo's own licence file.

Why this one first: it is the only verified-permissive, genuinely non-retail
warehouse that also carries shapes our own fixtures lack -- `DimDate`
referenced from several columns of one fact (role-playing), `FiscalYear` and
`FiscalQuarter` on that date dimension, a product category hierarchy, and
self-referencing employee and account trees. The vocabulary is manufacturing,
sales and finance rather than retail/orders.

The install archive holds one pipe-delimited file per table plus a T-SQL
loader. The loader is PARSED, never executed: we take column names and types
from its CREATE TABLE blocks, and its declared PRIMARY KEY and FOREIGN KEY
constraints become the relationship answer key.

**The constraints are deliberately NOT applied to the database.** Link has to
discover the keys from the data, exactly as it would on a warehouse where
nobody declared them -- which is the realistic case and the one we claim to
handle. The declared constraints are written to `declared_keys.json` beside the
database, as gold authored by Microsoft rather than by us.

    python fixtures/external/fetch_adventureworks_dw.py
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _fetch import banner, download, extract, require_duckdb, summarise

NAME = "adventureworks_dw"
TRACK = "P"
LICENCE = "MIT"
SOURCE = "https://github.com/microsoft/sql-server-samples"
ARCHIVE = ("https://github.com/microsoft/sql-server-samples/releases/download/"
           "adventureworks/AdventureWorksDW-data-warehouse-install-script.zip")
SHA256 = "73c27309d17cd30bf5351665401106abf649d9f9a9ecb0c770682f6be965aba8"

# T-SQL -> DuckDB. `money` is the one that matters: as VARCHAR it would read as
# a code rather than a measure, and every aggregability signal would be wrong.
TYPES = {
    "int": "INTEGER", "smallint": "SMALLINT", "tinyint": "TINYINT",
    "bigint": "BIGINT", "bit": "BOOLEAN", "money": "DECIMAL(19,4)",
    "smallmoney": "DECIMAL(10,4)", "decimal": "DECIMAL(18,4)",
    "numeric": "DECIMAL(18,4)", "float": "DOUBLE", "real": "FLOAT",
    "date": "DATE", "datetime": "TIMESTAMP", "datetime2": "TIMESTAMP",
    "smalldatetime": "TIMESTAMP", "time": "TIME",
    "nvarchar": "VARCHAR", "varchar": "VARCHAR", "nchar": "VARCHAR",
    "char": "VARCHAR", "sysname": "VARCHAR", "text": "VARCHAR",
    "ntext": "VARCHAR", "uniqueidentifier": "VARCHAR", "xml": "VARCHAR",
    "varbinary": "BLOB", "binary": "BLOB", "image": "BLOB",
}

_CREATE = re.compile(
    r"CREATE TABLE \[dbo\]\.\[(\w+)\]\((.*?)\n\)", re.DOTALL | re.IGNORECASE)
_COLUMN = re.compile(r"^\s*\[(\w+)\]\s+\[(\w+)\]", re.MULTILINE)
_PK = re.compile(
    r"ALTER TABLE \[dbo\]\.\[(\w+)\][^;]*?PRIMARY KEY[^(]*\(\s*(.*?)\s*\)",
    re.DOTALL | re.IGNORECASE)
_FK = re.compile(
    r"ALTER TABLE \[dbo\]\.\[(\w+)\] ADD(.*?)(?=\nGO|\nALTER TABLE)",
    re.DOTALL | re.IGNORECASE)
_FK_ONE = re.compile(
    r"CONSTRAINT \[\w+\] FOREIGN KEY\s*\(\s*(.*?)\s*\)\s*"
    r"REFERENCES \[dbo\]\.\[(\w+)\]\s*\(\s*(.*?)\s*\)", re.DOTALL | re.IGNORECASE)


def parse_ddl(sql: str) -> tuple[dict[str, list[tuple[str, str]]], dict, list]:
    """Pull typed columns, declared PKs and declared FKs out of the loader."""
    tables = {}
    for table, body in _CREATE.findall(sql):
        cols = [(c, TYPES.get(t.lower(), "VARCHAR")) for c, t in _COLUMN.findall(body)]
        if cols:
            tables[table] = cols

    pks = {}
    for table, cols in _PK.findall(sql):
        names = re.findall(r"\[(\w+)\]", cols)
        if names and table in tables:
            pks[table] = names

    fks = []
    for table, block in _FK.findall(sql):
        for from_cols, to_table, to_cols in _FK_ONE.findall(block):
            fks.append({
                "from_table": table,
                "from_columns": re.findall(r"\[(\w+)\]", from_cols),
                "to_table": to_table,
                "to_columns": re.findall(r"\[(\w+)\]", to_cols),
            })
    return tables, pks, fks


def main() -> int:
    d = banner(NAME, TRACK, LICENCE, SOURCE)
    archive = download(ARCHIVE, d / "AdventureWorksDW.zip", SHA256)
    raw = extract(archive, d / "raw")

    loader = next(raw.rglob("instawdbdw.sql"), None)
    if loader is None:
        print("  loader script not found; archive layout changed")
        return 1
    tables, pks, fks = parse_ddl(loader.read_text(errors="replace"))
    print(f"  parsed DDL: {len(tables)} tables, {len(pks)} declared PKs, "
          f"{len(fks)} declared FKs")

    duckdb = require_duckdb()
    db = d / f"{NAME}.duckdb"
    if db.exists():
        db.unlink()
    con = duckdb.connect(str(db))
    loaded, skipped = 0, []
    try:
        for table, cols in sorted(tables.items()):
            csv_path = raw / f"{table}.csv"
            if not csv_path.exists():
                skipped.append(f"{table} (no data file)")
                continue
            ddl = ", ".join(f'"{c}" {t}' for c, t in cols)
            names = "{" + ", ".join(f"'{c}': '{t}'" for c, t in cols) + "}"
            con.sql(f'CREATE TABLE "{table}" ({ddl})')
            try:
                con.sql(
                    f'INSERT INTO "{table}" SELECT * FROM '
                    f"read_csv('{csv_path}', delim='|', header=false, "
                    f"columns={names}, ignore_errors=true, nullstr='')")
                loaded += 1
            except Exception as e:
                skipped.append(f"{table} ({type(e).__name__})")
                con.sql(f'DROP TABLE "{table}"')
    finally:
        con.close()

    for s in skipped:
        print(f"  skipped {s}")
    if not loaded:
        print(f"\n  NOTHING LOADED. Inspect {raw} and fix this script.")
        return 1

    keys = d / "declared_keys.json"
    keys.write_text(json.dumps(
        {"source": SOURCE, "licence": LICENCE,
         "note": "Declared in the vendor loader script. NOT applied to the "
                 "database: Link must discover these from the data. Use as the "
                 "relationship answer key.",
         "primary_keys": pks, "foreign_keys": fks}, indent=1))
    summarise(db)
    print(f"  answer key: {keys.name} "
          f"({len(pks)} PKs, {len(fks)} FKs, not enforced in the database)")
    print(f"\n  track {TRACK}: numbers may be published, attributing the "
          "MIT-licensed Microsoft SQL Server samples.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
