# Changelog

## Unreleased

### The review queue is questions, and agents can answer them

`semlayer review` groups the queue: two claims are one question only when
the engine made the identical claim for the identical reason at the same
confidence, so a question is the per-item questions with the name blanked
out. The 2,000-table benchmark estate's 1,708 items are seven questions
plus two lines that are shown but not asked (1,198 columns the engine could
not type at all, and the inventory-only tiers), because a verdict there
would change nothing. Each question prints the claim in words, what the
engine saw in words, the confidence, the distinct names, the objects, what
accept and reject do, and a stable key. Interactive answers can expand a
question (`e`) and leave objects out (`x 3,7`); `--answer K=accept
--exclude K=obj` does the same from a script; `--question N` prints one
question in full; `--flat` is the old one-at-a-time walk; `--list --json`
carries everything, untruncated, with the engine's raw reason strings.
Agents sign verdicts `--agent --by <name>`: the provenance signal is
`agent`, `--decisions` shows them under an `[agent]` heading, and `--note
"K=why"` leaves a note on a question an agent could not decide, stored in
`review_notes` and shown under the question. `--decisions` prints one line
per (day, author, verdict, claim) with the count and the first objects.
`infer -o` onto a file holding verdicts warns to use `--into`. Schema:
`agent` signal, `review_notes`. docs/review.md.

### `semlayer review --decisions`: the file says what you decided

A verdict used to leave `review: accept` in the column's provenance and
nothing else: no date, no author, and on a reject not even what was
rejected, since the claim is reset. Every verdict now writes a `human`
provenance entry with `date`, `by` (git `user.name` where the document
lives, or `--by`) and a detail in words: `accepted: holds the display name
of the row; grouped by (type name)`, `rejected: holds a dated version of
each record per row (a snapshot table) (table_type snapshot_scd2); now of
unknown kind`. No raw type name is printed to a person: `name rule ->
name` is gone. `semlayer review layer.yaml --decisions` lists the verdicts
newest first, grouped by date and author, with a closing count and the
number of items still undecided; `--json` for scripts. Entries written
before this release print with "(claim not recorded)". Schema: provenance
entries may carry `date` and `by`; SPEC producer rule 9.

### `semlayer stitch --refresh`, and seam verdicts that stick

`semlayer stitch --refresh acme.yaml` re-infers a hub's seam from the
members it lists. Edges the analyst accepted (`reviewed`), certified or
rejected (`deprecated`) are kept; inferred edges are replaced; a kept edge
whose end disappeared from its member becomes `orphaned` with the missing
end in its provenance. A fresh inference is dropped when a kept edge joins
the same two ends, or the same key column under an accepted edge; a
rejected edge blocks only itself, and a key whose only parent was rejected
is listed under `unresolved` saying so. `semlayer review acme.yaml` now
applies verdicts to seam edges and `same_entity` pairs (accept: reviewed;
reject: deprecated) and writes them to the hub file itself: before this it
saved the resolved set, which would have turned the hub into a copy of
every member's tables. Compiled SQL no longer traverses a deprecated or
orphaned relationship. SPEC §4 states the refresh rule. docs/sets.md.

### Stitch names copies: `same_entity`, `canonical`, `extends_with`

A member's copy of another member's dimension (finance's `dim_customer`,
the platform rows for its own customers plus two columns of its own) is
the same entity, not a fact and its parent. `semlayer stitch` now finds
such pairs (single-column entity keys sharing a name, key values contained
one in the other, tested through `--via`) and writes a `one_to_one`
relationship with `same_entity: true`, `canonical` (a certified copy first,
else the key superset; absent and queued for review when neither decides),
`shared_columns`, `extends_with` and the measured `key_overlap` both ways.
Surrogate names (`id`, `event_id`) never make a pair; an SCD2 table's
natural key counts as its entity key; among tables in one member holding
the same keys the one in the copy's naming layer wins. Set validation
checks the cardinality and that `canonical` is an end. On the 5,000-table
multi-schema estate: 6 shadow dimensions, 6 found, 0 false, canonical named
on all; seam unchanged at 1,019/1,019. Spec and schema gain the five fields.
docs/sets.md.

### The document is named for what it describes, and the file follows

`semlayer infer` without `--name` names the document for the schema it is
scoped to, the schemas joined with `_` when several, else the warehouse
itself (Snowflake database, BigQuery project, DuckDB file stem), and writes
`<name>.yaml`; it no longer falls back to `profiled` and
`semantic_layer.yaml`. `-o` and `--name` still override.

### A hub cannot yet be a member; names never contain `/`

Both are refused by set validation so that hubs of hubs, when they come,
have a clear grammar to grow into (`emea/finance/fct_gl`) and nobody today
builds on a top hub that silently found no keys.

### The "tested to 2,000 tables" warning is gone

The run no longer warns past 2,000 tables or 30,000 columns. The line was
drawn when the candidate-parent cap truncated silently; the cap on named
parents is gone and the ladder is measured to 50,000 tables both as one
file and per schema, so the warning said something that was no longer
true. docs/scale.md now states what is measured and on what: 50,000 on a
synthetic estate, about 2,000 on the largest real warehouse so far.

### `semlayer stitch`: infer the seam between several layers

`semlayer stitch finance.yaml sales.yaml platform.yaml -o acme.yaml --via
snowflake` finds every key column in the members that points outside its
own document, tests it against the other members' keys the way Link tests
inside one layer (name-ranked candidates, one inclusion statement per
child), through the connection the analyst supplies and so with the
analyst's grants, and writes the hub. Name-corroborated parents are written
at Link's confidence; statistics-only parents at 0.5, below the review
threshold; keys that match nothing go in the hub's new `unresolved` list.
Among equally-named parents a certified table wins. With no connection,
edges are proposed from names and distinct counts at 0.3 and marked
untested. Members are never modified. Statistics come from the member
documents, not from re-profiling. docs/sets.md.

Two things the first multi-schema estate taught it. An unresolved reference
is any column Link would test as a child that ended up with no target, not
just a `foreign_key` role: Link only promotes a column to that role when it
finds the parent, so a key whose parent lives in another document keeps its
profile role. ETL bookkeeping columns and plain attributes are excluded, so
`unresolved` lists keys, not `segment`. And the co-location tiebreak now
reads a table's stage prefix through its `member/` name, so a staging fact
in one schema still prefers the staging copy of its dimension in another,
as it would inside one layer. On a 300-table, 7-schema synthetic estate the
seam scores precision 1.0 and recall 1.0 against the 83 cross-schema gold
keys.

### The scale generator has a `--schemas` layout

`fixtures/generators/scale.py --schemas` puts each domain's star schema in a
schema of its own, the conformed dimensions in `platform`, every copy in the
schema of what it copies, and a shadow `dim_<entity>` in some domains: a
filtered copy of a platform dimension with two columns of its own. The
sidecar names tables `schema.table` and records which gold keys cross
schemas. The flat layout is byte-identical to before.

### Every command takes a hub; compiled SQL uses `source`

`semlayer mcp`, `lint`, `review` and `compile_metric` work on a hub as they
do on one file: the CLI resolves a hub and its members into one document
on load. Compiled SQL now names tables by their `source` with an alias
derived from the set name (`FROM finance.fct_gl AS finance_fct_gl`) wherever
the bare name would not do, which also fixes a table in a non-default
schema compiling to an unqualified name; a single-schema layer compiles
byte-identically to before. A join path that crosses tables in different
warehouses is refused with both named unless the hub declares a
`federation` engine. The linter matches SQL table references by `source` as
well as by name. MCP lists each member as a domain. Review of a hub shows
the seam edges below the threshold and the hub's own claims, and members
keep their own queues. docs/sets.md.

### Sets: a hub document combines several teams' layers into one surface

