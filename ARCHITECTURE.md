# Architecture

## The pipeline

`infer` runs four stages over a source, each emitting confidence + provenance:

1. **Profile** (`profile/`) — batched statistical profiling (2 wide SELECTs per
   table, not per-column round-trips) + an ordered rule pipeline for semantic
   typing. Columns under 0.7 confidence escalate to the LLM, one batched call
   per table. Deterministic without an LLM (`--no-llm`).
2. **Link** (`link/`) — pruned inclusion-dependency FK candidates → the
   corroboration rule: statistics alone NEVER auto-include; strong naming
   agreement + LLM plausibility do; the rest go to the review queue. Zero
   auto-included seeded traps is a hard test gate.
3. **Describe** (`describe/`) — two-pass context propagation: pass 2 re-describes
   each table knowing its join-graph neighbors' descriptions.
4. **Enrich** (`enrich/`) — deterministic: dictionary decodes (joining the dims
   Link found), metric candidates, aggregate reconciliation (which also
   *discovers* business-rule filters by hypothesis testing), deprecation,
   freshness, routing/domains.

`drift` re-snapshots the warehouse, diffs, applies the orphaning state machine
(existence is governed by the warehouse; reviewed/certified *content* is never
rewritten — conflicts are recorded instead), computes a depth-capped blast
radius, and re-runs affected competency questions as the alarm.

## Interfaces (the only couplings)

- `source.MetadataProvider` + `source.QueryExecutor` — everything a warehouse
  must provide. Dialect quirks (BigQuery backticks) live in optional
  `qualify`/`quote_ident` hooks on the source, never in pipeline code.
- `llm.provider.LLMProvider` — the single LLM seam. Every call is
  temperature-pinned and cassette-cached by input hash; CI replays at $0 and
  can never silently go live.

## Concurrency model

Single-process, sequential pipeline by design. The specific shared surfaces:
- **Cassettes**: written atomically (tmp + `os.replace`); concurrent recorders
  race safely (last-writer-wins on identical inputs).
- **MCP server**: async over a document that is read-only after load.
- **DuckDB**: one process owns a database file; tests use `:memory:`; the
  fixture build directory is single-writer (multi-process DuckDB file access
  blocks on an exclusive lock — see `fixtures/build.py`).

## Deliberately declined engineering (and why)

- **Distributed tracing**: a single-process CLI has no propagation boundary;
  the per-run report (stage wall-clock, counts, spend) + `-v` logs are the
  right-sized instrument.
- **Pooling / memory-layout optimization**: this is a metadata-plane tool
  moving kilobytes; the real hot paths (fixture bulk-loads, IND candidate
  explosion, per-column round-trips) are each addressed structurally
  (CSV+COPY, pruning + caps, batched SELECTs).
- **LLM-everything**: the LLM is an escalation tier. It never overrides a
  decided heuristic (measured to *reduce* accuracy); it fills unknowns and
  validates candidates, with disagreements recorded as conflicts.

## Determinism

Pinned model + prompt versions; input-hash cassettes; re-inference re-runs an
element only when its inputs changed. Benchmark results are reproducible given
pinned engine+model; cross-model variance is reported, never hidden.
