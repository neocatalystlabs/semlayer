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
| query volume | ~2,000 tables | foreign-key testing dominates, and the cost is per CHILD column, not per column. On a 116-table warehouse: **23,198 inclusion tests over 667 children** — 1,312 columns exist but only half issue any test. That is **34.8 per tested child, median 40, with 74% sitting at the `MAX_QUERIES_PER_CHILD` cap of 40**. A 29-table warehouse measured 13.9 per child with none at the cap, so the rate **rises with table count** as more parents compete for the same child. **Budget against the cap.** At ~15,000 columns that is **~600,000 queries**; at **815 ms serial**, **~136 h**, or ~20 h at 8-way client concurrency (`--query-workers 8`; tests/test_fk_query_cost.py) |
| wall-clock | ~2,000 tables | ~8.8 s of other fixed cost per table — round-trips plus two LLM calls. The second Describe pass is serial; typing escalation, foreign-key validation and the first Describe pass fan out over `--llm-workers` (default 4) and contend on nothing of yours. Measured live on the 116-table `dbt_synthea` warehouse before validation was fanned out: its 92 validation chunks took 910 s in series in both arms, more than the whole of Describe at 8 workers, because each chunk returns ~1,270 output tokens and chunks ran one after another; inclusion queries fan out over `--query-workers` (default 4) and do |
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

**Measured live, end to end (2026-10-27).** The LLM-side pool was argued from
the query-side curve until it was run: the whole pipeline on the 116-table
`dbt_synthea` warehouse, discovery mode with `--manifest`, Haiku 4.5, on a
20-core machine with an in-process DuckDB file, so the warehouse term is
small and what remains is the model.

| `--llm-workers` | profile | link | describe | total | speedup |
|---|---|---|---|---|---|
| 1 | 413 s | 925 s | 1,218 s | 2,556 s | 1.0x |
| 8 | 62 s | 933 s | 678 s | 1,673 s | 1.53x |

Typing escalation reached the 6.7x the query curve predicts, so the API is not
the limit at eight. Describe gained 1.8x because its second pass is serial and
is ~600 s of the 678. Link did not move at all, and that was the finding: its
foreign-key validation ran its 92 chunks one after another and never read the
pool setting, 910 s in both arms, the largest single LLM term in the run. It
fans out now; the table will be re-measured.

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

Tell us. Supporting tens of thousands of tables needs a different approach in
two specific places — how foreign-key queries are batched, and whether the
output is one document or many — and we would rather design that against a real
warehouse than guess at it. In the meantime `--only` and a schema filter will
get you a correct layer over the part of the warehouse that matters.

Note the honest version of the 2,000-table line: it is where we have **tested**,
and at that size foreign-key discovery alone is tens of hours even with
client-side concurrency. It is a supported-scale statement, not a promise that
2,000 tables is quick.
