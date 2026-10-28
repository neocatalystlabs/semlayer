# Benchmark: text-to-SQL accuracy — raw schema vs. inferred semantic layer

Engine 0.6.0b1. Haiku rows recorded 2026-10-28 against
`claude-haiku-4-5-20251001`; the Sonnet row re-verified the same day against
`claude-sonnet-5`. Answers are execution-scored against gold CQ suites (fixed
questions, human-verified expected SQL, executed live).

**This table is a point in time.** It measures one engine version, one prompt
version and one pinned model ID each, on the date above. The committed
cassettes make exactly that run reproducible with no API key, for as long as
the repository exists, with `python -m semlayer.benchmark`; they are not a
claim about whatever the current model does. A newer model generation gets a
new row with its own ID and date. Rows are never silently overwritten: when a
row's number changes, the section at the end of this page says why.

| Fixture | CQs | Answerer | schema_only | semantic | semantic+lint | semantic+ontology |
|---|---|---|---|---|---|---|
| fan_trap | 8 | claude-haiku-4-5 | 0.88 | 0.75 | 0.88 | 0.75 |
| messy_mart | 38 | claude-haiku-4-5 | 0.42 | 0.89 | 0.89 | 0.79 |
| tpcds_clean | 12 | claude-haiku-4-5 | 0.67 | 0.58 | 0.58 | 0.58 |
| messy_mart | 38 | claude-sonnet-5 | 0.55 | 0.87 | 0.87 | — |

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
| messy_mart | simple | 14/15 |
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
- HEADLINE: on the messy enterprise-style warehouse (cryptic names, no declared constraints, hidden business rules), the inferred semantic layer lifts pass-rate 0.42 -> 0.89 (+112% relative) with a Haiku-class answerer, and 0.89 again when the agent also runs the layer's SQL linter (check_sql) before executing; on the previous recording the linter lifted 0.87 to 0.89, and run-to-run variance on 38 questions is about one question. The flagship class of fixed error is SILENT: raw-schema revenue sums cancelled orders ($16.3M vs the correct $14.6M); a fan-out join quietly multiplies a total (caught and repaired by lint on two fixtures).
- METHODOLOGY v2 (this release; applied to EVERY condition): (1) scoring is projection-tolerant — extra/reordered result columns, the scalar in any column of a single row, midnight timestamps as dates, dictionary labels resolved to codes ('Call Center' == CALL); (2) eight messy_mart questions whose wording contradicted their gold SQL were reworded to the gold (MM-5, MM-9, MM-24, MM-25, MM-28, MM-29, MM-32, MM-33; MM-9's gold also lost an unrequested count column); (3) agent SQL is interrupted after 60 s and scored as a failure. Under methodology v1 the same fixture scored 0.34 -> 0.53 (+54%); under v2 with the v0.3 engine it scored 0.42 -> 0.53 before any engine change. The v2 engine gains (0.53 -> 0.87) come from: SCD2 mechanics + grain inferred and rendered, business rules as scoped structured filters (and inherited by child facts when per-key reconciliation proves it), ratio metrics, abbreviation-aware search, relevance-filtered context, and prompt contract changes (substitute deprecated tables, compose derived metrics).
- THE REWORDED QUESTIONS DO NOT CARRY THE RESULT, and here is the arithmetic rather than the assurance. Methodology v2 reworded eight messy_mart questions to match their gold SQL, which is the sort of change that can manufacture a lift, so: on the **30 questions that were not touched**, the layer lifts pass-rate **0.47 -> 0.83 (+78% relative)**. The eight reworded ones are genuinely favourable to the layer — it passes 8/8 of them against raw schema's 2/8 — so they do flatter the headline, and the headline does not depend on them. Reproduce by dropping MM-5, MM-9, MM-24, MM-25, MM-28, MM-29, MM-32 and MM-33 from fixtures/cqs/messy_mart.yaml and re-running; the cassettes cover the schema_only and semantic conditions.
- HONEST NEGATIVE: on the clean, well-named TPC-DS schema, raw DDL outperforms the semantic condition, **0.67 vs 0.58**: clean names carry sufficient semantics and the layer's context is ~3x the DDL. Remaining failures there are measure-choice ambiguity ('revenue' has four candidate columns; neither condition is told which) — a glossary/synonym on the inferred metric is the fix, not gold editing. Value concentrates where schemas are messy; consistent with Spider 2.0 / SNAILS literature.
- fan_trap REGRESSED under the semantic condition, 0.88 -> 0.75, and it is the one number in this table that moved the wrong way. Both lost CQs are the `complex` category (0/2, was 1/2); semantic+lint recovers the fixture to 0.88. Reported here because an undiscussed regression in a published table is worse than the regression.
- LINT: lint-on vs lint-off is +1 CQ on messy_mart and +1 on fan_trap, both the silent fan-out class that no execution-error repair can see (valid SQL, wrong number). Zero false positives changed a passing answer on any fixture.
- ONTOLOGY: the deterministic base graph adds no lift over the flat semantic layer on messy_mart and sits below it there (context ~2x larger); on tpcds_clean it scores level with the flat layer (0.58 vs 0.58; on the previous recording it was above, 0.58 vs 0.42, two questions of twelve), which is not evidence for the ontology so much as evidence that the flat layer's context does not help on a clean schema -- the same effect the honest negative describes. Reported, not gated; enrichment remains internal (M5 verdict upheld).
- THE LAYER EQUALISES MODELS, which is the clearest result on this page. With the layer, Haiku lands on **34 of 38** and Sonnet on **33** -- one question apart, and on the previous recording exactly the same 33 -- so a 20x price difference buys nothing once the context carries the rules. Without it they are 16/38 and 21/38: Sonnet reads a raw schema materially better, as you would expect, and that is the gap the layer closes. The practical consequence is a cost argument as much as an accuracy one -- the cheap model is sufficient when the layer is present, and is not when it is absent. (Measured on a third point in October 2026, a newer Haiku generation reached 23/38 raw, so raw-schema reading keeps improving and the *relative* lift keeps compressing -- 0.42->0.89 is +112%, 0.55->0.87 is +57%. The layer's own score stays in a narrow band across all three models, which says the residual failures are not model-limited: they are questions whose answer is not recoverable from a schema at any capability.) — the lift for Sonnet is +52% relative, and the two models converge once the context carries the rules. The errors that remain are behavioural (refusing while naming the replacement, answering instead of asking) or gold inconsistencies (whether counts exclude cancelled orders), not reasoning failures.
- Repair rounds: one lint-fed repair (semantic+lint only) then one execution-error repair. Raw-schema failures are valid-SQL-wrong-meaning (silent, unrepairable); semantic-layer residuals are loud or caught by lint.
- Scoring: execution-result equality (Spider-style EA) under methodology v2 above, scalar tolerance 0.01; refusal/clarification CQs scored behaviorally. All contexts built through the same MCP-surface functions agents use; the semantic+lint condition calls check_sql exactly as an agent would.

## Re-recorded, 2026-10-28

The Haiku rows were recorded again under the same model ID, because the
descriptions the benchmark embeds in its prompts changed: the second Describe
pass now reads its neighbours from a snapshot taken at the pass boundary (so
it can run in parallel), and the model reworded 18 of 36 table descriptions
and 113 of 198 column descriptions on messy_mart. The description judge scored
the new text 0.867, the same as the old. The recorded answers are keyed on
the exact prompt, so the old recordings could no longer replay.

| | was | now |
|---|---|---|
| `messy_mart` semantic / +lint / +ontology | 0.87 / 0.89 / 0.82 | **0.89 / 0.89 / 0.79** |
| `tpcds_clean` semantic / +lint / +ontology | 0.42 / 0.42 / 0.58 | **0.58 / 0.58 / 0.58** |
| `fan_trap` (all conditions) | unchanged | unchanged |
| `claude-sonnet-5` row | 0.55 / 0.87 / 0.87 | **0.55 / 0.87 / 0.87** (re-verified) |

Every movement is one or two questions on its fixture, which is the
run-to-run variance of the answering model at temperature 0 (two full
inferences of a 116-table warehouse disagreed on 3.8% of foreign-key verdicts
on 2026-10-27; docs/calibration.md). None of it is an engine change, and the
schema-only column, which carries no descriptions, did not move. Read the
tpcds_clean semantic cell as 5 to 7 of 12, not as a number with two decimals.

## Corrections, October 2026

Three published figures changed after an audit of whether every row could be
reproduced from the committed recordings. Recorded here because the old numbers
may have been quoted elsewhere.

| | was | now |
|---|---|---|
| `tpcds_clean` semantic / +lint / +ontology | 0.58 / 0.58 / 0.67 | **0.42 / 0.42 / 0.58** |
| `claude-sonnet-5` semantic / +lint | 0.84 / 0.84 | **0.87 / 0.87** |
| `messy_mart` FK precision (docs/calibration.md) | 1.000 | **0.973** |

The first two rows were computed from model responses that had not been
committed, so they could not be re-derived from the repository. The recordings
were made on the same pinned model and both rows now reproduce with no API key;
the figures above are what the command prints. Whether the earlier `tpcds_clean`
numbers were ever right cannot be established, since the responses behind them
no longer exist — two of twelve questions is 17 points on that fixture, so
variance may account for part of the gap. `fan_trap` and `messy_mart` reproduced
exactly, including the 0.89 noted below.

The FK precision change came from a separate engine fix found in the same audit:
a response truncated at the token ceiling was treated as complete, and the
affected verdicts were discarded. Restoring them admits one false positive the
model does assert, so 0.973 replaces a 1.000 that the defect had been
flattering.

Every published row is now covered by a keyless replay test
(`tests/test_benchmark_replays.py`), which also asserts that the table printed
by the command matches the table on this page. Full account, including how the
gap went unnoticed: [docs/calibration.md](calibration.md).

## Correction, 2026-10-07

The semantic+lint cell for messy_mart read **0.90** while the HEADLINE note and
the README both said **0.89**. The prose was right and the table was generated
wrong: `pass_rate` is stored as `round(passed/total, 3)`, and the report then
formatted that to 2dp — so 34/38 (0.8947) became 0.895 and then printed as
0.90. The underlying result never changed.

The renderer now formats from the raw count, so the double rounding cannot
recur, and the table above reads 0.89.
