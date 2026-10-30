# SPDX-License-Identifier: FSL-1.1-ALv2
"""Assisted build: probe, say what the evidence supports, estimate, then infer.

The engine's primitives are all shipped (`semlayer probe`, `semlayer infer`,
`semlayer validate`, `semlayer review`); this script chains them into one
run that tells you what it is about to do before it spends your warehouse's
compute or your API budget, and what it could not learn afterwards.

    python examples/assisted_build/build.py duckdb:fixtures/build/messy_mart.duckdb
    python examples/assisted_build/build.py bigquery --manifest target/manifest.json --sample

It is a planner, not an agent: one feedback step -- the
probe -- followed by a deterministic run. Every estimate it prints is an
extrapolation from a measured figure and is labelled as one.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# ---- Measured figures the estimate rests on. Each is [V] in docs/scale.md
# or docs/cost-model.md; what is derived from them below is [E]. ------------
FIXED_S_PER_TABLE = 8.8          # round-trips + two LLM calls, serial
LLM_SHARE = 0.75                 # of the fixed cost; parallelises freely
ROUND_TRIP_S = {"network": 0.815, "local": 0.03}   # BigQuery measured; DuckDB ballpark
TESTS_PER_CHILD_SMALL = 13.9     # 29-table warehouse, none at the cap
TESTS_PER_CHILD_LARGE = 34.8     # 116-table warehouse, 74% at the cap
CHILD_SHARE = 0.5                # of columns issue any inclusion test
# Spend is per unit of work, not per table: Describe scales with columns,
# escalation with tables, and FK validation with the candidates that survive
# inclusion testing -- which rise faster than table count (cost-model.md,
# dbt_synthea measurement). Haiku list price, $1/M in and $5/M out.
USD_PER_COLUMN_DESCRIBE = 0.00077    # ~305 in + ~93 out per column, both passes
USD_PER_TABLE_ESCALATION = 0.0027    # ~0.9 calls/table at ~1,060 in + ~390 out
USD_PER_INCLUSION_TEST = 0.000035    # ~12% of tests become candidates; ~84 in + ~42 out each
QUERY_SPEEDUP = {1: 1.0, 2: 2.1, 4: 3.3, 8: 6.7}   # measured on BigQuery, flat after


def estimate(probe, llm_workers: int, query_workers: int, local: bool,
             with_llm: bool) -> dict:
    """Wall-clock and spend for this warehouse at these settings. All [E]."""
    modelled = max(1, probe.tables - probe.duplicate_copies_removable)
    cols = probe.columns * modelled / max(1, probe.tables)
    fixed_llm = FIXED_S_PER_TABLE * LLM_SHARE * modelled if with_llm else 0.0
    fixed_rt = FIXED_S_PER_TABLE * (1 - LLM_SHARE) * modelled
    per_child = TESTS_PER_CHILD_LARGE if modelled > 60 else TESTS_PER_CHILD_SMALL
    queries = cols * CHILD_SHARE * per_child
    rt = ROUND_TRIP_S["local" if local else "network"]
    speed = QUERY_SPEEDUP.get(min(query_workers, 8), QUERY_SPEEDUP[8])
    if query_workers not in QUERY_SPEEDUP:
        speed = QUERY_SPEEDUP[max(k for k in QUERY_SPEEDUP if k <= min(query_workers, 8))]
    link_s = queries * rt / speed
    # LLM calls overlap with nothing of the warehouse's; the pool divides them.
    llm_s = fixed_llm / llm_workers
    return {
        "tables_modelled": modelled,
        "tables_skipped_as_copies": probe.duplicate_copies_removable,
        "inclusion_queries": int(queries),
        "seconds": {"profile_and_describe_llm": round(llm_s),
                    "profile_round_trips": round(fixed_rt),
                    "link_inclusion": round(link_s),
                    "total": round(llm_s + fixed_rt + link_s)},
        "usd_llm": round(cols * USD_PER_COLUMN_DESCRIBE
                         + modelled * USD_PER_TABLE_ESCALATION
                         + queries * USD_PER_INCLUSION_TEST, 2) if with_llm else 0.0,
    }


def _hms(s: float) -> str:
    s = int(s)
    if s < 90:
        return f"{s} s"
    if s < 5400:
        return f"{s // 60} min"
    return f"{s / 3600:.1f} h"


def _say(line: str = "") -> None:
    print(line, flush=True)


def plan(args: argparse.Namespace) -> tuple:
    """Step 1: probe, print the evidence, decide the inputs, estimate."""
    from semlayer.pipeline import open_source_from_uri
    from semlayer.probe import render, tier1, tier2

    lineage = None
    if args.manifest:
        from semlayer.lineage import load_dbt_manifest
        lineage = load_dbt_manifest(args.manifest)
    src = open_source_from_uri(args.source)
    probe = tier1(src, lineage=lineage)
    if args.sample:
        probe = tier2(src, probe)

    _say(render(probe))
    _say()
    _say("## What this run will do")
    if probe.grade == "weak" and not probe.tier2_ran:
        _say("- the grade is PROVISIONAL: tier 2 did not run. Re-run with --sample to "
             "learn whether several tables share key values; without it this cannot "
             "tell a hard warehouse from one with ugly names.")
    if not probe.has_manifest:
        _say("- no ETL manifest: join targets are inferred from statistics and naming. "
             "Pass --manifest target/manifest.json if this is a dbt project; it is "
             "worth ~0.12 of foreign-key precision on a real warehouse.")
    if probe.duplicate_copies_removable:
        _say(f"- {probe.duplicate_copies_removable} of {probe.tables} tables are repeat "
             f"copies and will be recorded in `coverage`, not profiled.")
    _say(f"- confidence on disambiguation-dependent inferences is capped at "
         f"{probe.confidence_cap:.2f} ({probe.grade}).")
    _say(f"- {args.llm_workers} concurrent LLM calls, {args.query_workers} concurrent "
         f"inclusion queries in your warehouse. Output is identical at any count.")
    if args.no_llm:
        _say("- --no-llm: deterministic pass only; no descriptions, no typing escalation.")

    local = args.source.startswith("duckdb:")
    est = estimate(probe, args.llm_workers, args.query_workers, local, not args.no_llm)
    s = est["seconds"]
    _say()
    _say("## Estimate [E: extrapolated from measured figures in docs/scale.md]")
    _say(f"- tables modelled          : {est['tables_modelled']}")
    _say(f"- inclusion queries        : ~{est['inclusion_queries']:,}")
    _say(f"- link (inclusion tests)   : ~{_hms(s['link_inclusion'])}"
         + ("  (in-process DuckDB; a network warehouse is ~25x slower per query)"
            if local else ""))
    _say(f"- profile + describe (LLM) : ~{_hms(s['profile_and_describe_llm'])}"
         + ("  (never measured live at this concurrency; the serial figure is)"
            if args.llm_workers > 1 and not args.no_llm else ""))
    _say(f"- total                    : ~{_hms(s['total'])}")
    if not args.no_llm:
        _say(f"- LLM spend                : ~${est['usd_llm']:.2f} at Haiku")
    return src, probe, lineage, est


def confirm(args: argparse.Namespace) -> bool:
    """Ask before spending anything, unless told not to or there is nobody to ask."""
    if args.yes or not sys.stdin.isatty():
        return True
    try:
        return input("\nProceed? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def build(args: argparse.Namespace, src, lineage, probe, est) -> int:
    """Step 2: infer with both pools, validate, report, say what awaits review."""
    import yaml

    from semlayer.pipeline import InferOptions, infer_with_report
    from semlayer.review import collect
    from semlayer.validate import validate_document

    llm = None
    if not args.no_llm:
        from semlayer.llm.provider import AnthropicProvider
        llm = AnthropicProvider()
    context = None
    if args.context:
        from semlayer.context import load_context
        context = load_context(args.context)
    only = ({x.strip() for x in args.only.split(",") if x.strip()}
            if args.only else None)
    options = InferOptions(no_sample_egress=args.no_sample_egress, only=only,
                           use_declared=not args.no_declared_constraints,
                           llm_workers=args.llm_workers, query_workers=args.query_workers)

    _say()
    _say("## Building")
    t0 = time.perf_counter()
    doc, report = infer_with_report(src, llm=llm, options=options, context=context,
                                    lineage=lineage)
    wall = time.perf_counter() - t0
    report["probe"] = probe.telemetry()
    report["estimate"] = est
    report["wall_clock_s"] = round(wall, 1)

    from semlayer.source import document_name
    out = Path(args.out or f"{document_name(None, None, src)}.yaml")
    if args.into:
        from semlayer.merge import merge_layer, render_report
        base = yaml.safe_load(Path(args.into).read_text())
        merged = merge_layer(base, doc, scope=only)
        doc = base
        out = Path(args.into)
        _say(render_report(merged))
    result = validate_document(doc)
    out.write_text(yaml.safe_dump(doc, sort_keys=False, width=100))   # same as the CLI
    Path(str(out) + ".report.json").write_text(json.dumps(report, indent=1))

    sl = doc["semantic_layer"]
    cov = sl.get("coverage", {})
    _say()
    _say("## Result")
    _say(f"- wrote {out}  (valid={result.ok})")
    _say(f"- tables: {len(sl['tables'])} modelled"
         + (f", {len(cov.get('skipped', []))} recorded as copies" if cov.get("skipped") else "")
         + f"; relationships: {len(sl.get('relationships', []))}; "
         f"metrics: {len(sl.get('metrics', []))}")
    stages = ", ".join(f"{k} {v}s" for k, v in report["stages"].items())
    _say(f"- stages: {stages}; total {wall:.1f}s "
         f"(estimate was ~{_hms(est['seconds']['total'])})")
    if llm is not None:
        _say(f"- LLM: {llm.spend_summary}")
    _say(f"- confidence cap from probe: {probe.confidence_cap:.2f} ({probe.grade})")
    items = collect(doc)
    if items:
        _say(f"- {len(items)} inferences await a human: semlayer review {out}")
    else:
        _say("- nothing awaits review")
    if not result.ok:
        for e in result.errors[:5]:
            _say(f"  ! {e}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Plan, confirm, build. Exit 0 valid, 1 invalid or error, 2 declined."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source",
                    help="duckdb:<path> | postgresql://... | snowflake | bigquery | postgres")
    ap.add_argument("-o", "--out", default=None,
                    help="output file; default <document name>.yaml, the warehouse's name")
    ap.add_argument("--into", metavar="PATH", help="merge into this existing layer instead")
    ap.add_argument("--manifest", metavar="PATH", help="dbt target/manifest.json")
    ap.add_argument("--context", action="append", metavar="PATH",
                    help="knowledge docs as priors (repeatable)")
    ap.add_argument("--only", metavar="TABLES", help="comma-separated tables to profile")
    ap.add_argument("--sample", action="store_true",
                    help="probe tier 2: read rows to learn whether tables share key sets")
    ap.add_argument("--llm-workers", type=int, default=4, metavar="N")
    ap.add_argument("--query-workers", type=int, default=4, metavar="N")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--no-sample-egress", action="store_true")
    ap.add_argument("--no-declared-constraints", action="store_true")
    ap.add_argument("-y", "--yes", action="store_true", help="do not ask before building")
    args = ap.parse_args(argv)

    from semlayer.errors import SemlayerError
    try:
        src, probe, lineage, est = plan(args)
        if not confirm(args):
            _say("stopped before building; nothing was written.")
            return 2
        return build(args, src, lineage, probe, est)
    except SemlayerError as e:
        _say(f"error: {e}")
        if e.hint:
            _say(f"hint:  {e.hint}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
