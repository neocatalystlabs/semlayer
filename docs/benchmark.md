# Benchmark: text-to-SQL accuracy — raw schema vs. inferred semantic layer

Engine 0.4.0b1; answers execution-scored against gold CQ suites
(fixed questions, human-verified expected SQL, executed live). Reproducible
under pinned engine+model via committed cassettes; re-run with:
`python -m semlayer.benchmark`.

| Fixture | CQs | Answerer | schema_only | semantic | semantic+lint | semantic+ontology |
|---|---|---|---|---|---|---|
| fan_trap | 8 | claude-haiku-4-5 | 0.88 | 0.75 | 0.88 | 0.75 |
| messy_mart | 38 | claude-haiku-4-5 | 0.42 | 0.87 | 0.90 | 0.82 |
| tpcds_clean | 12 | claude-haiku-4-5 | 0.67 | 0.58 | 0.58 | 0.67 |
| messy_mart | 38 | claude-sonnet-5 | 0.55 | 0.84 | 0.84 | — |

## Failure breakdown by category (semantic condition)

| Fixture | Category | Passed/Total |
|---|---|---|
| fan_trap | complex | 0/2 |
| fan_trap | fan_out | 2/2 |
| fan_trap | simple | 4/4 |
| messy_mart | clarification | 1/2 |
| messy_mart | complex | 11/12 |
| messy_mart | current_row_scd_join | 1/1 |
| messy_mart | deprecated_table | 1/2 |
| messy_mart | fan_out | 2/2 |
| messy_mart | missing_required_filter | 1/1 |
| messy_mart | refusal | 2/2 |
| messy_mart | simple | 13/15 |
| messy_mart | wrong_grain_aggregate | 1/1 |
| tpcds_clean | complex | 1/2 |
| tpcds_clean | simple | 6/10 |
| messy_mart | clarification | 1/2 |
| messy_mart | complex | 10/12 |
| messy_mart | current_row_scd_join | 1/1 |
| messy_mart | deprecated_table | 2/2 |
| messy_mart | fan_out | 1/2 |
| messy_mart | missing_required_filter | 1/1 |
| messy_mart | refusal | 1/2 |
| messy_mart | simple | 14/15 |
| messy_mart | wrong_grain_aggregate | 1/1 |

## Notes
- HEADLINE: on the messy enterprise-style warehouse (cryptic names, no declared constraints, hidden business rules), the inferred semantic layer lifts pass-rate 0.42 -> 0.87 (+107% relative) with a Haiku-class answerer, and to 0.89 when the agent also runs the layer's SQL linter (check_sql) before executing. The flagship class of fixed error is SILENT: raw-schema revenue sums cancelled orders ($16.3M vs the correct $14.6M); a fan-out join quietly multiplies a total (caught and repaired by lint on two fixtures).
- METHODOLOGY v2 (this release; applied to EVERY condition): (1) scoring is projection-tolerant — extra/reordered result columns, the scalar in any column of a single row, midnight timestamps as dates, dictionary labels resolved to codes ('Call Center' == CALL); (2) eight messy_mart questions whose wording contradicted their gold SQL were reworded to the gold (MM-5, MM-9, MM-24, MM-25, MM-28, MM-29, MM-32, MM-33; MM-9's gold also lost an unrequested count column); (3) agent SQL is interrupted after 60 s and scored as a failure. Under methodology v1 the same fixture scored 0.34 -> 0.53 (+54%); under v2 with the v0.3 engine it scored 0.42 -> 0.53 before any engine change. The v2 engine gains (0.53 -> 0.87) come from: SCD2 mechanics + grain inferred and rendered, business rules as scoped structured filters (and inherited by child facts when per-key reconciliation proves it), ratio metrics, abbreviation-aware search, relevance-filtered context, and prompt contract changes (substitute deprecated tables, compose derived metrics).
- HONEST NEGATIVE: on the clean, well-named TPC-DS schema, raw DDL still outperforms the semantic condition (0.67 vs 0.58): clean names carry sufficient semantics and the layer's context is ~3x the DDL. Remaining failures there are measure-choice ambiguity ('revenue' has four candidate columns; neither condition is told which) — a glossary/synonym on the inferred metric is the fix, not gold editing. Value concentrates where schemas are messy; consistent with Spider 2.0 / SNAILS literature.
- LINT: lint-on vs lint-off is +1 CQ on messy_mart and +1 on fan_trap, both the silent fan-out class that no execution-error repair can see (valid SQL, wrong number). Zero false positives changed a passing answer on any fixture.
- ONTOLOGY: the deterministic base graph adds no lift over the flat semantic layer on any fixture and now sits below it on messy_mart (context ~2x larger). Reported, not gated; enrichment remains internal (M5 verdict upheld).
- Answerer model is NOT the bottleneck: Sonnet reads raw DDL better than Haiku (0.55 vs 0.42) but lands slightly BELOW Haiku with the layer (0.84 vs 0.87) — the lift for Sonnet is +52% relative, and the two models converge once the context carries the rules. The errors that remain are behavioural (refusing while naming the replacement, answering instead of asking) or gold inconsistencies (whether counts exclude cancelled orders), not reasoning failures.
- Repair rounds: one lint-fed repair (semantic+lint only) then one execution-error repair. Raw-schema failures are valid-SQL-wrong-meaning (silent, unrepairable); semantic-layer residuals are loud or caught by lint.
- Scoring: execution-result equality (Spider-style EA) under methodology v2 above, scalar tolerance 0.01; refusal/clarification CQs scored behaviorally. All contexts built through the same MCP-surface functions agents use; the semantic+lint condition calls check_sql exactly as an agent would.
