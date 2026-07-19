# Knowledge-doc priors (`--context`)

Your warehouse's schema can't say what your team knows. `--context` feeds
that knowledge — data dictionaries, wiki exports, a `CLAUDE.md` from your
ETL repo, a hand-written `knowledge.md` — into inference as **priors**:

```bash
semlayer infer snowflake -o layer.yaml \
  --context ./docs/ \
  --context ./etl-repo/CLAUDE.md \
  --context ./exports/data_dictionary.csv
```

## What you can pass

| Input | How it's used |
|---|---|
| `.md` / `.txt` / `.rst` / `.html` files | Heading-chunked; sections that mention a table or its columns join that table's LLM evidence (bounded, relevance-ranked) |
| A **directory** or glob | Walked recursively (VCS/build dirs skipped) — point it at your docs folder or ETL repo |
| `.csv` / `.tsv` **data dictionaries** | Detected by header shape (`table, column, description` — synonyms accepted); descriptions are mapped **deterministically** per column, no LLM needed. Works in `--no-llm` mode too |

Not supported by design: live wiki URLs (export Confluence/Notion/SharePoint
pages to files first — a 5-minute step that avoids per-vendor auth
connectors) and PDFs (convert to text).

## Priors, never truth

Docs inform inference; they never override observed data:

- Doc-supported claims carry a `docs` provenance signal you can audit.
- A doc claim that **contradicts** the data — e.g. your wiki says
  `X = Refunded` but the decode dimension in your warehouse says
  `X = Cancelled` — lands in the **conflicts envelope** for review:
  "your docs say X, your data says Y." The data's answer stands until a
  human rules otherwise.
- Doc-confirmed enum decodes upgrade from `llm_guess` to `docs`, which makes
  them legal in metric filters under the consumer contract (SPEC.md §2.8).
- Decode claims are only applied to values the data actually exhibits, and
  only when the doc names the column (table-level mentions never spill onto
  sibling columns).

Measured example from our test suite: `yr_mth` holding `YYYYMM` integers
types as `unknown` from statistics alone; one wiki paragraph explaining the
format flips it to `date` — with the doc recorded in provenance.

## Query logs: the summarize-to-doc recipe

We don't ingest raw query logs in the beta (native query-log mining is
roadmap). The working pattern: have an LLM summarize your logs into a prose
doc, then pass that doc as context.

```text
You are summarizing SQL query logs for a data-modeling tool.
From the queries below, write a markdown doc with sections per table:
- which tables are queried together and on which join keys
- filters that appear on nearly every query against a table
- tables that appear in the logs rarely or never (candidates for deprecation)
Name tables and columns exactly as they appear. Do not invent anything.

<paste your query log sample>
```

Save the output as `query_patterns.md` and add `--context query_patterns.md`.
Claims arrive with honest `docs` provenance (not `query_log`, which is
reserved for direct log mining).

## Privacy note

Context docs are sent to your LLM provider as inference evidence —
`--no-sample-egress` governs warehouse cell values, not docs you pass
explicitly. Don't pass files containing secrets or credentials.
