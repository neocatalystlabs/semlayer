# SPDX-License-Identifier: FSL-1.1-ALv2
"""Where a run has got to, as the stages themselves report it.

One callback, threaded from `pipeline.infer_with_report` into the four loops
that actually take the time: profiling tables, checking inclusion, validating
candidates with the model, and describing tables. Each emits units done out
of units total, plus `within` -- how far through its own stage it is.

`within` is computed by the STAGE rather than by the caller, because only the
stage knows its own shape: describe runs two passes over the same tables, and
link's cost is split between inclusion queries and model validation in a
ratio that depends on whether there is a model at all. A consumer that tried
to work that out from unit counts would be guessing.

What is NOT here: wall-clock, weights, fractions across stages, or any
estimate. Those belong to whoever is watching -- `session.InferJob` turns
these events into a fraction and an ETA, and the CLI ignores them entirely.
"""

from __future__ import annotations

from collections.abc import Callable


def ticker(progress: Callable[[dict], None] | None, stage: str, total: int,
           verb: str, within: Callable[[int, int], float] | None = None,
           ) -> Callable[[int], None]:
    """A per-unit progress callback for one loop, or a no-op if nobody watches.

    `done` is how many units are FINISHED, so a loop calls this BEFORE doing
    the next unit: a bar that moves only when the last table of two thousand
    completes is not a bar.

    `within` maps (done, total) to the stage fraction, for a stage whose
    units are not the whole stage -- describe's two passes, link's two
    phases. It defaults to done/total.
    """
    if progress is None:
        return lambda done: None

    def fraction(done: int, n: int) -> float:
        if within is not None:
            return within(done, n)
        return (done / n) if n else 1.0

    def tick(done: int) -> None:
        progress({"stage": stage, "event": "unit", "done": done, "total": total,
                  "within": max(0.0, min(1.0, fraction(done, total))),
                  "label": f"{verb} {min(done + 1, max(total, 1))} of {total}"})

    return tick
