# MCP: give your agent the semantic layer

`semlayer mcp` serves an inferred semantic layer to any MCP client — Claude
Desktop, Claude Code, or anything else that speaks the protocol. Your agent
stops guessing at column names and starts asking.

## It never touches your warehouse — so how does a query run?

The server reads one file: the layer YAML you produced with `semlayer infer`.
It holds no credentials, opens no connection, and sees no rows.

**semlayer is the map, not the road.** It knows what your tables mean, which
filters are mandatory, and how the query should be written — and it hands back
SQL as text. Something else runs that SQL.

In practice you attach two things to the same assistant:

| | what it does | what it needs |
|---|---|---|
| **semlayer MCP** | meaning, rules, correct SQL | the layer file |
| **your warehouse's MCP server** (or SQL client, or BI tool) | executes the SQL | your warehouse credentials |

The execution path is one you already control and already govern. Your
credentials, your permissions, your warehouse still deciding who may see what —
we record access semantics, we never enforce them, and your data never passes
through us.

That split is deliberate. Inferring the layer needs warehouse access;
consuming it does not.

## Setup

```bash
semlayer infer duckdb:warehouse.duckdb -o layer.yaml   # once, per episode 2
semlayer mcp layer.yaml                                # a stdio server
```

`semlayer mcp` speaks stdio, so you do not run it yourself — your client does.
Point the client at the command.

### Claude Desktop

Edit the config file:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "semlayer": {
      "command": "semlayer",
      "args": ["mcp", "/absolute/path/to/layer.yaml"]
    }
  }
}
```

Use an **absolute path** to the layer — the client does not run from your shell's
working directory. Restart Claude Desktop; `semlayer` appears in the tools list.

If `semlayer` is not on the client's PATH (common with `pipx`, `uv`, or a venv),
give the full path to the executable instead — `which semlayer` will tell you.

### Claude Code

```bash
claude mcp add semlayer -- semlayer mcp /absolute/path/to/layer.yaml
```

## What your agent can ask

| tool | what it answers |
|---|---|
| `semantic_search` | "where is revenue?" — tables, columns and metrics by keyword. The starting point. |
| `get_domains` | the business areas in the warehouse |
| `get_tables` | what tables exist, their type, lifecycle, and `type_confidence` |
| `table_detail` | columns, types, foreign keys, **required filters**, confidence per claim |
| `get_metrics` | defined measures and how to aggregate them |
| `route_intent` | *which* table to use for an intent, when several could answer |
| `check_sql` | lint SQL against the layer: missing filters, fan-out, deprecated tables |
| `compile_metric` | generate correct SQL for a metric, with the filters already applied |

On connect, the server also hands the client standing instructions: always apply
required filters, never use objects flagged `UNUSABLE`, run `check_sql` on every
query before executing, and caveat answers built on `lifecycle: inferred`
elements.

## Reading the numbers

Inferred claims carry `confidence`, and the numbers are calibrated against gold
fixtures — see [calibration.md](calibration.md) for the measured curves and the
known gaps.

**`type_confidence` on a table is not a quality score.** It says how sure we are
of the *label* — `fact`, `dimension`, `staging`. A high score on a staging table
means "confidently staging", which is a table you should *not* query.

Which table to use is a different question, answered by two other fields:

- `lifecycle` — `certified` > `reviewed` > `inferred`, plus `deprecated` and
  `orphaned`. `get_tables` flags those `UNUSABLE` and points at a replacement.
- `route_intent` — the layer's routing knowledge, when several tables could
  plausibly answer.

## Keeping it current

The served file is a snapshot. When the warehouse changes, re-run `semlayer
drift` and apply the changes — `drift --apply` preserves the review decisions
that `infer` would overwrite. The client picks up the new file on restart.

## Troubleshooting

**The tools do not appear.** Almost always the client cannot find `semlayer` or
the layer path is relative. Use absolute paths for both.

**Every answer is caveated as inferred.** That is correct until someone reviews
the layer: `semlayer review layer.yaml --list` then promote what is right.
Lifecycle is how the layer tells an agent what a human has actually blessed.

**`check_sql` reports errors on a query that runs fine.** That is the point —
it lints for *semantic* validity, not syntax. A query that executes happily can
still sum cancelled orders or triple a total through a fan-out join.
