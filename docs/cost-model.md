# Whole-Pipeline Cost Model (measured, M3)

Measured 2026-07-18 on messy_mart (36 tables, 198 columns) with the cheap tier
(claude-haiku-4-5), temperature-pinned, cassette-cached. Warehouse compute
(profiling SQL) is separate and customer-paid; on the fixtures it is seconds
of an XS warehouse.

| Stage | LLM calls | Tokens (in/out) | Cost @ Haiku ($1/$5 per M) | Per 100 tables |
|---|---|---|---|---|
| Profile — typing escalation (~20% of columns, 1 call/table w/ escalations) | 25 | 16.4K / 5.1K | $0.042 | ~$0.12 |
| Link — FK candidate validation (batched 30/call) | 4 | 8.7K / 4.7K | $0.032 | ~$0.09 |
| Describe — 2-pass context propagation (2 calls/table) | 66 | 65.2K / 22.5K | $0.178 | ~$0.50 |
| **Total inference** | **95** | **90K / 32K** | **$0.25** | **~$0.70** |

Against the PRD target of ~$1/100 tables: **met with ~30% headroom, on the
cheap tier alone** — no frontier-model escalation was needed to hit any M1–M3
accuracy target (typing 0.904, FK F1 1.0, descriptions 0.889 judge-approved).

Not included:
- Sonnet description-judging (~$0.10 per 45-item sample) — a development/eval
  cost, not a per-customer inference cost.
- Re-inference: input-hash cassettes mean unchanged tables cost $0 on re-runs;
  drift re-inference bills only the blast radius.
- CQ generation/verification (M5) — will be added to this table when built.

`--no-sample-egress` mode changes token counts marginally (no sample values in
prompts) and costs ~1 point of typing accuracy (0.904 -> 0.894).
