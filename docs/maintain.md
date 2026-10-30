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

`maintain` is the runner, and a person or a job still has to start it. There
is no schedule or change-feed trigger (the Snowflake DDL feed exists in the
connector and is wired to nothing), no pull request, no policy deciding what
merges without a human and what escalates (`--apply` is that decision, taken
by whoever types it), and no measurement of whether the layer decays over
many events. Those are the maintainer's remaining pieces, in that order. The
policy itself is not part of this repository.
