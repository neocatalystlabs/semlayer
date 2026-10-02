# Metrics: one definition of revenue

Ask two people in your company for last quarter's revenue and you get two
numbers. Not because either of them is careless — because one of them knows
that cancelled orders were never supposed to count, and the other one doesn't.

A metric in a semlayer document is that knowledge written down: a measure, an
aggregation, the filter that makes the number right, and the date column that
makes "last quarter" mean something. You didn't write any of it. The engine
proposed it from your data, and it tells you where each part came from.

Every number and every block of SQL on this page comes from one run against
`fixtures/build/messy_mart.duckdb`, which ships with the repo. The fixtures are
seeded, so you get the same output.

## The shape of a metric

```yaml
name: total_tot_amt
type: simple
measure: ord_hdr.tot_amt
agg: sum
agg_time_dimension: ord_hdr.ord_dt
filter: sts_cd <> 'X'
lifecycle: inferred
confidence: 0.7
provenance:
  - signal: statistic
    detail: monetary measure on fact
  - signal: statistic
    detail: "business rule from aggregate reconciliation: sts_cd <> 'X'"
```

Six lines of definition and two lines saying where it came from. The first
provenance line is cheap — `tot_amt` is a money column on a fact table, so it
sums. The second line is the one that matters, and the rest of this page is
mostly about it.

`type` is `simple` (one measure, one aggregation) or `ratio` (a numerator and a
denominator). `agg_time_dimension` is the column that time-scoped questions
bucket on; metadata timestamps like `crt_dt` never qualify, because bucketing
revenue by its load date is exactly the silent error this field exists to
prevent.

## What it proposes, and from what

On messy_mart: **19 metrics — 14 simple, 5 ratios, 9 of them carrying a
discovered filter.** Four rules produce them.

| Rule | Produces | Example |
|---|---|---|
| A monetary measure on a fact table | `SUM(col)` | `total_tot_amt` |
| A fact table's primary key | `COUNT(pk)` | `ord_hdr_count` |
| A sum metric ÷ that table's key | the ratio every warehouse asks for | `avg_tot_amt_per_order` |
| A cleanly decoded status value meaning "done" | a filtered variant | `total_tot_amt_completed` |

The last one is deliberately narrow. A `_completed` variant is only proposed
when the status column's decodes came from joining a real decode table or from
your own documents — never from an LLM guess. A filter built on a guessed
meaning is a wrong number with a confident label on it, so the spec forbids it
outright (SPEC §2.8) and the producer never offers one.

Ratios are named from the table: `ord_hdr` becomes "order", so `avg_tot_amt_per_order`,
and it carries synonyms — "average order value", "tot_amt per order" — so an
agent asking in English finds it.

## The filter is the whole point

`total_tot_amt` filters on `sts_cd <> 'X'`. Nobody told the engine that. Here
is what it did.

messy_mart has a summary table, `dly_sls_agg`, that nobody documented. The
engine noticed it looked like an aggregate, guessed it summarised `ord_hdr`,
and checked — not on the grand total, which proves nothing, but group by group:

```
40/40 store_id groups; 1095/1095 agg_dt_key groups within 0.2%
```

It only reconciles under one condition: drop the rows where `sts_cd = 'X'`.
There are 818 of them. So the exclusion is not a guess, it is the only
hypothesis the arithmetic allows, and it goes into the document as a required
filter on the table with its evidence attached:

```yaml
required_filters:
  - expr: sts_cd <> 'X'
    enforcement: required
    scope: measures
    reason: >-
      amount aggregations over ord_hdr must exclude these rows: dly_sls_agg
      reconciles with ord_hdr only under this filter (discovered by per-group
      reconciliation); counts include all rows
```

and onto every money metric on that table.

What it is worth:

```
SELECT SUM(tot_amt) FROM ord_hdr                    -> 16,300,242.85
SELECT SUM(tot_amt) FROM ord_hdr WHERE sts_cd <> 'X' -> 14,621,503.30
```

