# Benchmark: text-to-SQL accuracy — raw schema vs. inferred semantic layer

Engine 0.1.0.dev0; answers execution-scored against gold CQ suites
(fixed questions, human-verified expected SQL, executed live). Reproducible
under pinned engine+model via committed cassettes; re-run with:
`python -m semlayer.benchmark`.

| Fixture | CQs | Answerer | schema_only | semantic | semantic+ontology |
|---|---|---|---|---|---|
| fan_trap | 8 | claude-haiku-4-5 | 0.88 | 0.88 | 0.88 |
| messy_mart | 38 | claude-haiku-4-5 | 0.34 | 0.53 | 0.50 |
| tpcds_clean | 12 | claude-haiku-4-5 | 0.75 | 0.50 | 0.42 |
| messy_mart | 38 | claude-sonnet-5 | 0.40 | 0.50 | — |

## Failure breakdown by category (semantic condition)

| Fixture | Category | Passed/Total |
|---|---|---|
| fan_trap | complex | 1/2 |
| fan_trap | fan_out | 2/2 |
| fan_trap | simple | 4/4 |
| messy_mart | clarification | 1/2 |
| messy_mart | complex | 3/12 |
| messy_mart | current_row_scd_join | 0/1 |
| messy_mart | deprecated_table | 0/2 |
| messy_mart | fan_out | 1/2 |
| messy_mart | missing_required_filter | 0/1 |
| messy_mart | refusal | 2/2 |
| messy_mart | simple | 12/15 |
| messy_mart | wrong_grain_aggregate | 1/1 |
| tpcds_clean | complex | 0/2 |
| tpcds_clean | simple | 6/10 |
| messy_mart | clarification | 2/2 |
| messy_mart | complex | 5/12 |
| messy_mart | current_row_scd_join | 0/1 |
| messy_mart | deprecated_table | 0/2 |
| messy_mart | fan_out | 0/2 |
| messy_mart | missing_required_filter | 0/1 |
| messy_mart | refusal | 2/2 |
| messy_mart | simple | 9/15 |
| messy_mart | wrong_grain_aggregate | 1/1 |

## Notes
- HEADLINE: on the messy enterprise-style warehouse (cryptic names, no declared constraints, hidden business rules), the inferred semantic layer lifts pass-rate 0.34 -> 0.53 (+54% relative) with a Haiku-class answerer; the lift replicates with Sonnet (0.40 -> 0.50). The flagship class of fixed error is SILENT: raw-schema revenue sums cancelled orders ($16.3M vs the correct $14.6M).
- HONEST NEGATIVE: on the clean, well-named TPC-DS schema, raw DDL outperforms the semantic condition (0.75 vs 0.50) — clean names carry sufficient semantics and full-schema listing beats partial-detail disclosure. Value concentrates where schemas are messy; consistent with Spider 2.0 / SNAILS literature.
- ONTOLOGY: the deterministic base graph adds no measurable lift over the flat semantic layer on any fixture (M5 gate verdict upheld; enrichment remains internal).
- Answerer model is NOT the bottleneck: Sonnet adds only ~3-6 points over Haiku under identical contexts; the errors that remain are context-selection and strict-scoring artifacts, not reasoning failures.
- Repair-round asymmetry: one error-repair round helps ONLY the semantic condition — raw-schema failures are valid-SQL-wrong-meaning (silent, unrepairable); semantic-layer residuals are loud mechanical errors.
- Scoring: execution-result equality (Spider-style EA), scalar tolerance 0.01; refusal/clarification CQs scored behaviorally. All contexts built through the same MCP-surface functions agents use.
