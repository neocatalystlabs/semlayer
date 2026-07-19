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

## Then

- `semlayer review layer.yaml` — walk the inferred-claims queue (the ≤30-minute
  human pass that turns inference into a trusted model).
- Commit `layer.yaml` to your repo; wire `semlayer drift layer.yaml <source>`
  into CI or cron (nonzero exit = drift found).
- `semlayer export` currently emits dbt semantic models; losses are printed,
  never silent.
