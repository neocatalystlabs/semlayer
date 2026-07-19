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

    @classmethod
    def from_env(cls, role: str = "reader") -> SnowflakeSource:
        """role: 'reader' (product persona) or 'admin' (fixture loading only)."""
        import snowflake.connector

        prefix = "SNOWFLAKE_READER" if role == "reader" else "SNOWFLAKE_ADMIN"
        con = snowflake.connector.connect(
            account=os.environ["SNOWFLAKE_ACCOUNT"],
            user=os.environ[f"{prefix}_USER"],
            password=os.environ[f"{prefix}_PASSWORD"],
            warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "SEMLAYER_WH"),
            database=os.environ.get("SNOWFLAKE_DATABASE", "SEMLAYER_TEST"),
        )
        return cls(con, os.environ.get("SNOWFLAKE_DATABASE", "SEMLAYER_TEST"))

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
