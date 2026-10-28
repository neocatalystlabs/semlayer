# SPDX-License-Identifier: FSL-1.1-ALv2
"""Snowflake connector: MetadataProvider + QueryExecutor over one database.

Runs under the customer's minimal-grant read-only service account (USAGE on
db/schemas/warehouse + SELECT on tables — exactly what `semlayer init`
generates). Anything requiring more is a documented opt-in, not an assumption.
"""

from __future__ import annotations

import os

from semlayer.source import ColumnMeta, TableMeta


class SnowflakeSource:
    """MetadataProvider + QueryExecutor over one Snowflake database."""

    def __init__(self, con, database: str):
        self.con = con
        self.database = database
        # recorded in the run report so the layer says which privileges
        # produced it; "unknown" when a caller built the source directly
        self.session_role: str = "unknown"

    @classmethod
    def from_env(cls, persona: str = "reader") -> SnowflakeSource:
        """persona: 'reader' (the product) or 'admin' (fixture loading only).

        The SESSION ROLE is passed explicitly and is not optional. Until now it
        was never passed at all, so the session ran under whatever the user's
        DEFAULT_ROLE happened to be. That worked only because `semlayer init`
        creates a service user with DEFAULT_ROLE set to the reader role it also
        creates -- and it silently broke the moment anyone ran semlayer as
        their own account, profiling under whatever privileges they hold. Our
        minimal-grant claim is about the role actually in effect, so inheriting
        one makes the claim unverifiable.
        """
        from semlayer.errors import SemlayerError

        # Config is validated BEFORE the driver import, deliberately. The
        # session role is a cheap environment check and the driver is an
        # optional extra, so importing first meant an install without
        # `[warehouses]` reported `No module named 'snowflake'` for what is
        # actually a missing setting -- and it made the test for this rule
        # unrunnable anywhere the extra is absent, which is CI.
        prefix = "SNOWFLAKE_READER" if persona == "reader" else "SNOWFLAKE_ADMIN"
        session_role = os.environ.get(f"{prefix}_ROLE") or os.environ.get("SNOWFLAKE_ROLE")
        if not session_role:
            raise SemlayerError(
                f"no Snowflake session role set: export {prefix}_ROLE "
                "(or SNOWFLAKE_ROLE)",
                hint="`semlayer init snowflake` generates a <prefix>_reader role; set "
                     "that name rather than relying on the user's DEFAULT_ROLE, which "
                     "silently profiles under whatever privileges the account holds",
            )

        import snowflake.connector

        # Password was the ONLY accepted credential, which locked out every
        # account on SSO -- the common case at any company large enough to have
        # a messy warehouse, and the exact person who wants to try the CLI on
        # their own grants. `authenticator` covers SSO (externalbrowser), Okta
        # (a URL), and OAuth; a private key covers key-pair auth, which is what
        # most service accounts are moving to.
        auth: dict = {}
        authenticator = os.environ.get(f"{prefix}_AUTHENTICATOR") or os.environ.get(
            "SNOWFLAKE_AUTHENTICATOR")
        key_path = os.environ.get(f"{prefix}_PRIVATE_KEY_PATH") or os.environ.get(
            "SNOWFLAKE_PRIVATE_KEY_PATH")
        password = os.environ.get(f"{prefix}_PASSWORD")
        if key_path:
            auth["private_key_file"] = key_path
            passphrase = os.environ.get(f"{prefix}_PRIVATE_KEY_PASSPHRASE") or \
                os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
            if passphrase:
                auth["private_key_file_pwd"] = passphrase
        elif authenticator:
            auth["authenticator"] = authenticator
            # externalbrowser prompts the user and takes no password; Okta and
            # oauth variants still need one, so pass it when it is set.
            if password and authenticator.lower() != "externalbrowser":
                auth["password"] = password
        elif password:
            auth["password"] = password
        else:
            raise SemlayerError(
                f"no Snowflake credential: set {prefix}_PASSWORD, "
                f"{prefix}_PRIVATE_KEY_PATH, or {prefix}_AUTHENTICATOR="
                "externalbrowser for SSO",
                hint="`semlayer init snowflake` prints the minimal grant; SSO and "
                     "key-pair let you run as yourself without a new password",
            )

        con = snowflake.connector.connect(
            account=os.environ["SNOWFLAKE_ACCOUNT"],
            user=os.environ[f"{prefix}_USER"],
            role=session_role,
            warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "SEMLAYER_WH"),
            database=os.environ.get("SNOWFLAKE_DATABASE", "SEMLAYER_TEST"),
            **auth,
        )
        src = cls(con, os.environ.get("SNOWFLAKE_DATABASE", "SEMLAYER_TEST"))
        src.session_role = session_role
        return src

    def list_comments(self) -> dict[tuple[str, str, str | None], str]:
        """Native COMMENT text from information_schema.

        Snowflake surfaces comments as a plain `comment` column on both
        information_schema.tables and information_schema.columns, so this
        needs no extra grants beyond the USAGE the reader role already has.

        NOT covered by the nightly live connector test, which has no commented
        objects to read -- `_native_comments` degrades to no comments if the
        query fails, so a wrong assumption here costs priors rather than a run.
        """
        out: dict[tuple[str, str, str | None], str] = {}
        for sql, has_col in (
            (f"""SELECT table_schema, table_name, NULL, comment
                 FROM {self.database}.information_schema.tables
                 WHERE comment IS NOT NULL
                   AND table_schema <> 'INFORMATION_SCHEMA'""", False),
            (f"""SELECT table_schema, table_name, column_name, comment
                 FROM {self.database}.information_schema.columns
                 WHERE comment IS NOT NULL
                   AND table_schema <> 'INFORMATION_SCHEMA'""", True),
        ):
            for schema, table, col, comment in self.query(sql):
                if comment:
                    out[(schema, table, col if has_col else None)] = comment
        return out

    def list_tables(self) -> list[TableMeta]:
        """Enumerate the database's tables/columns via information_schema."""
        rows = self.query(
            f"""
            SELECT table_schema, table_name, column_name, data_type
            FROM {self.database}.information_schema.columns
            WHERE table_schema NOT IN ('INFORMATION_SCHEMA')
            ORDER BY table_schema, table_name, ordinal_position
            """
        )
        tables: dict[tuple[str, str], list[ColumnMeta]] = {}
        for schema, table, col, dtype in rows:
            tables.setdefault((schema, table), []).append(ColumnMeta(col, dtype))
        return [
            TableMeta(schema, table, tuple(cols))
            for (schema, table), cols in tables.items()
        ]

    def query(self, sql: str) -> list[tuple]:
        """Run sql and return plain row tuples."""
        cur = self.con.cursor()
        try:
            cur.execute(sql)
            return cur.fetchall()
        finally:
            cur.close()

    def for_worker(self):
        """Per-worker executor: the shared connection.

        It may be used from several threads; `query` already opens a cursor
        per call.
        """
        return self

    def close(self) -> None:
        """Close the Snowflake connection."""
        self.con.close()

    def ddl_events_since(self, minutes: int = 120) -> list[tuple]:
        """DDL statements from ACCOUNT_USAGE.QUERY_HISTORY (drift watcher feed).

        OPT-IN grant required (IMPORTED PRIVILEGES ON DATABASE snowflake);
        latency is ~45min by design — the CI/CD hook is the fast path.
        """
        return self.query(f"""
            SELECT query_text, start_time
            FROM snowflake.account_usage.query_history
            WHERE query_type IN ('ALTER_TABLE_MODIFY_COLUMN','ALTER','ALTER_TABLE',
                                 'CREATE_TABLE','DROP','CREATE','RENAME_COLUMN',
                                 'ALTER_TABLE_ADD_COLUMN','ALTER_TABLE_DROP_COLUMN')
              AND start_time > DATEADD('minute', -{minutes}, CURRENT_TIMESTAMP())
            ORDER BY start_time DESC LIMIT 100
        """)
