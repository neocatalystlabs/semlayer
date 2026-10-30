# Maintain the layer on a schedule, as a pull request

`semlayer-maintain.yml` is a GitHub Actions workflow. Copy it to
`.github/workflows/` in the repository that holds your `layer.yaml`, set the
secrets it names for your warehouse, and adjust `LAYER`, `SOURCE` and the
cron line at the top.

Every night it runs `semlayer maintain layer.yaml <source> --apply`:

1. **Detect.** The warehouse is compared with what the layer claims. Dropped
   columns are orphaned, never deleted; new tables and columns are named for
   re-inference; competency questions that break are listed if you point
   `CQS` at a suite.
2. **Re-infer only what changed.** The named tables are profiled again and
   merged into the layer. Everything a person reviewed or certified is kept;
   a disagreement is recorded as a conflict for review, never overwritten.
3. **Open the pull request.** If anything changed, the new layer is pushed to
   one standing branch, `semlayer/maintain`, and a PR is opened or the open
   one is updated. Its description is the maintainer's own report: what
   changed, what was re-inferred, whether the LLM was used, how many review
   questions appeared. Nothing reaches your default branch until someone
   merges it. When nothing changed, the job ends quietly.

The PR is the gate. There is no separate dry-run mode to configure: the
branch is the dry run, and merging it is the decision.

**The engine is the one you built the layer with.** `maintain` uses the LLM
when `ANTHROPIC_API_KEY` is a repository secret and runs without it when it
is not, exactly as `semlayer infer` does, and the PR body says which. A
layer maintained by a different engine than built it drifts for reasons no
diff can show.

**What this does not do**, and what the hosted service is for: trigger on a
warehouse change feed instead of a clock, decide by policy what merges
without a person, rank the review queue by consumer impact, or run across
many teams' layers at once. Here, a human merges a PR once a night.

Run it by hand from the Actions tab (`workflow_dispatch`) the first time, and
read the PR before trusting the schedule.
