# Benchmark: text-to-SQL accuracy — raw schema vs. inferred semantic layer

Engine 0.7.0b2. Every row recorded 2026-11-05 against
`claude-haiku-4-5-20251001` and `claude-sonnet-5`, both pinned. Answers are execution-scored against gold CQ suites (fixed
questions, human-verified expected SQL, executed live).

**This table is a point in time: accurate as of release 0.7.0b2.** It
measures one engine version, one prompt version and one pinned model ID each,
on the date above. The committed cassettes make exactly that run reproducible
with no API key, for as long as the repository exists, with `python -m
semlayer.benchmark` checked out at that release; they are not a claim about
whatever the current model, or the current engine, does. The engine changes
between releases and those changes alter the layer the benchmark embeds in
its prompts, so the numbers are re-measured at a release, not per change
(`tests/test_benchmark_replays.py` runs under `SEMLAYER_RELEASE_CHECK=1`). A newer model generation gets a
new row with its own ID and date. Rows are never silently overwritten: when a
row's number changes, the section at the end of this page says why.

**Re-measured for 0.7.0b2 (2026-11-05); the 0.7.0b1 debt is paid.** The
previous release shipped without re-measuring and said so: 8 Haiku answers
and 76 Sonnet ones had stopped replaying, the replay step was made
non-blocking to avoid holding the MCP-first phase off PyPI, and every row was
to be read as 0.6.0b1 until re-recorded. That has now been done, and the
first thing it established is that the estimate in that paragraph was wrong.

Counting the misses before paying for them — keyless, one question at a time,
because the documented command aborts on the first one and cannot tell you
whether 8 answers are stale or 300 — found **190**, not 84: all 38 messy_mart
questions in all three semantic conditions for Haiku (114), plus the 76 known
Sonnet ones. The cause is broader than the one line in the `METRICS` block
that the earlier note identified. `semantic_context` renders from the
inferred document, the aggregate and routing work changed that document, and
messy_mart is the only fixture here with reconciled aggregates — which is
exactly why `fan_trap` and `tpcds_clean` still replayed clean, and why every
`schema_only` column was untouched.

All 190 were re-recorded against the pinned models, every published row named
explicitly rather than via the Haiku-only default command, and the table
below is what the command now prints. The replay step in `release.yml` is
**blocking again** as of this release: a non-blocking check that nobody
removes is how a published table came to describe an engine two releases old.

| Fixture | CQs | Answerer | schema_only | semantic | semantic+lint | semantic+ontology |
|---|---|---|---|---|---|---|
| fan_trap | 8 | claude-haiku-4-5 | 0.88 | 0.75 | 0.88 | 0.75 |
| messy_mart | 38 | claude-haiku-4-5 | 0.42 | 0.82 | 0.82 | 0.82 |
| tpcds_clean | 12 | claude-haiku-4-5 | 0.67 | 0.58 | 0.58 | 0.58 |
| messy_mart | 38 | claude-sonnet-5 | 0.55 | 0.89 | 0.92 | — |

## Failure breakdown by category (semantic condition)

`claude-haiku-4-5`:

| Fixture | Category | Passed/Total |
|---|---|---|
| fan_trap | complex | 0/2 |
| fan_trap | fan_out | 2/2 |
| fan_trap | simple | 4/4 |
| messy_mart | clarification | 1/2 |
| messy_mart | complex | 9/12 |
| messy_mart | current_row_scd_join | 1/1 |
| messy_mart | deprecated_table | 1/2 |
| messy_mart | fan_out | 2/2 |
| messy_mart | missing_required_filter | 1/1 |
| messy_mart | refusal | 2/2 |
| messy_mart | simple | 13/15 |
| messy_mart | wrong_grain_aggregate | 1/1 |
| tpcds_clean | complex | 1/2 |
| tpcds_clean | simple | 6/10 |

`claude-sonnet-5`, messy_mart:

| Fixture | Category | Passed/Total |
|---|---|---|
| messy_mart | clarification | 1/2 |
| messy_mart | complex | 10/12 |
| messy_mart | current_row_scd_join | 1/1 |
| messy_mart | deprecated_table | 2/2 |
| messy_mart | fan_out | 1/2 |
| messy_mart | missing_required_filter | 1/1 |
| messy_mart | refusal | 2/2 |
| messy_mart | simple | 15/15 |
| messy_mart | wrong_grain_aggregate | 1/1 |

