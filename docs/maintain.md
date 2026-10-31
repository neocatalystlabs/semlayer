# Maintaining a layer: the loop, replayed

A semantic layer decays the day after it is reviewed: a column is dropped, a
column is added, a table appears. The maintainer loop is what keeps the file
true. Every step of it is a shipped primitive:

| step | command | what it does |
|---|---|---|
| detect | `semlayer drift layer.yaml <source> --json` | diffs the warehouse against what the layer claims; eight event kinds, including DML drift (a new enum value, a stale table) |
| changeset | same | `orphaned`, `needs_inference`, `affected`, `broken_cqs`, `demoted`, each a separate field; nonzero exit on drift |
| re-infer | `semlayer infer <source> --only <tables> --into layer.yaml` | profiles only the tables the changeset names and folds the result in |
| merge | part of `--into` | reviewed and certified content is kept; a disagreement is a recorded conflict; a dropped column is orphaned, never deleted |
| ask | `semlayer review layer.yaml` | the new table's columns join the queue; the human's earlier verdicts are still there |

## One command: `semlayer maintain`

```bash
semlayer maintain layer.yaml snowflake --cqs cqs.yaml          # dry run: detect and plan
semlayer maintain layer.yaml snowflake --cqs cqs.yaml --apply  # re-infer and merge
semlayer maintain layer.yaml snowflake --json                  # one record for an agent
```

```
maintain: 3 change(s) in duckdb:mart.duckdb against mart.yaml
  table_added      loyalty_pts
  column_dropped   ord_hdr.tot_amt
  column_added     ord_hdr.gift_flg
changeset: 3 element(s) orphaned, never deleted; 2 demoted; 17 competency question(s) flagged
plan: re-infer 2 table(s) without an LLM: loyalty_pts, ord_hdr; merge into mart.yaml keeping reviewed and certified content
dry run: nothing written. Add --apply to do it.
```

**Dry run is the default.** The command detects, builds the changeset, names
the tables it would re-infer and whether it would call the LLM, and stops.
Exit 0 when nothing changed, exit 1 when there is work, so a cron or CI job
can tell the two apart. A team can run it nightly for a week and read the
plans before letting it write.

**`--apply`** re-infers only the tables the changeset names, merges them into
the layer with reviewed and certified content protected, saves, prints the
merge report, and says how many questions joined the review queue. A second
run then finds nothing: the baseline a maintained layer is compared against
leaves orphaned columns out, so a drop is reported once.

**It makes the same choices `infer` makes.** The LLM is used when
`ANTHROPIC_API_KEY` is set and `--no-llm` turns it off; `--llm-workers`,
`--query-workers`, `--manifest` and `--no-declared-constraints` mean what
they mean on `infer`. A layer is maintained by the engine that built it; a
maintainer that quietly used a different one would drift the layer for
reasons no diff could show.

**`--json`** carries everything in one record: the events, the changeset
(`orphaned`, `needs_inference`, `affected`, `broken_cqs`, `demoted`), the
plan, whether it applied, the merge report, and the review queue before and
after.

## On a schedule, as a pull request

`examples/maintain/semlayer-maintain.yml` is a GitHub Actions workflow to
copy into the repository that holds your layer. Nightly it runs `maintain
--apply`; when anything changed it pushes the new layer to one standing
branch, `semlayer/maintain`, and opens a pull request, or updates the open
one, with the maintainer's report as the description: what changed, what was
re-inferred, whether the LLM was used, how many review questions appeared.
Nothing reaches your default branch until a person merges it; when nothing
changed the job ends quietly. The PR is the gate, so there is no separate
dry-run stage to configure: the branch is the dry run and merging is the
decision. The README beside the template lists the secrets per warehouse.

**How big is the PR?** Measured on the messy_mart event below (drop a column,
add a column, create a table) on a layer of about 5,400 lines:

| engine | diff | what the extra lines were |
|---|---|---|
| without an LLM | +72 / −5 | the change itself, nothing else |
| with the LLM, before the merge rules below | +119 / −34 | 12 of the 13 unchanged `ord_hdr` columns re-described; six rewrites lost a fact the old text had ("foreign key to chnl_dim", "no nulls"); a dictionary-join decode replaced by a guess |
| with the LLM, now | +100 / −10 | the change itself, plus the new table's descriptions and a refreshed description for the table whose columns changed |

Three merge rules make the difference, and they hold for `infer --into` too.
A column whose SQL type and statistics did not move keeps its description:
re-describing it yields the model's variance, not information. A column
whose type, role and statistics did not move keeps its provenance. Decodes
that came from a dictionary join or a person are never replaced by a guess:
a partial re-inference of one table cannot see the dimension the decodes
came from. A table whose live column set is unchanged keeps its description
and `ai_context`; one that gained or lost a column gets a fresh one, since
the prose may name the columns. The merge report lists what it kept under
"Descriptions kept".

## The replay

The maintainer design recorded that this loop had been run once, by hand,
and that the run existed in a commit message and nowhere reproducible.
`tests/test_maintain_replay.py` is that run, scripted, on the `messy_mart`
fixture with a human verdict already on the table about to change:

- **event**: drop `ord_hdr.tot_amt`, add `ord_hdr.gift_flg`, create `loyalty_pts`
- **detect**: three events, `column_dropped`, `column_added`, `table_added`
- **changeset**: `tot_amt` orphaned with its metrics; 17 competency questions
  flagged, as SQL broken or in the blast radius of the orphaned revenue column
- **re-infer**: two tables, `ord_hdr` and `loyalty_pts`, nothing else read
- **merge**: 4 elements touched (one table added, one updated, one column
  added, one updated), 10 kept because their foreign-key evidence lives in
  tables outside the scope, the reviewed verdict on `ord_hdr.chnl_cd`
  untouched, `tot_amt` still orphaned and still present
- **ask**: `loyalty_pts` is in the queue; nothing already answered is asked again

The test replays with no API key: the layer it starts from is the committed
inference recording, and the targeted re-inference is deterministic.

## What is not built

`maintain` is the runner and the workflow template schedules it and opens
the PR. What is not here: a change-feed trigger instead of a clock (the
Snowflake DDL feed exists in the connector and is wired to nothing), a policy
deciding what merges without a human and what escalates (merging the PR is
that decision, taken by a person), a review queue ranked by consumer impact,
and a measurement of whether the layer decays over many events. Those are
the maintainer's remaining pieces and the shape of the hosted service. The
policy itself is not part of this repository.
