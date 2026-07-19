# Changelog

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
