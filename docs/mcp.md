# MCP: give your agent the semantic layer

`semlayer mcp` serves the semantic layer to any MCP client — Claude Desktop,
Claude Code, Cursor, or anything else that speaks the protocol. Your agent stops
guessing at column names and starts asking.

**Start it with no arguments.** It comes up whether or not you have inferred
anything yet, and its first tool, `status`, says what it has, which layers are
already on this machine, which warehouses it can see, and what to do next. You
do not have to read this page before it is useful.

One thing worth knowing before you start: **a semantic layer is not something
your machine has by default — it is built from your warehouse, and it takes
minutes.** The server can build it for you from inside the conversation
(`infer`), and until it exists there is nothing to search or query. That is
the first thing `status` says.

```bash
semlayer mcp                       # a stdio server, layer or no layer
semlayer mcp layer.yaml            # serve a specific layer
semlayer mcp --connection snowflake:prod   # the layer cached for one warehouse
```

## Setup

`semlayer mcp` speaks stdio, so you do not run it yourself — your client does.
Point the client at the command and leave the arguments off.

### With nothing installed

If you have [`uv`](https://docs.astral.sh/uv/), this is the whole of step one.
`uvx` fetches semlayer on first launch, so there is no terminal step at all:

```json
{
  "mcpServers": {
    "semlayer": {
      "command": "uvx",
      "args": ["--with", "semlayer[warehouses]", "semlayer", "mcp"]
    }
  }
}
```

Paste it into your client's config, restart the client, and ask it "what does
semlayer have?" — it will call `status`.

The `[warehouses]` extra is not decoration. The Snowflake, BigQuery and
Postgres drivers are optional dependencies, and without them the server can
*find* your warehouse and not open it — which is the same wall one step
further in. `status` reports which drivers are importable, so your agent can
tell you to reinstall rather than letting you discover it at connect time.

### Claude Desktop

Config file:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{ "mcpServers": { "semlayer": { "command": "semlayer", "args": ["mcp"] } } }
```

That form is for a semlayer you installed yourself
(`pip install "semlayer[warehouses]"`). If `semlayer` is not on the client's
PATH — common with `pipx`, `uv`, or a venv — give the full path to the
executable; `which semlayer` will tell you.

### Claude Code

```bash
claude mcp add semlayer -- semlayer mcp
# or, with nothing installed:
claude mcp add semlayer -- uvx --with "semlayer[warehouses]" semlayer mcp
```

### Cursor

`~/.cursor/mcp.json`, same shape as Claude Desktop.

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

You usually do not need this. With no path at all, the server loads a layer it
can already see — see [A layer you built yesterday](#a-layer-you-built-yesterday).

## Credentials: the file, not the chat

**Your agent will never ask you for a password, and you should not paste one.**
That is in the instructions the server hands the client at the handshake, and
it is the one rule here that code cannot enforce.

What it asks for instead is a name: a DSN without the password, a dbt profile
name, a BigQuery project id, a Snowflake connection name. Where the secret
itself goes, in order of preference:

1. **Somewhere that already holds it.** A dbt profile, `~/.snowflake/connections.toml`,
   `.pgpass`, or `gcloud auth application-default login`. A connection found in
   one of those says so, and the question never comes up.
2. **`~/.semlayer/.env`** (or `$SEMLAYER_HOME/.env`), one `KEY=value` per line.
   The server **re-reads this file on every call**, so the loop is: your agent
   tells you the exact line to add, you add it, you say "done", it calls
   `status` again. No restart. Remove the line and it stops working on the next
   call, too.
3. **The `env` block of your MCP config.** This works, and needs a client
   restart to take effect, which is why it is third.
4. **Pasted into the chat anyway?** It works, for this session only. The
   value is held in memory and goes nowhere else: not to disk, not to a log,
   not into telemetry, not into the layer or the run report, and not into the
   cache key — `status` shows the connection with the secret replaced, which
   is also what the cache is keyed on. Your agent is told to say three
   things: it worked, it is gone when the server restarts, and here is the
   durable line to add instead. Rotating a credential you pasted into a chat
   is the careful move.

`status` reports that file's path, whether it exists, and **which keys it
currently sets** — names only, never values — so your agent can see whether
the line it dictated actually landed.

A shell `export` is the one thing that will not work: Claude Desktop on macOS
launches servers with a minimal environment, and never sees your shell profile.

## A layer you built yesterday

A layer is a file, and the server looks for one before asking you anything. It
checks `$SEMLAYER_HOME/layers/` (where inference caches it, keyed by
connection) and your project — `layer.yaml`, `semlayer.yaml`, `.semlayer/*.yaml`,
or any YAML whose top-level key is `semantic_layer`, in the working directory
or your repository root.

- **Exactly one, and no path given:** it is loaded at startup. `status` comes
  up `ready` and `next` names the warehouse, so your agent says which one it
  loaded and you can object.
- **Several:** the server starts empty and asks which warehouse you mean. Your
  agent then calls `load_layer` with the `ref` from `status.layers`. The one
  you used last is marked, so "the usual one" has an answer.
- **A copy in your repo beats a cached copy of the same warehouse.** The repo
  is where your team's review decisions live.

The cache is deliberately in your home directory and not in your repository:
inference writes it without asking, and a file we put in your repo unasked is
a `.gitignore` decision we made for you. Putting a layer where your teammates
can see it is an explicit step, not a side effect.

Each entry carries its connection, table count and age, so an agent can caveat
a stale model instead of trusting it.

## A warehouse semlayer cannot open

Four kinds are supported: DuckDB, Postgres, BigQuery, Snowflake. **Redshift,
Supabase, Neon, Timescale, AlloyDB and anything else speaking the Postgres
protocol are the Postgres connection** — that is in the handshake instructions
so your agent does not send you away for a connector that already exists.

For the rest — Databricks, Trino, Athena, ClickHouse, SQL Server, Oracle,
Spark, Iceberg on object storage — your agent passes what you said to
`discover_connections(requested="...")` and gets back an honest answer and the
nearest route:

- **A dbt project** that targets the store gives table names, columns and
  lineage with no warehouse connection at all.
- **DuckDB** can attach some stores — an Iceberg REST catalog, Parquet in
  object storage — and semlayer then models it as DuckDB.

The name you said is classified into a fixed list of engines for telemetry
(never the words themselves) because that is the only record of demand we
cannot currently serve. See [telemetry.md](telemetry.md).

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

**`run_sql` reads rows, and only reads them.** This page used to say the
server never touched your warehouse; that stopped being true when `run_sql`
landed, and here is the actual line. One read-only SELECT per call, under the
connection the session is already using:

- **SELECT only, decided by parsing the statement** — not by looking for
  words. `DELETE`, `UPDATE`, `INSERT`, `MERGE`, `CREATE`, `DROP`, `GRANT`,
  `COPY` and `SELECT ... INTO` are refused, and so is a second statement
  smuggled in after a semicolon. A column named `deleted_at` or a literal
  containing `delete from` is not a write, and a parse knows the difference.
- **`check_sql` runs first, and an error means the SQL is not executed.** A
  query that sums cancelled orders runs perfectly well; "it ran" was never
  the standard. Warnings come back with the rows instead of blocking.
- **Your SQL is never rewritten.** No `LIMIT` is appended and no filter is
  injected. A required filter is reported; applying it is the agent's job, so
  what ran is what you can be shown.
- **Capped rows and a ten-minute time limit**, both stated in the result
  along with whether rows were truncated. Ten minutes because a real
  analytical query takes minutes — the 30 seconds this started at refused
  honest work — while a query with no limit holds the session's one
  warehouse connection for as long as your warehouse will run it. It is a
  default, not a rule: a caller can turn it off and let your own resource
  monitors and account timeouts be the only limits. Nothing is persisted
  anywhere.
- **Refused while an inference is running.** One warehouse connection is not
  shared between a profiling run and an agent's query — DuckDB forbids two
  threads on one connection outright, and on every engine the two would
  interleave. The inference waits for a query; a query does not wait for an
  inference, because a query is seconds and the agent can simply ask again.

**The document tools still touch nothing.** `semantic_search`, `table_detail`,
`check_sql`, `compile_metric` and the rest read the layer file and nothing
else: `check_sql` lints without running, `compile_metric` hands back SQL as
text.

**All of it runs on your machine.** We host nothing and store nothing. Your
layer is a file you own, your credentials never leave, and the rows never pass
through us — the server is a local process your client started.

### Do I still need my warehouse's MCP server?

Not for this. semlayer used to be the map and not the road: it knew what your
tables meant and something else had to run the SQL. `run_sql` closes that, so
one server answers a question end to end.

Attach your warehouse's own MCP server anyway if you want writes, DDL, or
queries bigger than a chat turn should carry. Those are deliberately not here.

If you use the hosted control plane later, note the difference: it compiles
and serves the model, and it never executes. Execution is local, under your
credentials, or it does not happen.

## What your agent can ask

| tool | what it answers |
|---|---|
| `status` | **call this first.** Is a layer loaded, how big and how old is it, how many claims has nobody reviewed, which layers are already on this machine, which warehouses can I see, which kinds this build can open, where credentials go, and what to do next |
| `discover_connections` | which warehouses are reachable from this machine, and which of them semlayer can open. With `requested`, what to do about a warehouse you named that is not one of them |
| `load_layer` | load one of the layers `status` listed, by ref, by connection, or by path |
| `semantic_search` | "where is revenue?" — tables, columns and metrics by keyword. The starting point once a layer is loaded |
| `get_domains` | the business areas in the warehouse |
| `get_tables` | what tables exist, their type, lifecycle, and `type_confidence` |
| `table_detail` | columns, types, foreign keys, **required filters**, confidence per claim |
| `get_metrics` | defined measures and how to aggregate them |
| `route_intent` | *which* table to use for an intent, when several could answer |
| `check_sql` | lint SQL against the layer: missing filters, fan-out, deprecated tables |
| `compile_metric` | generate correct SQL for a metric, with the filters already applied |
| `infer` | **build the layer**, from inside the conversation. Reads your warehouse; ask first |
| `cancel_infer` | stop a run that is going |
| `context_for_question` | **start every question here.** The part of the layer that bears on it, plus the claims underneath that nobody has confirmed |
| `run_sql` | run one read-only SELECT and return the rows, with the assumptions the answer rests on |

With no layer loaded, every tool answers with `status` instead of an error, so an
agent that starts asking about tables before anything has been inferred is told
what to do rather than left guessing. Read the `next` field.

On connect, the server also hands the client standing instructions: call `status`
first, always apply required filters, never use objects flagged `UNUSABLE`, run
`check_sql` on every query before executing, and caveat answers built on
`lifecycle: inferred` elements.

## What `discover_connections` reads — and what it never reports

It looks in these places, in this order:

| | where | what it reports |
|---|---|---|
| 1 | the working directory | `*.duckdb` and `*.db` files, not recursively |
| 2 | `PG_READER_DSN` / `PG_DSN`, from the environment **or `$SEMLAYER_HOME/.env`** | that a Postgres DSN is set — the variable name, never its value |
| 3 | `GCP_PROJECT`, a key-file variable, or application default credentials | the BigQuery project, when one is named |
| 4 | dbt's `profiles.yml` (`$DBT_PROFILES_DIR`, `./`, `~/.dbt/`) | each profile/target, mapped to a connection where we can map it |
| 5 | `SNOWFLAKE_ACCOUNT` | that a Snowflake account is configured, and which variables are still missing |
| 6 | `~/.snowflake/connections.toml` | the section names, and whether each carries a role and a database |

No connection is opened and no credential is read. Passwords, key files, tokens,
DSN values, Snowflake account identifiers and hostnames are not reported, even
where the file we are reading has them sitting right there — the tests for this
are written as attempts to leak one.

## There is no layer yet — now what?

Ask for one. Your agent calls `infer` with the connection and your question,
and no terminal is involved:

> **you:** how many customers do we have in each segment?
> **agent:** there's no semantic layer for this machine yet — I can see a
> DuckDB file at `warehouse.duckdb`. Shall I model it? It reads the warehouse
> and takes a few minutes on a real one.
> **you:** go ahead
> **agent:** *(calls `infer`, waits, gets `ready` back)* 36 tables, 198
> columns. Customers by segment: SMB 521, CON 495, ENT 484. One assumption:
> `seg_cd`'s meanings are the engine's guess…

What to know about it:

- **It reads your warehouse and it costs money.** Metadata and sample values,
  under the connection you gave, plus your own LLM key for the descriptions.
  Your agent is told to ask before starting.
- **A small warehouse finishes inside the call.** A DuckDB file with
  `use_llm: false` takes seconds, so discovery, inference and the answer
  happen in one turn.
- **A real one does not, and nothing will wake your agent when it ends.** MCP
  has no way to push. The call returns `inferring`, and the next `status` —
  the next time you say anything — reports it and reminds the agent of the
  question you asked. That is why `infer` takes your question.
- **`scope`** limits it to named schemas, which is how a big warehouse
  finishes quickly.
- **The layer is cached** under `$SEMLAYER_HOME/layers/`, so tomorrow's chat
  finds it. `save_layer` — putting a copy in your repo for your team — is the
  next piece of work; for now `semlayer review` and the CLI read the cached
  file by path.

The CLI still does all of this if you prefer a terminal:
`semlayer infer duckdb:warehouse.duckdb -o layer.yaml`. See
[quickstart.md](quickstart.md) for the per-warehouse credentials and
[warehouses.md](warehouses.md) for what each connector reads.

## Asking a question

Once a layer is loaded the order is fixed, and it is the order the benchmark
measured:

1. **`context_for_question`** — the tables that bear on the question, with
   their columns, keys, required filters, metrics and routing knowledge, plus
   `open_questions`: the claims underneath that nobody has confirmed.
2. **`check_sql`** on the SQL the agent writes.
3. **`run_sql`**, which lints again and refuses to execute anything with an
   error in it, and gives up after ten minutes.
4. **The answer first, assumptions under it.** Every result carries
   `assumptions` — each unconfirmed claim the answer rested on, with what the
   engine saw and whether accepting it would change any behaviour. Your agent
   should render them and offer to record a correction in one word.

```
Customers by segment: SMB 521, CON 495, ENT 484.

Assumed: seg_cd's values mean consumer / small-medium business / enterprise
(the engine guessed; nobody has confirmed it). Say "yes" and I'll record it.
```

Recording that verdict from the chat is the next piece of work; today it is
`semlayer review <layer> --list`.

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

Those same claims come back attached to answers, which is the useful form:
`context_for_question` returns the ones that bear on your question, and
`run_sql` returns the ones the answer actually rested on. Each says whether
accepting it would **change behaviour** — a decode guess that a metric's
filter is written against is a different kind of uncertainty from a type
label nobody has blessed — so the caveat you get is about this answer rather
than about the layer in general.

## Keeping it current

The served file is a snapshot. When the warehouse changes, re-run `semlayer
drift` and apply the changes — `drift --apply` preserves the review decisions
that `infer` would overwrite. The client picks up the new file on restart.

## Troubleshooting

**The tools do not appear.** Almost always the client cannot find `semlayer`.
Give the absolute path to the executable.

**`status` says `no_layer` and lists nothing.** The server starts in whatever
directory your client launches it from, which is usually not your project — so
a DuckDB file next to your code is not visible. Variables from your shell
profile are usually not visible either: put them in `~/.semlayer/.env`, which
is read wherever the server was started from, or pass the layer path directly.

**A connection is listed but cannot be opened.** Check
`status.supported[].driver_installed`. The warehouse drivers are optional
extras, so a `pip install semlayer` without `[warehouses]` discovers a
Snowflake account it cannot connect to. Reinstall as
`pip install "semlayer[warehouses]"`, or use the `uvx` launcher above, which
asks for the extra by name.

**You added a credential and nothing changed.** If you exported it in a shell,
the server never saw it — Claude Desktop on macOS launches servers with a
minimal environment. Put the line in `~/.semlayer/.env` instead and ask your
agent to call `status` again; that file is re-read on every call and needs no
restart.

**Every answer is caveated as inferred.** That is correct until someone reviews
the layer: `semlayer review layer.yaml --list`, then promote what is right.
Lifecycle is how the layer tells an agent what a human has actually blessed.

**`check_sql` reports errors on a query that runs fine.** That is the point —
it lints for *semantic* validity, not syntax. A query that executes happily can
still sum cancelled orders or triple a total through a fan-out join.
