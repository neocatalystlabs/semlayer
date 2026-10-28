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
uv run python fixtures/coverage_audit.py                 # what has data behind it
# the calibration runner itself is not shipped; the numbers below are its output
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

## Results: table type filled by the LLM

When the heuristic rules reach no decisive signal, the describe stage lets the
LLM fill the `table_type` (it is not allowed to *override* a decided heuristic —
that was measured in 2026-07 to reduce accuracy). Measured against gold, that
fill is right **7 of 10** times.

Until this pass those tables kept the heuristic's `unknown` confidence of 0.05
and its `no decisive signal` provenance, so the document asserted a type at the
confidence of the verdict it had just replaced, citing a rule that did not
produce it. A 70%-accurate claim was published as 5% confident.

They now carry **0.65** (7/10 shrunk toward 0.5 with a pseudo-count of 2) and a
`signal: llm` provenance entry naming the fill.

The failure mode is consistent and worth knowing: all three misses assign
`operational` to tables gold calls `fact` (`collision_heavy.shipments`,
`collision_heavy.tickets`, `messy_mart.web_evt`) — the same over-assignment of
`operational` to minimal tables that disqualified the LLM from overriding
decided heuristics.

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
- **THERE IS NO SAMPLING PATH. Every profile is a full scan.**
  `SAMPLE_THRESHOLD = 1_000_000` is defined in `profile/stats.py` and used
  **nowhere**; the module docstring says "sampling uses TABLESAMPLE where row
  counts are large" and no `TABLESAMPLE` appears anywhere in the source. So
  every column of every table is scanned in full, every run. On the fixture
  corpus that is invisible -- the largest table is 11.7M rows at TPC-DS SF 1,
  42 s to profile -- and on a multi-terabyte warehouse it is disqualifying.
  A published video states the threshold behaviour on camera: "Under a
  million rows it reads the whole column. Above that it samples." That is a
  public claim about a
  feature that does not exist. Its next sentence — uniqueness "always does in
  full" — is true today, because everything does. Earlier revisions of this
  entry misattributed it to video 3, having taken that from a superseded
  planning document rather than from the scripts.

  An earlier version of this entry claimed the sampling path had been measured
  and cost 0.024 of role accuracy. That was wrong: the SF 0.01 vs SF 1
  difference is entirely more rows in small dimension tables, not sampling
  versus full-scan. The part of it that holds:

- **The CI scale factor flatters us.**
  `tpcds_clean` is generated at scale factor 0.01 for CI speed, where **six
  TPC-DS tables hold exactly one row** (`call_center`, `reason`, `store`,
  `warehouse`, `web_page`, `web_site`). A one-row table makes every column
  trivially unique and non-null, so key detection has partly been scored
  against tables with nothing in them. Regenerated at SF 1 — 19,557,579 rows,
  four tables above the (unimplemented) 1,000,000-row threshold, 42 s —
  type accuracy is unchanged at 0.727 and PK precision/recall unchanged at
  1.000/0.531, while role accuracy falls 0.845 to 0.821. Every one of the 11
  columns that moved was already wrong at both scales: `store.s_store_id` is
  unique when the table has 1 row and repeats when it has 12 (SCD2 versioning,
  6 stores x 2 versions), so the role flips from `unique_key` to
  `foreign_key`, and gold says `natural_key`. Nothing was degraded by scale
  itself; the one-row tables were doing the damage.

- **`natural_key` is now emitted — 25 of 25 on `tpcds_clean`, 4 of 6 on
  `messy_mart`.** It previously came out never. The role rule is "unique ->
  key, otherwise foreign key", so a dimension's business key was `unique_key`
  when the table happened to be small enough for it to be unique and
  `foreign_key` when it was not — the answer depended on the row count.
  Enrich now marks it from the star-schema signal: the table has a surrogate
  primary key, the column is identifier-named and is not that key, and Link
  found no relationship for it (a foreign key points somewhere). Gated on a
  surrogate PK rather than on `table_type`, because `table_type` is wrong too
  (see below).

  **Corrected 2026-10-12.** This entry originally reported the gain as +0.010
  (messy_mart) and +0.009 (tpcds_clean) and called it invisible to the scorer.
  Those are the LENIENT numbers, and lenient role accuracy credits
  primary/unique/natural key confusions as adjacent — so it had been crediting
  the very confusion this change removed. `scoring.py` now also reports
  `role_accuracy_strict`, and measured against outputs from before the change:

  | tpcds_clean | lenient | strict |
  |---|---|---|
  | before | 0.845 | **0.800** |
  | after | 0.854 | **0.854** |

  So the real improvement is **+0.054 strict**, six times what the lenient
  metric showed. The leniency was reasonable while nothing emitted
  `natural_key`; it became misleading the moment something did. Both numbers
  are now reported, so no published figure moves.

  Two false positives per fixture. On `tpcds_clean` both (`cc_closed_date_sk`,
  `web_close_date_sk`) are genuine foreign keys that **Link failed to
  discover**, so this rule inherits Link's recall: "no relationship found"
  is only as good as Link is.

- **`table_type` accuracy, scored for the first time: 0.72 corpus-wide**
  (73 of 102), up from 0.65. Per fixture it ranges from 0.46
  (`collision_heavy`) to 0.89 (`messy_mart`) and 0.79 (`tpcds_clean`, up from
  0.50). An ungated fan-in rule reaches 0.74 but relabels a parent fact as a
  dimension, so it is guarded on table shape; the 0.02 is the price of not
  regressing the one fixture built to test that shape. On the one real warehouse we hold, AdventureWorksDW, it is 0.92 (up
  from 0.58) scored against the vendor's own Dim/Fact table-name prefixes.
  `scoring.py` still does not include it — these numbers come from
  `fixtures/coverage_audit.py`.

  The spread tracks naming, not difficulty: `messy_mart` has 69% of its tables
  name-signalled (`_dim`, `_ref`, `_mstr`) and scores 0.89; `collision_heavy`
  has 0% and scores 0.46. Treat a high table_type score as partly a statement
  about the warehouse's naming discipline.

  The 27 remaining errors are mostly `dimension -> unknown|operational` on
  tables where **Link found no relationships at all** (`fk_in == 0`, 20 of 36
  before the fix). Those are bounded by Link's recall, not by this classifier:
  with no edges, there is no fan-in to read.

- **The numeric fallback is the engine's last resort, and it has one
  statistic.** Six name rules run before it; a column only reaches it when we
  do not recognise its name, and then the only signal available is how many
  distinct values it has. `tot_amt` never gets there (the name says money);
  `s_floor_space` does, because "floor_space" is not in any vocabulary we
  ship. Accuracy in that population is therefore a measure of how complete our
  naming vocabularies are, which is why adding one abbreviation (`cntry`)
  moved three columns. Expect real warehouses to land in this fallback far
  more often than our fixtures do.

- **Key discovery WAS name-gated; it is now statistics-first.** Kept because
  the shape of the failure is worth remembering: for several releases the
  statistics were never consulted for a column outside our naming vocabulary.
  Parents are now any unique, populated, key-capable column, and children any
  non-unique dimension of a key-capable type. CMS went from 0 relationships to
  1 correct one; every other warehouse is unchanged. Two guards were needed --
  collapsing alternate-key paths while keeping role-playing, and refusing
  dates and names as key references -- and both were found by a measurement
  contradicting the design, not by foresight. THE ORIGINAL FINDING: The architecture is
  described as statistics, then naming, then LLM validation. In practice naming
  decides whether a column is ever *considered*, and the statistics only
  confirm what naming proposed. `link/candidates.py` builds its parent and
  child lists from `_KEYISH_RE`
  (`(_id|_key|_sk|_cd|_code|_no|_nbr|_number)$|^id$`) plus the entity roles that
  themselves came from naming rules.

  Measured on CMS's Medicare Physician files: `Rndrng_NPI` is the National
  Provider Identifier, unique across 30 summary rows, non-unique across 179
  detail rows, with **100% inclusion** — a textbook foreign key. It ends in
  `_npi`, matches nothing, is typed `unknown`/`dimension` at confidence 0.05,
  never becomes a candidate, and Link returns **0 relationships** on a
  two-table warehouse. The uniqueness and inclusion statistics are never
  consulted.

