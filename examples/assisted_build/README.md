# Assisted build

One script that chains the shipped primitives -- `probe`, `infer`,
`validate`, `review` -- into a run that says what it will do before it
spends anything, and what it could not learn afterwards.

```
python examples/assisted_build/build.py duckdb:fixtures/build/messy_mart.duckdb -o layer.yaml
python examples/assisted_build/build.py bigquery --manifest target/manifest.json --sample
```

What you see, in order:

1. **The probe.** Evidence found (manifest, declared keys, naming
   convention, duplicate copies), the evidence grade, and the confidence
   cap that grade imposes. With `--sample` it also reads rows to learn
   whether several tables share key values, the one question a catalogue
   cannot answer.
2. **What the run will do.** Which of its own warnings apply: a provisional
   grade, no manifest, how many tables will be skipped as copies, the worker
   counts.
3. **An estimate.** Wall-clock and LLM spend, extrapolated from the figures
   measured in `docs/scale.md`, and labelled as an extrapolation. The
   per-query round-trip is BigQuery's; an in-process DuckDB file is far
   faster and the script says so.
4. **The build.** `infer` with both concurrency pools. The output is
   identical at any worker count; only wall-clock changes.
5. **The result.** Validity, counts, stage timings against the estimate,
   LLM spend, the confidence cap, and how many inferences await a human.

It asks before building unless `--yes` is passed or stdin is not a terminal.
The report written beside the layer carries the probe payload, the estimate
and the options used, so a layer can be reproduced from its report.

This is a planner, not an agent: one feedback step
followed by a deterministic run. It makes no LLM calls of its own and does
not retry anything the engine does not already retry.