A hub is a layer with a `members` list naming other layer files; it may
have no tables of its own. Members are ordinary layers that know nothing
of the set, so finance, sales and platform each infer and own their own
file and the same file can sit in several hubs. Resolving a set qualifies
every member's tables, hierarchies and metrics as `<name>/<object>`
(`finance/fct_gl`) and rewrites every reference the same way, after which
the set is one ordinary document; `source` is untouched, since it is what
SQL uses. The hub's own cross-member relationships are the seam.
`semlayer validate hub.yaml` validates the resolved set and adds the set's
own rules: member names match and are unique, no two members describe one
`source` in the same warehouse, and a relationship across warehouses needs
a declared `federation` engine. SPEC.md section 4; the reserved
`document_set` and `part` fields are withdrawn, unused by any producer.
Consumers over a set (mcp, compile, lint, review) and `stitch`, which
infers the seam, follow.

### Scoping to a schema names the document and the file; documents record their warehouse (spec 0.5.0 → 0.6.0)

`semlayer infer <source> --schema finance` profiles only that schema, names
the document `finance` and writes `finance.yaml` unless `-o` says otherwise.
`--name` overrides the document name. Every document now carries
`warehouse`, a credential-free and portable identifier of the warehouse it
was inferred from (`duckdb:<file basename>`, `postgres:<host>:<port>/<db>`,
`snowflake:<database>`, `bigquery:<project>`). Both exist for the case
where several teams' layers are combined: `name` is what other documents
reference, and `warehouse` is how a consumer knows whether two tables can
meet in one SQL statement (SPEC.md producer rule 8). A run with no scope is
unchanged apart from the new field.

### Inventory-only tables are listed from the catalogue, not profiled

The tier is decided from the table's name and column names, which the
catalogue already gives, so a raw shard or scratch copy is known to be
inventory-only before any query runs. Profile now skips the per-column
statistics scan for those tables and lists them with their columns and
types, which is all SPEC.md 2.13 says a consumer gets for them. At 50,000
synthetic tables 21,642 of 35,925 were inventory-only and Profile was the
largest linear stage at 685 s; it is 235 s now, with peak memory 7.0 -> 6.0
GB, the layer 140 -> 122 MB and the same relationships. Modelled tables
are unchanged; on every real fixture every table is modelled, so nothing
there changed.

### `--query-workers` defaults to every core on an in-process DuckDB file

The default stays 4 on a network warehouse, where the queries contend on
the customer's compute. An in-process DuckDB file is this machine's own
compute, and at 50,000 synthetic tables 8 workers on 20 cores left the
cores idle and the workers waiting on reads; at 20 the same statements took
1,849 s instead of 2,618 s. The run report records the value actually used.

### Link reports where its time went

`infer` writes a `link` block to the run report and logs one line at the end
of candidate generation: parents and children considered, statements issued
and tests inside them, and seconds spent building the parent index, planning
each child's tests (Python, one core) and running the statements (the
warehouse, under `--query-workers`). Added because Link doubled at 50,000
synthetic tables with an identical statement count and nothing recorded
which of the three had grown.

### Named foreign-key parents are never truncated; the zero-score tail is bounded

At 50,000 tables (35,925 after folding) the candidate-parent cap of 5,000
dropped 2,952 of the 7,952 columns that were genuinely parents. The list
was in table order, so the ones dropped were the staging dimensions, and
every one of the 8,820 staging foreign keys was then asserted against the
core dimension holding the same key values: gold precision 0.378, recall
0.385, where 15,000 tables had scored 0.956 and 1.00. The cap predates the
parent index and existed to bound a per-child sort the index removed.

Now the named parents, unique columns the naming vocabulary already
proposes, are never cut; the cap applies only to the speculative key-shaped
list. What still cost children times parents was the zero-score tail: for
every child the engine walked every same-family parent with no name
evidence at all, which was the whole of Link's superlinear term (9x the
time for 3x the tables between 15,000 and 50,000). Those parents can only
reach the review queue on their own, so they are now read from the
narrowest key domain that can contain the child's values upward (a real
parent's distinct count sits just above its child's) and the read stops
when the child's budget is full or 400 have been examined. Parents with
name evidence are all still tested and are never displaced by unnamed ones,
in the ranking or in the co-location tiebreak.

Rerun on the same 50,000-table warehouse: recall 1.00, precision 0.958,
zero wrong parents; the 630 extras are dimension keys linked 1:1 to another
copy of the same entity. Link's wall time there rose from 1,444 s to 2,782 s
with the same statements per child, which docs/scale.md records as an open
regression at that one size. Measured on dbt_synthea without a manifest:
257 relationships where there were 256 (payer_transitions.SECONDARY_PAYER
found stg_synthea__payers; none lost), floors unchanged, and
claims.PATIENTID reaches the review queue against patients.Id for the first
time, since the two share 1,171 values and no name token. The scale warning
now says what is bounded: statistics-only candidates per column, not the
parent list.

### Every table gets a tier; raw and scratch tiers are listed, not modelled (spec 0.4.0 → 0.5.0)

Most of a large warehouse is not the part anyone models against. On a
15,000-table synthetic estate in a realistic mix (8% core star schemas,
22% staging copies, 45% raw shards and change feeds, 25% scratch) the
engine had no notion of that: it profiled, linked, reconciled and queued for
review every raw shard and backup as if it were a business table. At 2,000
tables 76% of the review queue sat on raw and scratch tables, every one of
them received a full foreign-key budget, and the raw copies competed as
join targets, so most statistical foreign keys pointed at a copy instead of
the dimension.

Two optional table fields. `tier` is one of core, staging, raw, scratch or
unknown, with confidence and provenance, inferred from the name's prefix and
suffix vocabulary in precedence order (scratch first; declared staging and
core prefixes beat raw-looking suffixes; raw by prefix, suffix or date shard)
and from ETL metadata columns (two or more). A name with no signal is core at
0.5: unprefixed is canonical, and under-modelling a real table is the error
the engine refuses. `modeling` is `full` or `inventory_only`; raw and scratch
at confidence 0.8 or above are inventory-only. SPEC.md 2.13 says what a
consumer does with one: it is in `tables` with its columns and types, nothing
else was inferred about it, and no query may be routed to it.

What inventory-only means in the engine: no typing escalation, no candidacy
on either side of a foreign key, no declared edges, no table type, no
composite-key probing, no hierarchies, no description, no metric, aggregate,
freshness, grain, SCD or filter claim, and one review line per tier instead
of one per column. Measured on the 2,000-table synthetic warehouse, no LLM:
foreign-key precision 0.31 → 0.94 against the gold with recall 1.00, review
queue 7,563 → 1,708 items, Link 220 s → 129 s. On every real fixture the
tier is core or staging throughout, so nothing else changed: every floor
and every cassette replay is as it was. Suffixes that fired on real tables
when tried (`_snapshot`, `_hist`, `_log`, `_source`, `_test`, `source_`)
are excluded with the reasons recorded in `semlayer/tier.py`.

The 0.31 figure was measured against a gold the data contradicted (staging
copies in the generator sampled a subset of keys); the generator is fixed
and both figures above are on the corrected warehouse.

### Enrich caches identical queries; layers open with the C loader

Aggregate reconciliation tries every rollup candidate against every fact
and runs per-group SUM queries on both sides per measure pair and per
exclusion hypothesis; the fact side is the same query for every candidate
and the aggregate side the same for every fact. On a 15,000-table
warehouse the stage took 477 s, growing as the square of the table count.
A read-through cache over the source for the life of one Enrich run (bounded
by rows held; the warehouse is read-only during a run, so the output cannot
change) took it to 7 s. 5,000 tables: 88 s to 4 s, layer byte-identical.

Every command that opens a layer used PyYAML's pure-Python loader: 44 s for
a 6 MB layer, minutes for the 42 MB one 15,000 tables produce. The C loader
(same grammar, verified to return the same document) parses the 42 MB file
in 14 s. After startup every MCP tool answers in under a millisecond except
semantic search at ~200 ms, measured on that layer. The scale warning no
longer claims the output "becomes impractical to load".

### Link scales: parents ranked from a name index, one statement per child

Two changes to foreign-key discovery, output byte-identical, measured on a
synthetic 5,000-table warehouse (`fixtures/generators/scale.py`, no LLM,
`--query-workers 8`, 20-core machine, in-process DuckDB): Link 917 s to
111 s, the whole free build 1,000 s to 204 s, peak memory 1.5 GB to 1.0 GB.