- **Reconciliation requires an ADDITIVE measure in the detail, which real
  published data often lacks.** The engine expresses one mapping,
  `SUM(fact_measure)`, over fact measures typed `monetary_value`. CMS's detail
  table carries averages (`Avg_Mdcr_Pymt_Amt`) rather than sums, so its
  official per-provider totals reconcile as `SUM(avg * Tot_Srvcs)` — a product
  the engine cannot express. Measured: a plain SUM matches **0 of 387**
  providers, the weighted form matches 15 exactly and 73 within 2% (the
  residual being the suppression rule). So the summary is now examined and
  attempted, and correctly finds nothing.

  Worth separating from a related non-finding. CMS and FEC both have the
  *detail* as the lossy side -- privacy suppression and itemisation thresholds
  -- which is the reverse of what reconciliation assumes. That is an artifact
  of PUBLISHING, not of warehouse design: inside a company the detail is the
  source of truth and ETL builds filtered rollups from it, which is the shape
  the mechanism targets and `messy_mart` models. The conclusion is about the
  datasets, not the design: **public data is a poor proxy for rule recovery
  specifically**, because publication removes the feature being tested.

- **Rule recovery NO LONGER requires a table NAMED like an aggregate.** Now
  decided by shape: measures present, group columns that map onto a fact, and
  strictly fewer rows than that fact. The name is kept as confidence --
  shape-only identifications are recorded 0.1 lower with their own provenance
  line -- rather than as a gate. `messy_mart` still finds both summaries with
  the cancelled-orders filter; `tpcds_clean`, `collision_heavy` and
  AdventureWorksDW still find none. THE ORIGINAL FINDING:
  `_detect_aggregates` skips any table whose name does not match
  `(agg|summary|rollup)` or start `dly_`/`mth_`/`wkly_`, unless it is already
  typed `aggregate`. So the capability we describe as central — recovering an
  undocumented business rule by reconciling a summary against its detail — is
  gated on the summary table being named the way we named ours
  (`dly_sls_agg`, `mth_cust_agg`).

  CMS publishes exactly the required shape: `mup_phy_prvdr` (their official
  per-provider totals) over `mup_phy_prvdr_svc` (per provider x HCPCS x place),
  and the two do not reconcile because service lines covering fewer than 11
  beneficiaries are suppressed from the detail while still counting in the
  totals. Verified by hand: 27 of 28 sampled providers show a positive gap, the
  gap is never negative, and `min(Tot_Benes)` in the detail is exactly 11. The
  engine reports no aggregate table and recovers nothing, because the file is
  called `Prov` rather than `prov_summary`.

- **Five instances, one root cause.** PascalCase key columns, PascalCase table
  names, the `_cd` code suffix, domain identifiers like NPI, and aggregate
  table naming all failed the same way: the vocabulary was drawn from fixtures
  we wrote ourselves. Every one was invisible until a schema we did not author
  was pointed at the engine. Treat any naming-dependent accuracy number as an
  upper bound that measures our vocabulary's coverage of OUR corpus.

- **Level hierarchies: 9 emitted, 4 of 8 gold chains matched — and gold is the
  weaker side of that number.** `kind: level` was the one hierarchy kind
  nothing produced. The detector orders levels by the semantic family where it
  knows one and by cardinality otherwise, then verifies each adjacent pair's
  functional dependency with a 5% tolerance (messy_mart's
  `postal_cd -> state_cd` is 2% violated by design).

  All 9 emitted chains are real. The four unmatched gold chains each have a
  recorded reason, and only one is a detector shortfall:

  | gold chain | why unmatched |
  |---|---|
  | `fan_trap.customers: region` | a single level; not a chain by construction |
  | `tpcds customer_address: country > state > county > city` | **the data does not contain it.** `ca_city -> ca_county` is 30% clean at SF1 on an unversioned table — `dsdgen` assigns these independently. The detector correctly refuses it |
  | `tpcds store: s_country > ...` | 12 rows, 1-2 distinct values. Degenerate |
  | `tpcds item: i_category > i_class > i_item_id` | real but needs SCD scoping, which never fires (below) |

  **Gold under-asserts this capability.** messy_mart contains six real
  geographic and product hierarchies — on `cust_addr`, `cust_mstr`, `whs_dim`,
  `prod_ref`, `store_dim` and `cust_mstr_legacy` — and gold declares one. So
  "agreement with gold" is currently the wrong scoring target here, and tuning
  precision further against it would be tuning against a flawed reference.

- **SCD detection never fires on TPC-DS naming.** `_SCD_FROM`/`_SCD_TO` are
  EXACT-match lists (`eff_start_dt`, `valid_from`, `start_dt`, ...) and
  `item` carries `i_rec_start_date`/`i_rec_end_date`. So `item` is typed
  `dimension` with no `scd` block, version scoping cannot apply, and
  `i_item_id -> i_class` — **34% clean across history, 100% clean within the
  current version** — is refused on the evidence available. Seventh instance
  of the naming-vocabulary root cause; needs suffix matching rather than exact
  names.

- **Composite primary keys: 8 of 9 detected, up from 0.** PK detection picked
  the best single unique identifier-named column, so a table keyed on a column
  PAIR got no `primary_key` at all — which is every line-item fact in TPC-DS.
  `store_sales` is keyed on `(ss_item_sk, ss_ticket_number)`: 180 distinct
  items and 2,400 distinct tickets over 28,810 rows, so neither column is
  remotely unique alone.

  Candidacy is **statistical, not role-based**. Gating on `entity_role`
  excluded `ss_ticket_number` — typed `quantity`/`measure` for ending in
  `_number`, while being half of a key unique over every row, and gold itself
  calls it `quantity`/`primary_key`. Among several unique tuples the tightest
  wins (smallest cardinality product), because taking the first found picked
  `(cr_returned_time_sk, cr_returned_date_sk)` on `catalog_returns` — unique,
  and a coincidence of two timestamps.

  `inventory` gets 2 of its 3 gold columns because `(inv_date_sk, inv_item_sk)`
  genuinely IS unique at the CI scale factor. Correct minimality on this data,
  wrong for real TPC-DS: the third capability the small scale factor distorts.

  `_count_metric` is now skipped for compound keys. The caller passes
  `primary_key[0]`, and one member of a compound key is not the grain —
  `COUNT(DISTINCT ss_item_sk)` is 180 against 28,810 rows — and the metric also
  granted that column a `count_distinct` affordance answering a different
  question. Composite-key row counts need a tuple-aware metric the format has
  no vocabulary for.

- **Gold under-asserts, in four separate places now.** Level hierarchies
  (messy_mart has six real geo/product chains, gold declares one), the
  geography chains on `cust_addr`/`cust_mstr`/`whs_dim`, composite FOREIGN keys
  (9 composite PKs declared, zero references to them, while `store_returns ->
  store_sales` and two siblings hold at inclusion 1.000), and now composite PKs
  on the aggregate tables — `dly_sls_agg = (agg_dt_key, store_id)` and
  `mth_cust_agg = (cust_id, yr_mth)` are exactly the grains gold declares under
  `aggregate_tables` without declaring them as keys. Treat "agreement with
  gold" on these capabilities as a floor, not a ceiling.

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

## PostgreSQL connector: what the live server showed (2026-10-12)

Built against a real PostgreSQL 16.2 rather than a mock, because `pgserver`
bundles the binary and the setup cost was an afternoon. Three of these findings
would not have surfaced from a mocked connector.

**Comments are not in `information_schema`.** Postgres keeps them in
`pg_description`, reachable through `obj_description` / `col_description`. A
connector written by analogy to the DuckDB one — which reads
`duckdb_tables()` / `duckdb_columns()` — or to the Snowflake one would have
returned `{}` here and silently lost the catalog priors. Both test comments are
now read and land as `catalog_prior` provenance.

**`pg_stats` needs no extra grant.** It filters its rows to tables the caller
can already `SELECT`, so the minimal-grant reader sees all 14 columns'
statistics. `pg_class.reltuples` is readable by any role. This is the Postgres
equivalent of the Oracle data-dictionary finding: row counts, distinct counts
and null rates are available **without scanning**, at the minimal grant, which
is the input A1 (sampling) needs. We do not use them yet — recorded, not built.

