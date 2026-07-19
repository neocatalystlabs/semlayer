# Semantic Layer Format — Specification v0.1

Machine-readable structure is defined by [`semantic-layer.schema.json`](semantic-layer.schema.json) (JSON Schema, Draft 2020-12). Documents are YAML or JSON. This file defines the **normative behavior** — what producers and consumers MUST do. Keywords MUST/SHOULD/MAY per RFC 2119.

## 1. Design principles

1. **Inference-native.** Every element can carry `confidence`, `provenance`, `conflicts`, and `lifecycle`. Absent `confidence` means human-authored. Consumers are told not just *what* is true but *how much to trust it* and *why it's believed*.
2. **Record, never enforce.** The format records access semantics (required filters, PII flags, access filters); the warehouse enforces access. A consumer that ignores recorded semantics produces wrong or unsafe answers — the contract below makes application mandatory.
3. **Compile, don't execute.** The format defines semantics; execution happens in the consumer's warehouse under the consumer's credentials.

## 2. Consumer contract (normative)

### 2.1 Trust: lifecycle × confidence

| lifecycle | confidence | Consumer behavior |
|---|---|---|
| `certified` / `reviewed` | any | MAY trust; SHOULD cite provenance on request |
| `inferred` | ≥ 0.9 | MAY use; MUST caveat ("inferred, unreviewed") in user-facing answers |
| `inferred` | 0.6 – 0.9 | MAY use only with caveat; SHOULD prefer a certified/reviewed alternative if one exists |
| `inferred` | < 0.6 | MUST NOT use to answer; MAY surface as a candidate |
| `deprecated` | any | MUST NOT use for new queries; MUST surface `deprecation.replacement` |
| `orphaned` | any | MUST NOT use; the underlying warehouse element no longer exists |

Thresholds are the published defaults; a document MAY NOT override them (engine configuration may, out of band).

### 2.2 Filter composition

The effective WHERE clause for any query over a table is the **union (AND)** of:
1. the metric's own `filter` (if querying via a metric),
2. every table `required_filters[]` entry with `enforcement: required`,
3. every applicable `access_filters[]` entry for the requesting user.

`enforcement: advisory` filters MUST be either applied or surfaced as an explicit caveat in the answer. Silence is non-conforming.

### 2.3 Fan-out safety

When an aggregation traverses a relationship with `fanout_risk: true` (or any `one_to_many` / `many_to_many` cardinality from the fact side), the compiler MUST either:
- apply the measure's `fanout_safety.strategy` (`symmetric_aggregate` or `distinct_key` using `symmetric_key`), or
- **refuse with an explanation** if no strategy is declared.

Silently summing across a fan-out is non-conforming. This is the single most common correctness failure in hand-built semantic layers; this format makes it structurally impossible for a conforming consumer.

### 2.4 Point-in-time (SCD2) traversal

A dimension column marked `temporal: scd2` MUST be resolved through the relationship whose `asof.enabled: true`, using `asof.on` as the driving date — native ASOF JOIN where the dialect supports it, otherwise `BETWEEN valid_from AND valid_to` from the table's `scd` block. A plain current-row join to an SCD2 attribute is non-conforming.

### 2.5 Time and hierarchy grains

Time bucketing MUST use a declared time hierarchy's `grain_expr` for the requested level — never ad hoc date math. "Quarter" and "year" respect `calendar.fiscal_year_start_month` when the question is fiscal, calendar otherwise; if ambiguous, ask. The level vocabulary is **closed**: a grain or dimension reference must name a declared level or `level_aliases` entry.

### 2.6 Disambiguation

When multiple objects plausibly answer an intent and no `repo_knowledge.routing` entry decides it, apply in order: (1) higher lifecycle (`certified` > `reviewed` > `inferred`); (2) higher confidence; (3) prefer a metric over a raw table aggregation. If the top two candidates remain within 0.1 confidence of each other at the same lifecycle, the consumer SHOULD ask a clarifying question instead of guessing.

### 2.7 Aggregate routing

A consumer MAY route a query to an `aggregate_tables[]` entry only when the requested grain and dimensions are covered by the aggregate's `grain` and the routing `status` is `verified`. `advisory` routing MAY be used with a caveat. `mapping_source: heuristic` aggregates MUST NOT be routed to automatically.

### 2.8 Enum decodes

Filters (metric or ad hoc) MUST NOT be built on enum values whose only `decode_source` is `llm_guess`. Guessed decodes exist to aid review, not to answer questions.

### 2.9 Unverified queries

`common_queries[]` entries with `verified: false` are advisory context. They are raw SQL outside the metric-safety contract (§2.2–2.3 do not apply to them); consumers SHOULD prefer compiling through metrics.

### 2.10 Metric compilation & refusal protocol

Consumers SHOULD obtain metric SQL via a conforming compiler (e.g. the
`compile_metric` tool) rather than assembling it from the definition — the
compiler applies metric filters, join paths, and time bucketing that manual
assembly gets silently wrong.

A conforming compiler MUST refuse (never guess) when: the requested group-by
is not on the metric's base table or an N:1-reachable dimension; a
time-scoped request targets a metric with no `agg_time_dimension`; an ad hoc
filter references unmodeled columns; or the metric/base table is
`deprecated`/`orphaned`. Every refusal MUST be constructive: it names the
reason and enumerates legal alternatives.

On refusal, a consumer MUST NOT silently fall back to self-assembled SQL over
the same tables. Conforming behavior is: retry within the enumerated legal
options, or surface the refusal reason to the human. Time-scoped questions
answered without a compiled time dimension MUST carry a caveat naming the
date column that was assumed.

## 3. Producer rules (normative)

1. Metrics MUST reference only modeled objects (measures, declared dimensions/levels) — never raw SQL against unmodeled columns.
2. Every SQL-bearing field is dialect-taggable; a bare string is the document's default dialect.
3. Re-inference MUST NOT overwrite `reviewed`/`certified` content; conflicts are recorded in `conflicts[]` and queued for review. When a certified object's warehouse element disappears, the producer MUST transition it to `orphaned` (never delete silently, never leave it `certified`).
4. Producers MUST populate `generated_by` (engine, model, timestamp) on generated documents; engine-version changes are a distinct drift class from warehouse changes.
5. `confidence` MUST be calibration-tested by the producing engine; a document's confidence values are meaningless without a published calibration report.

## 4. References

Reference syntax used throughout: `table`, `table.column`, `hierarchy.level`, `hierarchy.*` (all levels), `metric_name`. Producers MUST ensure all references resolve within the document; validators reject dangling references.