*Ranking.* Candidate parents were sorted by name similarity for every child
column, scoring children x parents pairs in Python before the first query:
at 5,000 tables that loop ran on one core for most of Link while the other
nineteen idled. `ParentIndex` keys parents by the four things a nonzero
score can depend on, scores only a child's block and appends the rest in
list order, which is exactly what the stable sort produced.
`tests/test_candidate_blocking.py` checks that against the brute-force
sort on seven fixtures and on the names that exercised each scoring branch.

*Batching.* Inclusion tests were one query per (child, parent) pair, about
two milliseconds of round trip around almost no work. A child's candidates
now go in one statement, up to the 40-per-child cap: the child's distinct
values computed once, one `sum(CASE WHEN NOT EXISTS ...)` column per parent.
Same predicate, same ratios; 24,847 statements became 637 on a 300-table
warehouse. The shape parses in duckdb, postgres, snowflake, bigquery and
oracle and the worker pool's unit is now a child column.

### Describe pass 2 fans out too; it reads a pass-boundary snapshot

The second Describe pass read each neighbour's description as it was
written, so a table described later in the pass saw a neighbour's pass-2
text rather than its pass-1 text. That was deterministic only because the
loop was serial and sorted, and it was the one LLM loop the worker pool
could not reach: about 600 of the 678 s Describe took at 8 workers on the
116-table `dbt_synthea` warehouse. Pass 2 now reads every neighbour from a
snapshot taken when the pass begins, builds all its prompts up front, and
runs them over `--llm-workers` like pass 1. Output is byte-identical at any
worker count. Measured live on that warehouse with all four LLM loops in the
pool: 354 s at 8 workers against 2,556 s serial, 7.2x (`docs/scale.md` §2).

The snapshot changes the prompts, so the model reworded descriptions: on
messy_mart 18 of 36 table descriptions and 113 of 198 column descriptions,
no table types. The description judge (Sonnet, `tests/test_describe.py`)
scored the new text 0.867, the same as the old, with all three negative
controls at 0.000. Because the benchmark embeds descriptions in its
prompts, its cassettes were re-recorded under the same model IDs; the
Sonnet row reproduced exactly (0.55 / 0.87 / 0.87); the Haiku rows moved by
one or two questions each (messy_mart semantic 0.87 -> 0.89, ontology 0.82
-> 0.79; tpcds_clean semantic 0.42 -> 0.58), which is the answering model's
run-to-run variance, and `docs/benchmark.md` now states the recording date
and the point-in-time policy beside the table.

### Spend estimate is per unit of work, not per table

`examples/assisted_build` estimated LLM spend as $0.70 per 100 tables, the
messy_mart figure from `docs/cost-model.md`. A live run on the 116-table
`dbt_synthea` warehouse cost 2.6x that, because Describe scales with
columns and FK validation with the candidates that survive inclusion
testing, which rise faster than table count. The estimate is now
`columns x $0.00077 + tables x $0.0027 + inclusion_tests x $0.000035`,
which reproduces both measured warehouses within 20%; cost-model.md
carries the second measurement and the derivation.

### Output-token ceilings sized to the payload

Every LLM call had a flat 1,500-token output ceiling. The provider detects
truncation and repeats the call at four times the budget, so nothing was
lost, but the first attempt was paid for twice. Measured live on the
116-table `dbt_synthea` warehouse: a 25-column typing escalation returned
~2,050 tokens and retried in both arms, and a 33-column Describe reply
(~1,440) and a 30-candidate FK chunk (~1,437) sat within 5% of the cap,
where a slightly chattier answer would tip them over. Each call site now
passes a ceiling from its item count at roughly twice the measured tokens
per item (escalation ~90 per column, Describe ~53, validation ~42 per
candidate), never below 1,500 and never above the provider's 24,000. A
ceiling costs nothing unless used; cassette keys do not include it, so
every recorded replay is unchanged.

### Link's foreign-key validation fans out over `--llm-workers`

The validator asks the model about 30 candidates per call and ran those
calls one after another. On the 116-table `dbt_synthea` warehouse that was
92 calls of ~1,270 output tokens each, 910 s in series, measured live in
both arms of the timing run: the single largest LLM term in the pipeline,
larger than the whole of Describe at 8 workers, and it ignored the worker
setting. It now runs `llm_workers` chunks at once with the same shape as
the other fan-outs: a worker only calls and parses, verdicts are merged on
the calling thread in chunk order, and a worker that raises fails the run.
Output is identical at any width: proven by thread tests on a fake
provider, and by replaying both arms' live cassettes through the full
pipeline at 1 and 8 workers to byte-identical YAML.

### `examples/assisted_build`: probe, estimate, confirm, build

One script that chains the shipped primitives into a run that says what it
will do before it spends anything: the probe's evidence grade and
confidence cap, which of its own warnings apply (provisional grade, no
manifest, how many tables are repeat copies), an estimate of wall-clock and
LLM spend extrapolated from the measured figures in `docs/scale.md` and
labelled as such, then `infer` with both concurrency pools, validation, and
the review-queue count. The report beside the layer carries the probe
payload, the estimate and the options, so a layer can be reproduced from
its report. It asks before building unless `--yes` is passed or stdin is
not a terminal.

### API failures surface as `LLMError` with a next step

A rate limit, a bad key, a 5xx or an unreachable API used to escape the
provider as a raw SDK traceback. Each is now an `LLMError` with a hint, and
the live client retries six times with the SDK's backoff instead of the
default two -- sized for a worker pool, where several calls land in the
same second and a small account's rate limit answers all of them at once.
A rate limit that survives the retries names `--llm-workers` as the fix,
and records nothing, so re-running resumes where it stopped.

### `InferOptions`: the run's settings as one object

`semlayer.pipeline.infer` and `infer_with_report` had grown to ten
positional-capable parameters, and each new knob meant threading it through
four signatures by hand. The five settings that shape a run -- `no_sample_egress`,
`only`, `use_declared`, `llm_workers`, `query_workers` -- now live in a frozen
`InferOptions` dataclass; the inputs a run operates on (source, LLM, context,
lineage, state) stay as parameters. The run report records the options under
`options`, so a document can be reproduced from its report. Worker counts
below 1 are refused up front. The individual keywords still work unchanged;
passing both forms is an error, not a merge.

### Foreign-key inclusion queries run concurrently (`--query-workers`, default 4)

Link's inclusion loop is the long pole at scale: ~600,000 queries at 2,000
tables, ~136 hours serial at BigQuery's 815 ms round-trip. The set of
queries is fixed before any of them runs -- the per-child cap counts tests
issued, not matches found -- so they now go out over a small pool and the
ratios are applied in the original order. The accepted candidates are
byte-identical at any worker count, and a test proves it on three fixtures.
Each worker thread gets its own executor via `source.for_worker()`: a fresh
cursor on DuckDB, the shared client on BigQuery and Snowflake, the shared
(serialising) connection on Postgres. A source without `for_worker` stays
serial rather than share a connection across threads. The default is 4
because these queries run in *your* warehouse: measured 3.3x at 4 and 6.7x
at 8, flat after (docs/scale.md).

### The two per-table LLM calls run concurrently (`--llm-workers`, default 4)

`semlayer infer` now issues Profile's typing escalation and the first
Describe pass -- one call per table each, from that table's own evidence --
over a thread pool. The document is
byte-identical at any worker count, and tests replay the committed
messy_mart cassettes at 1 and 8 workers to prove it for both calls; a worker that raises
fails the run rather than leaving a table silently undescribed. Pass 2 stays
serial on purpose: it reads neighbour descriptions as it writes them, so
fanning it out changes every prompt, and that change has to be judged
(docs/calibration.md), not assumed. Link's inclusion loop is unchanged and
remains the long pole at scale (docs/scale.md).

### Telemetry: a non-zero exit is classified

A command that returned exit code 1 without raising -- `validate` on an
invalid or missing document, `drift` finding drift -- recorded
`outcome: error` with no `error_class`. Those rows were indistinguishable
from a crash. They now carry `error_class: invalid_input`. No new field, no
schema bump; the receiver already stores the class.

### Duplicate copies are profiled once (spec 0.3.0 → 0.4.0)

