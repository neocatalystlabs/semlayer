# The VS Code and Cursor extension

The CLI is where inference happens. The extension exists for the one thing a
terminal structurally cannot do: tell you a query is wrong *while you are
writing it*.

Source: [`extension/`](../extension). Install with
`code --install-extension semlayer-<version>.vsix`, or from either marketplace
once published (`cursor --install-extension` for Cursor).

## What it is, architecturally

A front end over the CLI, and nothing else:

| | |
|---|---|
| Runtime npm dependencies | **none** — the VS Code API and Node built-ins |
| Engine | **none** — it runs `semlayer <command> --json` as a subprocess |
| MCP | **not used**; MCP is how the *agent* reads the layer, this is how the *human* does |
| Network calls | **none** from the extension itself |
| Bundle | ~25 KB |

One consequence worth stating plainly: the extension can never disagree with
the CLI, because it has no second implementation to disagree with. A lint rule
added in `src/semlayer/lint.py` shows up in the editor with no extension change.

## Linting

`semlayer lint <layer> - --json --dialect <d>` over stdin, so unsaved buffers
work without temp files.

- **on open and on save** — every rule
- **while typing**, debounced 500 ms — every rule except `parse_error`

That single exclusion is what makes on-type viable: half-written SQL does not
parse, and `select sum(` would otherwise raise a finding on every keystroke.
Engine time is ~1.15 ms against a 36-table layer; process startup dominates at
roughly 390 ms, which is why the debounce exists at all.

Linting needs **no warehouse connection** — it is the document plus a parse —
so every dialect `sqlglot` knows works, including Redshift, Databricks, T-SQL,
Trino, ClickHouse, Oracle and MySQL, for which semlayer has no connector.

### Source positions are reconstructed, not reported

A finding carries `rule`, `severity`, `message`, `table`, `column` and `fix`,
and **no source offset**. The extension puts the position back in two steps,
in [`src/positions.ts`](../extension/src/positions.ts):

1. **Locate the statement.** The engine echoes the SQL it linted, so this is an
   exact search; a forward-only cursor keeps two identical statements on their
   own occurrences.
2. **Locate the identifier.** `table.column` is tried first and the span
   narrowed to the column half, then a bare column, then the table, then the
   statement's first line.

The qualified-first order is load-bearing. An earlier version excluded a
preceding dot from the identifier match, so `tot_amt` never matched inside
`ord_hdr.tot_amt` and every qualified column silently underlined its *table*
instead. [`test/positions.test.mjs`](../extension/test/positions.test.mjs)
pins it.

Attaching real offsets in `lint.py` would delete this entire mechanism. The
nodes already carry token positions; it is a small change nobody has made yet.

## Layer discovery

Any `*.yaml` in the workspace whose head contains `semantic_layer:` or
`spec_version:`. Nearest the root wins, the result is cached for 30 seconds,
and a file-system watcher invalidates it so editing a layer re-lints every open
SQL file. `semlayer.layerPath` overrides, and `semlayer: Select the active
layer` writes that setting for you.

## MCP client configuration

Claude Code, Cline, Cursor and Claude Desktop. The entry is **merged** into the
existing `mcpServers` object — every other server and every unrelated key is
preserved, because that file is the user's, not ours. Clients read their config
at startup, so they need restarting afterwards.

## What it deliberately does not do

- **No SQL input box.** The editor is already the best text editor available; a
  webview `<textarea>` would lose highlighting, multiple cursors and saving.
  SQL from a ticket goes into an untitled buffer.
- **No "lint now" command.** Opening the file is the trigger.
- **No paste-into-chat path.** It works, because the agent calls `check_sql`,
  but nothing in the UI suggests it.
- **No export.** There is no `semlayer export` subcommand yet; export exists
  only as the MCP tool `export_layer`. The command is absent rather than
  present and broken.

## Publishing

[`extension/PUBLISHING.md`](../extension/PUBLISHING.md).
