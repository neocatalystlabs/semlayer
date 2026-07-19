# Changelog

## Unreleased

- **Reconciler: per-group verification.** Aggregate reconciliation now
  verifies every mapped grouping per group (key-aligned for same-name group
  columns; multiset for date-key↔date-column pairs) at ≥95% coverage within
  0.2% relative tolerance — grand totals alone are never accepted. Evidence
  rides in provenance ("40/40 store_id groups; 1095/1095 date groups").
- **compile_metric: multi-hop joins.** Snowflaked dimensions are now
  group-by-able across up to 3 N:1 hops; equal-length join-path ties
  (role-playing dims, diamonds) refuse constructively as ambiguous.
- **`--context` doc-promotion.** Columns explicitly named in your docs get a
  doc-prompted second look even when the heuristic was confident; corrections
  always land with a conflict recorded for review.
- **Fiscal calendars.** Date-dimension attributes are classified against the
  dim's own date column (`time_attribute`: calendar_* / fiscal_* — verified,
  never assumed). When a warehouse carries a verified fiscal calendar,
  `compile_metric` quarter/year requests require an explicit
  `calendar='fiscal'|'calendar'` choice — never a silent Gregorian
  assumption; fiscal bucketing groups by the customer's own fiscal columns.

## v0.3.0-beta.1 (2026-07-19)

- **`compile_metric`** (new MCP tool + `semlayer.compile`): compiles any
  declared metric to correct SQL — N:1 joins, business-rule filters, and
  time bucketing applied automatically. Refusals are constructive: illegal
  group-bys, time requests on metrics without a time dimension, and filters
  on unmodeled columns are refused *with the legal alternatives enumerated*
  (consumer protocol: SPEC.md §2.10). Dialect-aware time grains
  (DuckDB/Snowflake/BigQuery).
- **Time is first-class on metrics**: Enrich now emits `agg_time_dimension`
  per metric (business date preferred; metadata/load timestamps never
  qualify; date-key tables resolve through their date dimension).
- **Declared ratio metrics**: `type: ratio` compiles (same-base-table,
  Phase A); explicit `name = table.col / table.col` claims in `--context`
  docs land as review-gated ratio metrics with `docs` provenance.
- **Metric `synonyms`** (spec 0.2.0, MINOR): alternate names for
  natural-language lookup, wired into MCP search.
- **dbt exporter** now emits ratio metrics (MetricFlow `type_params`).
- Provider robustness: one automatic retry with a larger budget when a
  thinking-enabled model returns no text.

## v0.2.0-beta.1 (2026-07-18)

- **Knowledge-doc priors** (`--context`): feed data dictionaries, wiki
  exports, `CLAUDE.md`/`knowledge.md` files into inference as priors. Files,
  directories, and globs of `.md`/`.txt`/`.rst`/`.html`; CSV/TSV data
  dictionaries are detected by header shape and mapped deterministically
  (works in `--no-llm` mode). Priors never override data: doc-vs-data
  contradictions land in the conflicts envelope; doc-confirmed enum decodes
  upgrade `llm_guess → docs` (metric-filter legal per SPEC.md §2.8).
  [Guide](docs/context-priors.md), including the query-log summarize-to-doc
  recipe.
- **Spec 0.1.1** (PATCH per spec/VERSIONING.md): new optional provenance
  signal `docs`. All 0.1.0 documents remain valid.
- **Iceberg bridge documented + hardened**: the DuckDB connector is
  catalog-aware (`ATTACH`ed Iceberg REST catalogs enumerate and profile with
  catalog-qualified names); recipes in [docs/iceberg-bridge.md](docs/iceberg-bridge.md).
- No-context runs are byte-identical to v0.1 (doc excerpts join prompts as
  an additive evidence field; all v0.1 cassettes replay unchanged).

## v0.1.0-beta.1 (2026-07-18)

First public beta.

- **Inference pipeline**: Profile (batched stats + typed rule pipeline + LLM
  escalation), Link (corroborated FK discovery; zero-trap hard gate), Describe
  (2-pass context propagation), Enrich (dictionary decodes, metrics,
  aggregate reconciliation with business-rule discovery, deprecation,
  freshness, routing).
- **Format**: open spec with confidence/provenance/lifecycle envelope,
  fan-out safety, hierarchies, aggregate routing, and a normative consumer
  contract (spec/SPEC.md).
- **CLI**: `infer` (`--no-llm`, `--no-sample-egress`), `review`, `drift`,
  `mcp`, `export` (dbt), `validate`, `init`.
- **Connectors**: Snowflake, BigQuery, DuckDB. LLM: Anthropic API
  (Haiku-tier default).
- **Evaluation, shipped**: 9 fixture warehouses, gold semantic layers,
  82-question CQ suites, reproducible benchmark
  (messy-warehouse: 0.34 raw → 0.53 with the layer; clean-schema negative
  result published).
- Measured: ~$0.70/100 tables inference cost; ~1-point accuracy cost for
  no-sample-egress; drift feed latency (Snowflake sample: 2.2 min).

Known gaps: Bedrock/Vertex routing; LookML/RDF/Ossie exporters; hierarchy
auto-inference (review-queued by design pending corroboration signals);
hosted service.
