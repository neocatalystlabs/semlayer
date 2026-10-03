# Changelog

## v0.4.0-beta.5 (2026-10-03)

Spec `0.3.0`; §2.5 reworded to describe achievable behaviour (see below).

- **Nothing ever built a time hierarchy, so the compiler could not obey the
  spec that required one.** SPEC §2.5 said time bucketing MUST go through a
  declared time hierarchy and never use ad hoc date math; `compile.py` emitted
  `date_trunc`. Both were defensible alone and contradictory together, because
  no inference stage produced a hierarchy for the compiler to use.

  Enrich already classified each integer column on a date dimension by checking
  it against `EXTRACT(...)` on every row — `yr_nbr` → `calendar_year`,
  `qtr_nbr` → `calendar_quarter`, `mth_nbr` → `calendar_month`. Those are
  levels; nothing collected them. It now emits a `kind: time` hierarchy per
  date dimension, calendar and fiscal, levels most-general first.

  `compile_metric` buckets through it: `date_dim.yr_nbr, date_dim.mth_nbr`
  rather than `date_trunc('month', date_dim.cal_dt)` — the customer's own
  calendar columns instead of re-deriving what they already materialised. A
  fact's own date column still truncates, correctly, because there is no
  declared calendar to honour.

  This also removes an inconsistency: the compiler already grouped *fiscal*
  quarters by the customer's `fiscal_quarter` column while ignoring the
  verified `calendar_quarter` column beside it. `_fiscal_cols` and
  `_fiscal_group_cols` are replaced by one hierarchy lookup serving both.

  A month or quarter column carries no year, so grouping by it alone collapses
  the same month across years. The year level now always travels with it, and
  §2.5 states that as a MUST.

  §2.5 is conditioned on a hierarchy existing rather than demanding one
  unconditionally, and gains a SHOULD telling producers to declare one where
  they have verified a date dimension's attributes.

- **`--no-sample-egress` was documented as costing accuracy it does not cost.**
  The claim in `docs/cost-model.md` — "~1 point of typing accuracy
  (0.904 -> 0.894)" — was measured on one fixture in July, before the typing
  rules were calibrated. Re-measured live across 7 fixtures / 759 columns with
  `semlayer.scoring.score_types`:

  | mode | semantic type | entity role |
  |---|---|---|
  | `--no-llm` | 0.780 | 0.834 |
  | default | 0.809 | 0.838 |
  | `--no-sample-egress` | 0.809 | 0.842 |

  Corpus-wide the privacy mode is free: typing identical to three decimals,
  role marginally better. The old figure still reproduces exactly on messy_mart
  (-0.010), but collision_heavy moves +0.016 the other way and they cancel — on
  a warehouse built to collide names, withholding values stops the model
  over-trusting values that look alike. Both numbers are now published.

  New `tests/test_no_sample_egress.py` pins the privacy promise as well as the
  parity. `_evidence` has an `enum_decodes` branch that is not gated on the
  flag; it is harmless only because Describe runs before Enrich, so the only
  decodes present are the model's own guesses from column names. The test fails
  if that ordering ever changes, which would start sending real decode values
  in a mode that promises it does not.

## v0.4.0-beta.4 (2026-10-02)

Spec `0.3.0`, unchanged.

- **`compile_metric` silently fanned out on history tables.** A join path
  crossing a type-2 table joined every version of the entity, so each fact row
  was counted once per version — no error, no warning, a plausible answer that
  was wrong. On messy_mart, filtering average order value to customers in
  California counted 820 rows for 663 orders and overstated the answer by 1.2%.
  Our own linter already flagged it (`scd2_without_validity`), and SPEC §2.4
  calls a plain current-row join to an SCD2 attribute non-conforming.

  `compile_metric` (and the `compile_metric` MCP tool) now takes
  `scd: "current" | "asof"`, and refuses without it. The two are different
  questions — where a customer lives *now* versus where they lived *when they
  ordered* — and on this fixture they select 594 orders against 452 and return
  different numbers, so picking one silently would be guessing on the caller's
  behalf. Same stance the compiler already takes on ambiguous join paths and
  unstated fiscal calendars.

  `current` pins to `is_current_flag` (or an open `valid_to`); `asof` drives
  the validity window off the metric's `agg_time_dimension`, never a
  caller-supplied date. Both predicates are emitted in the `ON` clause, not
  `WHERE`, where they would turn the `LEFT JOIN` inner and silently drop fact
  rows. A `snapshot_scd2` table carrying no `scd` block is refused outright —
  there is nothing to resolve it with.
- **Every metric was emitted with an empty `grain`.** The metric producers copy
  their base table's grain, but ran ten lines before `_infer_grain` populated
  it, so all of them carried `''`. Grain is what one row of the source
  represents, and it is what distinguishes "average order value" from "average
  line value" — the same column, the same SQL, a different question. Grain
  inference now runs before the producers.

  Separately, a reconciled summary table has no primary key, so `_infer_grain`
  could not state its grain either — even though reconciliation had just
  measured the group columns against the fact table. Those are now backfilled
  from the proven mapping (`one row per agg_dt_key, store_id (pre-aggregated)`).
  On messy_mart all 19 metrics now state a grain, where none did before.

## v0.4.0-beta.3 (2026-10-02)

Spec `0.3.0`, unchanged.

- **The engine stamped documents with the wrong version.** `ENGINE_VERSION`
  was a hardcoded copy in `profile/run.py` that the 0.4.0-beta.2 bump missed,
  so that build wrote `generated_by.engine: 0.4.0b1`. SPEC §3 rule 4 makes
  engine version its own drift class, so a wrong stamp hides a re-inference it
  should have triggered. It is now derived from the installed distribution and
  asserted against both `__version__` and the distribution metadata, so it
  cannot drift again. Documents written by 0.4.0b2 carry the wrong engine
  version; re-run `infer` to correct the stamp.

## v0.4.0-beta.2 (2026-10-02)

Spec `0.3.0`, unchanged.

- **`semlayer review` covers the metric tier.** Three new item kinds, queued by
  claim kind rather than by a confidence threshold (metric confidences are rule
  constants, not measured rates): `discovered_filter` (a business rule
  recovered by reconciliation — accepting promotes it, rejecting removes it from
  the metric *and* the table's `required_filters`, and flags any table that
  inherited it), `aggregate_mapping` (accepting records the human verdict as a
  `reviewed` lifecycle; rejecting deprecates it and removes the table from
  routing), and `metric_plausibility` (a metric proposed on a table that is not
  an active fact — the shape of the known precision failures). Verdicts on
  these are the first precision labels the metric tier has had.
- **Drift stops a stale aggregate claiming it reconciles.** An aggregate whose
  base table or mapped source columns changed falls back to
  `consistency: unverified` / `routing: advisory` until it reconciles again,
  reported as a "Routing demoted" section in the changeset. The mapping itself
  is never rewritten.
- **Routing no longer drops aggregates.** A fact with two reconciled aggregates
  listed one of them; `repo_knowledge.routing[].use` now carries all of them.

## v0.4.0-beta.1 (2026-09-12)

Spec `0.3.0` (MINOR: one new optional field, one new contract section).

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
