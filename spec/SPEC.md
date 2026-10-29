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

`scope` narrows where a filter applies: `all` (default) covers every query over the table; `measures` covers aggregations of the table's measure columns (SUM/AVG-style) and NOT row/event counts. A rule discovered because an aggregate table reconciles only under it is emitted with `scope: measures` — "revenue excludes cancelled orders; order counts include them". A consumer that applies a `measures` rule to a count is over-filtering, not conforming.

Rules may be inherited: a child fact whose measure provably decomposes a parent's measure (per-key reconciliation, evidence in `reason`) carries the parent's rule as a semi-join on the foreign key. Joining the parent and applying its rule directly satisfies the same requirement.

### 2.3 Fan-out safety

When an aggregation traverses a relationship with `fanout_risk: true` (or any `one_to_many` / `many_to_many` cardinality from the fact side), the compiler MUST either:
- apply the measure's `fanout_safety.strategy` (`symmetric_aggregate` or `distinct_key` using `symmetric_key`), or
- **refuse with an explanation** if no strategy is declared.

Silently summing across a fan-out is non-conforming. This is the single most common correctness failure in hand-built semantic layers; this format makes it structurally impossible for a conforming consumer.

### 2.4 Point-in-time (SCD2) traversal

A dimension column marked `temporal: scd2` MUST be resolved through the relationship whose `asof.enabled: true`, using `asof.on` as the driving date — native ASOF JOIN where the dialect supports it, otherwise `BETWEEN valid_from AND valid_to` from the table's `scd` block. A plain current-row join to an SCD2 attribute is non-conforming.

A table carrying an `scd` block (producers MAY infer it from validity columns, with `naming` provenance) MUST be read through its validity window for as-of questions and through `is_current_flag` (or `valid_to IS NULL`) for current-state questions; entity counts over it use `COUNT(DISTINCT natural_key)`, never `COUNT(*)`.

### 2.5 Time and hierarchy grains

Time bucketing MUST use a declared time hierarchy's level — its `column` or `grain_expr` — whenever the metric's time dimension resolves to a table that declares one, and MUST NOT re-derive that level with date math. Where no time hierarchy is declared for that table, dialect date truncation is the fallback. A level column that does not itself identify a year (a month or quarter number) MUST be grouped together with the hierarchy's year level, or the same month collapses across years.

Producers SHOULD declare a time hierarchy for any date dimension whose calendar attributes they have verified against the dimension's own date column. "Quarter" and "year" respect `calendar.fiscal_year_start_month` when the question is fiscal, calendar otherwise; if ambiguous, ask. The level vocabulary is **closed**: a grain or dimension reference must name a declared level or `level_aliases` entry.

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

### 2.11 Verification of consumer-written SQL

A consumer that writes SQL itself (rather than compiling a metric) SHOULD verify it against the document before execution — via the `check_sql` tool of a conforming server, or an equivalent linter over this format. The verifier is deterministic and reports, at minimum: references to objects not in the document; unqualified columns that resolve to an outer query block; reads of `deprecated`/`orphaned` objects; absent `required_filters` within their `scope`; additive aggregation of a parent's measure in a block that also joins a `fanout_risk` child; and SCD2 tables read without their validity window or current flag. Findings are `error` (the answer is wrong or unsafe), `warning` (probably wrong), or `info`. A consumer MUST NOT execute SQL with unresolved `error` findings without surfacing them; it SHOULD repair and re-verify. Execution-error repair alone is insufficient: the findings above are silent at execution time.

### 2.12 Absence vs. omission

A document's `tables` list is not a claim that nothing else exists. Where the producer emits `coverage`, a consumer MUST distinguish three cases: a table in `tables` (modelled); a table in `coverage.skipped` (exists, deliberately not modelled); and a table in neither (nothing is known either way). Asked about a `skipped` table, a consumer MUST NOT report it as absent. When the entry carries `same_shape_as`, the consumer MAY answer structural questions about it from the named table — the two share an exact column signature, which is why one was skipped — but MUST NOT assume their rows agree, and MUST NOT compile a metric over the skipped table without profiling it. A document without `coverage` makes no claim, and a consumer MUST NOT infer completeness from its absence.