Two tables that share a name stem and an exact column signature are copies of
each other — `orders` / `orders_v2`, `ga_sessions_20161121` through `…25`.
Across 287 real warehouses, 67% of an analytics warehouse's tables are copies
of this kind (ga360 100%, ebi_chembl 94%), against ~0% of an application
schema (one ERP's 799 tables: zero).

`semlayer infer` now profiles one member of each group and records the rest in
a new document-level `coverage` block:

```yaml
coverage:
  tables_seen: 412
  tables_modelled: 139
  skipped:
    - name: analytics.ga_sessions_20161122
      reason: duplicate
      same_shape_as: analytics.ga_sessions_20161125
```

Nothing disappears: a consumer asked about a skipped table can still answer
that it exists and what shape it has (SPEC.md §2.12). `--only` disables
deduplication, because an explicit table list is the scope.

Which member gets profiled: the unsuffixed name if there is one, never a
`_backup`/`_old`/`_tmp` copy while anything else is available, otherwise the
lexicographically greatest name — which for dates and version numbers is the
most recent.

`coverage` is a new optional field, so this is a MINOR spec bump and existing
documents remain valid.

## v0.6.0-beta.1 (2026-10-20)

### Telemetry is now sent, and here is how to stop it

Previous releases recorded anonymous usage statistics to a local file and
transmitted nothing. **This release sends them** to
`telemetry.semlayer.dev`, which is our own infrastructure — no third-party
analytics service is involved.

The first run prints a notice saying so, once.

```sh
semlayer telemetry --show     # exactly what would be sent, before it is
semlayer telemetry --off      # permanently off
```

**What is sent**: command, duration, outcome, error class, warehouse type,
bucketed table and column counts, version, Python major.minor, OS,
architecture. **What is never sent**: table or column names, schema or
database names, connection URIs, credentials, SQL, cell values, file paths.

That is enforced rather than promised. Every field passes through a
whitelist; a caller cannot send an undeclared key because the serializer has
no path that writes one, and counts are bucketed there rather than at call
sites, so an exact count cannot be sent by mistake. Full list and reasoning:
[docs/telemetry.md](docs/telemetry.md).

**Events collected by earlier versions are discarded, not sent.** They were
recorded while the documentation promised that nothing left your machine, so
transmitting them would make that promise retroactively false. The first run
of this version deletes the existing spool and starts fresh.

Sending happens at the end of a command, in a background thread with a
1.5-second deadline. A slow or unreachable endpoint cannot slow or hang your
command, and the spool is cleared **only** when the server confirms receipt
— a failure never destroys pending events.

`--no-llm` is unchanged and still means no schema, names, or samples reach a
model provider. It has never been a statement about all network traffic, and
`SECURITY.md` says so explicitly.

### Ordinary mistakes no longer look like crashes

A mistyped filename and an unsupported warehouse URI both produced a Python
traceback. They now produce an error and a hint:

```
$ semlayer validate nope.yaml
FAIL  nope.yaml
        file not found: nope.yaml

$ semlayer infer snowflake://nope
error: unsupported source uri: snowflake://nope
hint:  expected one of: snowflake, bigquery, duckdb:<path>, postgres, postgresql://<dsn>
```

`validate` reports a missing file and carries on with the rest, rather than
stopping on the first one.

### `--no-llm` is documented more precisely

`SECURITY.md` previously said `--no-llm` meant "nothing leaves your
environment at all". It means no schema, column names, or sample values
reach a model provider — a statement about where your data goes, not about
all network traffic. The wording was corrected before telemetry made the old
phrasing false, not after.

### The telemetry off switch now works

`semlayer telemetry` shows whether telemetry is on, where the spool is, and
what is in it. `--off` turns it off permanently, `--on` turns it back on,
`--show` prints the spool exactly as it would be sent.

**`telemetry.py` previously documented an off switch that did not exist.**
Its docstring named a `telemetry` key in `~/.semlayer/config`; nothing has
ever read that file, and this package has no config file and no parser for
one. Only `SEMLAYER_TELEMETRY` worked. Anyone who followed that instruction
had not opted out.

Nothing was transmitted either way — there is still no sender, and this
release adds none. But the claim had to stop being false before one exists,
not alongside it.

`--off` writes a marker file rather than a config key, so there is nothing
to parse and therefore no malformed-input path that could leave telemetry on
while you believe it is off. `semlayer telemetry` is also the one command
that is never itself recorded.

If you have `SEMLAYER_TELEMETRY=off` exported, `--on` cannot clear it; the
status output says so by name rather than silently reporting success.

## v0.5.0-beta.1 (2026-10-17)

### Licence change: the engine and CLI are now Fair Source

**This release and every release after it are licensed under
[FSL-1.1-ALv2](LICENSE.md)** — the Functional Source License 1.1 with an
Apache-2.0 future grant. Previously everything was Apache-2.0.

**Releases 0.1.0b1 through 0.4.0b5 remain Apache-2.0, permanently.** Those
artifacts are published on PyPI with `Apache-2.0` in their metadata and cannot
be changed by us or anyone else.

What FSL permits, free, for anyone: internal use for any business purpose,
including in production and CI, with no fee, key or permission; modification,
kept private if you wish; redistribution with the licence attached;
non-commercial education and research; and professional services delivered to a
licensee. The single prohibition is **Competing Use** — offering semlayer to
third parties in a commercial product or service that competes with semlayer or
with anything we offer using it.

`semlayer mcp` is the question people ask first: running it inside your own
organisation, for your own agents and analysts, is internal use and permitted.
Running it as a service other companies connect to is Competing Use.

**Each version converts to Apache-2.0 on the second anniversary of its
publication**, automatically, by the terms of the licence. This release
converts on 2026-10-17 + 2 years.

**The format stays open.** The specification, its JSON Schemas and the dbt
exporter are Apache-2.0 and will stay that way — a semantic layer nobody else
can read is worth nothing. Each of those directories carries its own `LICENSE`,
and every source file carries an `SPDX-License-Identifier`.

We call this **Fair Source**, not open source. FSL is not OSI-approved, so
"open source" would be inaccurate and we do not use it. The README tagline and
the package description changed accordingly.

See [**LICENSING.md**](LICENSING.md) for the paths table, the conversion dates,
and policy text your security team can paste into an allow-list. Note that
GitHub's licence detector does not recognise FSL, so the repository sidebar now
reads "Other"; PyPI shows `FSL-1.1-ALv2` correctly, because the SPDX identifier
is registered.

### Also

- Package metadata moves to a PEP 639 licence expression
  (`License-Expression: FSL-1.1-ALv2`), which is machine-checkable; the old
  free-text `License` field was not. Requires `setuptools>=77` to build.
- `LICENSE` is now `LICENSE.md`, which is the canonical form for this licence.


- **SPEC (additive, MINOR at release): every column carries a `cardinality`
  block.** What is known about its distinct count, and how that was obtained:

  ```yaml
  cardinality:
    distinct: 60000          # point estimate, always inside the bounds
    lower_bound: 59753
    upper_bound: 60000
    method: hll_sketch       # exact | hll_sketch | engine_statistic | bounds_only
    null_count: 0
    as_of: 2026-10-13T06:06:54+00:00
    partitions_covered: 6
  ```

  Published because a consumer can then decide whether a `GROUP BY` is sane,
  whether a join will fan out, and whether a filter is selective, without
  issuing a query -- and because the UNCERTAINTY travels with the number
  instead of being dropped. A whole-table profile counts exactly and says
  `exact`; a partition-wise profile often only has an interval, and
  `bounds_only` admits that rather than presenting a bound as a measurement.
  `exact` is never claimed for an estimate.

  Note for the release cut: `calendar_semester`/`fiscal_semester` is the other
  additive change waiting, and both can ride one MINOR bump.

- **The Snowflake session role is passed explicitly, and required.**
  `snowflake.connector.connect` never received a `role=`, so the session ran
  under whatever the account's `DEFAULT_ROLE` happened to be. That worked only
  because `semlayer init` creates a service user with `DEFAULT_ROLE` set to the
  reader role it also creates, and broke silently the moment anyone ran
  semlayer as their own account -- profiling under every privilege they hold.
  A minimal-grant claim is about the role actually IN EFFECT, so inheriting one
  made the claim unverifiable.

  `SNOWFLAKE_READER_ROLE` (or `SNOWFLAKE_ROLE`) is now required, with an error
  naming the variable, and the role in effect is recorded in the run report
  under `access.session_role`. `semlayer init snowflake` prints the exports.
  `from_env(role=...)` is renamed `persona=...` on both connectors, since
  `role` now means the session role; BigQuery has no session-role equivalent,
  so its minimal-grant claim rests on the service account's IAM bindings.

- **Native COMMENT text is read as prior knowledge.** If a warehouse has
  column comments, a human has already written down what we are inferring, and
  their sentence is better evidence than any naming heuristic -- yet we ignored
  them entirely while nine separate naming vocabularies were failing on schemas
  we did not author.

  Table and column comments become a `description` carrying provenance
  `catalog_prior` (already in the spec's signal enum, so the format is
  unchanged), and **Describe no longer overwrites one**. That enforces the
  "priors are never overwritten, gaps are filled" rule for the first time,
  because comments were not read at all before.

  DuckDB is verified end to end, and exposes comments ONLY through
  `duckdb_tables()`/`duckdb_columns()` -- not `information_schema`, which is
  why a generic query finds nothing. Snowflake is implemented from its plain
  `comment` column; the nightly live test has no commented objects, so it is
  unverified and degrades to no priors rather than a failure. **BigQuery is
  deliberately not implemented**: its descriptions live in
  `INFORMATION_SCHEMA.COLUMN_FIELD_PATHS` and `TABLE_OPTIONS`, dataset-scoped
  rather than project-wide, and SQL I cannot run would fail silently as "this
  warehouse has no comments" -- indistinguishable from the truth and worse than
  a recorded gap.

- **`table_type` is now scored, and reported DIRECTIONALLY.** It was never in
  `scoring.py` -- only `fixtures/coverage_audit.py` computed it -- so a
  classifier sitting at 0.65 corpus-wide never appeared in any gate. `Score`
  gains `n_tables`, `table_type_accuracy`, `dim_called_fact`,
  `fact_called_dim` and `table_type_confusions`.

  Split because the two directions are not equally costly and one number hides
  that. A dimension called a fact produces spurious metrics, which land in the
  review queue where a human rejects them: visible and cheap. A fact called a
  dimension produces NOTHING and silently drops the table out of aggregate
  reconciliation -- the mechanism that recovers an undocumented filter -- with
  no wrong answer to notice. Across three fixtures: **5 dimension->fact
  against 1 fact->dimension**, so the expensive direction is the rare one,
  which the single 0.73 figure did not say.

- **`role_accuracy_strict`, and a number of ours it corrects.** Lenient role
  accuracy credits primary/unique/natural key confusions as adjacent --
  reasonable while nothing emitted `natural_key`, misleading the moment
  something did. The `natural_key` detector was reported in this release as
  worth +0.009 on `tpcds_clean`, which is the lenient figure; the leniency had
  been crediting the exact confusion the detector removed. Strictly it is
  **0.800 -> 0.854, +0.054** -- six times larger. Reported alongside rather
  than replacing, so no published number moves.

- **A table that looks like a rollup and reconciles with nothing now says so.**
  Before aggregate detection became shape-based, "no `aggregate_tables` entry"
  and "never examined" were the same thing and silence was honest. Now a
  candidate can be examined against several facts, have its grouping map, and
  still tie out with nothing -- and silence reads as "no summary table here",
  a different and wrong statement. CMS's per-provider totals are exactly this:
  they do not reconcile because the detail carries AVERAGES, so the totals are
  `SUM(avg * count)`, a product the engine cannot express.

  Gated on grain coverage -- one row per distinct combination of its grain in
  the detail -- not on matching column names. A name count was the first
  attempt and produced seven notes on AdventureWorksDW claiming `DimEmployee`
  was a rollup of `ProspectiveBuyer` and `FactInternetSales` a rollup of
  `FactResellerSales`. The test is one-directional: a rollup MAY cover grain
  combinations absent from the detail, which is precisely CMS's 13 of 400
  fully suppressed providers, and a symmetric 2% tolerance rejected the very
  phenomenon the note exists to report. One note on CMS, zero across four
  other warehouses.

- **`infer --only` profiles just the named tables.** The targeted half of
  incremental re-inference: `drift.py` already computes which tables changed
  and what depends on them, and this lets that answer be acted on instead of
  re-profiling the warehouse. The filter applies after `list_tables()` so no
  connector changes -- enumeration is one information_schema query,
  per-column profiling is the expensive part. A name matching nothing is an
  error with a hint, not a quietly thinner layer. Merging a subset back INTO
  an existing layer is deliberately not included: it has to interact with
  drift's orphaning state machine, which governs what may disappear.

- **Key discovery consulted naming before statistics, so a textbook foreign
  key could be invisible.** `link/candidates.py` built its parent and child
  lists from `_KEYISH_RE` (`(_id|_key|_sk|_cd|_code|_no|_nbr|_number)$`) and
  from entity roles that themselves came from naming rules. A column outside
  that vocabulary never became a candidate, so its uniqueness and inclusion
  were never measured. The tier order is documented as statistics, then
  naming, then LLM; naming was in fact the gate.

  Found on CMS's Medicare Physician files. `Rndrng_NPI` is the National
  Provider Identifier: unique across 400 summary rows, non-unique across the
  detail, **100% inclusion**. It ends in `_npi`, so Link returned **zero
  relationships on a two-table warehouse**. It now returns the one correct
  relationship and nothing else.

  Parents are now any unique, populated, key-capable column, whatever it is
  called. Continuous numerics are excluded -- a unique DOUBLE is a measure
  that happens not to repeat -- and a one-row table is excluded, because it
  makes every column unique. Named candidates are ordered first so
  `MAX_PARENTS` truncates the speculative tail rather than dropping a real key
  by dict order. Children are any non-unique dimension of a key-capable type,
  not only numeric ones: widening parents alone changed nothing, because the
  CMS detail column is a VARCHAR typed enum/dimension and failed every child
  test too.

  Two guards were needed, both discovered by measurement rather than design:

  **Alternate-key paths are collapsed, role-playing is kept.** Natural-key
  paths were previously invisible, so widening newly produced
  `ord_hdr.ord_dt -> date_dim.cal_dt` alongside the existing
  `ord_hdr.date_key -> date_dim.date_key`. Both have 100% inclusion and a
  unique parent; statistics cannot separate them. Asserting both hands the
  compiler two equally-short paths between the tables and it refuses to group
  through the ambiguity, so the redundant path costs a capability. The
  distinction is that role-playing uses different child columns referencing
  the SAME parent column (`cs_sold_date_sk` and `cs_ship_date_sk` both reach
  `d_date_sk`) while alternate keys reference DIFFERENT parent columns. Keying
  the de-duplication on the parent column instead collapsed role-playing and
  took `tpcds_clean` from 102 relationships to 79.

  **Dates and names are not key references.** `ord_rtn.rtn_dt` is 100%
  contained in `date_dim.cal_dt` with an exactly matching token, and
  `etl_log.job_nm -> job_ctrl.job_nm` scores 1.20 on naming -- identical to
  the CMS case we want. Statistics and naming both say yes; only the semantic
  type says no. Admitting them took messy_mart's FK precision from 0.973 to
  0.900 against a floor of 0.95 that this release would otherwise have
  shipped broken.

  Final state: `cms_physician` 0 -> 1 correct relationship; `messy_mart`,
  `tpcds_clean`, `collision_heavy` and AdventureWorksDW byte-identical to
  before (37/33, 102/101, 11/11, and 39 found / 32 correct at recall 0.78).

- **A dimension with two numeric columns was classified as a fact.** Every
  graph-signal dimension rule required `n_measures == 0`, which caught only 39
  of 99 gold dimensions corpus-wide; the rest fell through to the measure-heavy
  fact rule. `customer` in `tpcds_clean` is referenced by **eleven tables** and
  was called a fact because it has 2 measures. Fan-in beats measure count: a
  dimension with a few numeric attributes is still a dimension. Measured over
  the corpus, `fk_in > fk_out` catches 74 of 99 dimensions and only 2 facts.
  The strong fact rule also now requires `fk_in == 0` — nothing references a
  fact — which lifts its precision from 5 wrong dimensions to zero.

  Table naming rules now fold the name the same way column rules do. This was
  the third place the PascalCase blind spot was hiding: `DimCustomer`
  lowercased to `dimcustomer`, which the `_`-delimited dimension rule never
  matched, so on a PascalCase warehouse every `Dim*` table was left to the
  graph heuristics.

  Fan-in alone over-reaches, because a fact can be referenced by other
  facts: `fan_trap`'s `orders` has `payments` and `shipments` pointing at it,
  and the ungated rule relabelled it a dimension — breaking the fixture built
  to test that shape, and the only pre-existing table_type floor test we have
  (`test_describe.py`, fan_trap >= 0.99), which the `--no-llm` audit never
  exercised. The rule is guarded on shape instead: a dimension is DESCRIPTIVE
  and therefore wide, so a table with five columns or fewer that carries a
  measure is a transaction record, not a dimension. The guard costs 22 of the
  74 dimensions the bare rule catches.

  **`table_type` accuracy 0.65 -> 0.72 corpus-wide, 0.50 -> 0.79 on
  `tpcds_clean`, and 0.58 -> 0.92 on AdventureWorksDW** scored against the
  vendor's own Dim/Fact prefixes. Every existing test stays green, no fixture
  regresses, and semantic type and entity role are unchanged at 0.815/0.845.
  This metric had never been scored before this week — `scoring.py` covers
  semantic type, entity role and primary keys only.

  The 27 remaining corpus errors are mostly `dimension -> unknown|operational`
  on tables where Link found no relationships at all (`fk_in == 0`), so they
  are blocked on Link recall rather than on this classifier.

- **Numeric columns with few distinct values were labelled codes, so nobody
  could total them.** `store.s_floor_space` holds square footage; on a small
  table it has six distinct values, and the numeric fallback read "few distinct
  values" as "this is a status code". A code is something you group by, so
  total floor space became unaskable. 16 columns on `tpcds_clean` alone.

  Three separate changes, each measured on its own because the first attempt
  bundled them and traded one tier against the other:

  1. The fallback now returns `quantity` with role `dimension` — the right
     type, and the grouping role the low cardinality actually evidences.
     Returning `measure` would have been equally wrong: for 44 of 57 such
     columns in `tpcds_clean` gold says `dimension`. In the population that
     reaches this fallback gold says `quantity` 81 times and `code` never,
     because every numeric code in the corpus is name-signalled.
  2. A generic `_cd`/`_code` suffix rule, plus grade/bucket/band/tier. The
     existing code rule matched a vocabulary of stems (typ, cat, class, seg,
     chnl, rsn), so `zone_cd` and `priority_cd` fell through to the numeric
     fallback and came out as quantities.
  3. `cntry` added to the geo_country vocabulary. The new generic rule
     shadowed it: `cntry_cd` previously fell through to the LLM, which
     correctly called it `geo_country`, and the generic rule overwrote that
     with a plain `code` on three tables.

  Deterministic type accuracy **0.786 -> 0.815** across 785 gold columns, role
  unchanged at 0.845, and no fixture regresses on either.

  The LLM tier is unchanged at **0.904/0.778** on messy_mart (0.894 with
  `--no-sample-egress`), which took two further corrections to achieve. The
  generic rules are pinned at confidence **0.5**, not 0.55: `_rule_name_table`
  adds +0.15 to any code with <= 30 distinct values, so 0.55 landed on exactly
  0.70 — not *below* `ESCALATE_BELOW` — and those columns stopped reaching the
  LLM. Since `_role_for` maps every code to `dimension`, three columns that are
  really primary or foreign keys then regressed. The type signal from a `_cd`
  suffix is reliable; the role signal is not, so the LLM must still be asked.
  No committed cassette is invalidated.

- **Every PascalCase key column in the world typed as a measure.** Found by
  acquiring AdventureWorksDW, a real Microsoft sample warehouse, as the first
  external evaluation dataset. Every naming rule in `profile/typing_rules.py`
  matches `_`-delimited tokens — `_ID_NAME_RE` is `(^|_)(id|key|sk|pk)$` — so
  `DateKey` lowercased to `datekey`, matched nothing, and fell through to the
  numeric fallback as `semantic_type=quantity, entity_role=measure`.

  One miss, and the whole chain fell over: with no identifier columns there were
  no keys to test for uniqueness or inclusion, so on that warehouse we found
  **0 of 41 declared foreign keys and 1 of 28 declared primary keys**, and
  `DimDate` and `DimCustomer` were classified as fact tables.

  `classify()` now folds names to lower_snake_case at the entry point
  (`normalise_name`), so every existing rule sees tokens regardless of the
  warehouse's convention. Measured against Microsoft's own declared
  constraints: **FK recall 0.00 -> 0.78** (32 of 41, precision 0.82), **PK
  0.04 -> 0.75** (21 of 28), review queue 306 -> 159 items. Snake_case names
  are unchanged, and `messy_mart` scores byte-identically before and after
  (type 0.818, role 0.778, PK precision 0.870, recall 0.690) — the fixture
  corpus never exercised this because we authored all of it in snake_case.

- **New: `fixtures/external/`** — acquired real-world evaluation datasets. The
  repo ships the fetch script, the licence record and the checksum; the data is
  gitignored and never committed. Datasets are classified into two tracks: **P**
  (permissive explicit licence or a US-government work — gold may ship and
  numbers may be published) and **I** (share-alike, non-commercial, unknown, or
  credentialed — runs locally, no derived number may ever be published). A
  published number is valid only if every dataset behind it is track P, and P
  and I figures are never averaged.

- **New: `fixtures/coverage_audit.py`** — reports, per fixture, which shapes are
  present in the data, which are asserted in gold, and which the engine emits.
  These are three different findings with three different fixes, and conflating
  them produced four wrong gap claims in an earlier planning document.

- **New: `examples/agent_pipeline/`** — three agents in a chain (pull revenue,
  break it down by category, escalate a plan breach) run twice over the same
  warehouse: once with `list_schema` + `run_sql` only, once with the semlayer
  MCP server attached over real stdio. Replays from committed cassettes, so it
  needs no API key. Raw mode reports $16,300,242.85 against a true
  $14,621,503.30 and files a ticket for a $300,242.85 plan breach that did not
  happen; layer mode reports the right number and files nothing. The whole
  divergence is one `WHERE sts_cd <> 'X'` the layer recorded and the bare
  warehouse could not express. `tests/test_agent_pipeline.py` pins the result.

  Two things the example documents rather than hides: the layer worked here by
  **prevention, not correction** (the agent read the filter from `table_detail`
  and got it right first try), and fan-out never arose in this chain, so the
  fan-out guard is demonstrated by `test_fan_trap.py` instead of staged here.

- **`check_sql` turned an unknown dialect into a stack trace.** Found by the
  example above: a model called it with `dialect="sql"`, sqlglot raised
  `Unknown dialect`, the agent received opaque text and retried with `"tsql"`
  — linting DuckDB SQL as T-SQL. `lint_sql` now validates the dialect up front
  and returns an `unknown_dialect` finding whose `fix` lists the valid names,
  which the agent can act on. Recoverable by the loop, as every other finding
  already was.

- **The benchmark table reported 34/38 as 0.90.** `pass_rate` is stored as
  `round(passed/total, 3)` and the report formatted that again to 2dp, so
  0.8947 became 0.895 and printed as 0.90 — while the HEADLINE note and the
  README both correctly said 0.89. The measured result never changed; only the
  rendering was wrong. The table now formats from the raw count, so the double
  rounding cannot recur.

## v0.4.0-beta.5 (2026-10-03)

Spec `0.3.0`; §2.5 reworded to describe achievable behaviour (see below).

- **Nothing ever built a time hierarchy, so the compiler could not obey the
  spec that required one.** SPEC §2.5 said time bucketing MUST go through a
  declared time hierarchy and never use ad hoc date math; `compile.py` emitted
  `date_trunc`. Both were defensible alone and contradictory together, because
  no inference stage produced a hierarchy for the compiler to use.

  Enrich already classified each integer column on a date dimension by checking
  it against `EXTRACT(...)` on every row — `yr_nbr` → `calendar_year`,
  `qtr_nbr` → `calendar_quarter`, `mth_nbr` → `calendar_month`. Those are
  levels; nothing collected them. It now emits a `kind: time` hierarchy per
  date dimension, calendar and fiscal, levels most-general first.

  `compile_metric` buckets through it: `date_dim.yr_nbr, date_dim.mth_nbr`
  rather than `date_trunc('month', date_dim.cal_dt)` — the customer's own
  calendar columns instead of re-deriving what they already materialised. A
  fact's own date column still truncates, correctly, because there is no
  declared calendar to honour.

  This also removes an inconsistency: the compiler already grouped *fiscal*
  quarters by the customer's `fiscal_quarter` column while ignoring the
  verified `calendar_quarter` column beside it. `_fiscal_cols` and
  `_fiscal_group_cols` are replaced by one hierarchy lookup serving both.

  A month or quarter column carries no year, so grouping by it alone collapses
  the same month across years. The year level now always travels with it, and
  §2.5 states that as a MUST.

  §2.5 is conditioned on a hierarchy existing rather than demanding one
  unconditionally, and gains a SHOULD telling producers to declare one where
  they have verified a date dimension's attributes.

- **`--no-sample-egress` was documented as costing accuracy it does not cost.**
  The claim in `docs/cost-model.md` — "~1 point of typing accuracy
  (0.904 -> 0.894)" — was measured on one fixture in July, before the typing
  rules were calibrated. Re-measured live across 7 fixtures / 759 columns with
  `semlayer.scoring.score_types`:

  | mode | semantic type | entity role |
  |---|---|---|
  | `--no-llm` | 0.780 | 0.834 |
  | default | 0.809 | 0.838 |
  | `--no-sample-egress` | 0.809 | 0.842 |

  Corpus-wide the privacy mode is free: typing identical to three decimals,
  role marginally better. The old figure still reproduces exactly on messy_mart
  (-0.010), but collision_heavy moves +0.016 the other way and they cancel — on
  a warehouse built to collide names, withholding values stops the model
  over-trusting values that look alike. Both numbers are now published.

  New `tests/test_no_sample_egress.py` pins the privacy promise as well as the
  parity. `_evidence` has an `enum_decodes` branch that is not gated on the
  flag; it is harmless only because Describe runs before Enrich, so the only
  decodes present are the model's own guesses from column names. The test fails
  if that ordering ever changes, which would start sending real decode values
  in a mode that promises it does not.

## v0.4.0-beta.4 (2026-10-02)

Spec `0.3.0`, unchanged.

- **`compile_metric` silently fanned out on history tables.** A join path
  crossing a type-2 table joined every version of the entity, so each fact row
  was counted once per version — no error, no warning, a plausible answer that
  was wrong. On messy_mart, filtering average order value to customers in
  California counted 820 rows for 663 orders and overstated the answer by 1.2%.
  Our own linter already flagged it (`scd2_without_validity`), and SPEC §2.4
  calls a plain current-row join to an SCD2 attribute non-conforming.

  `compile_metric` (and the `compile_metric` MCP tool) now takes
  `scd: "current" | "asof"`, and refuses without it. The two are different
  questions — where a customer lives *now* versus where they lived *when they
  ordered* — and on this fixture they select 594 orders against 452 and return
  different numbers, so picking one silently would be guessing on the caller's
  behalf. Same stance the compiler already takes on ambiguous join paths and
  unstated fiscal calendars.

  `current` pins to `is_current_flag` (or an open `valid_to`); `asof` drives
  the validity window off the metric's `agg_time_dimension`, never a
  caller-supplied date. Both predicates are emitted in the `ON` clause, not
  `WHERE`, where they would turn the `LEFT JOIN` inner and silently drop fact
  rows. A `snapshot_scd2` table carrying no `scd` block is refused outright —
  there is nothing to resolve it with.
- **Every metric was emitted with an empty `grain`.** The metric producers copy
  their base table's grain, but ran ten lines before `_infer_grain` populated
  it, so all of them carried `''`. Grain is what one row of the source
  represents, and it is what distinguishes "average order value" from "average
  line value" — the same column, the same SQL, a different question. Grain
  inference now runs before the producers.

  Separately, a reconciled summary table has no primary key, so `_infer_grain`
  could not state its grain either — even though reconciliation had just
  measured the group columns against the fact table. Those are now backfilled
  from the proven mapping (`one row per agg_dt_key, store_id (pre-aggregated)`).
  On messy_mart all 19 metrics now state a grain, where none did before.

## v0.4.0-beta.3 (2026-10-02)

Spec `0.3.0`, unchanged.

- **The engine stamped documents with the wrong version.** `ENGINE_VERSION`
  was a hardcoded copy in `profile/run.py` that the 0.4.0-beta.2 bump missed,
  so that build wrote `generated_by.engine: 0.4.0b1`. SPEC §3 rule 4 makes
  engine version its own drift class, so a wrong stamp hides a re-inference it
  should have triggered. It is now derived from the installed distribution and
  asserted against both `__version__` and the distribution metadata, so it
  cannot drift again. Documents written by 0.4.0b2 carry the wrong engine
  version; re-run `infer` to correct the stamp.

## v0.4.0-beta.2 (2026-10-02)

Spec `0.3.0`, unchanged.

- **`semlayer review` covers the metric tier.** Three new item kinds, queued by
  claim kind rather than by a confidence threshold (metric confidences are rule
  constants, not measured rates): `discovered_filter` (a business rule
  recovered by reconciliation — accepting promotes it, rejecting removes it from
  the metric *and* the table's `required_filters`, and flags any table that
  inherited it), `aggregate_mapping` (accepting records the human verdict as a
  `reviewed` lifecycle; rejecting deprecates it and removes the table from
  routing), and `metric_plausibility` (a metric proposed on a table that is not
  an active fact — the shape of the known precision failures). Verdicts on
  these are the first precision labels the metric tier has had.
- **Drift stops a stale aggregate claiming it reconciles.** An aggregate whose
  base table or mapped source columns changed falls back to
  `consistency: unverified` / `routing: advisory` until it reconciles again,
  reported as a "Routing demoted" section in the changeset. The mapping itself
  is never rewritten.
- **Routing no longer drops aggregates.** A fact with two reconciled aggregates
  listed one of them; `repo_knowledge.routing[].use` now carries all of them.

## v0.4.0-beta.1 (2026-09-12)

Spec `0.3.0` (MINOR: one new optional field, one new contract section).

- **Semantic SQL linter** (`semlayer.lint`, new runtime dependency
  `sqlglot`, MIT). Deterministic, no LLM: `parse_error`, `unknown_table`,
  `unknown_column`, `correlated_reference` (an unqualified column that
  silently resolves to the outer query — the vacuous `IN (SELECT …)`),
  `deprecated_table`, `missing_required_filter` (scope-aware),
  `fanout_aggregate`, `scd2_without_validity`. Every finding carries a fix
  hint. Surfaces: `check_sql` MCP tool (server instructions ask agents to
  run it before executing), `semlayer lint <doc> [file|-]` (exit 2 on
  errors, 1 on warnings — CI-friendly), and a lint-fed repair round in the
  benchmark answerer (`semantic+lint` condition). SPEC.md §2.11.
- **`required_filter.scope`** (`all` | `measures`): a rule that applies to
  amount aggregations but not to event counts is now structured, not prose.
  Reconciliation-discovered rules are emitted this way. SPEC.md §2.2.
- **Rule propagation to child facts.** A parent fact's measure-scoped rule
  is inherited by a child fact only when `SUM(child.measure)` per FK key
  reconciles with the parent's measure (≥95% of keys within 0.2%); the
  evidence rides in the filter's `reason`. Unverified inheritance emits
  nothing.
- **SCD2 mechanics inferred.** `snapshot_scd2` tables get a `scd` block
  (valid_from / valid_to / current flag / natural key) from their validity
  columns, plus an as-of usage rule; `grain` is stated in words from the
  primary key.
- **Ratio metrics.** `avg_<measure>_per_<entity>` (SUM / COUNT(pk)) on
  every fact, carrying discovered rules; `compile_metric` aggregates ratio
  terms by their column's default (a key counts); the validator accepts
  `table.column` ratio terms (compile/export already did).
- **Search and context.** `semantic_search` expands warehouse
  abbreviations, stems, and ranks staging/deprecated tables below canonical
  ones; the agent context renders grain, SCD mechanics, join cardinality
  and fan-out warnings, filters before notes, only question-relevant
  metrics; deprecated tables render as a stub with columns withheld.
- **Benchmark methodology v2** (docs/benchmark.md): result scoring is
  projection-tolerant (extra/reordered columns, scalar in any column of a
  single row, midnight timestamps as dates, dictionary labels resolved to
  codes); eight messy_mart questions whose wording contradicted their gold
  SQL were reworded to the gold (MM-5, MM-9, MM-24, MM-25, MM-28, MM-29,
  MM-32, MM-33 — MM-9's gold also lost an unrequested count column). Both apply to
  every condition; before/after under both methodologies is published.
- Ontology non-inferiority is reported by the CQ harness, no longer
  asserted (the condition is internal-only and sits at the band's edge).

### Previously unreleased (now in this release)

- **Reconciler: per-group verification.** Aggregate reconciliation now
  verifies every mapped grouping per group (key-aligned for same-name group
  columns; multiset for date-key↔date-column pairs) at ≥95% coverage within
  0.2% relative tolerance — grand totals alone are never accepted. Evidence
  rides in provenance ("40/40 store_id groups; 1095/1095 date groups").
- **compile_metric: multi-hop joins.** Snowflaked dimensions are now
  group-by-able across up to 3 N:1 hops; equal-length join-path ties
  (role-playing dims, diamonds) refuse constructively as ambiguous.
- **`--context` doc-promotion.** Columns explicitly named in your docs get a
  doc-prompted second look even when the heuristic was confident; corrections
  always land with a conflict recorded for review.
- **Fiscal calendars.** Date-dimension attributes are classified against the
  dim's own date column (`time_attribute`: calendar_* / fiscal_* — verified,
  never assumed). When a warehouse carries a verified fiscal calendar,
  `compile_metric` quarter/year requests require an explicit
  `calendar='fiscal'|'calendar'` choice — never a silent Gregorian
  assumption; fiscal bucketing groups by the customer's own fiscal columns.
- **Benchmark runs from a pip install.** `python -m semlayer.benchmark`
  resolved `fixtures/` and `cassettes/` relative to its own file, which lands
  in site-packages once installed, so the documented reproduction command
  only worked inside a source checkout. Both now fall back to the directory
  the run starts in, and a cassette directory is accepted only if it actually
  holds recordings — an empty one created by an earlier failed run used to
  win and produce a `CassetteMiss` on the first prompt.
- **Drift survives a renamed column.** `semantic_drift` walked the columns in
  the document and probed each enum column by name against the live warehouse,
  without checking it still existed. A rename (the most ordinary schema change
  there is) killed the whole run with a binder error instead of reporting the
  change. It now probes only columns the warehouse still has: a renamed column
  is reported as the old one dropped plus the new one added, and the old one is
  orphaned. The freshness check had the same flaw and is fixed with it.

## v0.3.0-beta.1 (2026-07-19)

- **`compile_metric`** (new MCP tool + `semlayer.compile`): compiles any
  declared metric to correct SQL — N:1 joins, business-rule filters, and
  time bucketing applied automatically. Refusals are constructive: illegal
  group-bys, time requests on metrics without a time dimension, and filters
  on unmodeled columns are refused *with the legal alternatives enumerated*
  (consumer protocol: SPEC.md §2.10). Dialect-aware time grains
  (DuckDB/Snowflake/BigQuery).
- **Time is first-class on metrics**: Enrich now emits `agg_time_dimension`
  per metric (business date preferred; metadata/load timestamps never
  qualify; date-key tables resolve through their date dimension).
- **Declared ratio metrics**: `type: ratio` compiles (same-base-table,
  Phase A); explicit `name = table.col / table.col` claims in `--context`
  docs land as review-gated ratio metrics with `docs` provenance.
- **Metric `synonyms`** (spec 0.2.0, MINOR): alternate names for
  natural-language lookup, wired into MCP search.
- **dbt exporter** now emits ratio metrics (MetricFlow `type_params`).
- Provider robustness: one automatic retry with a larger budget when a
  thinking-enabled model returns no text.

## v0.2.0-beta.1 (2026-07-18)

- **Knowledge-doc priors** (`--context`): feed data dictionaries, wiki
  exports, `CLAUDE.md`/`knowledge.md` files into inference as priors. Files,
  directories, and globs of `.md`/`.txt`/`.rst`/`.html`; CSV/TSV data
  dictionaries are detected by header shape and mapped deterministically
  (works in `--no-llm` mode). Priors never override data: doc-vs-data
  contradictions land in the conflicts envelope; doc-confirmed enum decodes
  upgrade `llm_guess → docs` (metric-filter legal per SPEC.md §2.8).
  [Guide](docs/context-priors.md), including the query-log summarize-to-doc
  recipe.
- **Spec 0.1.1** (PATCH per spec/VERSIONING.md): new optional provenance
  signal `docs`. All 0.1.0 documents remain valid.
- **Iceberg bridge documented + hardened**: the DuckDB connector is
  catalog-aware (`ATTACH`ed Iceberg REST catalogs enumerate and profile with
  catalog-qualified names); recipes in [docs/iceberg-bridge.md](docs/iceberg-bridge.md).
- No-context runs are byte-identical to v0.1 (doc excerpts join prompts as
  an additive evidence field; all v0.1 cassettes replay unchanged).

## v0.1.0-beta.1 (2026-07-18)

First public beta.

- **Inference pipeline**: Profile (batched stats + typed rule pipeline + LLM
  escalation), Link (corroborated FK discovery; zero-trap hard gate), Describe
  (2-pass context propagation), Enrich (dictionary decodes, metrics,
  aggregate reconciliation with business-rule discovery, deprecation,
  freshness, routing).
- **Format**: open spec with confidence/provenance/lifecycle envelope,
  fan-out safety, hierarchies, aggregate routing, and a normative consumer
  contract (spec/SPEC.md).
- **CLI**: `infer` (`--no-llm`, `--no-sample-egress`), `review`, `drift`,
  `mcp`, `export` (dbt), `validate`, `init`.
- **Connectors**: Snowflake, BigQuery, DuckDB. LLM: Anthropic API
  (Haiku-tier default).
- **Evaluation, shipped**: 9 fixture warehouses, gold semantic layers,
  82-question CQ suites, reproducible benchmark
  (messy-warehouse: 0.34 raw → 0.53 with the layer; clean-schema negative
  result published).
- Measured: ~$0.70/100 tables inference cost; ~1-point accuracy cost for
  no-sample-egress; drift feed latency (Snowflake sample: 2.2 min).

Known gaps: Bedrock/Vertex routing; LookML/RDF/Ossie exporters; hierarchy
auto-inference (review-queued by design pending corroboration signals);
hosted service.

- **SPEC (additive, MINOR at release): `document_set` and `part` are reserved.**
  Insurance against a one-way door, taken before it costs anything.

  §4 required every reference to resolve *within the document*, with no notion
  of a document set anywhere in the spec or schema. A document measures ~1.2 KB
  per column, so a warehouse of 30,000 tables is ~544 MB in one file — meaning
  a large warehouse could not be described at all without splitting, and
  splitting would have broken §4 for every conforming consumer. In an
  Apache-2.0 spec whose point is interoperability, that is a breaking change to
  other people's code.

  Both fields are **optional and unemitted**. A document without `document_set`
  is self-contained and a validator that sees none behaves exactly as before —
  nothing changes for any producer or consumer today. §4 now reads "within the
  document — or, where the document declares a `document_set`, within that
  set", and a consumer handed one part of a set it cannot resolve SHOULD refuse
  rather than report the missing objects as dangling. `validate` does exactly
  that.

  The point is the asymmetry: one optional field now, versus a MAJOR break
  later.