**`n_distinct` is negative when it is a ratio.** `-1.0` means distinct in every
row (a key); `-0.3` means 0.3 × the row count. Reading it as a count would make
every unique column look like it held one value. Asserted in the test so a
future sampling path cannot get it wrong quietly.

**Minimal-grant claims are tested, not asserted.** `tests/test_postgres.py`
builds a role from the statements `semlayer init postgres` actually emits, then
proves inference succeeds under it and that INSERT, UPDATE, DELETE, CREATE (in
both `mart` and `public`) and DROP are all refused. This is the first
reader-cannot-write proof that runs in ordinary CI — the Snowflake and BigQuery
equivalents need someone's account.

**Honest result on a 3-table toy.** The connector found the composite primary
key `(ord_id, ln_num)`, both foreign keys, and the
`cntry_cd → state_cd → city_nm` level hierarchy. It classified `ord_hdr` as
`table_type: unknown` — "no decisive signal" — where a human would say fact. It
has one measure and five columns, and the dimension rule added for
AdventureWorks (`fk_in > fk_out` unless few columns with a measure) leaves it
undecided rather than wrong. Undecided is the better failure here, but it is a
failure, and it is the same table-type weakness already recorded above at 0.72
corpus-wide. Three tables is too small to move that number; noted so it is not
mistaken for a pass.

## FK recall 26% on a schema whose own ETL declares the answer (2026-10-12)

`dbt_synthea` (OHDSI, Apache-2.0) is the first dataset we hold that ships the
dbt project which **built** it, so its `manifest.json` declares 235 foreign-key
edges that humans wrote down. Measured in deterministic mode: the engine
recovers **61 of 235 = 26.0%** (42/153 unconditional, 19/82 conditional),
emitting 249 relationships over 116 tables in 2m43s.

**The 26% is mostly not missed joins. It is the engine picking a different
parent from the one the humans declared, and the cause is specific:**

`person` and `int__person` hold **identical** 27-row `person_id` sets, both
materialised as tables, both with `person_id` as a detected primary key. All 27
`person_id` edges were pointed at `int__person`. The project's own tests
declare `person`, and the manifest says `person` is built FROM `int__person` --
it is the published table, the other is an intermediate step.

No statistic can separate them. Uniqueness is equal, inclusion holds in both
directions, the names are equally plausible. The DAG separates them trivially.
The same pattern accounts for 0/12 edges into `stg_synthea__encounters`, 0/10
into `provider`, 0/9 into `visit_detail` and 0/9 into `visit_occurrence`, while
`stg_synthea__patients` -- which has no competing intermediate -- scores 93%.

This is the clearest evidence we have that lineage reading is load-bearing
rather than a nice-to-have, and it was invisible until a fixture shipped its
own ETL. It also means our FK precision figures on fixtures we authored are
flattering: we never built a warehouse containing a model and its own
intermediate, so we never faced the choice.

**How far this specific shape generalises -- scoped after Raj pushed back on
it.** dbt_synthea's form is non-idiomatic and should be read as a STYLISED
illustration: dbt convention is that `int__*` models are ephemeral, i.e. CTEs
rather than queryable tables, and this project materialises them as tables.
But the underlying situation is common. Counted on Cal-ITP, 624 real production
models: **only 6 are ephemeral** (294 table, 262 view, 62 incremental), there
are 174 `int_`-prefixed models essentially all of which are queryable, and
**162 models are the sole child of a single materialised parent** -- 162 places
where an intermediate and its near-copy downstream are both queryable and both
are candidate join targets.

What is NOT established [GAP]: how often the two hold *identical* key sets.
Cal-ITP's chains are named `..._deduped` -> `..._to_aggregations`, implying the
row sets differ, and we cannot query their warehouse to check. Note the
ambiguity does not require identical keys to bite: whenever two queryable
relations both contain the key and inclusion holds, the FK logic has two valid
parents and no basis for preferring the published one.

**Honest counterweight on how far this generalises.** Declared FKs are
plentiful here because OMOP CDM is a *published* data model. Counted the same
day on projects that are not implementing a published spec: Cal-ITP has 19
enabled `relationships` tests across 624 models, Dune Spellbook roughly 1
across 7,639, Tuva Health 0 across 353, and every Fivetran package 0. So
reading declared tests helps the minority of warehouses built to a standard
model; for the homegrown enterprise warehouse the joins have to be parsed out
of the SQL instead. Both paths are needed and they are not the same work.

**Parse feasibility, measured rather than assumed.** sqlglot parsed 87/87 of
dbt_synthea's compiled models (127 join equalities) and 624/624 of Cal-ITP's
production BigQuery models (778 join equalities, 211 window functions, 617
CTEs, 78 UNIONs). Parsing is not the bottleneck; attribution is.

## ETL lineage lifts FK recall 26.0% -> 41.7%, and two wrong turns on the way (2026-10-12)

The manifest reader's first scope: use the ETL DAG to settle which table a
foreign key points at. Measured on `dbt_synthea`, deterministic mode:

| parent | without manifest | with `--manifest` |
|---|---|---|
| `person` | 0/17 | **12/17** |
| `provider` | 0/10 | **7/10** |
| `visit_occurrence` | 0/9 | **7/9** |
| `concept` | 29/131 | 29/131 |
| **overall** | **61/235 = 26.0%** | **98/235 = 41.7%** |

Without a manifest the output is byte-identical to the old baseline -- 26.0%
and 249 relationships -- which is deliberate and tested.

**Wrong turn 1: "downstream" is not "the published entity table."** The first
rule was simply "if the ETL builds A from B, prefer A". That moved 26
`person_id` edges off `int__person` and onto `observation_period`, a FACT table
downstream of the staging step. Recall rose to only 33.6% and the new parent
was just as wrong as the old one. The missing discriminator is the key:
`person_id` is the primary key of both `person` and `int__person`, while
`observation_period`'s own key is `observation_period_id` and `person_id` is a
foreign key there.

**Wrong turn 2: preferring a primary key is not an independent signal, and
alone it is actively harmful.** Adding the key guard looked like a free
improvement, so it was applied unconditionally. On its own it changed recall by
nothing at all (26.0% either way), and it broke `messy_mart`: the right parent
for `prod_id` is `prod_ref`, but `sku_xref.prod_id` is BOTH sku_xref's primary
key AND itself a foreign key to prod_ref, so "prefer a primary key" picks the
cross-reference table. FK precision fell 0.973 -> 0.892 against a 0.95 floor
and the gate caught it.

A column being a key where it is stored says nothing about whether the entity
originates there. The guard is therefore scoped to the lineage rule it
protects: both tiebreaks fire only when a manifest was supplied, so no
warehouse can regress on the strength of this change.

**What did not move, and why it is one thing rather than many.** `concept`
stays at 29/131 and conditional-FK recall is unchanged at 19/82. OMOP uses
`concept_id = 0` for "no matching concept", so inclusion against `concept`
falls below threshold on the columns that carry it. That is a sentinel-value
problem, not a lineage one -- and it is exactly why 82 of this project's 235
declared edges are `relationships_where` rather than `relationships`: the
humans wrote the same exception down. Worth its own work; not folded in here.

## Rung 1, declared constraints: both numbers, because only one is a measure of us (2026-10-12)

The catalog's own `PRIMARY KEY`/`FOREIGN KEY` declarations are now read as
priors (`catalog_prior` provenance), implemented for DuckDB and PostgreSQL and
verified on both. It is rung 1 of the read-before-scan ladder, it is free -- one
catalog query, no table touched -- and it also REMOVES work: a declared primary
key makes `detect_composite_pk` return early, skipping up to 45
DISTINCT-over-a-tuple scans per table.

Seven of nine fixtures declare their primary keys, and those declarations were
written by our own generators. Reading them while scoring against gold would
credit us for a key we never inferred. So the eval harness and `benchmark.py`
run with `use_declared=False` and we publish both figures, exactly as we
already do for `--no-llm` and `--no-sample-egress`.

