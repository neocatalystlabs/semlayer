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

The loop above is run by a person typing three commands. There is no runner
that chains them, no schedule or change-feed trigger (the Snowflake DDL feed
exists in the connector and is wired to nothing), no pull request, no policy
deciding what merges without a human and what escalates, and no measurement
of whether the layer decays over many events. Those are the maintainer's
remaining pieces, in that order. The policy itself is not part of this
repository.
