"""Source abstraction: MetadataProvider + QueryExecutor.

The split is deliberate (PRD M1): a catalog-native metadata provider (Iceberg
REST Catalog, Glue) can later pair with any executor (customer engine or
embedded DuckDB) without refactoring. Warehouse connectors implement both.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ColumnMeta:
    """A single column's name and warehouse-reported SQL type."""

    name: str
    sql_type: str


@dataclass(frozen=True)
class TableMeta:
    """A table's schema, name, and columns as reported by a metadata provider."""

    schema: str
    name: str
    columns: tuple[ColumnMeta, ...]

    @property
    def qualified(self) -> str:
        """Schema-qualified name, e.g. 'analytics.orders'."""
        return f"{self.schema}.{self.name}"


@runtime_checkable
class MetadataProvider(Protocol):
    """Read-only catalog access: enumerate tables and columns, no data."""

    def list_tables(self) -> list[TableMeta]:
        """Return all tables visible to the connection, outside internal schemas."""
        ...


@runtime_checkable
class QueryExecutor(Protocol):
    """Read-only SQL execution against the warehouse, for profiling/sampling."""

    def query(self, sql: str) -> list[tuple]:
        """Run `sql` and return the result rows."""
        ...


class DuckDBSource:
    """MetadataProvider + QueryExecutor over a DuckDB connection.

    The fixture connector — proves the interface split (M1 exit criterion)
    and is the CI test bed for every pipeline stage.
    """

    INTERNAL_SCHEMAS = ("information_schema", "pg_catalog")

    def __init__(self, con):
        self.con = con

    def list_tables(self) -> list[TableMeta]:
        """Enumerate tables + columns via information_schema, skipping internal schemas."""
        rows = self.con.execute(
            """
            SELECT table_schema, table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema NOT IN ('information_schema', 'pg_catalog')
            ORDER BY table_schema, table_name, ordinal_position
            """
        ).fetchall()
        tables: dict[tuple[str, str], list[ColumnMeta]] = {}
        for schema, table, col, dtype in rows:
            tables.setdefault((schema, table), []).append(ColumnMeta(col, dtype))
        return [
            TableMeta(schema, table, tuple(cols))
            for (schema, table), cols in tables.items()
        ]

    def query(self, sql: str) -> list[tuple]:
        """Execute `sql` on the underlying DuckDB connection and fetch all rows."""
        return self.con.execute(sql).fetchall()
