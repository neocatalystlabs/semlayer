# semlayer

**The semantic layer that infers itself — skip the quarter of hand-writing dbt YAML.**
**Fair Source** ([FSL-1.1-ALv2](LICENSE.md)); the format and its schemas are Apache-2.0.

Point it at your warehouse. It profiles every column, discovers the foreign keys nobody declared, decodes the status columns, finds the business rules hiding in your aggregate tables, and writes the whole thing down as a portable semantic layer in an openly licensed format — with **confidence, provenance, and lifecycle on every single claim** — ready for any AI agent to consume over MCP.

```bash
pip install -e ".[warehouses]"
semlayer init snowflake            # generates the minimal-grant setup script
semlayer infer snowflake -o layer.yaml \
  --context ./docs/ --context ./etl-repo/CLAUDE.md   # optional: your wikis/dictionaries as priors
semlayer review layer.yaml         # accept/reject what the engine inferred
semlayer mcp layer.yaml            # serve it to Claude, Cursor, or any MCP client
semlayer lint layer.yaml query.sql  # check any SQL (yours or an agent's) against the layer
semlayer drift layer.yaml snowflake  # catch schema changes (cron- and CI-friendly)
```

## Why

AI agents fail on real warehouses: frontier models solved just **21.3%** of Spider 2.0's enterprise-warehouse tasks at publication (vs ~91% on the earlier academic Spider 1.0) — and even today's best agentic scaffolds only reach ~30%. The fix is a semantic layer — but every existing tool (dbt, LookML, Cube, Snowflake semantic views) makes humans write it by hand, and it goes stale the day someone runs an `ALTER TABLE`.

On our messy-warehouse benchmark (cryptic names, zero declared constraints, hidden business rules), an agent using the inferred layer answers **89% of business questions correctly vs 42% from the raw schema (+112% relative), with or without the layer's SQL linter** — and the errors it fixes are the *silent* kind: raw-schema "total revenue" happily sums cancelled orders; a fan-out join quietly triples a total. [Full benchmark, methodology changes included, and where we DON'T help →](docs/benchmark.md)

## What gets inferred

| | Examples from the test suite |
|---|---|
| **Semantic types + roles** | `sts_cd` → status code; `tot_amt` → monetary measure with sum/avg aggregations; PII flagged recall-first |
| **Keys & joins** | 104/104 undeclared FKs on TPC-DS-style naming, F1 = 1.0 on our messy fixture — with **zero** of the seeded false-FK traps accepted (statistics alone never auto-include; naming + LLM must corroborate) |
| **Enum decodes** | `C=Completed, X=Cancelled` — joined from the decode dimension the engine itself discovered |
| **Business rules** | "these aggregate tables reconcile only when `sts_cd <> 'X'`" — found by hypothesis-testing, scoped to revenue metrics (`scope: measures`), never to counts; inherited by a child fact only when its line amounts provably sum to the parent's total |
| **Grain, SCD2, ratios** | "one row per ord_id"; `cust_mstr` is SCD2 with `eff_start_dt`/`eff_end_dt`/`is_curr_flg` and an as-of join rule; `avg_tot_amt_per_order` = revenue / orders |
| **SQL linting** | `check_sql` (MCP) / `semlayer lint` (CLI): deterministic checks of any SQL against the layer — missing required filters, fan-out sums, deprecated tables, SCD2 without a validity window, hallucinated columns, vacuous correlated subqueries — each with a fix hint |
| **Metrics, routing, deprecation** | revenue/count metrics with contract-legal filters; "use `ord_hdr`, avoid `ord_hdr_legacy` (superseded)" |
| **Descriptions** | every table + column, LLM-written from evidence via join-graph context propagation, judged 0.82–0.89 correct+useful by an independent model |
| **Your docs as priors** | `--context` ingests data dictionaries, wiki exports, CLAUDE.md files — and *tells you where they're wrong*: doc-vs-data contradictions go to review, never silent override ([guide](docs/context-priors.md)) |

Everything lands with `confidence` ([calibrated, with the misses published](docs/calibration.md)), `provenance` (which signals produced it), and `lifecycle` (`inferred → reviewed → certified`, plus `deprecated`/`orphaned`), governed by a [normative consumer contract](spec/SPEC.md) that makes silent misuse — summing across a fan-out, joining SCD2 at current-row, filtering on guessed decodes — *non-conforming*, not merely unwise.

## Cost & privacy