## Notes
- HEADLINE: on the messy enterprise-style warehouse (cryptic names, no declared constraints, hidden business rules), the inferred semantic layer lifts pass-rate 0.42 -> 0.82 (+95% relative) with a Haiku-class answerer, and 0.82 again when the agent also runs the layer's SQL linter (check_sql) before executing -- the linter adds nothing on this fixture at this recording, where on the previous one it added a question. With a Sonnet-class answerer the same layer lifts 0.55 -> 0.89, and there the linter does still add a question, 0.89 -> 0.92. Run-to-run variance on 38 questions is about one question, so the Haiku semantic cell moving 0.89 -> 0.82 (three questions, in `complex` and `simple`) is larger than variance and is reported as a real movement in the dated section below. The flagship class of fixed error is SILENT: raw-schema revenue sums cancelled orders ($16.3M vs the correct $14.6M); a fan-out join quietly multiplies a total (caught and repaired by lint on two fixtures).
- METHODOLOGY v2 (this release; applied to EVERY condition): (1) scoring is projection-tolerant — extra/reordered result columns, the scalar in any column of a single row, midnight timestamps as dates, dictionary labels resolved to codes ('Call Center' == CALL); (2) eight messy_mart questions whose wording contradicted their gold SQL were reworded to the gold (MM-5, MM-9, MM-24, MM-25, MM-28, MM-29, MM-32, MM-33; MM-9's gold also lost an unrequested count column); (3) agent SQL is interrupted after 60 s and scored as a failure. Under methodology v1 the same fixture scored 0.34 -> 0.53 (+54%); under v2 with the v0.3 engine it scored 0.42 -> 0.53 before any engine change. The v2 engine gains (0.53 -> 0.82) come from: SCD2 mechanics + grain inferred and rendered, business rules as scoped structured filters (and inherited by child facts when per-key reconciliation proves it), ratio metrics, abbreviation-aware search, relevance-filtered context, and prompt contract changes (substitute deprecated tables, compose derived metrics).
- THE REWORDED QUESTIONS DO NOT CARRY THE RESULT, and here is the arithmetic rather than the assurance. Methodology v2 reworded eight messy_mart questions to match their gold SQL, which is the sort of change that can manufacture a lift, so: on the **30 questions that were not touched**, the layer lifts pass-rate **0.47 -> 0.80 (+70% relative)**. The eight reworded ones are genuinely favourable to the layer — it passes 7/8 of them against raw schema's 2/8 — so they do flatter the headline, and the headline does not depend on them. Reproduce by dropping MM-5, MM-9, MM-24, MM-25, MM-28, MM-29, MM-32 and MM-33 from fixtures/cqs/messy_mart.yaml and re-running; the cassettes cover the schema_only and semantic conditions.
- HONEST NEGATIVE: on the clean, well-named TPC-DS schema, raw DDL outperforms the semantic condition, **0.67 vs 0.58**: clean names carry sufficient semantics and the layer's context is ~3x the DDL. Remaining failures there are measure-choice ambiguity ('revenue' has four candidate columns; neither condition is told which) — a glossary/synonym on the inferred metric is the fix, not gold editing. Value concentrates where schemas are messy; consistent with Spider 2.0 / SNAILS literature.
- fan_trap REGRESSED under the semantic condition, 0.88 -> 0.75, and it is the one number in this table that moved the wrong way. Both lost CQs are the `complex` category (0/2, was 1/2); semantic+lint recovers the fixture to 0.88. Reported here because an undiscussed regression in a published table is worse than the regression.
- LINT: at this recording lint-on vs lint-off is +1 CQ on fan_trap and +1 on messy_mart with the Sonnet answerer, and **0 on messy_mart with Haiku**, where the previous recording had +1. What lint catches is the silent fan-out class that no execution-error repair can see (valid SQL, wrong number); on this run Haiku's remaining messy_mart failures are not that class, so there is nothing there for it to catch. Zero false positives changed a passing answer on any fixture, which is the property that matters most and has held across every recording.
- ONTOLOGY: the deterministic base graph adds no lift over the flat semantic layer on messy_mart and now scores exactly level with it there, 0.82 vs 0.82 (context ~2x larger; on the previous recording it sat below, 0.79 vs 0.89 -- it has stopped losing, not started winning); on tpcds_clean it scores level with the flat layer (0.58 vs 0.58; on the previous recording it was above, 0.58 vs 0.42, two questions of twelve), which is not evidence for the ontology so much as evidence that the flat layer's context does not help on a clean schema -- the same effect the honest negative describes. Reported, not gated; enrichment remains internal (M5 verdict upheld).
- THE LAYER EQUALISES MODELS, which is the clearest result on this page. With the layer, Haiku lands on **31 of 38** and Sonnet on **34** -- three questions apart. Without it they are 16/38 and 21/38, five apart: Sonnet reads a raw schema materially better, as you would expect, and the layer closes most of that gap but, at this recording, not all of it. The previous recording had them one apart (34 and 33) and this page said a 20x price difference bought nothing once the context carried the rules; on these numbers that claim is too strong, and the defensible version is that the layer moves the cheap model from well behind to close behind. The practical consequence is still a cost argument as much as an accuracy one -- the cheap model goes from unusable to nearly competitive when the layer is present -- but "sufficient" overstates it at this recording, three questions short of Sonnet. (Measured on a third point in October 2026, a newer Haiku generation reached 23/38 raw, so raw-schema reading keeps improving and the *relative* lift keeps compressing -- 0.42->0.82 is +95%, 0.55->0.89 is +62%. The layer's own score stays in a narrow band across all three models, which says the residual failures are not model-limited: they are questions whose answer is not recoverable from a schema at any capability.) — the lift for Sonnet is +62% relative, and the two models move much closer together once the context carries the rules, without meeting. The errors that remain are behavioural (refusing while naming the replacement, answering instead of asking) or gold inconsistencies (whether counts exclude cancelled orders), not reasoning failures.
- Repair rounds: one lint-fed repair (semantic+lint only) then one execution-error repair. Raw-schema failures are valid-SQL-wrong-meaning (silent, unrepairable); semantic-layer residuals are loud or caught by lint.
- Scoring: execution-result equality (Spider-style EA) under methodology v2 above, scalar tolerance 0.01; refusal/clarification CQs scored behaviorally. All contexts built through the same MCP-surface functions agents use; the semantic+lint condition calls check_sql exactly as an agent would.

## Re-recorded and re-measured, 2026-11-05 (release 0.7.0b2)

The debt 0.7.0b1 declared and did not pay. 190 answers were re-recorded
against the pinned models -- not the 84 that release estimated, because the
invalidation was broader than it believed (see the note at the top of this
page).

| | was (0.6.0b1) | now (0.7.0b2) |
|---|---|---|
| `messy_mart` semantic / +lint / +ontology | 0.89 / 0.89 / 0.79 | **0.82 / 0.82 / 0.82** |
| `claude-sonnet-5` semantic / +lint | 0.87 / 0.87 | **0.89 / 0.92** |
| `messy_mart` schema_only, both models | 0.42 / 0.55 | unchanged |
| `fan_trap`, `tpcds_clean` (all conditions) | unchanged | unchanged |

**The Haiku semantic cell went down, by three questions.** Two in `complex`
(11/12 -> 9/12) and one in `simple` (14/15 -> 13/15). That is beyond the
roughly one question of run-to-run variance this page documents, so it is
recorded as a movement rather than as noise. The Sonnet row moved the other
way, +1 on semantic and +2 on semantic+lint, and the ontology column rose
0.79 -> 0.82 to sit exactly level with the flat layer.

What did NOT move is the useful control: every `schema_only` cell is
identical, on both models, and `fan_trap` and `tpcds_clean` replayed byte for
byte without being re-recorded at all. The raw-schema baseline is therefore
the same measurement it was, and the movement is confined to the conditions
that embed the inferred document -- which is consistent with the cause, the
aggregate and routing changes altering what `semantic_context` renders for
the one fixture that has reconciled aggregates.

The derived figures on this page were recomputed from the new recordings
rather than carried over: the untouched-30 arithmetic is now 0.47 -> 0.80
(+70% relative), and the eight reworded questions now pass 7/8 under the
layer rather than 8/8.

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
recur, and the table then read 0.89. (It reads 0.82 today, for the unrelated
reason recorded in the 2026-11-05 section above.)
