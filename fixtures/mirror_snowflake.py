#!/usr/bin/env python3
"""Mirror DuckDB fixtures into the Snowflake test account (admin persona).

Usage (from repo root, with .env populated):
    .venv/bin/python oss/fixtures/mirror_snowflake.py fan_trap messy_mart
    .venv/bin/python oss/fixtures/mirror_snowflake.py --all

Each fixture becomes a schema in SEMLAYER_TEST (multi_tenant's 20 schemas get
a MT_ prefix). Load path: export tables to gzipped CSV -> PUT to table stage
-> COPY INTO. Idempotent: drops and recreates the fixture schema.
"""

from __future__ import annotations

import gzip
import sys
import tempfile
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(FIXTURES_DIR))

# duckdb type -> snowflake type (pass-through for most)
TYPE_MAP = {
    "INTEGER": "INTEGER", "BIGINT": "BIGINT", "HUGEINT": "BIGINT",
    "VARCHAR": "VARCHAR", "DATE": "DATE", "TIMESTAMP": "TIMESTAMP_NTZ",
    "BOOLEAN": "BOOLEAN", "DOUBLE": "DOUBLE", "FLOAT": "FLOAT",
}


def _sf_type(duck_type: str) -> str:
    up = duck_type.upper()
    if up.startswith("DECIMAL") or up.startswith("NUMERIC"):
        return up
    return TYPE_MAP.get(up, "VARCHAR")


def mirror_fixture(sf, name: str) -> None:
    import duckdb

    from generators import __name__ as _  # noqa: F401  (path check)
    import importlib
    mod = importlib.import_module(f"generators.{name}")
    duck = duckdb.connect(":memory:")
    mod.build(duck)

    schemas = [
        r[0] for r in duck.execute(
            "SELECT DISTINCT table_schema FROM information_schema.tables "
            "WHERE table_schema NOT IN ('information_schema','pg_catalog')"
        ).fetchall()
    ]
    for duck_schema in schemas:
        sf_schema = name.upper() if duck_schema == "main" else f"MT_{duck_schema.upper()}" if name == "multi_tenant" else f"{name}_{duck_schema}".upper()
        print(f"  schema {sf_schema}")
        sf.query(f'DROP SCHEMA IF EXISTS "{sf_schema}" CASCADE')
        sf.query(f'CREATE SCHEMA "{sf_schema}"')
        tables = duck.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = ?",
            [duck_schema],
        ).fetchall()
        for (table,) in tables:
            cols = duck.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
                [duck_schema, table],
            ).fetchall()
            col_ddl = ", ".join(f'"{c.upper()}" {_sf_type(t)}' for c, t in cols)
            fq = f'"{sf_schema}"."{table.upper()}"'
            sf.query(f"CREATE TABLE {fq} ({col_ddl})")

            with tempfile.NamedTemporaryFile(suffix=".csv.gz", delete=False) as tmp:
                path = Path(tmp.name)
            try:
                rows = duck.execute(f'COPY "{duck_schema}"."{table}" TO \'{path.with_suffix("")}\' (FORMAT csv, HEADER false)')  # noqa: E501
                # gzip for upload speed
                raw = path.with_suffix("").read_bytes()
                with gzip.open(path, "wb") as gz:
                    gz.write(raw)
                path.with_suffix("").unlink()
                stage = f'@"{sf_schema}".%"{table.upper()}"'
                sf.query(f"PUT 'file://{path}' {stage} AUTO_COMPRESS=FALSE")
                sf.query(
                    f"COPY INTO {fq} FROM {stage} "
                    f"FILE_FORMAT=(TYPE=CSV FIELD_OPTIONALLY_ENCLOSED_BY='\"' NULL_IF=('') EMPTY_FIELD_AS_NULL=TRUE COMPRESSION=GZIP)"
                )
                n = sf.query(f"SELECT count(*) FROM {fq}")[0][0]
                print(f"    {table}: {n} rows")
            finally:
                path.unlink(missing_ok=True)
    duck.close()


def main(argv: list[str]) -> int:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

    from semlayer.connectors.snowflake import SnowflakeSource
    from build import GENERATORS

    names = GENERATORS if argv == ["--all"] else argv
    if not names:
        print(__doc__)
        return 1
    sf = SnowflakeSource.from_env(role="admin")
    try:
        for name in names:
            print(f"mirroring {name} -> Snowflake")
            mirror_fixture(sf, name)
    finally:
        sf.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
