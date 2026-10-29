# Scale: what we support, and how to make it fast

**semlayer is tested to 2,000 tables / 30,000 columns.** Past that it still
runs, and it tells you it is past the line, but foreign-key discovery starts
truncating its candidate list and the output document becomes impractical to
load. Narrow the run instead.

Data **volume** is not the limit. 4TB and 45TB are the same architecture: the
cost is linear in rows and bounded by partitioning. What costs us is the number
of objects, because several costs are per-table or per-column regardless of how
much data each holds.

## Where the limits actually are

Measured, not estimated — the workings are in
[calibration.md](calibration.md).

| limit | bites at | why |
|---|---|---|
| **foreign-key accuracy** | **already, at any size** | when several tables contain the same key values, inclusion testing cannot tell them apart and the engine can pick the wrong one. On a 116-table dbt warehouse with no ETL manifest supplied, **79%** of the foreign keys it asserts on columns the project itself documents are right; supplying `--manifest` takes that to 91% (§4 below). It was 50% until 2026-10-16, when the ETL stage turned out to be readable from table-naming conventions (`stg_`, `int__`) and not only from a manifest. **A warehouse that follows no naming convention still gets the 50% path** -- the signal is the convention, not the names |
| query volume | ~2,000 tables | foreign-key testing dominates, and the cost is per CHILD column, not per column. On a 116-table warehouse: **23,198 inclusion tests over 667 children** — 1,312 columns exist but only half issue any test. That is **34.8 per tested child, median 40, with 74% sitting at the `MAX_QUERIES_PER_CHILD` cap of 40**. A 29-table warehouse measured 13.9 per child with none at the cap, so the rate **rises with table count** as more parents compete for the same child. **Budget against the cap.** At ~15,000 columns that is **~600,000 tests**. Since 2026-10-28 a child's tests go out as ONE statement (up to the cap of 40), so the round trips are per tested child, not per test; the **815 ms per statement** measured on BigQuery before batching gave ~136 h serial / ~20 h at 8 workers for 600,000 statements, and the batched form has not yet been timed on a network warehouse (tests/test_fk_query_cost.py) |
| wall-clock | ~2,000 tables | ~8.8 s of other fixed cost per table — round-trips plus two LLM calls. Typing escalation, foreign-key validation and both Describe passes fan out over `--llm-workers` (default 4) and contend on nothing of yours; the second Describe pass reads its neighbours from a snapshot taken at the pass boundary, which is what lets it join the pool. Measured live on the 116-table `dbt_synthea` warehouse: 2,556 s serial, **354 s at 8 workers (7.2x)**, §2 below. Inclusion queries fan out over `--query-workers` (default 4) and do |
| output document | ~30,000 columns | 1,209 bytes per column, so 450,000 columns is ~544 MB in one file |

**Foreign-key accuracy is a disambiguation problem, not a budget problem.**
An earlier version of this page said recall was limited by the 40-test budget
per column. That was wrong, and the correction is worth stating plainly because
it changes what you should do about it: on a 116-table warehouse we classified
every missed key by cause, and **none** of them was a parent crowded out of the
budget. In 43 of 47 the right parent was tested and did pass the containment
check — and then lost to another table that also passed. Declaring your foreign
keys, or pointing us at your dbt project, is what fixes that; a bigger test
budget is not.

## Making it fast — the five things that matter

In rough order of effect.

### 1. Partition your large fact tables, and pass `--state`

The single biggest lever, and the only one that changes the shape of the
problem rather than its size.

```bash
semlayer infer <source> --state layer.state.json -o layer.yaml
```

Partition-wise profiling records a change token per partition and re-reads only
the partitions that moved. On a 4TB warehouse that is the difference between
**~35 hours and ~2 hours** per run on a row-store engine. Without partitions
there is no incremental mode: every run re-reads everything.

