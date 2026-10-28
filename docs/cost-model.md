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

Against the design target of ~$1/100 tables: **met with ~30% headroom, on the
cheap tier alone** — no frontier-model escalation was needed to hit any M1–M3
accuracy target (on messy_mart: typing 0.904, FK F1 1.0, descriptions 0.889
judge-approved; across all 7 fixtures / 759 columns: typing 0.809).

## Second measurement: dbt_synthea, 116 tables, live (2026-10-27)

The table above is one fixture. A live run of the whole pipeline on the
116-table / 1,312-column `dbt_synthea` warehouse (discovery mode, no declared
keys, `--manifest` supplied), run twice, cost **2.6x the per-100-tables
extrapolation**, and the reason changes how the number should be quoted.

| Stage | LLM calls | Tokens (in/out) | Cost @ Haiku ($1/$5 per M) | Share |
|---|---|---|---|---|
| Profile — typing escalation (104 of 116 tables) | 104 | 110K / 40K | $0.31 | 15% |
| Link — FK validation (2,745 candidates, 30/call) | 92 | 231K / 117K | $0.81 | 38% |
| Describe — 2 passes (116 + 96 calls) | 212 | 400K / 121K | $1.01 | 47% |
| **Total inference** | **408** | **741K / 278K** | **$2.13** | **~$1.84 per 100 tables** |

Per 100 tables is not a constant, because the stages do not scale with tables:

- **Describe scales with columns**: ~305 in / ~93 out per column across both
  passes here, against ~329 / ~114 on messy_mart. Same rate; synthea tables are
  twice as wide (11.3 columns against 5.5).
- **Validation scales with FK candidates**, and candidates rise faster than
  tables as more parents compete for each child: messy_mart had 120 candidates
  over 36 tables (3.3 per table), synthea 2,745 over 116 (23.7 per table).
  Per candidate the rate is the same, ~84 in / ~42 out. This stage was 13% of
  messy_mart's spend and 38% here.
- **Escalation scales with tables** at ~0.9 calls per table, with output
  growing with the columns escalated (~90 tokens each).

So the honest estimate is per unit of work, and that is what the assisted
build script now computes:

```
spend ≈ columns × $0.00077 + tables × $0.0027 + inclusion_tests × $0.000035
```

The last term uses the measured 11.8% of inclusion tests that become
candidates. It reproduces $2.13 on synthea by construction and gives $0.30 on
messy_mart against $0.25 measured. Quote it as [E]; the run reports what it
actually spent.

Two further things the live run showed. Output tokens, not input, are the
larger cost term at Haiku's 5:1 price ratio, and every validation verdict
carries a one-line reason, which is 42% of all output tokens. And the two
runs were not the same run: 3.8% of validation verdicts, 6.7% of escalated
column types and 7 of 116 table types differed between them at temperature 0
(docs/calibration.md, 2026-10-27), so the tokens vary by a few percent too.

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
