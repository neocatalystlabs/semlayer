# SPDX-License-Identifier: FSL-1.1-ALv2
"""BigQuery connector: MetadataProvider + QueryExecutor over one project.

Reader persona = roles/bigquery.dataViewer + jobUser (the minimal grant
`semlayer init` documents). Datasets play the role of schemas.
"""

from __future__ import annotations

import os

from semlayer.source import ColumnMeta, TableMeta

_EXTRA_HINT = "pip install 'semlayer[warehouses]'"


def _driver():
    """Import the BigQuery client library, or say how to install it.

    One place, so a missing optional extra cannot reach the user as
    `No module named 'google.cloud'` -- which `cli._error_class` filed as
    `internal`, the class that is supposed to mean a defect in semlayer.
    """
    from semlayer.errors import SourceError
    try:
        from google.cloud import bigquery
    except ImportError as e:
        raise SourceError("the BigQuery driver is not installed",
                          hint=_EXTRA_HINT) from e
    return bigquery


def _service_account_creds(path: str, key: str):
    """Load a service-account key file, translating every way it can fail.

    This was unwrapped, so a path that did not exist, a file that was not
    JSON, or a key that had been revoked surfaced as whatever google.oauth2
    raised -- recorded as `internal` and reading like a crash in semlayer
    rather than a problem with a named file the user controls.
    """
    from semlayer.errors import SourceError
    try:
        from google.oauth2 import service_account
    except ImportError as e:
        raise SourceError("the BigQuery driver is not installed",
                          hint=_EXTRA_HINT) from e
    try:
        return service_account.Credentials.from_service_account_file(path)
    except Exception as e:
        raise SourceError(
            f"could not read the BigQuery key file at {path}",
            hint=f"{e} -- {key} names this file; unset it to use "
                 "`gcloud auth application-default login` instead") from e


def _adc_creds(key: str):
    """Application Default Credentials, or the advice for having none."""
    from semlayer.errors import SourceError
    try:
        import google.auth
    except ImportError as e:
        raise SourceError("the BigQuery driver is not installed",
                          hint=_EXTRA_HINT) from e
    try:
        return google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
    except Exception as e:
        # A SourceError, not a SemlayerError: both reach the user identically,
        # but only this one records as `connection`. Credential failures were
        # filed under `other`, so a BigQuery first run that failed to
        # authenticate was invisible in exactly the bucket we read to find out
        # whether anyone had tried.
        raise SourceError(
            f"no BigQuery credentials: set {key} to a service-account key "
            "file, or run `gcloud auth application-default login`",
            hint="`semlayer init bigquery` prints the minimal grant") from e


def _client(project: str, creds):
    """Build the client, translating a refusal into a connection error.

    `bigquery.Client()` validates the project and the credentials, so this is
    where a wrong project id or a revoked key actually surfaces -- and it was
    bare, so both read as our bug.
    """
    from semlayer.errors import SourceError
    bigquery = _driver()
    try:
        return bigquery.Client(project=project, credentials=creds)
    except Exception as e:
        raise SourceError(f"BigQuery refused the connection for project "
                          f"{project}", hint=str(e).strip()) from e


class BigQuerySource:
    """MetadataProvider + QueryExecutor over one BigQuery project (datasets = schemas)."""

    def __init__(self, client, project: str):
        self.client = client
        self.project = project

    @classmethod
    def from_uri(cls, uri: str, persona: str = "reader") -> BigQuerySource:
        """`bigquery` or `bigquery:<project>` -- the project, or the environment.

        Only the bare word was accepted, so `bigquery:my-project` -- the
        obvious spelling, and the one the Snowflake connector already takes
        for a connection name -- hit the unsupported-URI refusal, whose
        message then listed every connector except the one that had just been
        asked for. A project id is the only identifier BigQuery has, so there
        is no URL form to support beyond it.

        `bigquery://project` is accepted too: it is what anyone who just
        learned the `snowflake://` form will try.
        """
        rest = uri.split(":", 1)[1] if ":" in uri else ""
        return cls.from_env(persona=persona, project=rest.lstrip("/").strip() or None)

    @classmethod
    def from_env(cls, persona: str = "reader",
                 project: str | None = None) -> BigQuerySource:
        """Connect using .env credentials; persona 'reader' (product) or 'admin'.

        `project` overrides `GCP_PROJECT`, for the `bigquery:<project>` form.
        Passed in rather than exported into the environment from `from_uri`:
        a connector that edits the process environment as a side effect is a
        connector whose behaviour depends on the order things were opened in.

        Called `persona` rather than `role` because on Snowflake `role` now
        means the SESSION role actually in effect, which is a different thing:
        this only selects which credential set to read. GCP has no session-role
        equivalent -- the service account IS the principal -- so the IAM
        bindings on that account are what the minimal-grant claim rests on.

        Settings are validated BEFORE the driver is imported, the same rule
        the Snowflake connector follows: `[warehouses]` is an optional extra,
        so importing first meant a missing extra reported `No module named
        'google.cloud'` for what is usually a missing project or credential.
        """
        key = ("GOOGLE_APPLICATION_CREDENTIALS_READER" if persona == "reader"
               else "GOOGLE_APPLICATION_CREDENTIALS_ADMIN")
        adc_project = ""
        path = os.environ.get(key)
        if path:
            creds = _service_account_creds(path, key)
        else:
            # Application Default Credentials: `gcloud auth application-default
            # login`, a workload identity, or GOOGLE_APPLICATION_CREDENTIALS.
            #
            # Without this a service-account KEY FILE was mandatory, so someone
            # who already had gcloud set up could not run the CLI at all -- and
            # issuing a key file is exactly what a security team will push back
            # on. ADC also means the inference runs as the PERSON, inheriting
            # their own grants, which is what most first runs want.
            creds, adc_project = _adc_creds(key)
        # Precedence, highest first: the project named in the source, the
        # environment, then the quota project ADC happened to carry.
        project = project or os.environ.get("GCP_PROJECT") or adc_project or ""
        if not project:
            from semlayer.errors import SourceError
            raise SourceError(
                "no BigQuery project: export GCP_PROJECT or pass "
                "bigquery:<project>",
                hint="ADC did not carry a quota project; name it explicitly")
        return cls(_client(project, creds), project)

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

    def for_worker(self):
        """Per-worker executor: the BigQuery client is thread-safe; every worker may share it."""
        return self

    def close(self) -> None:
        """Release the underlying client."""
        self.client.close()