- **~$0.70 per 100 tables** end-to-end on the cheap model tier, with your own API key ([measured cost model](docs/cost-model.md)). LLM calls are escalation-only; ~80% of columns resolve from statistics alone.
- **Several teams, one surface**: each team infers its own schema into its own file; an analyst's hub lists them as `members` and holds the cross-document relationships, and every command takes the hub ([docs/sets.md](docs/sets.md)).
- **Tiers**: every table carries a `tier` (core, staging, raw, scratch) inferred from its name and ETL columns; raw and scratch tables are `modeling: inventory_only` -- listed with their shape, never joined, described, metricised or queued for review column by column. On a 2,000-table synthetic estate that is 62% of tables set aside, the review queue cut 4.4x and foreign-key precision 0.31 → 0.94 ([measured](docs/scale.md)).
- **`--no-llm`**: fully deterministic mode, zero API calls, still useful (0.780 typing / 0.834 role across 7 fixtures, against 0.809 / 0.838 with the model) — for orgs where LLM access needs procurement.
- **`--no-sample-egress`**: no value read from a warehouse cell is sent to the LLM, at **no net accuracy cost** across our 7 fixtures (0.809 typing / 0.842 role, versus 0.809 / 0.838 with samples). Per warehouse it trades rather than being uniformly free — up to a point better or worse. Re-measured 2026-10-03; see [docs/cost-model.md](docs/cost-model.md).
- **Two knobs for speed, output identical at any value**: `--llm-workers N` (default 4) runs that many model calls at once (typing escalation, foreign-key validation, both Describe passes; bounded by your API rate limit, measured 7.2x at 8) and `--query-workers N` runs that many foreign-key queries at once *in your warehouse* (default 4 on a network warehouse, since it spends your compute: measured 3.3x at 4 and 6.7x at 8 on BigQuery; every core on an in-process DuckDB file). `--no-llm` builds still use `--query-workers`. ([measured](docs/scale.md))
- **Read-only, minimal-grant**: `semlayer init` generates the grant script; the reader persona is proved unable to write — on PostgreSQL by a test that runs in ordinary CI against a real server (`tests/test_postgres.py`, no credentials needed), and on Snowflake by a live suite runnable against your own account (`tests/test_snowflake_live.py`).
- **Telemetry**: anonymous usage statistics — command, duration, outcome, warehouse type, bucketed table/column counts, version, OS. Never names, SQL, or data. See exactly what is sent with `semlayer telemetry --show`; opt out with `semlayer telemetry --off`, or per-shell with `SEMLAYER_TELEMETRY=off`. ([what and why](docs/telemetry.md))

## Honest scope (beta)

- **Best on messy warehouses.** On clean, well-named schemas our benchmark shows raw DDL is already sufficient — we publish that negative result rather than hide it. If your warehouse is tidy TPC-DS, you may not need us.
- ETL input: **dbt** (`--manifest target/manifest.json`) -- reads the model DAG to settle join targets statistics cannot. Catalog comments and `--context` docs are read as priors on every run.
- Verify it yourself, free: `ANTHROPIC_API_KEY= pytest -q` replays recorded
  model responses, so every accuracy floor and benchmark number above is
  checkable with no API key and no spend — including from a fork, where CI has
  no secrets ([what those recordings are, and what is still untidy about
  them](cassettes/README.md)). The warehouses behind the numbers, with sources,
  pinned versions and licences, are listed in
  [docs/warehouses.md](docs/warehouses.md).
- Scale: tested to **2,000 tables / 30,000 columns**; the run says so when a warehouse is past that. Data VOLUME is not the limit -- 4TB and 45TB are the same architecture. [How to make it fast](docs/scale.md).
- Warehouses: **Snowflake, BigQuery, PostgreSQL, DuckDB** (+ Iceberg on S3 via the [DuckDB bridge](docs/iceberg-bridge.md)). PostgreSQL also reaches Redshift and other Postgres-protocol engines, whose catalogs differ in places and are untested. Exporter: **dbt** (losses reported, never silent). LLM: Anthropic API (Bedrock/Vertex routing is next).
- Not included: hosted service, ontology enrichment (it failed our own ablation gate — [receipts](docs/ablation-m5.md)), LookML/RDF exporters.
- The eval harness ships in this repo — fixtures, gold layers, competency questions, benchmark runner. **Run our numbers yourself**: `python fixtures/build.py && pytest tests/ -q`.

## Beta feedback

Running the beta? File the [**Beta feedback** form](https://github.com/neocatalystlabs/semlayer/issues/new/choose) (attach the `*.report.json` the CLI writes next to your output — timings and counts only, never your schema or data), or email [hello@semlayer.dev](mailto:hello@semlayer.dev) for anything sensitive.

## Layout

[`spec/`](spec/) format schema + consumer contract · [`src/semlayer/`](src/semlayer/) the engine · [`fixtures/`](fixtures/) 9 eval warehouses + golds + CQ suites · [`docs/`](docs/) benchmark, cost model, spike reports · [`ARCHITECTURE.md`](ARCHITECTURE.md) how it works

## Licence

**Fair Source, not open source.** The engine and CLI are
[FSL-1.1-ALv2](LICENSE.md): free for any internal use, including in
production and CI, with no fee or key; the one prohibition is offering
semlayer to third parties as a competing product or service. Every release
converts to Apache-2.0 two years after it is published.

The format specification, its JSON Schemas and the exporters are
**Apache-2.0** — a semantic layer nobody else can read is worth nothing.

Releases 0.1.0b1 through 0.4.0b5 were published under Apache-2.0 and remain
so permanently. [**LICENSING.md**](LICENSING.md) has the paths table, the
conversion dates, and policy text for security teams.
