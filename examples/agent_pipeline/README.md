# Three agents, one warehouse, two runs

A chain of three agents does something ordinary: agent 1 pulls total revenue,
agent 2 breaks it down by product category for the exec summary, agent 3
compares revenue to the plan target and files a ticket if the plan is breached.

No human reviews the middle steps. Agent 3 never sees any SQL — only the number
handed to it. That is the shape most pipelines actually have, and it is the
reason a bad number is unchallengeable by the time it becomes an action.

The same chain runs twice:

- **`raw`** — the agents get the warehouse: `list_schema` and `run_sql`. This is
  what an agent on a warehouse has today.
- **`layer`** — the same two tools, plus every tool the semlayer MCP server
  exposes, and the standing instruction to read the layer before writing SQL
  and to `check_sql` before executing.

```bash
python examples/agent_pipeline/pipeline.py            # both modes
python examples/agent_pipeline/pipeline.py --mode raw
python examples/agent_pipeline/pipeline.py --json     # full transcript
```

No API key needed: every model turn replays from the cassettes in
`cassettes/`. With `ANTHROPIC_API_KEY` set, any turn not already recorded is
recorded.

## What happens

Truth, computed directly from the fixture and not from any agent:

| | truth |
| --- | --- |
| total revenue | $14,621,503.30 |
| average order value | $2,035.85 |
| top category | Televisions, $1,495,019.61 |

| | `raw` | `layer` |
| --- | --- | --- |
| revenue reported | **$16,300,242.85** (1.11x) | **$14,621,503.30** (1.00x) |
| average order value | $2,037.53 | $2,035.85 |
| top category | Televisions, $1,668,132.94 | Televisions, $1,495,019.61 |
| `check_sql` calls | 0 | 3 |
| **action taken** | **ticket filed** | no ticket |

The ticket `raw` mode files, verbatim:

> Plan Breach Alert: Total revenue of $16,300,242.85 exceeds plan target of
> $16,000,000 by $300,242.85. Requires Finance review.

The plan breach did not happen. Revenue is $14.6m against a $16m target, well
under. The $300,242.85 overage is an artefact of one rule the agent could not
see: at this company cancelled orders never count toward revenue, and they are
the 818 rows with `sts_cd = 'X'`, worth $1,678,739.55.

Nothing errored. Both agents wrote valid SQL, ran it in milliseconds, and got
plausible numbers. The 11% difference is small enough to look like a rounding
disagreement and large enough to carry the number across a threshold — which is
how a reporting error becomes an action.

## What actually made the difference

Both modes wrote structurally sound SQL. The category breakdown is
`SUM(ol.line_amt)` in both, correctly aggregating at line level. The entire
divergence is one clause:

```sql
-- raw
SELECT SUM(tot_amt) FROM ord_hdr

-- layer
SELECT SUM(oh.tot_amt) FROM ord_hdr oh WHERE oh.sts_cd <> 'X'
```

The layer agent did not discover that filter by being smarter. It called
`table_detail` on `ord_hdr` before writing anything, and the filter was
recorded there — inferred during `semlayer infer` by noticing that the daily
summary table only reconciles with `ord_hdr` when those rows are excluded.

Worth being precise, because it is the opposite of what a demo usually claims:
**the layer worked here by prevention, not by correction.** The agent read the
rule and applied it on its first attempt. `check_sql` confirmed the query was
clean rather than repairing it.

It did repair one thing, and the repair is the part worth watching. The
agent called `check_sql` with `dialect: "sql"`, which is not a dialect. It got
back a finding whose `fix` lists the valid names and says which is the default,
and its next turn used `duckdb` -- the correct one for this warehouse. Before
that finding existed the same model guessed `tsql` and linted DuckDB SQL as
T-SQL, which "passes" while checking the wrong grammar. Small, but it is the
whole loop in miniature: a machine-readable finding, specific enough to act
on, acted on without a human.

## The honest gaps

- **Fan-out never arises in this chain.** `semlayer` catches aggregates
  multiplied by a one-to-many join, and that is a real failure mode, but these
  three tasks did not provoke it — both modes aggregated at the right grain
  unprompted. The fan-out case is covered by `tests/test_fan_trap.py` instead
  of being staged here.
- **The model is haiku.** A stronger model may well find the filter unaided on
  a warehouse this small. The claim is not that agents cannot get this right;
  it is that nothing in `raw` mode tells them when they got it wrong.
- **The numbers are not a benchmark.** This is one chain, one fixture, one
  recorded run. `semlayer benchmark` is the measured claim.
- **One fixture, synthetic.** `messy_mart` is built to resemble a warehouse
  that several teams have touched. It is not a customer's.

## How it is wired

- `pipeline.py` — the orchestrator, the three tasks, the handoffs, the report.
- `agents.py` — one agent's loop. The tool protocol is text (`CALL <tool>
  {...}`) rather than the provider's native tool API, which keeps every turn a
  single `(system, user)` pair — what the cassette key is built from.
- `tools.py` — the two worlds. `run_sql` and `list_schema` are the warehouse and
  exist in both modes. Layer mode adds the semlayer tools over **real MCP
  stdio**, by launching `semlayer mcp layer.yaml` as a subprocess and speaking
  the protocol to it. Tool schemas are read from `list_tools` and handed to the
  model the way any MCP client would.

Two guards keep the comparison from flattering us:

1. **The task text is byte-identical across modes.** Only the tool list and the
   layer's standing instruction differ. If the prompts differed, the result
   would mean nothing.
2. **Every reported number is checked against the `run_sql` results.** Models
   under a text protocol will emit a call, invent its result, and answer from
   the invention; an early version of this example reported $1,534,824.50 that
   way, a number no query ever returned. Answers are now labelled
   `NOT EXECUTED` when they do not trace to a tool result, and the agent loop
   stops at the first call so a fabricated result never enters the transcript.

`layer.yaml` is inferred on first run from the fixture, replaying the
inference cassettes in the repo root, and is gitignored. It was committed by
mistake at first, which was worse than untidy: `_ensure_layer` returns early
when the file exists, so every clone was pinned to whichever engine happened to
build it and would have kept demonstrating that engine forever. Delete it to
rebuild after an engine change.

The numbers in the table above survived three engine changes (PascalCase name
folding, `natural_key` detection, and the `table_type` rule) unchanged, which
is what the test is for. The agents' TRACES do shift — turn counts and which
tools they reach for move around — so the test pins the numbers and the action,
never the route.