It also fixes the SHAPE of the output. Without `--state`, each partition is
profiled as a table in its own right, so a warehouse with 120 monthly
partitions emits 120 "tables" nobody models against. With it, the parent
absorbs them.

Backfills are handled: the change token moves when a partition is rewritten in
place, even though the row count does not.

### 2. Give the engine more parallelism

Everything heavy is pushed down as SQL, so your warehouse's own parallelism is
most of the speed. Serial to 8-way is roughly **31 hours to 5 hours** at 4TB.
A larger Snowflake warehouse, a higher Oracle DOP, more BigQuery slots. This
costs money and scales close to linearly.

Client-side concurrency is the other half, and it is measured rather than
assumed: on BigQuery, issuing the foreign-key queries over a small thread pool
gives **2.1× at 2 workers, 3.3× at 4 and 6.7× at 8**, with returns flattening
after that. Those figures are with BigQuery's result cache disabled and the
arms ordered so warm-up favours the serial baseline, so they are a floor. We
default to a small pool deliberately -- `--query-workers 4` -- because the
queries run in *your* warehouse and contend *your* slots; `--query-workers 8`
is available if you want it, and the output is identical at any value.

**Measured live, end to end (2026-10-27 and 2026-10-28).** The LLM-side pool
was argued from the query-side curve until it was run: the whole pipeline on
the 116-table `dbt_synthea` warehouse, discovery mode with `--manifest`, Haiku
4.5, on a 20-core machine with an in-process DuckDB file, so the warehouse
term is small and what remains is the model. Each row is one live run, about
$2.15 of Haiku, and the serial row was run once.

| `--llm-workers` | engine | profile | link | describe | total | speedup |
|---|---|---|---|---|---|---|
| 1 | 2026-10-27 | 413 s | 925 s | 1,218 s | 2,556 s | 1.0x |
| 8 | 2026-10-27, two of the four LLM loops in the pool | 62 s | 933 s | 678 s | 1,673 s | 1.53x |
| 8 | 2026-10-28, all four loops in the pool | 53 s | 142 s | 159 s | **354 s** | **7.2x** |

The first 8-worker run is kept because of what it found. Typing escalation
reached the 6.7x the query curve predicts, so the API was not the limit at
eight; Describe gained only 1.8x because its second pass was serial; and Link
did not move at all, because its foreign-key validation ran its 92 chunks one
after another and never read the pool setting, 910 s in both arms and the
largest single LLM term in the run. With validation and the second Describe
pass in the pool, every stage is within 10% of the 8x ceiling: profile 7.8x,
link 6.5x (of its 142 s, about 22 s are the inclusion queries themselves), and
describe 7.7x. Output was the same shape in all three runs, 116 tables and
1,312 columns, with 260 to 265 relationships; the run-to-run spread is the
temperature-0 disagreement described in `calibration.md`, not the worker count.

### 3. Declare your keys, and keep statistics fresh

Both are free and both remove work.

- **Declared `PRIMARY KEY` / `FOREIGN KEY`** are read from your catalog and
  used directly. A declared primary key also skips composite-key probing, which
  is up to 45 extra scans per table.
