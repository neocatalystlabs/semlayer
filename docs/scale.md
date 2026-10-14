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
[calibration.md](calibration.md) and [the cost note](../../prd/scan-cost-and-connectors.md).

| limit | bites at | why |
|---|---|---|
| **foreign-key accuracy** | **already, at any size** | when several tables contain the same key values, inclusion testing cannot tell them apart and the engine can pick the wrong one. On a 116-table dbt warehouse with no ETL manifest supplied, **half** the foreign keys it asserts on columns the project itself documents point at the wrong table. Supplying `--manifest` takes that from 50% to 91% correct (§4 below) |
| query volume | ~2,000 tables | foreign-key testing dominates: ~15,000 child columns × 40 tests ≈ 600,000 queries. Measured on BigQuery at **815 ms per query serial**, that is **~136 hours**, or ~20 hours at 8-way client concurrency |
| wall-clock | ~2,000 tables | ~8.8 s of other fixed cost per table — round-trips plus two LLM calls — and both are currently serial |
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
default to a small pool deliberately: the queries run in *your* warehouse and
contend *your* slots, and 8-way is available if you want it.

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
