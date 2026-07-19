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
    p.add_argument("warehouse", choices=["snowflake", "bigquery"])
    p.add_argument("--database", default="<your_database>")
    p.add_argument("--project", default="<your_project>")
    p.add_argument("--warehouse-name", default="<your_warehouse>")

    p = sub.add_parser("infer", help="Infer a semantic layer from a warehouse")
    p.add_argument("source", help="duckdb:<path> | snowflake | bigquery")
    p.add_argument("-o", "--out", default="semantic_layer.yaml")
    p.add_argument("--no-llm", action="store_true", help="Deterministic dry-run (no LLM calls)")
    p.add_argument("--no-sample-egress", action="store_true",
                   help="Never include cell values in LLM prompts")

    p = sub.add_parser("review", help="Review inferred claims (terminal queue)")
    p.add_argument("doc", help="semantic layer YAML path")
    p.add_argument("--list", action="store_true", help="List items without prompting")

    p = sub.add_parser("mcp", help="Serve a semantic layer over MCP (stdio)")
    p.add_argument("doc", help="semantic layer YAML path")

    p = sub.add_parser("drift", help="Detect warehouse drift vs a semantic layer")
    p.add_argument("doc", help="semantic layer YAML path")
    p.add_argument("source", help="duckdb:<path> | snowflake | bigquery")
    p.add_argument("--apply", action="store_true",
                   help="Apply orphaning/conflicts to the doc (writes in place)")
    p.add_argument("--cqs", help="CQ suite YAML to regression-check")
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

    print(render(args.warehouse, database=args.database,
                 project=args.project, warehouse_name=args.warehouse_name))
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
    src = open_source_from_uri(args.source)
    doc, report = infer_with_report(src, llm=llm, no_sample_egress=args.no_sample_egress)
    Path(args.out + ".report.json").write_text(json.dumps(report, indent=1))
    result = validate_document(doc)
    _save_doc(args.out, doc)
    n = len(doc["semantic_layer"]["tables"])
    mode = f"({llm.spend_summary})" if llm else "(deterministic mode)"
    print(f"wrote {args.out}: {n} tables, valid={result.ok} {mode}")
    items = collect(doc)
    if items:
        print(f"{len(items)} items await review: semlayer review {args.out}")
    return 0 if result.ok else 1


def _cmd_review(args: argparse.Namespace) -> int:
    from semlayer.review import apply, collect

    doc = _load_doc(args.doc)
    items = collect(doc)
    if not items:
        print("review queue empty")
        return 0
    for i, it in enumerate(items, 1):
        loc = f"{it.table}.{it.column}" if it.column else it.table
        print(f"[{i}/{len(items)}] {it.kind}: {loc}\n    claim: {it.claim}"
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
    print(drift_mod.render_changeset(cs))
    if args.apply:
        _save_doc(args.doc, doc)
        print(f"applied to {args.doc}")
    return 1  # nonzero: drift found (CI-hook friendly)


def _cmd_mcp(args: argparse.Namespace) -> int:
    from semlayer.mcp_server import build_server

    build_server(_load_doc(args.doc)).run()
    return 0


_HANDLERS = {
    "validate": _cmd_validate,
    "init": _cmd_init,
    "infer": _cmd_infer,
    "review": _cmd_review,
    "drift": _cmd_drift,
    "mcp": _cmd_mcp,
}


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, dispatch, and render taxonomy errors with hints."""
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    from semlayer import telemetry

    telemetry.record("cli", command=args.command)
    try:
        return _HANDLERS[args.command](args)
    except SemlayerError as e:
        print(f"error: {e}", file=sys.stderr)
        if e.hint:
            print(f"hint:  {e.hint}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
