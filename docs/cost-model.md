# Whole-Pipeline Cost Model (measured, M3)

Measured 2026-07-18 on messy_mart (36 tables, 198 columns) with the cheap tier
(claude-haiku-4-5), temperature-pinned, cassette-cached. Warehouse compute
(profiling SQL) is separate and customer-paid; on the fixtures it is seconds
of an XS warehouse.

| Stage | LLM calls | Tokens (in/out) | Cost @ Haiku ($1/$5 per M) | Per 100 tables |
|---|---|---|---|---|
| Profile — typing escalation (~20% of columns, 1 call/table w/ escalations) | 25 | 16.4K / 5.1K | $0.042 | ~$0.12 |
| Link — FK candidate validation (batched 30/call) | 4 | 8.7K / 4.7K | $0.032 | ~$0.09 |
| Describe — 2-pass context propagation (2 calls/table) | 66 | 65.2K / 22.5K | $0.178 | ~$0.50 |
| **Total inference** | **95** | **90K / 32K** | **$0.25** | **~$0.70** |

Against the PRD target of ~$1/100 tables: **met with ~30% headroom, on the
cheap tier alone** — no frontier-model escalation was needed to hit any M1–M3
accuracy target (on messy_mart: typing 0.904, FK F1 1.0, descriptions 0.889
judge-approved; across all 7 fixtures / 759 columns: typing 0.809).

Not included:
- Sonnet description-judging (~$0.10 per 45-item sample) — a development/eval
  cost, not a per-customer inference cost.
- Re-inference: input-hash cassettes mean unchanged tables cost $0 on re-runs;
  drift re-inference bills only the blast radius.
- CQ generation/verification (M5) — will be added to this table when built.

## `--no-sample-egress` is free across the corpus, ~1 point on the worst fixture

Re-measured 2026-10-03 with live calls across 7 fixtures (759 columns), scored
with `semlayer.scoring.score_types` — the same scorer every eval gate reads:

| mode | semantic type | entity role | type, strict |
|---|---|---|---|
| `--no-llm` (zero API calls) | 0.780 | 0.834 | 0.750 |
| default (samples in prompts) | 0.809 | 0.838 | 0.772 |
| `--no-sample-egress` | 0.809 | 0.842 | 0.777 |

Corpus-wide the privacy mode is **free**: typing is identical to three decimal
places and role is marginally better. The last column repeats the comparison
with leniency off (no accepted type equivalences, no role adjacency); it moves
all three modes down together and does not change the ordering. The model itself
buys about three points of typing (0.780 -> 0.809).

Per fixture it is not uniformly free — it trades:

| fixture | columns | default | `--no-sample-egress` | delta |
|---|---|---|---|---|
| collision_heavy | 62 | 0.823 | 0.839 | **+0.016** |
| tpcds_clean | 425 | 0.732 | 0.734 | +0.002 |
| eav / fan_trap / obt / snapshot_noval | 74 | — | — | 0.000 |
| messy_mart | 198 | 0.904 | 0.894 | **-0.010** |

An earlier version of this file claimed "~1 point of typing accuracy
(0.904 -> 0.894)". That number was correct and still reproduces exactly — but it
was messy_mart alone. On the fixture with the most ambiguous names, sample values
genuinely help; on the fixture built to collide names, withholding them helps
more, because the model stops over-trusting values that look like each other.
The two cancel. Quote the corpus number, and do not promise either sign on a
warehouse you have not measured.

**What `--no-sample-egress` actually withholds**, precisely: the typing
escalation drops `top_values` and `range`; Describe drops `sample_values`. No
value read out of a warehouse cell is sent. What is still sent: table and column
names, SQL types, cardinality, null rate, uniqueness, row counts, and — for the
columns where the model itself guessed a code's meaning from the column name —
those guesses echoed back to it. On the fixtures that last case is 6 columns,
all calendar codes on `date_dim` (`1` -> `Monday`), invented by the model rather
than read from the table. `tests/test_no_sample_egress.py` asserts the
containment and the parity, and fails if Describe is ever reordered after Enrich
(which would start sending real decode values in a mode that promises it does
not).

Token counts still change marginally in this mode.
