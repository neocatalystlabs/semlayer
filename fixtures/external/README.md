# External evaluation datasets

Real multi-table databases we evaluate against, to stop every published number
coming from a warehouse we authored ourselves.

**Nothing here downloads data into the repo.** Each dataset has a fetch script
that writes into `data/`, which is gitignored. What is committed is the script,
the licence record in `registry.yaml`, and — for publishable datasets only — a
hand-authored gold file.

```bash
python fixtures/external/fetch_<name>.py     # downloads into data/<name>/
python fixtures/coverage_audit.py            # what the corpus does and does not cover
```

## The two tracks

**Track P (publishable)** — permissive explicit licence, or a US-government
work. Gold ships in the repo; numbers may be published and the dataset named.

**Track I (internal only)** — share-alike (CC BY-SA, ODbL, GPL), non-commercial,
unknown licence, or credentialed access. We may run the engine against it on a
developer machine. No number derived from it may be published, with or without a
caveat. Its gold stays in `fixtures/golds/internal/`, gitignored.

A number is publishable only if **every** dataset behind it is Track P. P and I
figures are never averaged.

If a Track I dataset reveals something interesting, that is a reason to go find
a Track P dataset with the same property — not a reason to publish with a
footnote.

## Why "unknown" means Track I

Most attractive public data is share-alike or states no terms at all. An
unstated licence is not a permissive one, and reasoning our way to permissive is
how a project ends up with a licence island inside its own repo. If the terms
are ambiguous, the dataset is Track I and `licence_notes` says why.
