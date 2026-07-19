# M5 Ablation Report — the honest ontology gate

Measured 2026-07-18. Answerer: Haiku + one error-repair round, contexts built
through the same MCP-surface functions agents use. Gold CQ suites (fixed,
human-verified expected answers, executed live). Cassette-pinned.

## Results (pass-rate)

| Fixture | schema_only | + semantic layer | + ontology base graph |
|---|---|---|---|
| messy_mart (38 CQs, messy warehouse) | 0.34 | **0.45** | 0.45 |
| fan_trap (8 CQs, small clean schema) | 0.88 | 0.88 | 0.88 |

## Findings

1. **The semantic layer materially lifts a messy warehouse: +11 points
   absolute (+32% relative).** The single clearest case: "total revenue"
   under raw schema returns $16.3M (sums cancelled orders); with the
   discovered business rule it returns the correct $14.6M. Raw-schema
   errors of this class are SILENT — valid SQL, wrong number.
2. **The repair round only helps the semantic condition.** Raw-schema
   failures are semantically wrong but executable (unrepairable without
   meaning); semantic-layer residuals are loud mechanical errors that one
   repair round fixes. Semantics converts silent errors into fixable ones.
3. **Clean small schemas don't need us** (fan_trap flat at 0.88) —
   consistent with the published literature (benefit concentrates in
   schema complexity) and with our positioning claims.
4. **The base ontology graph adds NOTHING measurable over the flat
   semantic layer on this workload** (0.45 = 0.45).

## Gate verdict (per PRD §6)

The ontology-skeptic judge's prediction is CONFIRMED BY DATA: the flat
layer (routing + domains + FK graph + required filters) already carries
the value; the graph re-encoding adds form, not information, for this
workload. **The LLM enrichment projection stays INTERNAL. The deterministic
base graph remains as plumbing** (join-path computation, MCP navigation,
provenance-anchored derivation) — cheap, honest, useful as infrastructure,
not sold as a product layer.

Re-open trigger: a workload heavy in multi-hop entity questions (the
data.world-style regime) at a design partner; re-run this ablation on
their CQ suite before any repositioning.

## Residual gap (carried to M6)

Absolute pass-rates (0.45 on messy) are answerer-limited (Haiku one-shot;
strict execution-equality scoring): the M6 benchmark adds a stronger-model
row and per-category failure breakdown before any external claim.

## Harness quality gates (also measured)

- Skeptic (Sonnet, adversarial) rejects seeded-bad CQs: 6/6 = 1.00 (gate >=0.8)
- Generator (Haiku) CQs surviving the skeptic: 8/12 = 0.67 (gate >=0.5)
