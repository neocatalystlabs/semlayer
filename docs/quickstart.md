# Quickstart

## DuckDB (60 seconds, no accounts)

```bash
pip install -e .
python fixtures/build.py fan_trap
semlayer infer duckdb:fixtures/build/fan_trap.duckdb -o layer.yaml --no-llm
semlayer review layer.yaml --list
semlayer mcp layer.yaml     # then add to your MCP client config
```

Add `ANTHROPIC_API_KEY` to your environment and drop `--no-llm` for full
inference (descriptions, decode validation, type escalation).

## Snowflake

```bash
pip install -e ".[warehouses]"
semlayer init snowflake --database analytics --warehouse-name wh_xs  # run output as ACCOUNTADMIN
export SNOWFLAKE_ACCOUNT=ORG-ACCOUNT SNOWFLAKE_READER_USER=semlayer_reader_svc \
       SNOWFLAKE_READER_PASSWORD=... SNOWFLAKE_WAREHOUSE=wh_xs SNOWFLAKE_DATABASE=analytics
semlayer infer snowflake -o layer.yaml
```

## BigQuery

```bash
pip install -e ".[warehouses]"
semlayer init bigquery --project my-project   # run output with gcloud
export GCP_PROJECT=my-project \
       GOOGLE_APPLICATION_CREDENTIALS_READER=/path/semlayer-reader.json
semlayer infer bigquery -o layer.yaml
```

## PostgreSQL

Also covers Redshift and other Postgres-protocol engines, though their catalogs
differ in places and are not covered by our tests.

```bash
pip install -e ".[warehouses]"
semlayer init postgres --database analytics --schema mart  # run output as a superuser
export PG_READER_DSN=postgresql://semlayer_reader:...@host:5432/analytics
semlayer infer postgres -o layer.yaml
```

Or skip the environment and pass the connection string as the source, which is
how most people already hold a Postgres credential:

```bash
semlayer infer postgresql://user:pass@host:5432/analytics -o layer.yaml
```

The grants `init` prints are the whole requirement — in particular **not**
`pg_read_all_stats`. `pg_stats` already filters its rows to tables the caller
can `SELECT`, so the reader role can read cardinality and null rates from the
planner's statistics rather than scanning. `tests/test_postgres.py` builds that
exact role on a real server and proves both halves: inference works, and every
write is refused.

## If you have a dbt project

Point `--manifest` at it and the engine reads how the tables were authored, not
just what they look like now:

```bash
dbt parse                      # or any dbt build you already run
semlayer infer duckdb:warehouse.duckdb --manifest target/manifest.json -o layer.yaml
```

`dbt parse` is enough -- no warehouse connection required to produce the
manifest -- because only the model DAG is read, and dbt has already resolved
every `ref()` and macro.

Today this settles one thing: which table a foreign key points at, when the
statistics cannot tell. A warehouse that holds both a published table and the
staging step it is built from offers two candidates with identical keys, and
the DAG says which one consumers should join to. On our `dbt_synthea` fixture
that lifts foreign-key recall against the project's own declared keys from
**26.0% to 41.7%**. Every edge the DAG decided is marked `etl_lineage` in its
provenance, so you can see which ones they were.

## Then

- `semlayer review layer.yaml` — walk the inferred-claims queue (the ≤30-minute
  human pass that turns inference into a trusted model).
- Commit `layer.yaml` to your repo; wire `semlayer drift layer.yaml <source>`
  into CI or cron (nonzero exit = drift found).
- `semlayer lint layer.yaml models/revenue.sql` — check SQL (a dbt model, a
  saved query, an agent's answer) against the layer; exit 2 on errors, 1 on
  warnings, so it drops into CI. Agents get the same checks as the
  `check_sql` MCP tool.
- `semlayer export` currently emits dbt semantic models; losses are printed,
  never silent.

## Large warehouses

Tested to **2,000 tables / 30,000 columns**; past that the run warns and
foreign-key discovery begins truncating. Data volume is a different question —
4TB and 45TB are the same architecture, bounded by partitioning.

The levers, in order of effect: partition your big facts and pass
`--state layer.state.json`; give the warehouse more parallelism; declare your
keys and keep statistics analyzed; pass `--manifest target/manifest.json`; and
narrow with `--only`. Details and the measured numbers: [scale.md](scale.md).
