# Confidence calibration report

SPEC §3 rule 5: *"`confidence` MUST be calibration-tested by the producing engine;
a document's confidence values are meaningless without a published calibration
report."* This is that report. Until it existed, semlayer was in breach of its own
spec — it shipped confidence numbers that had never been measured against anything.

Engine version: 0.4.0b1. Measured 2026-09-16.

## What the number means

A column's `confidence` is the probability that **the whole column element is
right** — both `semantic_type` and `entity_role`. Not "the type is probably right."
If we say 0.7 and the type is right but the role is wrong, that counts as a miss.

This definition is deliberate and it is the conservative reading. It matters
because our rules are not uniformly good at both halves. `integer fallback` gets
the type right 97% of the time and the role right 13% of the time. `N distinct
values` is the mirror image: type 20%, role 100%. A single scalar cannot be
honest about both, so it reports the joint.

That is a limitation of the format, not of the measurement, and it is the main
argument for replacing the scalar with per-facet bands. See *Known gaps*.

## Method

- **Corpus**: 785 gold-typed columns across the 9 fixtures in `fixtures/golds/`.
  These are synthetic marts built to look like real enterprise schemas (dirty
  names, EAV, fan traps, snapshots, a TPC-DS-shaped warehouse), not a sample of
  any customer's warehouse. Numbers here transfer to the extent your schema looks
  like these.
- **Grading**: exact match on `semantic_type` and `entity_role` against gold.
  Tables below report the strict grade. The scorer's lenient mode (which forgives
  `code`↔`enum`, `date`↔`timestamp_event`, and similar) is *not* used to set any
  published constant, because a consumer that acts on the type cares about the
  distinction the lenient mode forgives.
- **Shrinkage**: each calibrated constant is the measured joint hit-rate shrunk
  toward 0.5 with a pseudo-count of 2, so a rule that fired 3 times cannot claim
  1.00, then rounded to the nearest 0.05.
- **ECE**: expected calibration error, bucketing by reported confidence and
  comparing to the bucket midpoint. Lower is better; 0 means the number means
  what it says.

Reproduce:

```
uv run python ../prd/confidence-calibration-run.py        # heuristic tier
uv run python ../prd/confidence-calibration-run.py --llm  # LLM tier (cassette replay)
```

## Results: heuristic tier (no LLM)

Accuracy across the corpus: type 0.786, role 0.811, PK recall 0.654.

Element-level reliability, statistical mechanism — reported confidence vs.
measured hit-rate:

| reported | n | hit-rate |
|---|---|---|
| 0.05 | 37 | 0.03 |
| 0.15 | 23 | 0.17 |
| 0.25 | 40 | 0.17 |
| 0.40 | 3  | 0.33 |
| 0.70 | 26 | 0.73 |
| 0.85 | 51 | 0.88 |
| 0.90 | 6  | 0.83 |

Monotone and close to the diagonal. Element ECE **0.093** (was 0.130 before this
pass). The 0.90 row is the only inversion and it is 6 columns from rules that
fire too rarely to calibrate.

The naming mechanism is **not yet calibrated** and shows it:

| reported | n | hit-rate |
|---|---|---|
| 0.6 | 85  | 0.71 |
| 0.7 | 234 | 0.66 |
| 0.8 | 371 | 0.93 |

Reported 0.7 is *less* reliable than reported 0.6. Two rules drive it, both
badly over-scored (strict joint accuracy in parentheses): `name rule -> code`
claims 0.75 (0.06) and `name rule -> pii_address` claims 0.75 (0.06). Calibrating
the naming tier is the next pass; it is a larger diff because those constants
live in a name-rule table with many entries.

Foreign keys are **under**-confident: every FK bucket hits 1.00 while reporting
0.7–0.8, ECE 0.212. An FK backed by both naming and an inclusion dependency has
not been wrong once on this corpus. That is a real signal we are hiding.

## Results: LLM tier

Covers 759 of the 785 columns. Two fixtures (`multi_tenant`, `self_ref`) have no
cassette for the current prompt and are skipped; `tpcds_clean` was re-recorded
against the live model for this report.

Element ECE **0.195** — worse than the heuristic tier, and worse than the 0.128
an earlier draft of this report published when `tpcds_clean` was missing. The
easy fixtures were flattering the number. That is the main reason this section
is now measured on the full corpus.

Where the escalation lands, split by what the column's confidence is claiming:

| mechanism | reported | n | type | role | element |
|---|---|---|---|---|---|
| `statistic+llm` | 0.8 | 116 | 0.61 | 0.85 | 0.53 |
| `naming+llm` | 0.8 | 41 | 0.54 | 0.71 | 0.32 |

These are the model's own self-reported confidences, passed through untouched.
The split matters: the LLM's **entity-role** calls are well calibrated (role ECE
0.060) while its **semantic-type** calls are over-confident by roughly twenty
points (type ECE 0.159). Escalated columns claiming 0.8 get the type right about
three times in five. A consumer that trusts an escalated type at face value is
being over-served, and the aggregate element number hides which half is at fault.

FK remains the tier's strength: ECE 0.120, and every FK bucket still hits 1.00.

Not fixed here. Passing the model's self-report through unadjusted is the same
class of error this pass removed from the rule tier — an unmeasured number
presented as a measured one — and it now has a measurement.