$1.68M. Both queries run, both return a number, neither errors. An agent given
the raw schema has no way to know which one it was asked for.

### Scope: money, not counts

Read the last clause of that `reason` again — *counts include all rows*. A
cancelled order is still an order that happened. It belongs in "how many orders
did we take" and not in "how much did we make".

So the rule carries `scope: measures`, and the engine applies it accordingly:

```sql
-- total_tot_amt
SELECT SUM(ord_hdr.tot_amt) AS total_tot_amt
FROM ord_hdr
WHERE (sts_cd <> 'X')

-- ord_hdr_count
SELECT COUNT(ord_hdr.ord_id) AS ord_hdr_count
FROM ord_hdr
```

Same table, same discovered rule, and the count does not carry it. A consumer
that applies a `measures` rule to a count is over-filtering, which SPEC §2.2
says is just as non-conforming as under-filtering.

### The rule travels, when the data proves it

Cancelled orders have line items too, and `ord_ln` is a separate table that
knows nothing about `sts_cd`. The engine tested whether `SUM(line_amt)` per
order reconciles with `ord_hdr.tot_amt` — 8000 of 8000 keys within 0.2% — which
means the line measure genuinely decomposes the header measure, so the header's
rule must reach it:

```yaml
- expr: ord_ln.ord_id IN (SELECT ord_id FROM ord_hdr WHERE sts_cd <> 'X')
  scope: measures
  enforcement: required
  reason: >-
    inherited from ord_hdr (sts_cd <> 'X'): SUM(line_amt) per ord_id
    reconciles with ord_hdr.tot_amt (8000/8000 ord_id keys within 0.2%)
```

Inheritance is earned per table, by measurement. A rule does not spread because
two tables are joined.

## Compile the metric; do not assemble it

The definition is not the query. Turning one into the other is the step agents
get wrong: they forget the filter, they pick a join the schema allows but the
grain forbids, they bucket by the wrong date column. So the document ships with
a compiler, and SPEC §2.10 says consumers SHOULD use it rather than reading the
definition and writing SQL themselves.

```python
from semlayer.compile import compile_metric
compile_metric(doc, "total_tot_amt",
               group_by=["store_dim.store_nm"], time_grain="month")
```

```sql
SELECT date_trunc('month', ord_hdr.ord_dt) AS period,
       store_dim.store_nm AS store_nm,
       SUM(ord_hdr.tot_amt) AS total_tot_amt
FROM ord_hdr
LEFT JOIN store_dim ON ord_hdr.store_id = store_dim.store_id
WHERE (sts_cd <> 'X')
GROUP BY date_trunc('month', ord_hdr.ord_dt), store_dim.store_nm
ORDER BY date_trunc('month', ord_hdr.ord_dt), store_dim.store_nm
```

You asked for a metric, a dimension and a grain. The join, the business rule
and the date column came from the document. The same call over MCP is the
`compile_metric` tool; over the API it is the function above.

A date range narrows through the same time dimension, never through a column
the caller names:

```python
compile_metric(doc, "total_tot_amt", time_grain="week",
               time_start="2025-01-01", time_end="2025-04-01")
```

```sql
WHERE (sts_cd <> 'X') AND ord_hdr.ord_dt >= '2025-01-01' AND ord_hdr.ord_dt < '2025-04-01'
```

### Refusals, and why they are the feature

The compiler refuses rather than guesses, and every refusal names the reason
and lists what would have worked. Real responses:

```
compile_metric(doc, "total_tot_amt", group_by=["tot_amt"])
  refused: cannot group 'total_tot_amt' by 'tot_amt': it is a measure, not a dimension
  legal_group_by: chnl_dim.chnl_cd, chnl_dim.chnl_nm, curr_dim.curr_cd, ...

compile_metric(doc, "total_tot_amt", group_by=["ord_ln.line_sts_cd"])
  refused: cannot group 'total_tot_amt' by 'ord_ln.line_sts_cd': not on the base
           table or any N:1-reachable dimension

compile_metric(doc, "total_tot_amt", extra_filter="foo = 1")
  refused: filter references unknown columns: foo

compile_metric(doc, "revenue")
  refused: no metric named 'revenue'
  legal_group_by: total_tot_sls_amt, total_tot_amt, ord_hdr_count, ...
```

