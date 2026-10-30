# Review: questions, verdicts, decisions

`semlayer infer` writes claims with a confidence. `semlayer review` is where a
person, or an agent, turns the uncertain ones into decisions, and where the
decisions are read back. The layer file is the only record: there is no
review file, no database, nothing outside the YAML you commit.

## The queue is questions, not items

```bash
semlayer review layer.yaml --list
```

```
Review queue for 'profiled': 1708 undecided claims, 7 questions. ...

1. [low_confidence] 328 columns: each holds the display name of the row; grouped by (type name)
   engine saw: the column name ends in _name; confidence 0.65
   82 distinct names: account_name, ads_campaign_name, ads_contract_name, ... and 70 more
   objects: dim_account.account_name, dim_ads_campaign.ads_campaign_name, ... and 322 more (--list --question 1)
   accept -> keeps the type, marked reviewed; reject -> clears the type; the column stays, untyped
   key: q-4bb1485d
```

**The grouping tenet.** Two objects are in one question only when the engine
knows exactly the same things about them apart from their names: the same kind
of claim, the same spec fields (semantic type and role for a column, table
type for a table), the same reason string the engine wrote, the same
confidence. Not similar. Identical. A grouped question is the per-item
questions with the name blanked out; nothing the engine could have shown you
one at a time is lost by answering them together. What you do lose is the
chance to notice an odd name while scrolling, which is why the question lists
the distinct names first (82 of them above), and why `--list --question N`
prints every object with its evidence.

**Words, not rule names.** Every claim is printed in words with the spec
vocabulary in brackets: "holds the display name of the row; grouped by (type
name)". "Engine saw" is the reason in words. The raw strings the engine wrote
are in the JSON (`engine_saw_raw`), never in the prose.

**Shown, not asked.** Two kinds of line sit under the questions and take no
verdict: columns the engine could not type at all (there is no claim to accept
or reject; they stay untyped until a rule or a reviewer names them), and the
inventory-only tiers (the lever is `tier` on the table, not a verdict). They
are listed so nothing is hidden. On the 2,000-table benchmark estate these
two lines carry 1,198 and 1,246 objects respectively; the seven questions
carry the other 508 claims.

## Answering

Interactively, `semlayer review layer.yaml` walks the questions. `a` accepts,
`r` rejects, `s` skips, `e` expands to every object, and `x 3,7` leaves
objects 3 and 7 out of the verdict you then give. Non-interactively:

```bash
semlayer review layer.yaml --answer "q-64c3907b=accept,q-9706b59a=reject" \
                           --exclude "q-64c3907b=int__risk_claim_snapshot" --by raj
```

Question numbers change as questions are answered. The key does not: it is a
hash of the claim and the reason, the same across runs and across documents,
so scripts and agents use it.

**What a verdict does** is printed on every question. Accept marks each object
`reviewed`. Reject removes the claim and never the object: a column stays with
its type cleared, a metric is deprecated rather than deleted, a seam edge is
deprecated so the next `stitch --refresh` does not propose it again. Every
object touched gets its own provenance line, so the record is exact even when
328 columns were decided at once.

Verdicts are sticky. `infer --into layer.yaml` keeps `reviewed` and `certified`
content and records a disagreement as a conflict (SPEC producer rule 3).
`infer -o layer.yaml` onto a file that holds verdicts starts over, and warns
you first.

## The record

```bash
semlayer review layer.yaml --decisions
```

```
2026-10-30  maintainer/claude-opus-5-5  [agent]
  accepted  328 objects: holds the display name of the row; grouped by (type name)
            dim_account.account_name, dim_ads_campaign.ads_campaign_name, ... and 324 more
2026-10-30  raj
  rejected  30 objects: holds a count or quantity; summed (type quantity); now untyped
            dim_ads_campaign.sk_ads_campaign, dim_ads_employee.sk_ads_employee, ... and 26 more
  accepted  18 objects: holds a dated version of each record per row (a snapshot table) (table_type snapshot_scd2)

401 decisions: 371 accepted, 30 rejected.  Undecided items in the queue: 1307 (semlayer review --list)
```

Each verdict lives on the object it judged, as a provenance entry:

```yaml
- signal: human          # or agent
  date: '2026-10-30'
  by: raj
  detail: 'rejected: holds a count or quantity; summed (type quantity); now untyped'
```

The detail names the claim as it stood, so a reject still says what was
rejected after the claim itself is reset. `--decisions --json` lists every
object individually. Lines written before this release carry no date and no
claim, and print as "(claim not recorded)".

## Agents

An automated reviewer uses the same commands with two differences. It reads
`--list --json`, which carries every question with its key, kind, spec fields,
confidence, the raw and worded reasons, every object with its evidence, and
both consequences. And it signs its verdicts `--agent --by <agent/model>`, so
they are written with `signal: agent` and shown under an `[agent]` heading.
A verdict an agent took is never mistaken for one a person took.

An agent that cannot decide a question leaves a note instead of a verdict:

```bash
semlayer review layer.yaml --agent --by maintainer/claude-opus-5-5 \
  --note "q-64c3907b=cannot tell whether rows are versions or events without the load job"
```

The note is stored on the document (`review_notes`) and printed under its
question the next time anyone lists the queue. The rule an agent should
follow, and that the autonomy policy enforces: answer only where you hold
evidence the engine did not (a dbt description, a catalog tag, a stated
convention, a query log). Where your only information is what the engine
already saw, your yes is a coin flip dressed as a verdict; leave the note.

## Hubs: one answer across the members

A hub's own queue is its seam: the cross-member joins and same-entity pairs
it inferred. Its members' questions belong to the members, and `semlayer
review acme.yaml` leaves them alone. But the same question recurs in every
member file. On the 5,000-table estate, 41 members asked 254 questions
between them and only 11 were distinct: every team's `_name` columns, every
team's `int__` tables.

```bash
semlayer review acme.yaml --members --list
semlayer review acme.yaml --members --answer "q-4bb1485d=accept" --by raj
```

`--members` asks each question once across the whole set, with every object
addressed as `member/table.column`, and writes a verdict into the member
file that holds each object. The hub file gains nothing. It saves exactly
the member files a verdict reached and prints each one, so you need write
access to those files, and the result is the same as if each team had
answered in its own file: each object carries its own dated, authored
provenance line, and `--decisions --members` reads them all back with the
member in every address. On the 5,000-table estate the listing takes 15
seconds and one accept reached 753 columns in 41 files.

## Everything in one place

| | people | agents |
|---|---|---|
| see the queue | `review --list`, `--question N` | `review --list --json` |
| decide | interactive, or `--answer K=accept --exclude K=obj` | `--answer ... --agent --by NAME` |
| cannot decide | skip, or `--note` | `--note "K=why" --agent --by NAME` |
| read the record | `review --decisions` | `review --decisions --json` |
| one item at a time | `review --flat` | |
| a hub, once across its members | `review hub.yaml --members ...` | same, with `--json` |