## Results: table type

A table's `confidence` answers one question — **how sure are we of the
`table_type` label**. It is not a quality score. A high number on a staging
table means "confidently staging", which is a table you should not query.
"Which table should I use" is answered by `lifecycle` (`certified` > `reviewed`
> `inferred`, plus `deprecated`/`orphaned`) and `repo_knowledge.routing`, not by
this number.

Until this pass the value was computed and then discarded by `link/run.py`, so
the document asserted a table type with no caveat at all. It is now calibrated
the same way as the column tiers and emitted, and `get_tables` surfaces it as
`type_confidence`.

| reported | n | picks gold type |
|---|---|---|
| 0.05 | 12 | 0.00 |
| 0.10 | 14 | 0.00 |
| 0.35 | 4  | 0.25 |
| 0.50 | 4  | 0.50 |
| 0.65 | 4  | 0.75 |
| 0.75 | 19 | 0.79 |
| 0.95 | 33 | 1.00 |

ECE **0.084**, down from 0.228. Before this pass the bottom of the range was
pure fiction: a rule reporting 0.55 ("measure-heavy, few FKs resolved") was
right 0 times out of 6, and the 0.3 and 0.4 fallbacks were right 0 of 20.

Six rules fired 3 times or fewer and are **left uncalibrated** at their original
values rather than fitted to a handful of observations: staging naming (0.85),
aggregate naming (0.75), ops naming (0.70), validity-window columns (0.80),
denormalized-by-column-count (0.60), and the fact rule for a table nothing
references (0.65). Each measured 1.00 on its few cases, so they are most likely
under-confident. They account for most of the residual ECE.

## What changed in this pass

1. **Removed `unique numeric`.** A numeric column with high cardinality and
   uniqueness was typed `identifier`/`primary_key` at confidence 0.6. Measured:
   0 correct out of 15, on both type and role. Because id-named columns are
   already caught by an earlier rule, this one fired precisely on the non-id
   numerics — which are measures. It was wrong by construction and it shadowed
   the decimal and integer fallbacks that handle those columns correctly.
2. **Removed `_rule_weak_id`.** Columns whose names end in `_no`/`_nbr`/`_number`
   were typed as identifiers at 0.6. Measured: 1 correct out of 13. Most such
   columns are text (phone numbers, order numbers) that gold types as `free_text`.
3. **Recalibrated 7 statistical constants** to measured joint hit-rate, per the
   method above.
4. **Decoupled escalation from the trust number.** `decimal fallback` calibrates
   to 0.7, which is genuinely how often it is right — but it lands exactly on
   `ESCALATE_BELOW`, so honest calibration would have switched off the LLM pass
   that fixes its known failure (it calls every bare decimal `monetary_value`, so
   weights and ratios come out as money). Trust and routing are different
   questions. That rule now escalates regardless of confidence.

Removing the two dead rules **improved** accuracy — type 0.775 → 0.786, role
0.789 → 0.811, PK recall unchanged — because their columns fell through to rules
that were already better.

## Known gaps

- **One scalar, two facts.** Documented above. Per-facet confidence (or a
  structured basis with per-element bands) is the fix; it is a format change.
- **Naming tier uncalibrated.** Numbers above; next pass.
- **FK under-confident.** Raising it is a reviewed diff, not a free win: FK
  confidence feeds the review queue.
- **Metrics are constants.** All metric confidences are one of three hard-coded
  values (0.6, 0.7, 0.75) assigned by code path. They are uncalibrated and should
  be read as provenance markers rather than probabilities. They are not, however,
  unmeasurable: metric quality is downstream of two classifiers that *do* have
  gold labels — table type and column role — and the errors chain. A timezone
  offset typed `measure` instead of `dimension` trips the "two FKs plus a
  measure means fact" rule, turning a dimension table into a fact, which
  manufactures metrics that sum timezone offsets at confidence 0.70. The honest
  statement is that we have not labeled metric precision, not that we cannot.
- **Table type is 0.647.** De-duplicated across the corpus, table-type
  classification picks the gold type 66 of 102 times. Its confidence is now
  calibrated and emitted (see below), but the underlying accuracy is unchanged
  and is the weakest classifier we ship.
- **Tables and required filters carry no `confidence` at all.** Per SPEC §1,
  absent confidence means *human-authored*. A conforming consumer therefore reads
  our most heavily inferred content as hand-written. For tables this is a
  one-line producer fix (the value is computed, then dropped); it is not a
  measurement problem.
- **Corpus is synthetic.** 9 fixtures, one shape of "enterprise-looking". Real
  warehouses will move these numbers.

## Queued rule fixes the data points at

Measured confusions that are specific enough to fix, each its own reviewed diff:

- Numeric, unique, name ends in `_number`/`_no`/`_nbr` → `primary_key`
  (6 columns, e.g. `catalog_returns.cr_order_number`, currently typed as measures).
- `n_distinct <= 2` → `flag`, not `enum` (8 columns, e.g. `date_dim.d_holiday`).
- `small int domain` → `quantity`, not `code`, when values are ordinal
  (19 columns, e.g. `reviews.rating`, `shoes.shoe_size`).
- `integer fallback` role: 21 columns gold-typed `dimension` come out `measure`
  (e.g. `date_dim.wk_of_yr`).