- **`ANALYZE`** (or your engine's equivalent) lets us read null rates and
  nominate key candidates from statistics the engine already has, instead of
  computing them. Stale or absent statistics mean we fall back to scanning.

### 4. Point us at your dbt project

```bash
semlayer infer <source> --manifest target/manifest.json -o layer.yaml
```

`dbt parse` is enough — no warehouse connection needed to produce the manifest.
It costs nothing at run time and it is **the single largest accuracy lever we
have measured**, not a nicety. On a 116-table OMOP warehouse, foreign keys on
the columns the project documents:

| | correct parent | of documented keys found |
|---|---|---|
| warehouse alone | 50.4% | 43.2% |
| with `--manifest` | **90.8%** | **86.5%** |

The reason is specific. When `stg_orders.customer_id`, `int_customers` and
`customers` all contain the same ids, containment cannot say which one the
model meant — but the project's own layout can, because a staging model means
the staging table and a published model means the published one. Your model and
column descriptions are also used as-is rather than regenerated.

### 5. Narrow the run

```bash
semlayer infer <source> --only orders,customers,order_lines
```

Most warehouses have far more tables than anybody models against — staging,
temp, deprecated, empty. `--only` takes table or `schema.table` names and fails
loudly on a name it cannot find, so a typo does not silently produce a thinner
layer.

## If you are past 2,000 tables

The free build has been walked up a ladder of synthetic warehouses
(`fixtures/generators/scale.py`: N tables in an enterprise mix of 8% core
star schemas, 22% staging copies, 45% raw shards, 25% scratch; every copy of
an entity shares its key values, so only names separate them). No LLM,
`--query-workers 8`, a 20-core machine, in-process DuckDB, 2026-10-28. Link
was the whole story, and two changes to it, both with byte-identical
output, moved the line:

| tables | columns | engine | profile | link | enrich | total | peak RSS |
|---|---|---|---|---|---|---|---|
| 2,000 | ~13,000 | before | 19 s | 362 s | 3 s | 395 s | |
| 2,000 | | parents ranked from a name index | 18 s | 118 s | 2 s | 150 s | |
| 2,000 | | + one statement per child | 19 s | 37 s | 3 s | 70 s | 0.55 GB |
| 5,000 | ~29,000 | before | 42 s | 917 s | 29 s | 1,000 s | 1.5 GB |
| 5,000 | | parents ranked from a name index | 43 s | 370 s | 32 s | 461 s | 1.5 GB |
| 5,000 | | + one statement per child | 43 s | 111 s | 35 s | 204 s | 1.0 GB |
| 15,000 | ~87,000 | + one statement per child | 149 s | 257 s | 477 s | 929 s | 2.4 GB |
| 15,000 | | + Enrich caches identical queries | 157 s | 225 s | 7 s | 443 s | 2.6 GB |
| 15,000 | | + tiers (raw/scratch set aside) | 143 s | 159 s | 5 s | 307 s | 2.6 GB |
| 50,000 | ~274,000 | + tiers (parent cap bites: recall 0.385) | 602 s | 1,444 s | 37 s | 2,240 s | 7.0 GB |
| 50,000 | | + named parents never truncated (recall 1.00) | 630 s | 2,782 s | 38 s | 3,609 s | 6.7 GB |

Totals include generating the synthetic warehouse (11 s, 33 s, 208 s) except
in the last three rows, where generation ran separately (229 s at 15,000;
3,230 s at 50,000, most of it one checkpoint of a 50,000-table catalog) and
the last row reused the kept warehouse. Table counts are
the generator's; the engine folds mechanical copies (same column signature
and name stem) into one representative, so the layers hold 1,844, 4,008,
11,682 and 35,925 tables respectively.

What the ladder found, in order. The per-child sort of candidate parents ran
on one core before any query was issued and grew as children times parents.
Then the inclusion tests themselves, one round trip per pair, at about two
milliseconds of overhead each around almost no work. Then aggregate
reconciliation, which tried every rollup candidate against every fact and
re-ran the same per-group SUM queries for each pairing, growing as the
square of the table count; it was invisible behind Link until Link was fixed.
All three are gone, each with byte-identical output. From 5,000 to 15,000
tables the engine now takes 1.4x the time for 3x the tables. Peak memory is
the one figure still climbing, roughly linearly in columns.

