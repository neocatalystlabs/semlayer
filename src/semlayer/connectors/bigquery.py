# SPDX-License-Identifier: FSL-1.1-ALv2
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
    def from_env(cls, persona: str = "reader") -> BigQuerySource:
        """Connect using .env credentials; persona 'reader' (product) or 'admin'.

        Called `persona` rather than `role` because on Snowflake `role` now
        means the SESSION role actually in effect, which is a different thing:
        this only selects which credential set to read. GCP has no session-role
        equivalent -- the service account IS the principal -- so the IAM
        bindings on that account are what the minimal-grant claim rests on.
        """
        from google.cloud import bigquery

        key = ("GOOGLE_APPLICATION_CREDENTIALS_READER" if persona == "reader"
               else "GOOGLE_APPLICATION_CREDENTIALS_ADMIN")
        path = os.environ.get(key)
        if path:
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_file(path)
        else:
            # Application Default Credentials: `gcloud auth application-default
            # login`, a workload identity, or GOOGLE_APPLICATION_CREDENTIALS.
            #
            # Without this a service-account KEY FILE was mandatory, so someone
            # who already had gcloud set up could not run the CLI at all -- and
            # issuing a key file is exactly what a security team will push back
            # on. ADC also means the inference runs as the PERSON, inheriting
            # their own grants, which is what most first runs want.
            import google.auth

            from semlayer.errors import SemlayerError
            try:
                creds, adc_project = google.auth.default(
                    scopes=["https://www.googleapis.com/auth/cloud-platform"])
            except Exception:
                raise SemlayerError(
                    f"no BigQuery credentials: set {key} to a service-account "
                    "key file, or run `gcloud auth application-default login`",
                    hint="`semlayer init bigquery` prints the minimal grant") from None
            os.environ.setdefault("GCP_PROJECT", adc_project or "")
        project = os.environ.get("GCP_PROJECT") or ""
        if not project:
            from semlayer.errors import SemlayerError
            raise SemlayerError(
                "no BigQuery project: export GCP_PROJECT",
                hint="ADC did not carry a quota project; set GCP_PROJECT explicitly")
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