Exact primary-key match against gold, deterministic mode:

| fixture | declared PKs | discovery only | with declarations |
|---|---|---|---|
| collision_heavy | 26 | 26/26 = 100% | 26/26 = 100% |
| eav | 2 | 2/2 = 100% | 2/2 = 100% |
| fan_trap | 4 | 4/4 = 100% | 4/4 = 100% |
| messy_mart | 0 | 20/29 = 69% | 20/29 = 69% |
| multi_tenant | 80 | 4/4 = 100% | 4/4 = 100% |
| obt | 1 | 1/1 = 100% | 1/1 = 100% |
| self_ref | 3 | **2/3 = 67%** | 3/3 = 100% |
| snapshot_noval | 1 | **1/2 = 50%** | 2/2 = 100% |
| tpcds_clean | 0 | 17/24 = 71% | 17/24 = 71% |
| **total** | | **77/95 = 81.1%** | **79/95 = 83.2%** |

Read the "declared PKs" column carefully: it counts declarations in the
database, not scored assertions. `multi_tenant` declares 80 keys while its gold
asserts 4, so the columns are not comparable.

**+2.1 points, and the interesting part is where.** Only `self_ref` and
`snapshot_noval` move -- the two fixtures where discovery is WEAKEST (67% and
50%). Declarations patch precisely our soft spots, which is the argument for
measuring discovery separately: under a single declarations-on number those two
would read 100% and any future regression in recursive-key or snapshot-key
discovery would be invisible behind the DDL.

**No fixture declares a foreign key**, nor does any of the three acquired real
datasets, so rung 1's FK half is currently unmeasurable on our corpus. It is
implemented and verified against a live PostgreSQL warehouse (two FKs, correct
ordinal pairing, composite keys included) but it earns no number here, and that
is stated rather than papered over.

**One further reason the eval runs discovery-only**: declarations change the
Describe evidence, which changes the prompt hash, which invalidates the
cassettes. Running declarations-on in CI would have forced a paid re-record of
400+ cassettes and silently shifted every published LLM-dependent number. The
`fan_trap` cassette miss that surfaced this was the first symptom.

**Not implemented, deliberately**: Snowflake and BigQuery. Both expose
`INFORMATION_SCHEMA.TABLE_CONSTRAINTS`, and neither enforces what it records, so
`enforced=False` would be the right value -- but I have no instance to verify
against, and a constraint reader that silently returns nothing is
indistinguishable from a warehouse that declares nothing. Same standard as the
BigQuery comments gap.

## Partition-wise merging: what is exact, what is only bounded (2026-10-12)

Rung 3 reframed. Measured first, which changed the plan: approximating the
distinct counts is worth far less than claimed. On a 25M-row 20-column table
the combined nulls+distinct pass is 23.0s exact and 16.1s approximate -- only
**30% cheaper end to end**, because the null count is irreducible and is 9.8s
of that 23.0s on its own. Distinct-counting alone is 2.1x cheaper (13.2s ->
6.3s), which works out to roughly **18% of total profile cost**, not the 62%
implied by quoting the combined pass.

So `approx_count_distinct` is not the prize. Not re-reading unchanged data is:
in a warehouse the large tables are partitioned, usually by date, and almost
all of the bytes are history that will never change again.

### Merging per-partition statistics, verified against a whole-table profile

On a real partitioned PostgreSQL table (30,000 rows, 3 monthly partitions),
merged partition stats equal a full-table profile field by field:

| statistic | merge rule | result |
|---|---|---|
| row_count | sum | exact |
| n_null | sum | exact |
| min / max | min of mins, max of maxes | exact |
| avg_len | weighted by non-null rows | exact |
| top_values | merge value -> count maps | approximate: each partition keeps only its top `TOP_K` = 10 |
| **n_distinct** | **not summable at all** | **bounds only** |
| **is_unique** | **not derivable** | **needs a global check** |

**The min/max merge has a trap worth naming**: `ColumnStats` stores `min_val`
and `max_val` as STRINGS, so a lexical merge decides `"9" > "10"` and reports a
maximum of 9 for a column reaching 10. Numeric families are cast before
comparison; ISO dates need no cast because lexical order is already
chronological. Both cases are tested.

### The distinct count is the whole difficulty, and merging cannot find keys

A value can appear in every partition, so only bounds are knowable:
`max(d_i) <= D <= sum(d_i)`. Both are exact and free. The merged `n_distinct`
is set to the LOWER bound deliberately: understating distinctness makes a key
look non-unique and costs recall, while overstating it would assert a FALSE
primary key. Failing toward "not a key" is the safe direction.

The consequence is sharper than "less precise". On the test table `evt_id` is a
genuine key -- 60,000 distinct over 60,000 rows -- and its merged bounds are
`[10,000 .. 60,000]`, because each partition holds 10,000 distinct values. So
**partition merging alone cannot detect primary keys at all.** The mergeable
HLL sketch (0.05% error measured) is therefore a requirement of this design,
not an optimisation of it, and exact verification stays for anything we intend
to assert as a key.

### The change token, and the backfill that would defeat a naive one

Re-profiling only what changed needs a per-partition "has this changed?"
token, and the failure mode to design against is the BACKFILL: a correction
rewriting an old partition in place.

Verified on PostgreSQL 16: an `UPDATE` touching ~10,000 rows of one partition
moved `pg_stat_user_tables.n_tup_upd` by 10,000 and left both `count(*)` and
`pg_class.reltuples` **completely unchanged**. A token built from row counts
would keep a stale sketch for that partition forever, and nothing would ever
indicate it. The token is therefore built from `n_tup_ins:n_tup_upd:n_tup_del`.

Two implementation details that are easy to get wrong:

- **`pg_stat_force_next_flush()` first.** The counters are reported
  asynchronously. Reading without forcing a flush returned pre-UPDATE values,
  and the backfill looked like no change at all -- I nearly recorded that as a
  finding before re-measuring.
- **Compare tokens for EQUALITY ONLY, never ordering.** `n_tup_*` is cumulative
  since the last stats reset, so `pg_stat_reset()` makes them go backwards. A
  decrease is still a change. An empty token means the source could not tell
  us, which also forces a re-profile rather than being read as "unchanged".

### The state file is a two-way door, and a test holds it open

Where per-partition state lives -- a sidecar beside `layer.yaml`, or the hosted
control plane -- is a product decision we do not have to make yet, and should
not. What makes it deferrable is one rule:

**State is a cache, never a source of truth.** Everything in it must be
re-derivable from the warehouse. Delete it, corrupt it, move it to another
machine or another backend, and inference is slower, never wrong. That is what
lets the format change unilaterally, the sidecar migrate into the cloud, or the
whole mechanism be abandoned, with nothing lost.

The door shuts the moment something un-derivable lands in there -- a human's
review decision, an accepted-FK override, a renamed metric. Then state is
load-bearing and there is a migration problem forever. The line to hold in
review: state holds only what a scan could recompute.

Mechanics that follow:

- A **two-method store** (`load`, `save`), so any backend fits.
  `NullStateStore` remembers nothing and is the default -- its existence is the
  point, because the engine must stay fully functional with no state at all or
  the hosted plane becomes mandatory and CLAUDE.md's standalone-engine stance
  is dead. `LocalStateStore` is a JSON sidecar. The remote store is
  deliberately unimplemented.
- **Keyed by content, not location**: `sha256(source_id/schema/table/partition)`,
  so the same key resolves in a file, a row or an object.
- **A versioned envelope** where an unknown `state_version` means ignore and
  re-profile, never raise. Same for an unparseable file.
- **Sketch bytes stay out of `layer.yaml`.** The published layer carries the
  derived `cardinality` with its bounds -- the answer. The sketch is working
  material. Consumers then depend on the number, so the sketch mechanism can
  change without a spec break.
- **Producer recorded per entry.** Snowflake `HLL_ACCUMULATE`, BigQuery
  `HLL_COUNT.INIT` and Apache DataSketches blobs are not mutually readable
  [R -- DataSketches is explicitly an interchange format, but neither vendor's
  native format has been verified against it], so merging across producers
  would be silent garbage. A mismatch forces a re-profile, which is harmless
  precisely because of the cache rule. It also means a warehouse migration
  invalidates the sketches, and that is fine.

