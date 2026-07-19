"""BigQuery connector: MetadataProvider + QueryExecutor over one project.

Reader persona = roles/bigquery.dataViewer + jobUser (the minimal grant
`semlayer init` documents). Datasets play the role of schemas.
"""

from __future__ import annotations

import os

from semlayer.source import ColumnMeta, TableMeta


class BigQuerySource:
    """MetadataProvider + QueryExecutor over one BigQuery project (datasets = schemas)."""

    def __init__(self, client, project: str):
        self.client = client
        self.project = project

    @classmethod
    def from_env(cls, role: str = "reader") -> BigQuerySource:
        """Connect using .env credentials; role 'reader' (product) or 'admin' (fixtures)."""
        from google.cloud import bigquery
        from google.oauth2 import service_account

        key = ("GOOGLE_APPLICATION_CREDENTIALS_READER" if role == "reader"
               else "GOOGLE_APPLICATION_CREDENTIALS_ADMIN")
        creds = service_account.Credentials.from_service_account_file(os.environ[key])
        project = os.environ["GCP_PROJECT"]
        return cls(bigquery.Client(project=project, credentials=creds), project)

    def list_tables(self) -> list[TableMeta]:
        """Enumerate all datasets' tables/columns via INFORMATION_SCHEMA."""
        tables: dict[tuple[str, str], list[ColumnMeta]] = {}
        for ds in self.client.list_datasets():
            ds_id = ds.dataset_id
            rows = self.query(
                f"""
                SELECT table_name, column_name, data_type
                FROM `{self.project}.{ds_id}`.INFORMATION_SCHEMA.COLUMNS
                ORDER BY table_name, ordinal_position
                """
            )
            for table, col, dtype in rows:
                tables.setdefault((ds_id, table), []).append(ColumnMeta(col, dtype))
        return [
            TableMeta(schema, table, tuple(cols))
            for (schema, table), cols in tables.items()
        ]

    def qualify(self, schema: str, table: str) -> str:
        """BigQuery identifiers use backticks with project qualification."""
        return f"`{self.project}.{schema}.{table}`"

    def quote_ident(self, name: str) -> str:
        """Backtick-quote one identifier."""
        return f"`{name}`"

    def query(self, sql: str) -> list[tuple]:
        """Run sql and return plain row tuples."""
        return [tuple(row.values()) for row in self.client.query(sql).result()]

    def close(self) -> None:
        """Release the underlying client."""
        self.client.close()
