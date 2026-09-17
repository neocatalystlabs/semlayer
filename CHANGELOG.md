# Changelog

## v0.4.0-beta.1 (unreleased)

Spec `0.3.0` (MINOR: one new optional field, one new contract section).

- **Confidence is now calibrated, and the calibration is published**
  ([docs/calibration.md](docs/calibration.md)) — SPEC §3 rule 5 requires it and
  we were in breach. A column's `confidence` is defined as the probability that
  *both* `semantic_type` and `entity_role` are right, measured against the 785
  gold-typed columns in `fixtures/golds/`. Two rules were removed as wrong by
  construction: `unique numeric` (0 of 15 correct, reported 0.6) and the weak-id
  name rule (1 of 13, reported 0.6). Seven statistical constants were re-fitted
  to measured hit-rate with shrinkage. Statistical-tier reliability is now
  monotone, ECE 0.130 -> 0.093, and accuracy improved as a side effect (type
  0.775 -> 0.786, role 0.789 -> 0.811). LLM escalation no longer keys solely off
  the trust number: `decimal fallback` calibrates to 0.7 but is systematically
  money-vs-quantity ambiguous, so it escalates regardless. The naming tier, FK
  confidence and metric confidences remain uncalibrated and the report says so.

- **Semantic SQL linter** (`semlayer.lint`, new runtime dependency
  `sqlglot`, MIT). Deterministic, no LLM: `parse_error`, `unknown_table`,
  `unknown_column`, `correlated_reference` (an unqualified column that
  silently resolves to the outer query — the vacuous `IN (SELECT …)`),
  `deprecated_table`, `missing_required_filter` (scope-aware),
  `fanout_aggregate`, `scd2_without_validity`. Every finding carries a fix
  hint. Surfaces: `check_sql` MCP tool (server instructions ask agents to
  run it before executing), `semlayer lint <doc> [file|-]` (exit 2 on
  errors, 1 on warnings — CI-friendly), and a lint-fed repair round in the
  benchmark answerer (`semantic+lint` condition). SPEC.md §2.11.
- **`required_filter.scope`** (`all` | `measures`): a rule that applies to
  amount aggregations but not to event counts is now structured, not prose.
  Reconciliation-discovered rules are emitted this way. SPEC.md §2.2.
- **Rule propagation to child facts.** A parent fact's measure-scoped rule
  is inherited by a child fact only when `SUM(child.measure)` per FK key
  reconciles with the parent's measure (≥95% of keys within 0.2%); the
  evidence rides in the filter's `reason`. Unverified inheritance emits
  nothing.
- **SCD2 mechanics inferred.** `snapshot_scd2` tables get a `scd` block
  (valid_from / valid_to / current flag / natural key) from their validity
  columns, plus an as-of usage rule; `grain` is stated in words from the
  primary key.
- **Ratio metrics.** `avg_<measure>_per_<entity>` (SUM / COUNT(pk)) on
  every fact, carrying discovered rules; `compile_metric` aggregates ratio
  terms by their column's default (a key counts); the validator accepts
  `table.column` ratio terms (compile/export already did).
- **Search and context.** `semantic_search` expands warehouse
  abbreviations, stems, and ranks staging/deprecated tables below canonical
  ones; the agent context renders grain, SCD mechanics, join cardinality
  and fan-out warnings, filters before notes, only question-relevant
  metrics; deprecated tables render as a stub with columns withheld.
- **Benchmark methodology v2** (docs/benchmark.md): result scoring is
  projection-tolerant (extra/reordered columns, scalar in any column of a
  single row, midnight timestamps as dates, dictionary labels resolved to
  codes); eight messy_mart questions whose wording contradicted their gold
  SQL were reworded to the gold (MM-5, MM-9, MM-24, MM-25, MM-28, MM-29,
  MM-32, MM-33 — MM-9's gold also lost an unrequested count column). Both apply to
  every condition; before/after under both methodologies is published.
- Ontology non-inferiority is reported by the CQ harness, no longer
  asserted (the condition is internal-only and sits at the band's edge).

### Previously unreleased (now in this release)

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
- **Benchmark runs from a pip install.** `python -m semlayer.benchmark`
  resolved `fixtures/` and `cassettes/` relative to its own file, which lands
  in site-packages once installed, so the documented reproduction command
  only worked inside a source checkout. Both now fall back to the directory
  the run starts in, and a cassette directory is accepted only if it actually
  holds recordings — an empty one created by an earlier failed run used to
  win and produce a `CassetteMiss` on the first prompt.
- **Drift survives a renamed column.** `semantic_drift` walked the columns in
  the document and probed each enum column by name against the live warehouse,
  without checking it still existed. A rename (the most ordinary schema change
  there is) killed the whole run with a binder error instead of reporting the
  change. It now probes only columns the warehouse still has: a renamed column
  is reported as the old one dropped plus the new one added, and the old one is
  orphaned. The freshness check had the same flaw and is fixed with it.

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
