# SPDX-License-Identifier: FSL-1.1-ALv2
"""semlayer command-line interface.

Structure: one handler per subcommand, dispatched from `main`. The CLI is the
error boundary (BETA Q1): `SemlayerError` renders as message + actionable
hint; raw third-party tracebacks never reach users. Exit codes: 0 success,
1 domain outcome (invalid doc / drift found), 2 usage or environment error.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from semlayer.errors import SemlayerError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="semlayer", description="Semantic layer toolkit")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Stage-by-stage progress logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="Validate semantic layer document(s) against the spec")
    p.add_argument("paths", nargs="+", help="YAML/JSON document paths")
    p.add_argument("-q", "--quiet", action="store_true", help="Only print failures")

    p = sub.add_parser("init", help="Generate minimal-grant setup script for a warehouse")
    p.add_argument("warehouse", choices=["snowflake", "bigquery", "postgres"])
    p.add_argument("--database", default="<your_database>")
    p.add_argument("--project", default="<your_project>")
    p.add_argument("--warehouse-name", default="<your_warehouse>")
    p.add_argument("--schema", default="<your_schema>", help="Postgres: schema to grant on")

    p = sub.add_parser("infer", help="Infer a semantic layer from a warehouse")
    p.add_argument("source",
                   help="duckdb:<path> | postgresql://... | snowflake | bigquery | postgres")
    p.add_argument("-o", "--out", default="semantic_layer.yaml")
    p.add_argument("--no-llm", action="store_true", help="Deterministic dry-run (no LLM calls)")
    p.add_argument("--no-sample-egress", action="store_true",
                   help="Never include cell values in LLM prompts")
    p.add_argument("--state", metavar="PATH",
                   help="Partition-wise profiling with a state sidecar: "
                        "re-read only partitions whose change token moved")
    p.add_argument("--no-declared-constraints", action="store_true",
                   help="Ignore catalog PK/FK declarations and infer them "
                        "(this is how our published accuracy is measured)")
    p.add_argument("--manifest", metavar="PATH",
                   help="dbt target/manifest.json: read how the tables were authored")
    p.add_argument("--context", action="append", metavar="PATH",
                   help="Knowledge docs as inference priors: files, dirs, or globs of "
                        ".md/.txt/.rst/.html prose or .csv/.tsv data dictionaries "
                        "(repeatable). Doc text IS sent to the LLM — don't pass secrets. "
                        "Docs never override data: contradictions land in review.")
    p.add_argument("--only", metavar="TABLES",
                   help="Profile only these tables (comma-separated, schema-qualified "
                        "or bare). The targeted half of incremental re-inference: "
                        "`semlayer drift` reports which tables changed and what depends "
                        "on them, and this profiles just those instead of the whole "
                        "warehouse. Pass --into to fold the result back into the layer "
                        "you already have.")
    p.add_argument("--into", metavar="PATH",
                   help="Merge the result into this existing layer and write it back, "
                        "instead of emitting a standalone document. Reviewed and "
                        "certified content is kept and disagreement is recorded as a "
                        "conflict; a column the warehouse no longer has becomes "
                        "`orphaned`, never deleted. Tables outside --only are untouched.")

    p = sub.add_parser("review", help="Review inferred claims (terminal queue)")
    p.add_argument("doc", help="semantic layer YAML path")
    p.add_argument("--list", action="store_true", help="List items without prompting")

    p = sub.add_parser("telemetry",
                       help="Show or change anonymous usage telemetry")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--off", action="store_true",
                   help="turn telemetry off permanently (writes a marker file)")
    g.add_argument("--on", action="store_true", help="turn telemetry back on")
    g.add_argument("--show", action="store_true",
                   help="print the local spool, exactly as it would be sent")

    p = sub.add_parser("mcp", help="Serve a semantic layer over MCP (stdio)")
    p.add_argument("doc", help="semantic layer YAML path")

    p = sub.add_parser("lint", help="Check SQL against a semantic layer (deterministic, no LLM)")
    p.add_argument("doc", help="semantic layer YAML path")
    p.add_argument("sql", nargs="?", default="-",
                   help="SQL file (statements separated by ';'), or '-' for stdin")
    p.add_argument("--dialect", default="duckdb")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p = sub.add_parser("probe", help="What evidence does this warehouse offer, "
                                     "and what accuracy follows from it?")
    p.add_argument("source",
                   help="duckdb:<path> | postgresql://... | snowflake | bigquery | postgres")
    p.add_argument("--manifest", metavar="PATH",
                   help="dbt target/manifest.json, if there is one")
    p.add_argument("--sample", action="store_true",
                   help="Also read rows: do several tables hold the same key "
                        "values? It is the one question a catalog cannot answer "
                        "and the one that decides whether naming matters here.")
    p.add_argument("--json", action="store_true", help="machine-readable")
    p.add_argument("--share", action="store_true",
                   help="Print the anonymous telemetry payload. Counts, fractions "
                        "and booleans only -- never a table or column name, not "
                        "even hashed. Printing it is the point: the claim is "
                        "checkable rather than promised.")

    p = sub.add_parser("drift", help="Detect warehouse drift vs a semantic layer")
    p.add_argument("doc", help="semantic layer YAML path")
    p.add_argument("source",
                   help="duckdb:<path> | postgresql://... | snowflake | bigquery | postgres")
    p.add_argument("--apply", action="store_true",
                   help="Apply orphaning/conflicts to the doc (writes in place)")
    p.add_argument("--cqs", help="CQ suite YAML to regression-check")
    p.add_argument("--json", action="store_true",
                   help="machine-readable changeset (for agents and CI)")
    return parser


def _load_doc(path: str) -> dict:
    import yaml

    try:
        doc = yaml.safe_load(Path(path).read_text())
    except FileNotFoundError as e:
        raise SemlayerError(f"document not found: {path}",
                            hint="run `semlayer infer` first, or check the path") from e
    except yaml.YAMLError as e:
        raise SemlayerError(f"document is not valid YAML: {path} ({e})") from e
    if not isinstance(doc, dict) or "semantic_layer" not in doc:
        raise SemlayerError(f"not a semantic layer document: {path}",
                            hint="expected a top-level `semantic_layer:` mapping")
    return doc


def _save_doc(path: str, doc: dict) -> None:
    import yaml

    Path(path).write_text(yaml.safe_dump(doc, sort_keys=False, width=100))


def _cmd_validate(args: argparse.Namespace) -> int:
    from semlayer.validate import validate_file

    failed = 0
    for p in args.paths:
        result = validate_file(Path(p))
        if result.ok:
            if not args.quiet:
                warn = f"  ({len(result.warnings)} warnings)" if result.warnings else ""
                print(f"OK    {p}{warn}")
                for w in result.warnings:
                    print(f"        warn: {w}")
        else:
            failed += 1
            print(f"FAIL  {p}")
            for e in result.errors:
                print(f"        {e}")
    return 1 if failed else 0


def _cmd_init(args: argparse.Namespace) -> int:
    from semlayer.init_cmd import render

    print(render(args.warehouse, database=args.database, project=args.project,
                 warehouse_name=args.warehouse_name, schema=args.schema))
    return 0


def _cmd_infer(args: argparse.Namespace) -> int:
    import json

    from semlayer.pipeline import infer_with_report, open_source_from_uri
    from semlayer.review import collect
    from semlayer.validate import validate_document

    llm = None
    if not args.no_llm:
        from semlayer.llm.provider import AnthropicProvider

        llm = AnthropicProvider()
    state = None
    if args.state:
        from semlayer.incremental import LocalStateStore

        state = LocalStateStore(args.state)
    lineage = None
    if args.manifest:
        from semlayer.lineage import load_dbt_manifest

        lineage = load_dbt_manifest(args.manifest)
        print(f"manifest: {len(lineage.parents)} tables with declared lineage")
    context = None
    if args.context:
        from semlayer.context import load_context

        context = load_context(args.context)
        print(f"context: {len(context.chunks)} doc sections, "
              f"{len(context.dictionary)} dictionary rows")
    src = open_source_from_uri(args.source)
    only = {x.strip() for x in args.only.split(",") if x.strip()} if args.only else None
    if only:
        print(f"profiling {len(only)} table(s) only: {', '.join(sorted(only))}")
    doc, report = infer_with_report(src, llm=llm, no_sample_egress=args.no_sample_egress,
                                    context=context, only=only, lineage=lineage,
                                    use_declared=not args.no_declared_constraints,
                                    state=state)
    if context:
        report["context"] = {"chunks": len(context.chunks),
                             "dictionary_rows": len(context.dictionary)}
    out_path = args.into or args.out
    Path(out_path + ".report.json").write_text(json.dumps(report, indent=1))
    if args.into:
        from semlayer.merge import merge_layer, render_report

        base = _load_doc(args.into)
        merged = merge_layer(base, doc, scope=only)
        result = validate_document(base)
        _save_doc(args.into, base)
        print(render_report(merged))
        n = len(base["semantic_layer"]["tables"])
        mode = f"({llm.spend_summary})" if llm else "(deterministic mode)"
        print(f"\nwrote {args.into}: {n} tables, valid={result.ok} {mode}")
        doc = base
    else:
        result = validate_document(doc)
        _save_doc(args.out, doc)
        n = len(doc["semantic_layer"]["tables"])
        mode = f"({llm.spend_summary})" if llm else "(deterministic mode)"
        print(f"wrote {args.out}: {n} tables, valid={result.ok} {mode}")
    items = collect(doc)
    if items:
        print(f"{len(items)} items await review: semlayer review {out_path}")
    return 0 if result.ok else 1


def _cmd_review(args: argparse.Namespace) -> int:
    from semlayer.review import apply, collect

    doc = _load_doc(args.doc)
    items = collect(doc)
    if not items:
        print("review queue empty")
        return 0
    for i, it in enumerate(items, 1):
        print(f"[{i}/{len(items)}] {it.kind}: {it.location}\n    claim: {it.claim}"
              + (f"\n    evidence: {it.evidence}" if it.evidence else ""))
        if args.list:
            continue
        ans = input("    accept/reject/skip [a/r/s]? ").strip().lower()
        if ans in ("a", "accept"):
            apply(doc, it, "accept")
        elif ans in ("r", "reject"):
            apply(doc, it, "reject")
    if not args.list:
        _save_doc(args.doc, doc)
        print(f"saved {args.doc}")
    return 0


def _cmd_probe(args: argparse.Namespace) -> int:
    import json

    from semlayer.pipeline import open_source_from_uri
    from semlayer.probe import render, tier1, tier2

    lineage = None
    if args.manifest:
        from semlayer.lineage import load_dbt_manifest

        lineage = load_dbt_manifest(args.manifest)
    src = open_source_from_uri(args.source)
    p = tier1(src, lineage=lineage)
    if args.sample:
        p = tier2(src, p)

    if args.json:
        print(json.dumps(p.telemetry(), indent=1))
    else:
        print(render(p))
        if not args.sample:
            print("\nRun with --sample to measure whether several tables hold the "
                  "same key values. That reads rows, so it is opt-in.")
    if args.share:
        print("\n# Telemetry payload (nothing is sent by this command):")
        print(json.dumps(p.telemetry(), indent=1))
    return 0


def _cmd_drift(args: argparse.Namespace) -> int:
    from semlayer import drift as drift_mod
    from semlayer.pipeline import open_source_from_uri

    doc = _load_doc(args.doc)
    src = open_source_from_uri(args.source)
    # the modeled tables ARE the structural baseline: drift is defined
    # relative to what the document claims, not a stored snapshot
    old = {t["name"]: {c["name"]: c["sql_type"] for c in t["columns"]}
           for t in doc["semantic_layer"]["tables"]
           if t.get("lifecycle") not in ("orphaned",)}
    events = drift_mod.diff_snapshots(old, drift_mod.snapshot(src))
    events += drift_mod.semantic_drift(src, doc)
    if not events:
        print("no drift detected")
        return 0
    cs = drift_mod.apply_drift(doc, events)
    if args.cqs and args.source.startswith("duckdb:"):
        import duckdb
        import yaml

        suite = yaml.safe_load(Path(args.cqs).read_text())["cq_suite"]["questions"]
        con = duckdb.connect(args.source.split(":", 1)[1])
        try:
            cs.broken_cqs = drift_mod.cq_regression(con, suite, events, doc)
        finally:
            con.close()
    if args.json:
        import json

        print(json.dumps({
            "events": [{"kind": e.kind, "table": e.table, "column": e.column,
                        "detail": e.detail} for e in cs.events],
            "orphaned": cs.orphaned,
            "needs_inference": cs.needs_inference,
            "affected": cs.affected,
            "broken_cqs": cs.broken_cqs,
            "demoted": cs.demoted,
            "retable": sorted({e.table for e in cs.events}),
        }, indent=1))
    else:
        print(drift_mod.render_changeset(cs))
    if args.apply:
        _save_doc(args.doc, doc)
        if not args.json:
            print(f"applied to {args.doc}")
    return 1  # nonzero: drift found (CI-hook friendly)


def _cmd_lint(args: argparse.Namespace) -> int:
    import json

    from semlayer.lint import lint_sql, render_findings

    doc = _load_doc(args.doc)
    text = sys.stdin.read() if args.sql == "-" else Path(args.sql).read_text()
    statements = [x.strip() for x in text.split(";") if x.strip()]
    worst = 0
    results = []
    for stmt in statements:
        r = lint_sql(doc, stmt, dialect=args.dialect)
        results.append({"sql": stmt, **r})
        worst = max(worst, 2 if r["errors"] else (1 if r["warnings"] else 0))
        if not args.json:
            head = stmt.replace("\n", " ")
            print(f"-- {head[:90]}{'…' if len(head) > 90 else ''}")
            print(render_findings(r))
            print()
    if args.json:
        print(json.dumps(results, indent=2, default=str))
    return worst


def _cmd_telemetry(args: argparse.Namespace) -> int:
    """Report or change the telemetry setting.

    With no flag this prints status rather than doing nothing, because the
    question a user has when they type `semlayer telemetry` is "is it on?".
    """
    from semlayer import telemetry

    if args.off:
        telemetry.turn_off()
    elif args.on:
        telemetry.turn_on()
    elif args.show:
        spool = telemetry.spool_path()
        if spool.exists():
            sys.stdout.write(spool.read_text())
        else:
            print(f"no events recorded ({spool})")
        return 0

    why = telemetry.disabled_by()
    if why is None:
        print("telemetry: on")
        print("  events are recorded to a local file and NOT sent anywhere "
              "in this release")
        print("  see them:  semlayer telemetry --show")
        print("  turn off:  semlayer telemetry --off")
    else:
        print("telemetry: off")
        # Naming the reason matters: --on removes the marker but cannot clear
        # an exported variable, so without this a user would turn it "on",
        # see success, and still be off.
        print(f"  turned off by: {why}")
        if why == "SEMLAYER_TELEMETRY":
            print("  this is an environment variable; `--on` cannot clear it. "
                  "Unset it in your shell.")
    print(f"  spool: {telemetry.spool_path()}")
    return 0


def _cmd_mcp(args: argparse.Namespace) -> int:
    from semlayer.mcp_server import build_server

    build_server(_load_doc(args.doc)).run()
    return 0


_HANDLERS = {
    "validate": _cmd_validate,
    "init": _cmd_init,
    "infer": _cmd_infer,
    "review": _cmd_review,
    "probe": _cmd_probe,
    "drift": _cmd_drift,
    "lint": _cmd_lint,
    "mcp": _cmd_mcp,
    "telemetry": _cmd_telemetry,
}


def _error_class(e: BaseException) -> str:
    """Map an exception TYPE to the reported class. Never the message.

    The message is where warehouse names, hostnames and SQL live, so it is
    not read here at all. That is why this maps types, and why it is coarser
    than the enum allows: `auth` is declared in the schema but unreachable,
    because SourceError covers a bad URI, a refused login and a missing
    grant alike and telling them apart would mean parsing text.

    Builtins are mapped too. A missing file or an unreadable one is an
    ordinary user mistake, and calling it "internal" would make that bucket
    -- which should mean OUR bug -- mean nothing.
    """
    from semlayer.errors import DriftError, LLMError, SourceError, SpecError

    # Most specific first: FileNotFoundError and PermissionError are both
    # OSError, and SourceError/SpecError are both SemlayerError.
    for kinds, label in (
        ((SourceError,), "connection"),
        ((SpecError,), "invalid_input"),
        ((LLMError,), "llm"),
        ((DriftError, SemlayerError), "other"),
        ((PermissionError,), "permission"),
        ((FileNotFoundError, IsADirectoryError, NotADirectoryError), "invalid_input"),
        ((TimeoutError,), "timeout"),
        ((ConnectionError,), "connection"),
    ):
        if isinstance(e, kinds):
            return label
    return "internal"


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, dispatch, and render taxonomy errors with hints."""
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    from semlayer import telemetry

    # Recorded in `finally`, not here. duration_s, outcome and error_class
    # do not exist until the work is over, and recording up front is why the
    # event could previously carry nothing but a command name. `t` is still
    # the start, so a run is attributed to the minute it began -- only the
    # write waits.
    started_wall, started = int(time.time()), time.monotonic()
    outcome, error_class = "ok", None
    try:
        rc = _HANDLERS[args.command](args)
        if rc != 0:
            outcome = "error"      # a handler can fail without raising
        return rc
    except SemlayerError as e:
        outcome, error_class = "error", _error_class(e)
        print(f"error: {e}", file=sys.stderr)
        if e.hint:
            print(f"hint:  {e.hint}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        outcome = "interrupted"
        raise
    except Exception as e:
        # Caught only to classify. Re-raised immediately, so the user sees
        # the same traceback as before -- but an unexpected failure stops
        # being invisible to us.
        outcome, error_class = "error", _error_class(e)
        raise
    finally:
        # The one command we do not record. Logging that someone opted out,
        # at the moment they opted out, is the behaviour the switch exists to
        # prevent -- and `--show` must print what WOULD be sent, not a spool
        # this invocation just appended to.
        if args.command != "telemetry":
            telemetry.record(
                "cli", t=started_wall, command=args.command,
                duration_s=int(time.monotonic() - started),
                outcome=outcome, error_class=error_class)


if __name__ == "__main__":
    sys.exit(main())