Asserted, not just intended: cold store, warm store and no store produce
**identical** output; deleting the file changes nothing but time; an unknown
version and a corrupt file both re-profile rather than crash; and after a
backfill only the touched partition is re-read while the answer still equals a
from-scratch run. These are the analogue of the `MinimalSource` test that keeps
the connector protocol at two methods.

Known scaling limit, stated rather than discovered later: one JSON file for a
warehouse with hundreds of thousands of partitions is the wrong shape. That is
when the remote store earns its place, not before.

### Sketches recover key detection, and three bugs the measurement exposed

The merge section above established that partition-wise profiling cannot find
primary keys: a genuine 60,000-row key spread over six partitions merges to
bounds of `[10,000 .. 60,000]`, because `max(d_i) <= D <= sum(d_i)` is all
arithmetic can know. Mergeable HLL sketches close that, and the before/after is
the clearest statement of why they are a requirement rather than an
optimisation:

| column | true | merging only | with sketches |
|---|---|---|---|
| `evt_id` (a real key) | 60,000 | `[10,000 .. 60,000]`, est 10,000 | `[59,753 .. 60,000]`, est 60,000 |
| `cust_id` | 500 | `[500 .. 3,000]`, est 500 | `[500 .. 3,000]`, est 500 |
| `part_cd` | 6 | `[6 .. 6]`, est 6 | `[6 .. 6]`, est 6 |
| **nominated for an exact check** | | **none** | **`evt_id`** |

A sketch still never proves uniqueness. Its job is to narrow the interval
enough to say whether the row count falls inside it, i.e. whether an exact
check is worth running. `evt_id` lands in `needs_exact`; `cust_id` does not.

**Only locally-distinct columns are sketched.** A globally unique column must
be unique within every partition, so a column that already repeats inside one
partition can never be a key, needs no sketch, and must not cost state. That
filter is what keeps the state file small -- 1 of 3 columns here, and roughly
10 of 90 on a wide fact table.

Three bugs found by measuring rather than reasoning, all now tested:

1. **The estimate was not clamped to the proven bounds.** The sketch reported
   61,715 distinct values for a column that provably has at most 60,000 -- the
   sum of its partitions' counts. An estimate outside a bound we have already
   proved is incoherent on its face. Clamped, and the clamp happens to make
   `evt_id` exactly right.
2. **Low-cardinality columns were badly wrong.** `part_cd` has one value per
   partition and six overall, and was reported as having ONE -- an 83% error on
   a published number -- because the lower bound is all arithmetic offers. Fixed
   for free: when every partition's `top_values` lists all of its distinct
   values, the union of those value sets is the EXACT global count, needing no
   sketch and no extra scan. This is the common case for dimensions and enums.
3. **Malformed sketch bytes raised instead of degrading.** Those bytes come out
   of our own state file, so malformed means corrupt state -- which the cache
   rule says must re-profile, never abort.

Remaining honest gap: a mid-cardinality column that is neither locally distinct
nor low-cardinality-complete gets bounds only, and its point estimate is the
lower bound. `cust_id` above is right by luck, not by construction. The bounds
are published alongside, so the uncertainty is visible rather than hidden.

**Cross-producer unions are refused outright.** A forged state entry claiming
`producer: snowflake-hll` makes the whole table fall back to arithmetic bounds
rather than produce a confident wrong number. Likewise an incomplete set --
if one partition contributed no sketch, no union is attempted.

## Rung 0 does NOT give trustworthy cardinality (2026-10-12)

The cost ladder lists rung 0 as "the
engine's own optimizer statistics -- ~free -- cardinality, null counts,
min/max". Two of those three do not survive measurement.

Implemented for PostgreSQL (`pg_stats` + `pg_stat_user_tables` for staleness)
and measured against exact counts on 1,000,000 rows, deliberately awkward
columns:

| column | engine | exact | error |
|---|---|---|---|
| `uniq_key` (a key) | 1,000,000 | 1,000,000 | **0.0%** |
| `near_unique` | 845,894 | 900,000 | 6.0% |
| `text_key` (md5) | 363,850 | 400,000 | 9.0% |
| `skewed` (log-bucketed) | 18,520 | 41,733 | **55.6%** |
| `mostly_one` (1 value in 10,000 rows) | 5 | 101 | **95.0%** |

**So `n_distinct` from the engine cannot replace a `COUNT(DISTINCT)`.** It is
sampled, and distinct-count estimation from a sample is the same
unseen-species problem that makes row sampling useless for this -- 55% to 95%
wrong on exactly the skewed shapes a real warehouse is full of. Using it to
populate a published `cardinality` with `method: engine_statistic` would mean
shipping numbers that are an order of magnitude out.

What IS trustworthy, measured:

- **Null fractions.** Maximum absolute error 0.0020 across columns at 0%, 14%
  and 99% null. Usable directly.
- **Key-candidate nomination.** Postgres stores `n_distinct` as -1.0 exactly
  when its sample saw no duplicates. Of the five columns above it flagged
  `uniq_key` and nothing else: 5 correct out of 5, no false positives and no
  false negatives. [One synthetic table, so treat 5/5 as a single result rather
  than a rate -- but the mechanism is principled, and it can only
  false-POSITIVE on a near-unique column, which an exact check then rejects.]

That second one matters more than the cardinality it replaces. The sketch
section above established that key detection is the hard part of partition-wise
profiling. Rung 0 nominates key candidates for **zero** cost and zero scanning,
so on a 45TB warehouse the exact uniqueness check -- the one thing that is
genuinely not approximable -- can be scoped to the nominees instead of every
column.

**One way to get rung 0 completely wrong**, worth naming because it is silent:
`n_distinct` is NEGATIVE when it means a ratio of the row count. -1.0 is
"distinct in every row"; -0.3 is 0.3x the rows. Read as a count, every key in
the warehouse becomes a single-valued column. The conversion is against
`reltuples` and the raw form is flagged on the result.

`analyzed_at` is carried from `last_analyze`/`last_autoanalyze` so a caller can
judge staleness. Trusting a stale figure silently is the failure mode we sell
against, and a statistic with no analyze timestamp is reported as never
analyzed rather than as fresh.

Not implemented: Oracle `ALL_TAB_COL_STATISTICS` (plus `ALL_TAB_HISTOGRAMS`,
which is a frequency histogram and therefore our `top_values` for free) and the
Snowflake/BigQuery equivalents. No instance to verify against, and the same
standard applies as elsewhere -- a reader that silently returns nothing is
indistinguishable from a warehouse with no statistics.

### Wiring it into the product, and what `--state` actually changes (2026-10-13)

The merge, the state store, the sketches and the `cardinality` field were built
and tested over four slices while **nothing in `pipeline.py` or `cli.py` called
any of it**. The tests exercised the modules directly, so everything passed
green while no user could reach the feature. That is a delivery failure, not a
test failure, and it is worth recording as the reason the wiring now has its own
tests.

`semlayer infer <source> --state layer.state.json`, measured on the partitioned
PostgreSQL fixture:

```
FIRST  (cold state):   8.6s  tables=3  partitions={'profiled': 6, 'reused': 0}
SECOND (warm state):   9.1s  tables=3  partitions={'profiled': 0, 'reused': 6}
no --state at all:    12.7s  tables=9  partitions=n/a
```

Three things that reading shows:

- **`reused: 6 of 6` on the second run** is the mechanism working. The run
  report carries it because a user who passes `--state` otherwise has no way to
  tell whether it helped.
- **`--state` also fixes the model's SHAPE.** Without it the six partitions are
  profiled as six separate tables, so a real warehouse would emit 120 monthly
  "tables" nobody models against. The parent absorbs them instead.
- **Wall-clock did not improve**, and saying so matters. At 30,000 rows the
  partition reads are microseconds and the fixed costs -- Link, Enrich, catalog
  queries -- dominate. The saving is proportional to data volume, which this
  fixture has none of. The 45TB arithmetic is where it lands.

State is **off by default**, so the default path is byte-for-byte what it always
was, and that is asserted.