### 2.13 Inventory-only tables

A table may carry `modeling: inventory_only`. It is in `tables` so that it can be found, and it carries its shape -- name, columns and their SQL types, and a `tier` with confidence and provenance saying why the producer set it aside (a raw landing zone, a change feed, a date shard, a backup, a scratch copy). Nothing else about it was inferred: no relationships point to or from it, no descriptions, metrics or aggregate claims were made over it, and it generated no review items. A consumer MUST NOT route a query to an inventory-only table, MUST NOT compile a metric over it, and MUST NOT report it as absent; asked about it, the consumer SHOULD say what it is and which modelled table holds the same data when the document names one. Absent `modeling` means `full`. A reviewer who disagrees with the tier changes it; on the next run the table is modelled in full.

## 3. Producer rules (normative)

1. Metrics MUST reference only modeled objects (measures, declared dimensions/levels) — never raw SQL against unmodeled columns.
2. Every SQL-bearing field is dialect-taggable; a bare string is the document's default dialect.
3. Re-inference MUST NOT overwrite `reviewed`/`certified` content; conflicts are recorded in `conflicts[]` and queued for review. When a certified object's warehouse element disappears, the producer MUST transition it to `orphaned` (never delete silently, never leave it `certified`).
4. Producers MUST populate `generated_by` (engine, model, timestamp) on generated documents; engine-version changes are a distinct drift class from warehouse changes.
5. A producer that enumerates more tables than it models SHOULD emit `coverage`. Skipping work is legitimate — profiling the four hundredth copy of a shape you already have is waste — but doing it silently makes the document indistinguishable from one describing a smaller warehouse.
6. `confidence` MUST be calibration-tested by the producing engine; a document's confidence values are meaningless without a published calibration report. (semlayer's own report: [docs/calibration.md](../docs/calibration.md).)
7. A producer that sets a table aside as `inventory_only` MUST still list it with its columns and a `tier` carrying the reason, and MUST err towards modelling: a table whose name declares no tier is modelled in full.
8. `name` is the document's identity when documents are combined, and `warehouse` says which warehouse the document describes. A producer scoping a run to one schema SHOULD name the document after that schema, and SHOULD write `warehouse` as a credential-free, portable identifier (the same string on every machine pointing at the same warehouse: `duckdb:<file basename>`, `postgres:<host>:<port>/<database>`, `snowflake:<database>`, `bigquery:<project>`). A consumer holding several documents compares `warehouse` values to decide whether two tables can meet in one SQL statement; it MUST NOT emit a single statement joining tables from documents whose `warehouse` differs unless the user has declared an engine that federates them.

## 4. References

Reference syntax used throughout: `table`, `table.column`, `hierarchy.level`, `hierarchy.*` (all levels), `metric_name`. Producers MUST ensure all references resolve within the document, and validators reject dangling references.

**Sets.** Several documents become one surface through a **hub**: a document whose `members` list names the other documents, each by its `name` and file path. Only the hub declares the set; a member carries no mark of membership, is a complete and valid document on its own, and may belong to several hubs. A hub may have no `tables` of its own. Inside a set every table, hierarchy and metric is referenced as `<document name>/<name>` (`finance/fct_gl`, `finance/fct_gl.amount`, `platform/cost_center_geo.*`), the hub's own objects included as `<hub name>/<name>`; a consumer resolves the set by qualifying every member's names and references that way, after which the set is one ordinary document. The hub's own `relationships` whose ends lie in different documents are the **seam**, the only place cross-document knowledge lives. A consumer MUST validate a hub together with its members, and MUST additionally reject a set in which a member's `name` disagrees with the hub's entry, two members describe the same `source` in one warehouse, or a relationship joins tables whose documents' `warehouse` differ while the hub declares no `federation` engine (rule 8).
