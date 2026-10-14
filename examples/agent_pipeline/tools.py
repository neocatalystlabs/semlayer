"""The two worlds an agent can be given: a bare warehouse, or one with a layer.

`run_sql` and `list_schema` belong to the warehouse and are present in both
modes -- that is the agent's connection, not something semlayer provides.
Layer mode adds the semlayer tools, served over real MCP stdio by the same
`semlayer mcp` command a user would run.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import duckdb

MAX_ROWS = 20


class Warehouse:
    """Read-only DuckDB connection plus the two tools every agent gets."""

    def __init__(self, db: Path):
        self.con = duckdb.connect(str(db), read_only=True)

    def list_schema(self) -> str:
        """Every table and column, the way an agent sees a warehouse today."""
        rows = self.con.sql(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema='main' ORDER BY table_name, ordinal_position"
        ).fetchall()
        by_table: dict[str, list[str]] = {}
        for table, col, dtype in rows:
            by_table.setdefault(table, []).append(f"{col} {dtype}")
        return "\n".join(f"{t}({', '.join(c)})" for t, c in by_table.items())

    def run_sql(self, sql: str) -> str:
        """Execute read-only SQL and return rows as JSON, or the error verbatim."""
        if not sql.strip():
            return "ERROR: no SQL supplied. Pass it as {\"sql\": \"SELECT ...\"}"
        try:
            cur = self.con.sql(sql)
            cols = cur.columns
            rows = cur.fetchmany(MAX_ROWS)
        except Exception as e:  # the agent must see its own SQL errors
            return f"ERROR: {type(e).__name__}: {e}"
        body = [dict(zip(cols, (float(v) if hasattr(v, "quantize") else v for v in r),
                         strict=False)) for r in rows]
        return json.dumps(body, default=str)

    def close(self) -> None:
        """Release the warehouse connection."""
        self.con.close()


def _signature(tool) -> str:
    """Render an MCP tool as one line of name, arguments, and purpose."""
    props = (tool.inputSchema or {}).get("properties", {})
    required = set((tool.inputSchema or {}).get("required", []))
    args = ", ".join(f'"{k}": <{v.get("type", "any")}>'
                     + ("" if k in required else " (optional)") for k, v in props.items())
    doc = " ".join((tool.description or "").split())
    return f"{tool.name} {{{args}}} -- {doc}"


class McpTools:
    """The semlayer MCP server, over stdio, in a background event loop.

    The agents are synchronous, the MCP client is async, and the server is a
    subprocess whose session must outlive each individual call -- so the loop
    runs on its own thread and calls are submitted to it.
    """

    def __init__(self, layer: Path, python: str):
        self.layer, self.python = layer, python
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self._ready = threading.Event()
        self._stop = asyncio.Event()
        self.session = None
        self.names: list[str] = []
        self.signatures: list[str] = []
        asyncio.run_coroutine_threadsafe(self._serve(), self.loop)
        if not self._ready.wait(timeout=60):
            raise RuntimeError("semlayer mcp server did not start")

    async def _serve(self) -> None:
        """Hold the stdio session open until close() is called."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=self.python, args=["-m", "semlayer.cli", "mcp", str(self.layer)])
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            self.names = [t.name for t in tools]
            self.signatures = [_signature(t) for t in tools]
            self.session = session
            self._ready.set()
            await self._stop.wait()

    def call(self, tool: str, args: dict) -> str:
        """Invoke one MCP tool on the server thread and wait for its text result."""
        async def _go() -> str:
            res = await self.session.call_tool(tool, args)
            return "\n".join(c.text for c in res.content if getattr(c, "type", "") == "text")

        return asyncio.run_coroutine_threadsafe(_go(), self.loop).result(timeout=120)

    def close(self) -> None:
        """Shut the session down and reap the server subprocess."""
        self.loop.call_soon_threadsafe(self._stop.set)
        self.thread.join(timeout=10)
        self.loop.call_soon_threadsafe(self.loop.stop)


_SQL_KEYS = ("sql", "query", "statement")

_WAREHOUSE_SIGS = [
    'list_schema {} -- every table and column in the warehouse',
    'run_sql {"sql": "SELECT ..."} -- execute read-only SQL, returns up to '
    f'{MAX_ROWS} rows as JSON',
]


def _sql_arg(args: dict) -> str:
    """Accept whichever key the model reached for. Only the SQL matters."""
    for k in _SQL_KEYS:
        if isinstance(args.get(k), str):
            return args[k]
    return ""


def raw_tools(wh: Warehouse):
    """What an agent has on a warehouse today: the schema and a SQL connection."""

    def dispatch(tool: str, args: dict) -> str:
        if tool == "list_schema":
            return wh.list_schema()
        if tool == "run_sql":
            return wh.run_sql(_sql_arg(args))
        return f"ERROR: no such tool {tool!r}. Available: list_schema, run_sql"

    return dispatch, list(_WAREHOUSE_SIGS)


def layer_tools(wh: Warehouse, mcp: McpTools):
    """The same warehouse, plus every tool the semlayer MCP server exposes."""

    def dispatch(tool: str, args: dict) -> str:
        if tool == "run_sql":
            return wh.run_sql(_sql_arg(args))
        if tool == "list_schema":
            return wh.list_schema()
        if tool in mcp.names:
            return mcp.call(tool, args)
        return (f"ERROR: no such tool {tool!r}. "
                f"Available: run_sql, list_schema, {', '.join(mcp.names)}")

    return dispatch, [*_WAREHOUSE_SIGS, *mcp.signatures]
