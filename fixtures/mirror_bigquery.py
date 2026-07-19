#!/usr/bin/env python3
"""Mirror DuckDB fixtures into BigQuery (admin persona).

Usage: .venv/bin/python oss/fixtures/mirror_bigquery.py fan_trap [--all]
Each fixture becomes a dataset (multi_tenant schemas get mt_ prefixes).
Load path: DuckDB -> parquet -> load_table_from_file. Idempotent.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(FIXTURES_DIR))


def mirror_fixture(client, project: str, name: str) -> None:
    import duckdb
    import importlib
    from google.cloud import bigquery

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
        ds_id = name if duck_schema == "main" else (
            f"mt_{duck_schema}" if name == "multi_tenant" else f"{name}_{duck_schema}"
        )
        print(f"  dataset {ds_id}")
        client.delete_dataset(f"{project}.{ds_id}", delete_contents=True, not_found_ok=True)
        client.create_dataset(f"{project}.{ds_id}")
        tables = duck.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = ?",
            [duck_schema],
        ).fetchall()
        for (table,) in tables:
            with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
                pq = Path(tmp.name)
            try:
                duck.execute(f'COPY "{duck_schema}"."{table}" TO \'{pq}\' (FORMAT parquet)')
                job_config = bigquery.LoadJobConfig(source_format=bigquery.SourceFormat.PARQUET)
                with open(pq, "rb") as f:
                    job = client.load_table_from_file(f, f"{project}.{ds_id}.{table}", job_config=job_config)
                job.result()
                n = client.get_table(f"{project}.{ds_id}.{table}").num_rows
                print(f"    {table}: {n} rows")
            finally:
                pq.unlink(missing_ok=True)
    duck.close()


def main(argv: list[str]) -> int:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

    from semlayer.connectors.bigquery import BigQuerySource
    from build import GENERATORS

    names = GENERATORS if argv == ["--all"] else argv
    if not names:
        print(__doc__)
        return 1
    src = BigQuerySource.from_env(role="admin")
    try:
        for name in names:
            print(f"mirroring {name} -> BigQuery")
            mirror_fixture(src.client, src.project, name)
    finally:
        src.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