**At 50,000 tables the candidate-parent cap bit, and it has been removed for
named parents.** The engine kept at most 5,000 candidate parent columns; the
cap predates the name index and existed to bound a per-child sort the index
has since removed. The 50,000-table warehouse has 7,952 key columns that are
genuinely parents, so 2,952 were never considered, and because the list was
in table order the ones dropped were the staging copies. The failure was
legible rather than random: every one of the 8,820 staging foreign keys the
engine missed was asserted instead against the core dimension holding the
same key values, so no key went undiscovered, but against the generator's
gold (staging facts join staging dimensions) precision read 0.378 and recall
0.385. Named parents, the unique columns the naming vocabulary already
proposes, are now never truncated; the cap applies only to the speculative
key-shaped list. Rerun on the same warehouse: 14,972 relationships against
14,342 gold keys, recall 1.00, precision 0.958, zero wrong parents, and the
630 extras are the same 1:1 dimension-copy class as the 201 at 15,000.

The zero-score tail of each child's ranking, every same-family parent with
no name evidence, used to be walked in full per child and was the whole of
the Python term that grew as children times parents. It is now read from
the narrowest parent that can contain the child's values upward and stops
when the child's budget of 40 tests is full or 400 parents have been
examined; parents with name evidence are all still tested and are never
displaced by unnamed ones. Planning the 15,000-table run fell from 115 s to
37 s with an identical 528,060 statements.

Link's wall time at 50,000 nevertheless rose, 1,444 s to 2,782 s, with the
same statements per child, memory at 6.7 GB and about five cores busy. The
statement count did not change; the parents inside the statements did,
drawn from a pool six times larger that now includes every fact table's
unique key, and the open question is whether those parents cost more rows
per anti-join. It is recorded here as a measured regression at this one
size, correct output, not yet explained.

Tiers held at 50,000: 21,642 of 35,925 tables inventory-only, no core or
staging table set aside, 37 scratch tables read as core (0.8% of scratch).
The 140 MB layer parses in 46 s and every MCP tool but search still answers
in under 20 ms; search is 650 ms over 36,000 tables.

**Using the result.** The 15,000-table layer is 42 MB. Through the MCP server
on the same machine: the file parses once at startup in 14 s, the server
builds in 1 s, and after that every tool answers in under a millisecond
(table detail, metrics, routing, domains, listing all 11,682 tables in 5 ms)
except semantic search at about 200 ms per call, which scans every table and
column and would want an index past ~40,000 tables. The file was never meant
to be read whole by a person or a model; agents reach it a table at a time.

**Tiers.** The usability limit that remained was the review queue: 7,563
low-confidence items at 2,000 tables, 76% of them on raw and scratch copies
the engine had classified as business tables because it had no notion of a
warehouse tier. It has one now. Every table carries a `tier` (core, staging,
raw, scratch or unknown) inferred from its name and ETL metadata columns,
with confidence and provenance, and raw and scratch tables at 0.8 or above
are `modeling: inventory_only`: listed with their shape, excluded from
everything else (SPEC.md 2.13). On the same 2,000-table warehouse that took
the review queue to 1,708 items, Link from 220 s to 129 s, and foreign-key
precision against the gold from 0.31 to 0.94 with recall 1.00, because raw
copies no longer compete as join targets. At 15,000 tables (11,698 after
folding; 7,387 of them inventory-only) the engine asserted 4,562
relationships against 4,361 gold keys: recall 1.00, precision 0.956, zero
wrong parents; the 201 extras are dimension key columns linked 1:1 to
another copy of the same entity. The review queue there is 11,845 items
over the 4,311 modelled tables, plus two lines for the 7,387 set aside. On every real fixture the tier is
core or staging throughout, so nothing there changed. A reviewer who
disagrees with a tier changes it, and the next run models the table in full.

Two honest limits remain. The ladder is an in-process DuckDB file, so the
query term is CPU on one machine and does not predict a network warehouse,
where each statement is a round trip to your compute. And the warehouse is
synthetic: it has the shape of a large estate, not the content of yours. The
2,000-table line above is where the engine is tested on REAL warehouses and
it has not moved; the ladder says what to expect past it, not that it is
supported there. If you are past 2,000 tables on a real warehouse, tell us;
`--only` and a schema filter will get you a correct layer over the part that
matters meanwhile.
