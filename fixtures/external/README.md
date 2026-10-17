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

## The bar for appearing here

A dataset is listed only if its licence is permissive and explicit, or it is a
US-government work. Then its gold may ship, numbers measured on it may be
published, and the dataset may be named — which is the point: a figure nobody
can reproduce is not a figure worth quoting, and reproducing one means being
able to obtain the data on the same terms we did.

`licence_verified` is the date a human opened the actual licence file. Not the
date someone read a README, a blog post, or recalled the terms.

## Why an unstated licence is not a permissive one

Most attractive public data is share-alike or states no terms at all. Reasoning
our way from "no terms stated" to "we may publish results" is how a project
ends up with a licence island inside its own repo. If the terms are ambiguous
or absent, the dataset does not go in this file.