The second one is the interesting refusal. `ord_hdr` → `ord_ln` is one-to-many:
grouping an order total by a line attribute multiplies every order by its line
count, and the answer is wrong by an amount nobody can see. The compiler only
walks N:1 and 1:1 edges, so it cannot emit that query at all. It will also
refuse a dimension reachable by two equally short paths rather than silently
pick one (SPEC §2.6), and refuse a quarter or year bucket on a warehouse with a
verified fiscal calendar unless you say which calendar you mean.

A refusal is not a dead end, and it is specifically not permission to go around
the compiler. SPEC §2.10: a consumer MUST NOT fall back to self-assembled SQL
after a refusal. Retry inside the listed options, or show the human the reason.

## Routing: the summary table nobody told you about

Back to `dly_sls_agg`. Having proved what it is, the engine records it:

```yaml
- table: dly_sls_agg
  aggregates: ord_hdr
  grain: [agg_dt_key, store_id]
  measure_mappings:
    - source: SUM(tot_amt) WHERE sts_cd <> 'X'
      target: tot_sls_amt
  mapping_source: heuristic
  routing: {rule: pre-aggregated tot_amt by agg_dt_key, store_id, status: advisory}
  consistency: {method: per_group_reconciliation, status: consistent}
  confidence: 0.75
```

Read `measure_mappings.source` slowly. That is not "this table looks like a
rollup of that one". That is the business rule, recovered from inside a summary
table that no human annotated, stated as the SQL that reproduces it. messy_mart
has a second one, `mth_cust_agg`, reconciled the same way over 1484 of 1484
customer groups.

And it holds:

```
SELECT SUM(tot_sls_amt) FROM dly_sls_agg          -> 14,621,503.30
SELECT SUM(tot_amt) FROM ord_hdr WHERE sts_cd <> 'X' -> 14,621,503.30
```

To the cent. Which is what routing means: for a question at or above
`[agg_dt_key, store_id]`, the summary table answers it and gives the same
number. On a 1095-day fixture that saves nothing; on the warehouse where
somebody built the summary table in the first place, it is the difference
between a dashboard that loads and one that doesn't.

Separately, `repo_knowledge.routing` answers "which table do I even start
from", including pointing away from tables that are no longer the live ones:

```yaml
- intent: analysis of ord hdr
  use: [ord_hdr, dly_sls_agg, mth_cust_agg]
  avoid:
    - table: ord_hdr_legacy
      reason: legacy-named table; superseded by ord_hdr
  confidence: 0.6
```

`use` is a pointer list, not a decision. Before routing to anything after the
first entry a consumer reads that aggregate's own `grain` from
`aggregate_tables[]` and checks the question is covered — which is what SPEC
§2.7 requires anyway.

### Why it still will not swap the table for you

Every aggregate found this way arrives as `mapping_source: heuristic` and
`routing.status: advisory`, and SPEC §2.7 is blunt: heuristic aggregates MUST
NOT be routed to automatically. The validator enforces it — a heuristic mapping
with verified routing is a contract error.

That is not timidity, it is the honest reading of what was proved. The
arithmetic held *on the rows that exist today*. Nothing inspected the job that
builds the table, so nothing knows whether tomorrow's load applies the same
rule. `mapping_source: lineage` — the mapping read out of the ETL that actually
populates the table — is the value that earns automatic routing, and reading
dbt manifests is roadmap, not shipped.

Whether a human verdict should also earn it is an open question, deliberately
not settled here. What the engine does today is put the claim in front of a
person with its evidence:

```
[aggregate_mapping] aggregate_tables.dly_sls_agg
    claim: dly_sls_agg is SUM(tot_amt) WHERE sts_cd <> 'X'
           grouped by agg_dt_key, store_id
    evidence: reconciles per-group with ord_hdr.tot_amt excluding sts_cd <> 'X'
              (40/40 store_id groups; 1095/1095 agg_dt_key groups within 0.2%)
    accept/reject/skip [a/r/s]?
```

