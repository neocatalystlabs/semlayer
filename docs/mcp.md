# MCP: give your agent the semantic layer

`semlayer mcp` serves the semantic layer to any MCP client — Claude Desktop,
Claude Code, Cursor, or anything else that speaks the protocol. Your agent stops
guessing at column names and starts asking.

**Start it with no arguments.** It comes up whether or not you have inferred
anything yet, and its first tool, `status`, says what it has, which warehouses
it can see from your machine, and what to do next. You do not have to read this
page before it is useful.

```bash
semlayer mcp                       # a stdio server, layer or no layer
semlayer mcp layer.yaml            # serve a specific layer
semlayer mcp --connection snowflake:prod   # the layer cached for one warehouse
```

## Setup

`semlayer mcp` speaks stdio, so you do not run it yourself — your client does.
Point the client at the command and leave the arguments off.

### Claude Desktop

Edit the config file:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "semlayer": {
      "command": "semlayer",
      "args": ["mcp"]
    }
  }
}
```

Restart Claude Desktop; `semlayer` appears in the tools list. Ask it "what does
semlayer have?" and it will call `status`.

If `semlayer` is not on the client's PATH (common with `pipx`, `uv`, or a venv),
give the full path to the executable instead — `which semlayer` will tell you.

### Claude Code

```bash
claude mcp add semlayer -- semlayer mcp
```

### Cursor

`~/.cursor/mcp.json`, same shape as Claude Desktop:

```json
{ "mcpServers": { "semlayer": { "command": "semlayer", "args": ["mcp"] } } }
```

### Pointing it at one layer

If you already have a layer, or you keep several, name the file:

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

Use an **absolute path** — the client does not run from your shell's working
directory, so a relative path will not be found. An explicit path always wins:
it is served whatever else is configured.

## What touches your warehouse, and what does not

Two different things, with two different answers.

**Inference reads your warehouse.** `semlayer infer` connects under the
credential you give it and reads metadata and sample values — that is where a
semantic layer comes from, and there is no version of this that works without
reading your data. What it does with that access:

- It only ever reads. The minimal grant that `semlayer init` prints is USAGE +
  SELECT and nothing else.
- The role that is actually in effect is recorded in the layer, from the
  warehouse itself rather than from what you asked for. A minimal-grant claim
  about the role you *requested* has checked nothing.
- A dedicated reader role is the recommendation, not a requirement. Running as
  yourself — SSO, a Postgres DSN you already hold, `gcloud auth
  application-default login` — works, and inherits your own grants.
- `--no-sample-egress` means no value read out of a warehouse cell reaches the
  model provider. Statistical profiling still runs locally, and the measured
  cost to accuracy is about a point on one fixture and nothing corpus-wide.

**Serving the layer does not.** The tools below read the layer document and
nothing else. They hold no credential, open no connection, and see no rows:
`check_sql` lints SQL without running it, and `compile_metric` hands back SQL as
text. Something else executes it.

**All of it runs on your machine.** The CLI is the whole product; we host
nothing, store nothing, and serve nothing. Your layer is a file you own.

### So how does a query actually run?

**semlayer is the map, not the road.** It knows what your tables mean, which
filters are mandatory, and how the query should be written. In practice you
attach two things to the same assistant:

| | what it does | what it needs |
|---|---|---|
| **semlayer MCP** | meaning, rules, correct SQL | the layer file |
| **your warehouse's MCP server** (or SQL client, or BI tool) | executes the SQL | your warehouse credentials |

The execution path is one you already control and already govern. Your
credentials, your permissions, your warehouse still deciding who may see what —
we record access semantics, we never enforce them, and your data never passes
through us.

## What your agent can ask

| tool | what it answers |
|---|---|
| `status` | **call this first.** Is a layer loaded, how big and how old is it, how many claims has nobody reviewed, which warehouses can I see, and what should I do next |
| `discover_connections` | which warehouses are reachable from this machine, and which of them semlayer can open |
| `semantic_search` | "where is revenue?" — tables, columns and metrics by keyword. The starting point once a layer is loaded |
| `get_domains` | the business areas in the warehouse |
| `get_tables` | what tables exist, their type, lifecycle, and `type_confidence` |
| `table_detail` | columns, types, foreign keys, **required filters**, confidence per claim |
| `get_metrics` | defined measures and how to aggregate them |
| `route_intent` | *which* table to use for an intent, when several could answer |
| `check_sql` | lint SQL against the layer: missing filters, fan-out, deprecated tables |
| `compile_metric` | generate correct SQL for a metric, with the filters already applied |

With no layer loaded, every tool answers with `status` instead of an error, so an
agent that starts asking about tables before anything has been inferred is told
what to do rather than left guessing. Read the `next` field.

On connect, the server also hands the client standing instructions: call `status`
first, always apply required filters, never use objects flagged `UNUSABLE`, run
`check_sql` on every query before executing, and caveat answers built on
`lifecycle: inferred` elements.

## What `discover_connections` reads — and what it never reports

It looks in six places, in this order:

| | where | what it reports |
|---|---|---|
| 1 | the working directory | `*.duckdb` and `*.db` files, not recursively |
| 2 | `PG_READER_DSN` / `PG_DSN` | that a Postgres DSN is set — the variable name, never its value |
| 3 | `GCP_PROJECT`, a key-file variable, or application default credentials | the BigQuery project, when one is named |
| 4 | dbt's `profiles.yml` (`$DBT_PROFILES_DIR`, `./`, `~/.dbt/`) | each profile/target, mapped to a connection where we can map it |
| 5 | `SNOWFLAKE_ACCOUNT` | that a Snowflake account is configured, and which variables are still missing |
| 6 | `~/.snowflake/connections.toml` | the section names, and whether each carries a role and a database |

No connection is opened and no credential is read. Passwords, key files, tokens,
DSN values, Snowflake account identifiers and hostnames are not reported, even
where the file we are reading has them sitting right there — the tests for this
are written as attempts to leak one.

## There is no layer yet — now what?

Infer one. It takes minutes, it runs on your machine, and it is the one step the
server cannot yet do for you:

```bash
semlayer infer duckdb:warehouse.duckdb -o layer.yaml      # or postgres, bigquery, snowflake
semlayer mcp layer.yaml
```

`status` will have named the connections it could see, so an agent can tell you
exactly which command to run. See [quickstart.md](quickstart.md) for the
per-warehouse credentials, and [warehouses.md](warehouses.md) for what each
connector reads.

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

`status` reports `undecided_review_items`: how many inferred claims nobody has
looked at yet. A high number is not a warning, it is a to-do list —
`semlayer review layer.yaml --list`.

## Keeping it current

The served file is a snapshot. When the warehouse changes, re-run `semlayer
drift` and apply the changes — `drift --apply` preserves the review decisions
that `infer` would overwrite. The client picks up the new file on restart.

## Troubleshooting

**The tools do not appear.** Almost always the client cannot find `semlayer`.
Give the absolute path to the executable.

**`status` says `no_layer` and lists nothing.** The server starts in whatever
directory your client launches it from, which is usually not your project — so a
DuckDB file next to your code is not visible, and variables set in your shell
profile may not be either. Pass the layer path, or set the connection
explicitly.

**Every answer is caveated as inferred.** That is correct until someone reviews
the layer: `semlayer review layer.yaml --list`, then promote what is right.
Lifecycle is how the layer tells an agent what a human has actually blessed.

**`check_sql` reports errors on a query that runs fine.** That is the point —
it lints for *semantic* validity, not syntax. A query that executes happily can
still sum cancelled orders or triple a total through a fan-out join.
