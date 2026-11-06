# Changelog

## 0.1.0

First release.

- SQL diagnostics from `semlayer lint`, on open, on save and while you type.
- Sidebar panel with three states: no engine, no layer, and a resting state
  once a layer exists.
- One-click MCP client configuration for Claude Code, Cline, Cursor and
  Claude Desktop.
- Status bar item naming the layer being linted against.

Known gaps, both in the engine rather than here:

- Findings carry no source offset, so diagnostic ranges are reconstructed by
  locating the statement and then the column or table identifier inside it.
  They are close, not exact.
- There is no `semlayer export` subcommand yet, so exporting to dbt is
  available only to an agent over MCP. The command is absent rather than
  present and broken.