**Honest limitation this exposed: on PostgreSQL the key column reads
`method: bounds_only`.** Postgres has no core HLL, so there is no sketch to
merge, so partition-wise profiling on Postgres cannot detect primary keys --
the `[10,000 .. 60,000]` problem, unresolved. That makes the remaining A1 item
(use rung 0's free key nomination to scope an exact uniqueness check) a
**correctness requirement on Postgres**, not the optimisation it was filed as.

`source_identity` keys the state file and is credential-free -- `postgres:
host:port/dbname`, never the password, because this file is something a user may
commit. It matters for correctness rather than tidiness: two warehouses sharing
a state file would otherwise share cached statistics, and one would be profiled
with the other's numbers.

## Correction: 41.7% was measured against the wrong denominator (2026-10-13)

The lineage section above reports FK recall on `dbt_synthea` as 61/235 = 26.0%
without a manifest and 98/235 = 41.7% with one. **The denominator is wrong**, and
it is wrong in our favour, which is why it needs saying plainly.

Of the 235 declared edges, 87 cannot be inferred by any method:

| | count | share |
|---|---|---|
| inferable | 148 | 63.0% |
| child table is **empty** | 48 | 20.4% |
| child column is **entirely NULL** | 39 | 16.6% |

`concept_synonym` is an empty table -- its seed CSV is a header row and nothing
else -- and `care_site.place_of_service_concept_id` is 63 rows of NULL. A
foreign key over a column with no values is unprovable by statistics, by a
sketch, or by anything else. The project declares them because OMOP's
specification declares them; this 27-patient Synthea extract does not populate
them. That is the "27 patients" limitation recorded at acquisition, showing up
as a measurement artefact.

So both figures belong in the record:

- **41.7% of declared edges** -- the number to quote if someone asks what
  fraction of the project's own assertions we reproduce.
- **98/148 = 66.2% of inferable edges** -- the number that measures the engine.

Neither is the honest figure on its own, and quoting only the second would be
self-serving. The gap that remains is 50 edges, not 137.

### And the sentinel-value hypothesis was wrong

The planned next slice was sentinel handling: the theory was that OMOP's
`concept_id = 0` for "no matching concept" broke inclusion, costing 102 edges
into `concept`. Measured before building: **all 319 raw declared edges have zero
missing values.** Inclusion holds everywhere, because OMOP defines a real
`concept_id = 0` row and it is present in the parent. There was no problem to
fix.

Diagnosing the real causes of the 50, from the candidate audit:

- **Wrong parent chosen among competitors** -- `location` vs `int__location`,
  `vocabulary` vs `stg_vocabulary__vocabulary`, `person` vs `int__person`,
  `concept` vs `int__source_to_source_vocab_map`. This is the
  staging-versus-published problem the lineage reader already addresses, and it
  is why the no-manifest run is so much worse. Lineage recovered 37 of these;
  the rest are cases where neither candidate is an ancestor of the other, so
  `prefer` correctly declines.
- **Numeric-suffixed columns.** `concept_id_1` and `concept_id_2` score 0.500
  against `concept.concept_id`, below the 0.6 auto-accept threshold, so they
  reach review rather than the layer. `name_similarity` has no notion of a
  trailing ordinal. Small in count, but it is a real naming gap of the same
  family as the nine found earlier.

Neither of those is sentinel handling, and the measurement is what stopped a
slice being built against a hypothesis that was simply untrue.

### Positional key suffixes: one blind spot costing two layers (2026-10-13)

The remaining diagnosed FK gap was `concept_id_1` and `concept_id_2` in OMOP's
`concept_relationship`, both referencing `concept.concept_id` and both scoring
0.500 -- below the 0.6 auto-accept threshold.

Fixing the naming score changed **nothing**, and that was the useful result.
The column never reached naming similarity at all, because:

```
_ID_NAME_RE = (^|_)(id|key|sk|pk)$|^id($|_)
```

anchors on `$`, so `concept_id_1` did not match the identifier rule and typed as
**`quantity` / `measure`** -- and the foreign-key child gate excludes measures.
One regex cost us the typing, and the typing then cost us the link, before any
naming logic got a say. It took two measurements to find the second layer: fix
the link, measure no change, then look underneath.

Folded at both layers, measured on `dbt_synthea` with the manifest:

| | before | after |
|---|---|---|
| recall of declared | 98/235 = 41.7% | 102/235 = 43.4% |
| **recall of inferable** | **98/148 = 66.2%** | **102/148 = 68.9%** |

All four ordinal edges are now found, and both positional columns survive as
role-playing references to the same parent column -- which is exactly what they
are.

The fold is deliberately conservative: a trailing ordinal is stripped only when
the stem still ends in a key token (`id`, `key`, `sk`, `cd`, `no`, `nbr`, `num`,
`code`).

| folds | left alone |
|---|---|
| `concept_id_1` -> `concept_id` | `sales_2024` |
| `cust_key_3` -> `cust_key` | `q1_revenue` |
| `order_sk_10` -> `order_sk` | `addr_line_1` |

Stripping `sales_2024` would invent a reference to a `sales` table, and
`addr_line_1` is not a key at all. Both negatives are asserted.

This generalises past OMOP: any warehouse with positionally-named keys hits it
-- graph and relationship tables (`node_id_1`/`node_id_2`), bridge tables,
before/after pairs. It is the tenth instance of the same root cause recorded in
this file: a naming vocabulary written against warehouses we authored
ourselves, where nobody numbered their keys.

### A1's last piece: one exact check, scoped to the only columns that could be keys (2026-10-13)

Partition-wise profiling bounds a distinct count at `max(d_i) <= D <= sum(d_i)`,
so a genuine key spread over six partitions reads as `[10,000 .. 60,000]` and
`is_unique` stays False. A mergeable sketch narrows that enough to nominate; on
PostgreSQL, which has no core HLL, it does not narrow at all -- so `--state` on
Postgres was silently losing every primary key. This piece was filed as an
optimisation and is a **correctness requirement** there.

The nomination is free and sound. A globally unique column must be unique within
EVERY partition, so a column that repeats inside any one of them is eliminated
with no query at all. That is a necessary condition, so it can never discard a
real key. Measured on the partitioned PostgreSQL fixture:

| column | before | after |
|---|---|---|
| `evt_id` | `bounds_only [10,000..60,000]`, not a key | **`exact [60,000..60,000]`, `primary_key`** |
| `cust_id` | `bounds_only [250..1,500]` | unchanged -- eliminated free, never queried |
| `sts_cd`, `evt_dt` | `exact` (complete value maps) | unchanged |

**One exact count, for one column out of five.** The whole ladder now reads:
eliminate for free, narrow with a sketch where one exists, then prove what
survives with a single count. Capped at 12 checks per table, because scoping the
work must not become doing more of it.

**The case that justifies the exact check** is partitions that are each
internally unique but overlap: three partitions of `0..999` nominate the column
and are 3,000 rows with 1,000 values. Only a count separates that from a real
key, and getting it wrong asserts a FALSE primary key. Tested explicitly.

A refused cross-producer sketch union now degrades the ESTIMATE but not the
correctness, because the exact check is independent of sketches.

**Four pre-existing tests failed on this change, and that was the right
signal** -- their assertions encoded the limitation being removed
(`is_unique is False`, `method == hll_sketch`, bounds staying wide). Each was
rewritten to preserve what it actually guarded rather than loosened: the
"sketches are mandatory" test now asserts that MERGING still cannot nominate,
which remains true, while the free filter plus a count recovers the key anyway.

No effect on `dbt_synthea` (102/148 = 68.9%, unchanged): it is not partitioned,
so this path never runs there.

## The dbt manifest was already open, and we threw away its documentation (2026-10-13)

`--manifest` has read the dbt manifest since the lineage slice, used only for
the DAG. The same file carried documentation a human wrote, and all of it was
discarded. On `dbt_synthea`:

| | count |
|---|---|
| table descriptions | **56** of 87 models |
| **column descriptions** | **447** |
| metrics / semantic models | 0 |

Free priors, from a file we already parse, needing no credential, no API and no
new connector -- the cheapest catalog win available and the one we had walked
past. They now apply as `catalog_prior`, the same signal as a native warehouse
COMMENT, on the same argument: a sentence a human wrote beats anything we
generate.

**Precedence is unchanged and tested.** Native comments are applied first, dbt
descriptions fill only the gaps, and where both exist and DIFFER the
disagreement is recorded in `conflicts` rather than silently resolved. Two
humans documenting the same column differently is worth a review, not a coin
toss.

**Metrics are read but unexercised by any fixture.** `metrics` and
`semantic_models` are parsed into name, label, type, measure, aggregation,
expression and filter -- MetricFlow keeps the aggregation on the SEMANTIC MODEL
rather than the metric, so both have to be joined to get a usable definition,
and a filter arrives as a list of `where_sql_template` fragments. `dbt_synthea`
defines **zero** metrics, so this path is covered only by a hand-built manifest
in `tests/test_lineage.py`. The one real project surveyed with MetricFlow
metrics -- dbt-labs' jaffle-shop rewrite -- has **no licence file**, so it is
track I and cannot back a published number. That gap is stated rather than
papered over: the reader is tested, but not against anybody else's project.

## The scale that matters is table count, not terabytes (2026-10-13)

Raj asked at what scale the architecture has to change. Measured, the answer is
that **data volume never forces it** -- 4TB and 45TB are the same design, linear
in rows and bounded by partitioning -- while **object counts do**, because
several costs are per-table or per-column regardless of how much data each
holds.

Four breakpoints, in the order they hit:

| # | bites at | what breaks | kind |
|---|---|---|---|
| 1 | **~150 tables** | Link's candidate space | **architectural** |
| 2 | ~2,000 tables | serial execution | tuning |
| 3 | ~30,000 columns | the output document | **architectural, spec-touching** |
| 4 | ~100,000 state entries | the single-file state store | architectural but localised |

**1. Link's candidate space is already at its limit.** Measured at 116 tables /
1,312 columns: **163 key-shaped parent columns against `MAX_PARENTS` (200 when this was measured; 5,000 since `4057c46`).**
Shaped parents alone consume 82% of the budget before named parents are added.
Candidate parents grow ~1.4 per table, so at 1,000 tables we would consider 14%
of them and at 30,000 tables **0.5%**. The fix is not a bigger cap -- generation
is `O(child columns x parent columns)` and a bigger cap buys recall at
quadratic cost -- it is **blocking**: bucket candidates by name token, type
family and cardinality band so each child tests only plausible parents.

To be precise about what is NOT claimed: the 95 missed `dbt_synthea` edges were
verified as empty or all-NULL columns, not cap truncation. The cap has not
demonstrably cost recall yet. We are sitting on it, and it truncates silently,
which is the dangerous kind of limit.

**2. Serial execution** is ~8.8 s of fixed cost per table -- round-trips plus
two LLM calls. 2,000 tables is ~5 h, 5,000 is ~12 h. Concurrency buys an order
of magnitude and needs no redesign.

**3. The output document is the first break that touches the spec.** Measured
**1,209 bytes per column**. So 1,000 tables is ~18 MB, 5,000 is ~90 MB, and
30,000 tables at 15 columns is **~544 MB in one file** -- unloadable, and no
agent can read it. Sharding is therefore a FORMAT decision, and
**SPEC.md #4 is a one-way door**: "Producers MUST ensure all references resolve
within the document; validators reject dangling references." There is no notion
of a document set anywhere in the spec or schema, so the day we shard, every
cross-shard reference violates the contract and `check_sql`'s "object not in
the document" error becomes a false positive for conforming consumers. In an
Apache-2.0 spec whose pitch is interoperability, that is a breaking change to
other people's code.

The insurance is two optional reserved fields (`document_set`, `part`) and one
clause in #4 -- references resolve within the document, or within the declared
document set where one is given. Validators seeing no `document_set` behave
exactly as today. That turns a future MAJOR break into a MINOR addition.
Not taken unilaterally; it is a published spec.

**4. The state store** is ~470 bytes per (partition, column) entry, so 30,000
tables x 50 partitions x 25 columns is ~17.6 GB of single-file JSON. Cheap to
fix, because the store is already a two-method interface with the backend
deliberately undecided -- a swap to SQLite or object storage, not a redesign.
That two-way door was worth building.

### The line we hold, and the flag

**2,000 tables / 30,000 columns**, documented in `docs/scale.md` with the five
performance levers. `SCALE_TABLES` / `SCALE_COLUMNS` warn in the run report
past that. It warns and never refuses: a user who knows their warehouse may be
right to proceed, and what we must not do is hand them a silently truncated
layer and let them believe it is complete.

COLUMNS is the better primary signal -- three of the four limits scale with
columns and only wall-clock scales with tables -- and both are reported.

One self-inflicted lesson: the first version wrote `_scale` into the document
root, which carries `additionalProperties: false`, and **11 tests failed**. The
existing `_link_audit`/`_partition_audit` convention survives only because the
pipeline strips those before validation, and `_partition_audit` had never fired
on an unpartitioned fixture -- so the convention is more fragile than it looks.
The pipeline already computes the counts, so there was nothing to smuggle
through the document at all.

### Correction: the FK candidate cap was silently wrong above ~100 tables (2026-10-13)

The scale note above said Link's candidate space was an *architectural* break
already behind us at ~150 tables. The first half was right and understated; the
second half was wrong about the fix.

**Right, and worse than stated.** Measured parents per table across the corpus
-- 1.46 collision_heavy, 1.53 dbt_synthea, 1.72 adventureworks_dw and
messy_mart, 2.58 tpcds_clean -- puts `MAX_PARENTS` (200 when this was measured; 5,000 since `4057c46`) at **77-137 tables**,
an order of magnitude below the 2,000-table line we had just published. At
2,000 tables we would have kept 5.8% of candidate parents.

And it degraded in two stages, because `named` parents are listed before
`shaped` ones:

1. from ~120 tables the `shaped` tail is cut first, so **statistics-only FK
   discovery disappears entirely** and Link silently becomes name-dependent --
   exactly the capability this session worked hardest to build;
2. from ~200 tables named parents start dropping too, **alphabetically by
   table**, which is not a defensible basis for discarding a key.

**Wrong about the fix.** `MAX_PARENTS` bounds a SORT, not queries: inclusion
tests stay capped by `MAX_QUERIES_PER_CHILD = 40` whatever the parent list
holds. So raising it costs client CPU and no warehouse time at all. Measured:
3,440 parents x 25,000 children is ~24 minutes of Python ranking, against a
2,000-table run already measured in hours -- about 8% overhead.

Raised to **5,000**, covering ~2,900 tables at the median rate. A strict no-op
on every current fixture, whose largest parent list is 177, so the existing FK
floors prove it changes nothing today.

Blocking -- index parents by name core and type family, ~4 tests per child
instead of ranking thousands -- is therefore a PERFORMANCE follow-up, not the
correctness prerequisite I called it. Measured on dbt_synthea: 48 blocks,
median size 2, mean 3.7 candidates per child against the current budget of 40.
Its risk is unchanged and still the interesting part: a name-based block key
loses precisely the FKs naming cannot see, so it needs a cardinality-and-type
banded fallback or it would undo statistics-first discovery.

`docs/scale.md` is corrected: foreign-key discovery now reads ~2,900 tables,
with the client-CPU cost stated separately from the breaking point.

### And the binding constraint was never MAX_PARENTS (2026-10-13)

Third position on this, now measured rather than reasoned. Instrumenting
`_inclusion` on `dbt_synthea`:

```
child columns issuing any inclusion test : 667   (51% of all columns)
total inclusion tests                    : 24,827
children exhausting the 40-test budget   : 567   (85%)
```

**`MAX_QUERIES_PER_CHILD = 40` is the binding constraint, and it binds on 85%
of child columns at 116 tables** -- today, at fixture scale, not at 2,000
tables. `MAX_PARENTS` was never the mechanism.

Two consequences:

**Raising `MAX_PARENTS` did not fix correctness.** 85% of children can test only
40 parents, ranked by name similarity, so a name-mismatched true parent is
crowded out -- and a LARGER parent pool makes the crowding worse. The raise is
still right (a hard-dropped parent can never be tested, whereas a crowded-out
one is reachable by better ranking) but it is not the fix claimed above.

**Link's inclusion testing is the dominant cost at scale.** 24,827 queries on
116 tables; scaled, ~15,000 children x 40 at 2,000 tables is ~600,000 tests and
~17 h of round-trips. The cost models in
An earlier estimate put ALL round-trips at 0.4 h for 800 tables --
understated by roughly 10x, and corrected there.

So blocking is a CORRECTNESS fix and the largest cost lever, ahead of
parallelising Describe (~1.3 h at 4TB). The three positions taken, in order:
"architectural, already past it" (right that it mattered, wrong mechanism);
"just performance, raise the cap" (wrong, because the per-child budget was
never checked); and this one, measured.

## `infer` was non-deterministic, and a stale file hid it (2026-10-13)

Two runs of `semlayer infer` over an unchanged warehouse produced documents
differing in **198 lines on `messy_mart`, every one of them a
`cardinality.as_of` timestamp**. Everything else was byte-identical. The field
is now removed and two builds hash the same.

This mattered more than a noisy diff. The workflow the product is built around
is that inference output lands as a **PR in the customer's repo** and is
re-inferred when the schema changes; a re-run that rewrites 198 lines for no
reason makes that diff unreviewable and makes drift detection cry wolf.

**How it stayed hidden is the more useful part.** The local suite reported 580
passed. `examples/agent_pipeline/layer.yaml` is **untracked** and had been
sitting on disk since 2026-10-11, built before the `cardinality` block existed:
`_ensure_layer()` only infers when that file is absent, so every local run
replayed against a layer whose shape predated the bug, the agent prompts
matched the committed cassettes, and the tests passed. Deleting the file
reproduced six cassette misses immediately. A fresh checkout — which is what CI
is — had never been exercised, so the green suite was green on an artifact
nobody had rebuilt in two days.

Two lessons worth keeping separately: a test that depends on a generated file
it does not itself create is not testing what it appears to, and
`python fixtures/build.py` determinism (which we do assert) says nothing about
**output** determinism (which we did not).

### Deferred: real staleness reporting

Removing `as_of` costs nothing that worked, because it did not work. In
incremental mode the writer was `prev.get("as_of") or _now()` where `prev` is
the *previous document*, so once set the timestamp was preserved **whether or
not that column had just been re-profiled**. It recorded when a statistic was
first measured and never when it was last measured — the inverse of staleness,
in the one mode where staleness is the point.

The design when it is picked up:

- `PartitionState` gains a measured-at, next to `change_token` — the field that
  already knows whether a partition moved. State is a cache, never a published
  artifact, so timestamps there cannot affect document determinism.
- The document carries a per-column marker **only** where the statistic was
  reused from a previous run. Those values come from state and do not move
  until the partition does, so repeated incremental runs over unchanged data
  stay byte-identical too.
- Wall-clock run time belongs in the pipeline report, the same precedent as
  `_scale`.

Until then the honest position is that we do not report statistic staleness,
rather than reporting it wrongly.

## Reproducibility audit of the benchmark rows (2026-10-14)

Checked whether every row in `docs/benchmark.md` can be re-derived from the
committed recordings. Two could not, because the responses behind them had not
been committed, so `python -m semlayer.benchmark` with no API key stopped on a
cassette miss rather than printing a table.

| row | was | reproduces as |
|---|---|---|
| `tpcds_clean` semantic / +lint / +ontology | 0.58 / 0.58 / 0.67 | **0.42 / 0.42 / 0.58** |
| `claude-sonnet-5` semantic / +lint | 0.84 / 0.84 | **0.87 / 0.87** |

53 recordings were missing for the first and 40 for the second; both were made
on the same pinned model, and both rows now replay with no key. Whether the
earlier `tpcds_clean` figures were ever right cannot be established, because the
responses behind them no longer exist — two of twelve questions is 17 points on
that fixture, so variance may account for part of the gap. The published page
carries the figures that reproduce. `fan_trap` and `messy_mart` reproduced
exactly.

The `tpcds_clean` result is the more interesting one: the honest negative is
larger than previously reported. On a clean, well-named schema the layer scores
0.42 against raw DDL's 0.67.

**Why it was not caught earlier.** A run with an API key records whatever is
missing and passes, so the gap does not appear to anyone holding a key — only to
a keyless run. The LLM-tier section above shows the same pattern once before:
`tpcds_clean` "was re-recorded against the live model for this report", and an
earlier draft's 0.128 ECE was flattered by the fixtures that did replay. Local
re-recordings for a report were not committed either time.

`tests/test_benchmark_replays.py` now runs every published row with an empty key
and asserts the printed table matches the committed page. It is marked `slow`
(~3.5 minutes; it shells out to the benchmark) and runs in CI, which is the only
keyless environment available.

### A defect found in the same audit

A response truncated at the token ceiling was returned as if complete, and
`link/validate_llm.py` discarded every verdict in a chunk whose JSON would not
parse. It was active on the shipped model, costing 30 candidates their
corroboration during a benchmark run, and two committed cassettes held truncated
arrays — replay reproduced the fragment, so no code fix alone could repair them.
Re-recorded.

Fixing it moved `messy_mart` FK precision **1.000 -> 0.973**: restoring the
discarded verdicts admits one false positive the model does assert. The 1.000
had been flattered by the defect. Floor is 0.95, so it still holds, and 0.973 is
the figure that reproduces.

## Two live runs of the same warehouse disagree (2026-10-27)

The timing run on `dbt_synthea` was two full live runs over identical input,
identical prompts, temperature 0, differing only in how many LLM calls were in
flight. The relationship count came out 265 in one and 260 in the other. The
cassettes from both arms were kept, so the disagreement can be measured rather
than guessed at.

**Every one of the 281 prompts the two runs shared got a different answer as
text.** Structurally, the disagreement is smaller but reaches every layer:

| layer | items | differed between the two runs |
|---|---|---|
| FK validation verdicts | 2,745 | 103 (3.8%) |
| escalated column `semantic_type` | 432 | 29 (6.7%) |
| column `entity_role` | 1,312 | 34 |
| column PII flag | 1,312 | 2 |
| `table_type` | 116 | 7 |
| metrics | 76 | 1 swapped |
| relationships | 266 distinct | 7 (6 in one run only, 1 in the other) |

The 259 relationships both runs produced are byte-identical, confidence
included. The seven that differ are the borderline cases a reviewer would want
to see: `encounter_id` against `visit_id` across OMOP staging and mart layers,
concept ids against vocabulary mapping tables, provider source values,
self-references. The model gave each of these the opposite verdict with an
equally confident one-line reason each time.

One of them is not borderline. `imaging_studies.PATIENT -> patients.Id` is a
genuine foreign key in the Synthea source data. One run asserted it at
confidence 0.90; the other dropped it, because the validation verdict flipped
from plausible to implausible on identical input (the plausible run's reason:
"one patient unused, normal"). Nothing in the output marks the asserted
version as contested, and nothing in the other output records that it was
ever a candidate.

The two PII flips deserve their own line: a column tagged `pii_name` in one
run and not the other is exactly the case the record-never-enforce policy has
to be honest about. The warehouse enforces; we record; and what we record can
differ between two runs.

What this does and does not mean:

- The pipeline is deterministic given the model's answers. Replaying either
  arm's cassettes through the whole pipeline at 1 and 8 workers gives
  byte-identical YAML. The earlier non-determinism (2026-10-13) was ours and
  is fixed; this one is the model's.
- It is the empirical case for three things the product already does:
  cassettes pin a run so a re-run is a diff and not a lottery; every inference
  carries confidence and provenance; and a review surface exists because
  reruns disagree. It is also launch-post material, since no vendor publishes
  this number.
- It is also a limit on what a single confidence value means. A flipped
  verdict carried 0.90, the same as a stable one. Running the validator twice
  on the borderline set and marking disagreement as low confidence would route
  exactly these cases to review, for a second call on a subset only. Not
  built; recorded here as the obvious next experiment.
- Token counts vary with the wording, so a cost estimate from one run is
  within a few percent of the next, not exact.
