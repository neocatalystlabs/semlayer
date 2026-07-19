# semlayer

**The open-source semantic layer that infers itself — skip the quarter of hand-writing dbt YAML.**

Point it at your warehouse. It profiles every column, discovers the foreign keys nobody declared, decodes the status columns, finds the business rules hiding in your aggregate tables, and writes the whole thing down as an open, portable semantic layer — with **confidence, provenance, and lifecycle on every single claim** — ready for any AI agent to consume over MCP.

```bash
pip install -e ".[warehouses]"
semlayer init snowflake            # generates the minimal-grant setup script
semlayer infer snowflake -o layer.yaml \
  --context ./docs/ --context ./etl-repo/CLAUDE.md   # optional: your wikis/dictionaries as priors
semlayer review layer.yaml         # accept/reject what the engine inferred
semlayer mcp layer.yaml            # serve it to Claude, Cursor, or any MCP client
semlayer drift layer.yaml snowflake  # catch schema changes (cron- and CI-friendly)
```

## Why

AI agents fail on real warehouses: frontier models solved just **21.3%** of Spider 2.0's enterprise-warehouse tasks at publication (vs ~91% on the earlier academic Spider 1.0) — and even today's best agentic scaffolds only reach ~30%. The fix is a semantic layer — but every existing tool (dbt, LookML, Cube, Snowflake semantic views) makes humans write it by hand, and it goes stale the day someone runs an `ALTER TABLE`.

On our messy-warehouse benchmark (cryptic names, zero declared constraints, hidden business rules), an agent using the inferred layer answers **53% of business questions correctly vs 34% from the raw schema (+54% relative)** — and the errors it fixes are the *silent* kind: raw-schema "total revenue" happily sums cancelled orders. [Full benchmark, including where we DON'T help →](docs/benchmark.md)

## What gets inferred

| | Examples from the test suite |
|---|---|
| **Semantic types + roles** | `sts_cd` → status code; `tot_amt` → monetary measure with sum/avg aggregations; PII flagged recall-first |
| **Keys & joins** | 104/104 undeclared FKs on TPC-DS-style naming, F1 = 1.0 on our messy fixture — with **zero** of the seeded false-FK traps accepted (statistics alone never auto-include; naming + LLM must corroborate) |
| **Enum decodes** | `C=Completed, X=Cancelled` — joined from the decode dimension the engine itself discovered |
| **Business rules** | "these aggregate tables reconcile only when `sts_cd <> 'X'`" — found by hypothesis-testing, scoped to revenue metrics, never to counts |
| **Metrics, routing, deprecation** | revenue/count metrics with contract-legal filters; "use `ord_hdr`, avoid `ord_hdr_legacy` (superseded)" |
| **Descriptions** | every table + column, LLM-written from evidence via join-graph context propagation, judged 0.82–0.89 correct+useful by an independent model |
| **Your docs as priors** | `--context` ingests data dictionaries, wiki exports, CLAUDE.md files — and *tells you where they're wrong*: doc-vs-data contradictions go to review, never silent override ([guide](docs/context-priors.md)) |

Everything lands with `confidence`, `provenance` (which signals produced it), and `lifecycle` (`inferred → reviewed → certified`, plus `deprecated`/`orphaned`), governed by a [normative consumer contract](spec/SPEC.md) that makes silent misuse — summing across a fan-out, joining SCD2 at current-row, filtering on guessed decodes — *non-conforming*, not merely unwise.

## Cost & privacy

- **~$0.70 per 100 tables** end-to-end on the cheap model tier, with your own API key ([measured cost model](docs/cost-model.md)). LLM calls are escalation-only; ~80% of columns resolve from statistics alone.
- **`--no-llm`**: fully deterministic mode, zero API calls, still useful (0.81 typing accuracy on the messy fixture) — for orgs where LLM access needs procurement.
- **`--no-sample-egress`**: cell values never leave for the LLM; measured cost ≈ 1 point of accuracy.
- **Read-only, minimal-grant**: `semlayer init` generates the grant script; the nightly test suite proves the reader persona cannot write.
- **Telemetry**: anonymous command counts spooled *locally only* — nothing leaves your machine in this release; opt out with `SEMLAYER_TELEMETRY=off`. ([details](SECURITY.md))

## Honest scope (v0.1 beta)

- **Best on messy warehouses.** On clean, well-named schemas our benchmark shows raw DDL is already sufficient — we publish that negative result rather than hide it. If your warehouse is tidy TPC-DS, you may not need us.
- Warehouses: **Snowflake, BigQuery, DuckDB** (+ Iceberg on S3 via the [DuckDB bridge](docs/iceberg-bridge.md)). Exporter: **dbt** (losses reported, never silent). LLM: Anthropic API (Bedrock/Vertex routing is next).
- Not included: hosted service, ontology enrichment (it failed our own ablation gate — [receipts](docs/ablation-m5.md)), LookML/RDF exporters.
- The eval harness ships in this repo — fixtures, gold layers, competency questions, benchmark runner. **Run our numbers yourself**: `python fixtures/build.py && pytest tests/ -q`.

## Layout

[`spec/`](spec/) format schema + consumer contract · [`src/semlayer/`](src/semlayer/) the engine · [`fixtures/`](fixtures/) 9 eval warehouses + golds + CQ suites · [`docs/`](docs/) benchmark, cost model, spike reports · [`ARCHITECTURE.md`](ARCHITECTURE.md) how it works

License: Apache-2.0.