Accepting records a `reviewed` lifecycle and a `human` provenance entry, which
ranks the aggregate above an inferred one when a consumer is choosing between
candidates (SPEC §2.6). Rejecting deprecates the mapping and takes the table out
of `repo_knowledge.routing`, so nothing routes there by accident. Neither
verdict makes an agent swap the table silently.

### A reconciliation goes stale

The arithmetic was checked once, against rows that change. When the base table
or a mapped source column changes, `semlayer drift` stops the aggregate
claiming it reconciles — `consistency: unverified`, routing back to `advisory`
— until it reconciles again:

```
## Routing demoted (re-reconcile to restore)
- aggregate_tables.dly_sls_agg
```

The mapping itself is never rewritten. What expires is the measurement, not the
claim.

## What this does not do yet

Stated plainly, because the numbers above are good and these are not.

- **Recall is 8 of 20.** Against the hand-written gold metrics across the nine
  fixtures, the engine recovers 8. Ten of the twelve misses trace to two
  upstream causes, not to the metric rules: a table whose type was inferred
  wrong or left unknown (so no metric rule fires on it), and a fact table with
  no primary key inferred (so no count metric). Fix those and most of the gap
  closes.
- **The confidence numbers on metrics are not earned.** 0.60, 0.70 and 0.75 are
  constants chosen by rule, not measured rates — unlike the column and
  table-type tiers, which are calibrated and published in
  [calibration.md](calibration.md). Treat a metric's confidence as "which rule
  produced this", not "how often that rule is right".
- **Precision is unlabelled.** Some proposed metrics are wrong on sight —
  averaging a timezone offset, summing a unit price, metrics on a dimension
  table that was misclassified as a fact. Nothing on this page claims a
  precision figure, because nobody has labelled the set yet. The
  `metric_plausibility` review item is how that label starts being collected,
  but it is a net for one failure shape, not a measurement.
- **Derived and cross-table metrics are not attempted.** Ratios compile only
  when numerator and denominator share a base table. A returns rate over two
  fact tables is a definition you write yourself.
- **Fan-out guards are declared, not compiled.** The compiler sidesteps fan-out
  by refusing those paths (Phase A). SPEC §2.3's symmetric-aggregate strategies
  are Phase B.

## Using them

```bash
semlayer infer duckdb:warehouse.duckdb -o layer.yaml
semlayer review layer.yaml     # the queue, including the metric tier
semlayer mcp layer.yaml        # get_metrics + compile_metric for your agent
```

`review` queues three metric-tier claims, by claim kind rather than by a
confidence threshold — metric confidences are rule constants, so a threshold
would queue every ratio and nothing else:

| Kind | The question | On messy_mart |
|---|---|---|
| `discovered_filter` | is this recovered business rule real? | 2 |
| `aggregate_mapping` | does this summary table still mean this? | 2 |
| `metric_plausibility` | is this a metric at all? | 2 |

The third one is a precision net. It fires on a metric whose base table is not
an active fact, which is the shape of every known precision failure — a metric
on a dimension table misclassified as a fact, an average over something that
should never be averaged. On messy_mart it catches `total_tot_sls_amt` and
`total_tot_spend_amt`, both of which sum a column that is already a sum.

There is an indirect route too: promoting a guessed decode unblocks the
filtered metric variants that depend on it. A status column whose meanings were
only guessed cannot produce a `_completed` metric (SPEC §2.8); once a human
confirms the decodes, the next run can propose one.

If you edit the file by hand instead, mind which command you run next:
`semlayer infer` writes a new document and your edit is gone, while
`semlayer drift --apply` updates the document and keeps it.

Related: [MCP](mcp.md) · [calibration](calibration.md) ·
[context priors](context-priors.md) · [SPEC §2.2, §2.3, §2.7, §2.10](../spec/SPEC.md)
