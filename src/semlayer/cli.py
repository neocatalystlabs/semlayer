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

from semlayer.errors import SemlayerError, SpecError


def _add_stitch_parser(sub) -> None:
    p = sub.add_parser("stitch", help="Infer the seam between several layers and write a hub")
    p.add_argument("members", nargs="*", help="member layer files (each inferred by its own team)")
    p.add_argument("-o", "--out", default=None, help="the hub to write, e.g. acme.yaml")
    p.add_argument("--name", default=None, help="the hub's name (default: the output file's stem)")
    p.add_argument("--refresh", metavar="HUB", default=None,
                   help="Re-infer an existing hub's seam from the members it lists. Edges you "
                        "reviewed, certified or rejected are kept; inferred ones are replaced; "
                        "an edge whose end disappeared becomes orphaned.")
    p.add_argument("--via", metavar="SOURCE", default=None,
                   help="connection the inclusion tests run through, with YOUR grants: "
                        "duckdb:<path> | postgresql://... | snowflake | bigquery. Default: "
                        "snowflake or bigquery when every member records that warehouse; "
                        "otherwise names-only proposals at low confidence.")
    p.add_argument("--no-queries", action="store_true",
                   help="Propose from names and distinct counts only; test nothing.")
    p.add_argument("--query-workers", type=int, default=None, metavar="N")


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
    p.add_argument("-o", "--out", default=None,
                   help="Output path. Default: <name>.yaml, where the name is --name, else the "
                        "schema(s) given to --schema, else the warehouse (database, project "
                        "or file stem).")
    p.add_argument("--schema", metavar="SCHEMAS", default=None,
                   help="Profile only these schemas (comma-separated). One schema also "
                        "names the document and the output file: a team scoping to its "
                        "own schema gets finance.yaml with name: finance, the identity "
                        "other teams' documents reference when the layers are combined.")
    p.add_argument("--name", default=None,
                   help="The document's name (default: the schema(s) given to --schema, "
                        "joined with _ when several, else the warehouse: database, project "
                        "or file stem). The output file is <name>.yaml unless -o is given.")
    p.add_argument("--no-llm", action="store_true", help="Deterministic dry-run (no LLM calls)")
    p.add_argument("--no-sample-egress", action="store_true",
                   help="Never include cell values in LLM prompts")
    p.add_argument("--no-llm-cache", action="store_true",
                   help="Do not cache model responses on disk. The cache lives in "
                        "your platform cache directory, holds the model's text about "
                        "your schema, and is what makes an interrupted run cheap to "
                        "resume; SEMLAYER_LLM_CACHE=off is the same switch, and "
                        "SEMLAYER_LLM_CACHE=<dir> moves it")
    p.add_argument("--llm-workers", type=int, default=4, metavar="N",
                   help="Concurrent LLM calls in typing escalation, foreign-key "
                        "validation and both Describe passes (default 4). "
                        "The output is identical at any value; only wall-clock "
                        "changes. Raise it if your API rate limit allows; 1 is "
                        "fully serial.")
    p.add_argument("--query-workers", type=int, default=None, metavar="N",
                   help="Concurrent foreign-key inclusion queries. Default: 4 on a network "
                        "warehouse (these run in YOUR warehouse and use your compute; "
                        "measured 3.3x at 4 and 6.7x at 8 on BigQuery, flat after), every "
                        "core on an in-process DuckDB file. Output is identical at any "
                        "value. 1 is fully serial.")
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

    _add_review_parser(sub)
    _add_maintain_parser(sub)

    p = sub.add_parser("telemetry",
                       help="Show or change anonymous usage telemetry")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--off", action="store_true",
                   help="turn telemetry off permanently (writes a marker file)")
    g.add_argument("--on", action="store_true", help="turn telemetry back on")
    g.add_argument("--show", action="store_true",
                   help="print the local spool, exactly as it would be sent")

    _add_stitch_parser(sub)

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


def default_out(out: str | None, name: str) -> str:
    """The output path: as given, else `<name>.yaml`.

    The filename follows the document's identity (`semlayer.source.document_name`)
    so that three teams scoping to three schemas do not end up with three
    files of the same name, and a whole-warehouse run is named for the
    warehouse rather than a generic word.
    """
    return out or f"{name}.yaml"


def _warn_if_overwriting_verdicts(args: argparse.Namespace) -> None:
    """`-o` onto a file that holds human or agent verdicts loses them; `--into` keeps them."""
    if args.into or not Path(args.out).exists():
        return
    text = Path(args.out).read_text()
    n_verdicts = text.count("signal: human") + text.count("signal: agent")
    if n_verdicts:
        print(f"warning: {args.out} holds {n_verdicts} recorded verdict(s) and -o will "
              f"overwrite them; use --into {args.out} to keep them", file=sys.stderr)


def _add_maintain_parser(sub) -> None:
    p = sub.add_parser("maintain",
                       help="Keep a layer true: detect drift, plan the re-inference, "
                            "and with --apply do it (dry run by default)")
    p.add_argument("doc", help="semantic layer YAML path")
    p.add_argument("source",
                   help="duckdb:<path> | postgresql://... | snowflake | bigquery | postgres")
    p.add_argument("--apply", action="store_true",
                   help="Re-infer the tables the changeset names and merge them into DOC")
    p.add_argument("--cqs", help="CQ suite YAML to regression-check (DuckDB sources)")
    p.add_argument("--json", action="store_true", help="one machine-readable record")
    p.add_argument("--json-out", metavar="PATH",
                   help="Also write the record to PATH (the report still prints); what a "
                        "scheduled job hands to the step that opens the pull request")
    p.add_argument("--no-llm", action="store_true",
                   help="Re-infer without the LLM (same meaning as on infer; the default, "
                        "like infer's, is the LLM when ANTHROPIC_API_KEY is set)")
    p.add_argument("--no-llm-cache", action="store_true",
                   help="Do not cache model responses on disk (same meaning as on infer)")
    p.add_argument("--llm-workers", type=int, default=4, metavar="N")
    p.add_argument("--query-workers", type=int, default=None, metavar="N")
    p.add_argument("--manifest", help="dbt manifest.json, as on infer")
    p.add_argument("--no-declared-constraints", action="store_true")


def _cmd_maintain(args: argparse.Namespace) -> int:
    import json
    import os

    from semlayer import maintain
    from semlayer.pipeline import open_source_from_uri

    doc = _load_doc(args.doc)
    src = open_source_from_uri(args.source)
    with_llm = not args.no_llm and bool(os.environ.get("ANTHROPIC_API_KEY"))
    suite = con = None
    if args.cqs and args.source.startswith("duckdb:"):
        import duckdb
        import yaml

        from semlayer.pipeline import _duckdb_args
        suite = yaml.safe_load(Path(args.cqs).read_text())["cq_suite"]["questions"]
        con = duckdb.connect(*_duckdb_args(args.source.split(":", 1)[1]))
    try:
        r = maintain.plan(doc, src, suite, con, with_llm=with_llm)
    finally:
        if con is not None:
            con.close()
    if not r.empty and args.apply:
        llm = None
        if with_llm:
            from semlayer.llm.provider import AnthropicProvider
            llm = AnthropicProvider(cache=not args.no_llm_cache, repo_cache=False)
        lineage = None
        if args.manifest:
            from semlayer.lineage import load_dbt_manifest
            lineage = load_dbt_manifest(args.manifest)
        maintain.apply(doc, src, r, llm, lineage=lineage,
                       options_kw={"use_declared": not args.no_declared_constraints,
                                   "llm_workers": args.llm_workers,
                                   "query_workers": args.query_workers})
        _save_doc(args.doc, doc)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(r.as_dict(), indent=1))
    if args.json:
        print(json.dumps(r.as_dict(), indent=1))
    else:
        print(maintain.render(r, args.doc, args.source))
    if r.empty:
        return 0
    if r.applied:
        return 0 if r.valid else 1
    return 1  # drift found, nothing written: the CI-hook answer


def _add_review_parser(sub) -> None:
    p = sub.add_parser("review", help="Review inferred claims as grouped questions")
    p.add_argument("doc", help="semantic layer YAML path")
    p.add_argument("--list", action="store_true", help="List the questions without prompting")
    p.add_argument("--flat", action="store_true",
                   help="One item at a time instead of grouped questions")
    p.add_argument("--members", action="store_true",
                   help="On a hub: ask every member's questions too, once each across the set, "
                        "and write verdicts into the member files (you need write access to them)")
    p.add_argument("--question", metavar="N|KEY", default=None,
                   help="With --list: print one question with every object it covers")
    p.add_argument("--answer", metavar="N=accept,KEY=reject,KEY=set:<value>,...", default=None,
                   help="Answer questions non-interactively (by number or stable key); "
                        "set:<value> replaces the claim with yours, in spec vocabulary "
                        "(see each question's `set` line)")
    p.add_argument("--exclude", metavar="N=obj,obj;...", default=None,
                   help="With --answer: objects to leave out of a question's verdict")
    p.add_argument("--note", metavar="N=text", action="append", default=None,
                   help="Leave a note on a question without deciding it (repeatable)")
    p.add_argument("--decisions", action="store_true",
                   help="List the verdicts already recorded in the document, newest first")
    p.add_argument("--by", default=None,
                   help="Who is deciding (default: git user.name where the document lives)")
    p.add_argument("--agent", action="store_true",
                   help="Verdicts are an automated reviewer's: recorded as signal `agent` "
                        "(requires --by naming the agent)")
    p.add_argument("--json", action="store_true",
                   help="machine-readable output for --list and --decisions")


def _load_doc(path: str) -> dict:
    import yaml

    from semlayer.validate import load_yaml

    try:
        doc = load_yaml(Path(path).read_text())
    except FileNotFoundError as e:
        raise SpecError(f"document not found: {path}",
                            hint="run `semlayer infer` first, or check the path") from e
    except yaml.YAMLError as e:
        raise SpecError(f"document is not valid YAML: {path} ({e})") from e
    if not isinstance(doc, dict) or "semantic_layer" not in doc:
        raise SemlayerError(f"not a semantic layer document: {path}",
                            hint="expected a top-level `semantic_layer:` mapping")
    from semlayer.setdoc import is_hub, load_set
    if is_hub(doc):
        # a hub and its members become one document; every command below
        # then works on the set exactly as it does on one file
        return load_set(path).doc
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

    from semlayer.pipeline import InferOptions, infer_with_report, open_source_from_uri
    from semlayer.review import collect
    from semlayer.source import document_name
    from semlayer.validate import validate_document

    llm = None
    if not args.no_llm:
        from semlayer.llm.provider import AnthropicProvider

        llm = AnthropicProvider(cache=not args.no_llm_cache, repo_cache=False)
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
    schema = {x.strip() for x in args.schema.split(",") if x.strip()} if args.schema else None
    if schema:
        print(f"profiling schema(s): {', '.join(sorted(schema))}")
    name = document_name(args.name, schema, src)
    args.out = default_out(args.out, name)
    _warn_if_overwriting_verdicts(args)
    options = InferOptions(no_sample_egress=args.no_sample_egress, only=only, schema=schema,
                           name=name, use_declared=not args.no_declared_constraints,
                           llm_workers=args.llm_workers, query_workers=args.query_workers)
    doc, report = infer_with_report(src, llm=llm, options=options, context=context,
                                    lineage=lineage, state=state)
    _note_run(report, doc, llm)
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


def _git_author(doc_path: str) -> str | None:
    import subprocess

    try:
        r = subprocess.run(["git", "config", "user.name"], cwd=Path(doc_path).resolve().parent,
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() or None if r.returncode == 0 else None


def _print_decisions(doc: dict, undecided: int, is_hub: bool, as_json: bool,
                     members=None) -> None:
    import json
    from dataclasses import asdict

    from semlayer.review import decisions, decisions_across

    rows = decisions_across(doc, {k: v[1] for k, v in members.items()}) if members \
        else decisions(doc)
    if as_json:
        print(json.dumps([asdict(d) for d in rows], indent=1))
        return
    name = doc["semantic_layer"].get("name", "")
    print(f"Decisions recorded in semantic layer '{name}', newest first. "
          "Each line: verdict, the table / table.column / metric / relationship it "
          "was given on, the claim as it stood in words (spec type in brackets), "
          "and after ';' what changed in the document. A heading says who decided "
          "and whether that was a person or an agent. One verdict given to many "
          "objects at once prints as one line with the count; --json lists every object.")
    if not rows:
        print("no decisions recorded in this document")
    group = None
    # one line per (day, author, verdict, claim), however the objects are spread in the file
    runs_by: dict[tuple, list] = {}
    for d in rows:
        runs_by.setdefault((d.date, d.by, d.signal, d.verdict, d.what), []).append(d)
    for run in runs_by.values():
        d = run[0]
        who = d.by or "(no author recorded)"
        head = f"{d.date or 'undated'}  {who}" + ("  [agent]" if d.signal == "agent" else "")
        if head != group:
            print(head)
            group = head
        if len(run) <= 3:
            for x in run:
                print(f"  {x.verdict:9} {x.where:45} {x.what}")
        else:
            where = ", ".join(x.where for x in run[:4]) + f" ... and {len(run) - 4} more"
            print(f"  {d.verdict:9} {len(run)} objects: {d.what}\n{'':12}{where}")
    n_a = sum(d.verdict == "accepted" for d in rows)
    n_r = sum(d.verdict == "rejected" for d in rows)
    n_s = sum(d.verdict == "set" for d in rows)
    print(f"\n{len(rows)} decisions: {n_a} accepted, {n_r} rejected"
          + (f", {n_s} set" if n_s else "") + ".  "
          f"Undecided items in the queue: {undecided} (semlayer review --list)")
    if is_hub:
        print("members hold their own decisions: run this on each member file")


def _print_question(q, full: bool) -> None:
    n_obj = len(q.items)
    if not q.decidable:
        head = f"{n_obj} columns " if q.kind == "untyped" else ""
        print(f"{q.n}. [{q.kind}] {head}{q.what}")
        print(f"   {q.reason}")
        print(f"   a verdict here {q.on_reject}")
    else:
        print(f"{q.n}. [{q.kind}] {n_obj} {q.noun}{'s' if n_obj != 1 else ''}: "
              f"{'each ' if n_obj > 1 else ''}{q.what}")
        conf = f"; confidence {q.confidence}" if q.confidence is not None else ""
        print(f"   engine saw: {q.reason}{conf}")
        names = q.distinct_names()
        if n_obj > 1 and len(names) < n_obj:
            label = (f"on {len(names)} tables" if q.noun == "metric"
                     else f"{len(names)} distinct names")
            print(f"   {label}: " + ", ".join(names[:12])
                  + (f" ... and {len(names) - 12} more" if len(names) > 12 else ""))
        objs = q.objects
        if full:
            for i, o in enumerate(objs, 1):
                print(f"   {i:4}. {o}    {q.items[i - 1].evidence}")
        else:
            print("   objects: " + ", ".join(objs[:6])
                  + (f" ... and {len(objs) - 6} more (--list --question {q.n})"
                     if len(objs) > 6 else ""))
        print(f"   accept -> {q.on_accept}; reject -> {q.on_reject}")
        from semlayer.review import set_shape
        shape = set_shape(q)
        if shape:
            print(f"   set {shape} -> replaces the claim with yours, human-authored")
    for nt in q.notes:
        who = nt.get("by") or nt.get("signal")
        print(f"   note ({who}, {nt.get('date')}): {nt.get('note')}")
    print(f"   key: {q.key}")
    print()


def _find_question(qs, ref: str):
    ref = ref.strip()
    for q in qs:
        if q.key == ref or str(q.n) == ref:
            return q
    raise SpecError(f"no question {ref!r}; numbers change as questions are answered, so "
                    "scripts should use the key (q-xxxxxxxx) from `semlayer review --list`")


def _parse_pairs(spec: str | None, sep: str) -> list[tuple[str, str]]:
    out = []
    for part in (spec or "").split(sep):
        if not part.strip():
            continue
        k, _, v = part.partition("=")
        if not v:
            raise SpecError(f"expected N=value, got {part!r}")
        out.append((k.strip(), v.strip()))
    return out


def _cmd_review(args: argparse.Namespace) -> int:
    from semlayer.review import collect, for_hub, group, set_author
    from semlayer.validate import load_yaml

    if args.agent and not args.by:
        raise SpecError("--agent needs --by naming the agent "
                        "(e.g. --by maintainer/claude-opus-5-5)")
    doc = _load_doc(args.doc)
    # a hub is reviewed through the resolved set but SAVED as itself: the
    # resolved document holds every member's tables, and writing it back
    # would turn the hub file into a copy of the whole estate
    hub = load_yaml(Path(args.doc).read_text()) if doc["semantic_layer"].get("members") else None
    target = hub if hub is not None else doc
    if args.members and hub is None:
        raise SpecError(f"{args.doc} is not a hub; --members applies to a set")
    members = _load_members(args.doc, hub) if (hub is not None and args.members) else None
    items = collect(doc)
    if hub is not None and members is None:
        items = for_hub(items, doc["semantic_layer"]["name"])
    if args.decisions:
        _print_decisions(target, len(items), hub is not None and members is None, args.json,
                         members)
        return 0
    set_author(args.by or _git_author(args.doc), "agent" if args.agent else "human")
    if args.flat:
        return _review_flat(args, doc, hub, items)
    asked, not_asked = group(items, doc)   # the resolved set: a hub has no tables of its own
    if args.answer or args.note:
        return _review_answer(args, doc, hub, asked, not_asked, members)
    return _review_questions(args, doc, hub, items, asked, not_asked, members)


def _load_members(hub_path: str, hub: dict) -> dict[str, tuple[Path, dict]]:
    """Each member's own file, by name, for `--members` to write verdicts into."""
    from semlayer.validate import load_yaml

    out = {}
    for m in hub["semantic_layer"]["members"]:
        p = Path(m["path"])
        if not p.is_absolute():
            p = Path(hub_path).resolve().parent / p
        out[m["name"]] = (p, load_yaml(p.read_text()))
    return out


def _save_reviewed(doc_path: str, target: dict, members, touched: set[str], note: str) -> None:
    """Save the reviewed document and, with --members, every member file a verdict reached."""
    _save_doc(doc_path, target)
    print(f"saved {doc_path}{note}")
    for name in sorted(touched):
        path, mdoc = members[name]
        _save_doc(str(path), mdoc)
        print(f"saved {path}")


def _parse_excludes(args, asked) -> dict[str, set[str]]:
    excludes: dict[str, set[str]] = {}
    for k, v in _parse_pairs(args.exclude, ";"):
        q = _find_question(asked, k)
        wanted = {x.strip() for x in v.split(",") if x.strip()}
        unknown = wanted - set(q.objects)
        if unknown:
            raise SpecError(f"not in question {q.n}: {', '.join(sorted(unknown))}")
        excludes[q.key] = wanted
    return excludes


def _review_answer(args, doc, hub, asked, not_asked, members=None) -> int:
    from semlayer.review import answer, note

    target = hub if hub is not None else doc
    mdocs = {k: v[1] for k, v in members.items()} if members else None
    touched: set[str] = set()
    # parse everything before changing anything, so a bad flag changes nothing
    excludes = _parse_excludes(args, asked)
    from semlayer.review import check_set

    verdicts = []
    for k, v in _parse_pairs(args.answer, ","):
        q = _find_question(asked + not_asked, k)
        if v.startswith("set:"):
            why = check_set(q, v[4:].strip(), doc)
            if why:
                raise SpecError(f"question {q.n}: {why}")
        elif v not in ("accept", "reject"):
            raise SpecError(f"verdict must be accept, reject or set:<value>, got {v!r}")
        verdicts.append((q, v))
    notes = []
    for spec in args.note or []:
        k, _, text = spec.partition("=")
        if not text.strip():
            raise SpecError(f"expected N=text, got {spec!r}")
        notes.append((_find_question(asked + not_asked, k), text.strip()))
    changed = 0
    for q, v in verdicts:
        n = answer(doc, q, v, excludes.get(q.key), hub, mdocs, touched)
        word = "set" if v.startswith("set:") else f"{v}ed"
        print(f"{word} question {q.n} ({q.key}): {n} of {len(q.items)} {q.noun}(s)")
        changed += n
    for q, text in notes:
        note(target, q, text)
        print(f"noted question {q.n} ({q.key})")
        changed += 1
    if changed:
        _save_reviewed(args.doc, target, members, touched, "")
    return 0


def _review_questions(args, doc, hub, items, asked, not_asked, members=None) -> int:
    import json

    from semlayer.review import answer

    name = doc["semantic_layer"].get("name", "")
    mdocs = {k: v[1] for k, v in members.items()} if members else None
    touched: set[str] = set()
    if args.json:
        print(json.dumps({"document": name, "undecided_items": len(items),
                          "questions": [q.as_dict() for q in asked],
                          "not_asked": [q.as_dict() for q in not_asked]}, indent=1))
        return 0
    if not items:
        print("review queue empty")
        return 0
    if args.question:
        _print_question(_find_question(asked + not_asked, args.question), full=True)
        return 0
    print(f"Review queue for '{name}': {len(items)} undecided claims, {len(asked)} "
          f"question{'s' if len(asked) != 1 else ''}. A verdict on a question applies to every "
          "object listed under it: they are in one question only because the engine made the "
          "identical claim about each for the identical reason. Each question: the claim in "
          "words (spec type in brackets), what the engine saw, confidence, the objects, and "
          "what accept / reject does. Numbers change as questions are answered; the key does "
          "not.\n")
    saved = 0
    for q in asked:
        _print_question(q, full=False)
        if not args.list:
            verdict, exclude = _ask(q, doc)
            if verdict is not None:
                saved += answer(doc, q, verdict, exclude, hub, mdocs, touched)
    if not_asked:
        print("Shown, not asked:\n")
        for q in not_asked:
            _print_question(q, full=False)
    if not args.list and saved:
        _save_reviewed(args.doc, hub if hub is not None else doc, members, touched,
                       f": {saved} verdict(s) written")
    return 0


def _ask(q, doc: dict) -> tuple[str | None, set[str]]:
    """Prompt for one question: a verdict (or None for skip) and the objects left out."""
    from semlayer.review import check_set, set_shape

    exclude: set[str] = set()
    shape = set_shape(q)
    menu = "accept/reject/skip/expand" + ("/set" if shape else "") + ", or x 3,7 to leave out"
    keys = "[a/r/s/e" + ("/t" if shape else "") + "/x]"
    while True:
        ans = input(f"    {menu} {keys}? ")
        ans = ans.strip().lower()
        if shape and ans in ("t", "set"):
            value = input(f"    set {shape}: ").strip()
            why = check_set(q, value, doc)
            if why:
                print(f"    {why}")
                continue
            return f"set:{value}", exclude
        if ans in ("e", "expand"):
            for i, o in enumerate(q.objects, 1):
                print(f"    {i:4}. {o}    {q.items[i - 1].evidence}")
            continue
        if ans.startswith("x"):
            for tok in ans[1:].replace(",", " ").split():
                if tok.isdigit() and 1 <= int(tok) <= len(q.items):
                    exclude.add(q.items[int(tok) - 1].location)
            print(f"    leaving out {len(exclude)}: {', '.join(sorted(exclude))}")
            continue
        verdict = {"a": "accept", "accept": "accept", "r": "reject", "reject": "reject"}.get(ans)
        return verdict, exclude


def _review_flat(args: argparse.Namespace, doc: dict, hub: dict | None, items) -> int:
    from semlayer.review import apply, apply_to_hub

    if not items:
        print("review queue empty")
        return 0
    for i, it in enumerate(items, 1):
        print(f"[{i}/{len(items)}] {it.kind}: {it.location}\n    claim: {it.claim}"
              + (f"\n    evidence: {it.evidence}" if it.evidence else ""))
        if args.list:
            continue
        ans = input("    accept/reject/skip [a/r/s]? ").strip().lower()
        verdict = {"a": "accept", "accept": "accept", "r": "reject", "reject": "reject"}.get(ans)
        if verdict is None:
            continue
        if hub is not None:
            if not apply_to_hub(hub, it, verdict):
                print("    (belongs to a member: review that file)")
        else:
            apply(doc, it, verdict)
    if not args.list:
        _save_doc(args.doc, hub if hub is not None else doc)
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

    from semlayer import telemetry
    telemetry.note(probe=p.telemetry())

    if args.json:
        print(json.dumps(p.telemetry(), indent=1))
    else:
        print(render(p))
        if not args.sample:
            print("\nRun with --sample to measure whether several tables hold the "
                  "same key values. That reads rows, so it is opt-in.")
    if args.share:
        # This used to say "nothing is sent by this command", which was true
        # only while no sender existed. It is sent now, like every other
        # event, unless telemetry is off.
        print("\n# Telemetry payload (sent with this run unless telemetry is off):")
        print(json.dumps(p.telemetry(), indent=1))
    return 0


def _cmd_drift(args: argparse.Namespace) -> int:
    from semlayer import drift as drift_mod
    from semlayer.pipeline import open_source_from_uri

    doc = _load_doc(args.doc)
    src = open_source_from_uri(args.source)
    # the modeled tables ARE the structural baseline: drift is defined
    # relative to what the document claims, not a stored snapshot
    events = drift_mod.diff_snapshots(drift_mod.baseline(doc), drift_mod.snapshot(src))
    events += drift_mod.semantic_drift(src, doc)
    if not events:
        print("no drift detected")
        return 0
    cs = drift_mod.apply_drift(doc, events)
    if args.cqs and args.source.startswith("duckdb:"):
        import duckdb
        import yaml

        suite = yaml.safe_load(Path(args.cqs).read_text())["cq_suite"]["questions"]
        from semlayer.pipeline import _duckdb_args
        con = duckdb.connect(*_duckdb_args(args.source.split(":", 1)[1]))
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

    # These are the one place telemetry is allowed to fail loudly. Elsewhere
    # it swallows everything, because it must never break the command the
    # user actually ran -- but here changing the setting IS the command, and
    # silently failing would leave them believing a privacy choice took
    # effect when it did not.
    try:
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
    except OSError as e:
        where = telemetry.spool_path().parent
        if args.off:
            raise SemlayerError(
                f"could not turn telemetry off: {where} is not writable",
                hint="telemetry is still ON. Set SEMLAYER_TELEMETRY=off in "
                     "your environment, which needs no file, or make that "
                     "directory writable") from e
        if args.on:
            raise SemlayerError(
                f"could not turn telemetry on: {where} is not writable",
                hint="telemetry remains off, which is the safe direction. "
                     "Make that directory writable to change it") from e
        raise SemlayerError(f"could not read the telemetry spool in {where}",
                            hint="check that directory is readable") from e

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


def _stitch_source(args: argparse.Namespace, member_docs: list[dict]):
    """The executor for inclusion tests: --via, else the members' shared cloud warehouse."""
    from semlayer.pipeline import default_query_workers, open_source_from_uri
    if args.no_queries:
        return None, None
    uri = args.via
    if uri is None:
        kinds = {(d["semantic_layer"].get("warehouse") or "").split(":", 1)[0] for d in member_docs}
        if len(kinds) == 1 and kinds <= {"snowflake", "bigquery"}:
            uri = next(iter(kinds))
    if uri is None:
        print("no --via and the members do not share a cloud warehouse: proposing from names "
              "only (nothing tested)")
        return None, None
    src = open_source_from_uri(uri)
    workers = args.query_workers or default_query_workers(src)
    return src, workers


def _cmd_stitch(args: argparse.Namespace) -> int:
    from semlayer.stitch import refresh, stitch
    from semlayer.validate import load_yaml, validate_file

    if args.refresh:
        if args.members or args.name:
            raise SemlayerError("--refresh takes the hub alone: its members and name are in it")
        hub_sl = load_yaml(Path(args.refresh).read_text())["semantic_layer"]
        if not hub_sl.get("members"):
            raise SemlayerError(f"{args.refresh} is not a hub: it lists no members")
        base = Path(args.refresh).resolve().parent
        member_docs = [_load_doc(str(base / m["path"])) for m in hub_sl["members"]]
        src, workers = _stitch_source(args, member_docs)
        result = refresh(args.refresh, source=src, query_workers=workers or 1, via_label=args.via)
        args.out = args.out or args.refresh
    else:
        if not args.members or not args.out:
            raise SemlayerError("stitch needs member files and -o, or --refresh HUB")
        member_docs = [_load_doc(m) for m in args.members]
        src, workers = _stitch_source(args, member_docs)
        name = args.name or Path(args.out).stem
        result = stitch(args.members, name, args.out, source=src, query_workers=workers or 1,
                        via_label=args.via)
    _save_doc(args.out, result.hub)
    ok = validate_file(args.out).ok
    kept = (f"{result.kept} kept from before ({result.orphaned} orphaned), "
            f"{result.superseded} fresh superseded, " if args.refresh else "")
    print(f"wrote {args.out}: {len(result.hub['semantic_layer'].get('relationships', []))} seam "
          f"edges ({result.accepted} accepted, {result.review} for review, "
          f"{result.copies} same-entity copies, {kept}"
          f"{len(result.unresolved)} unresolved), valid={ok} ({result.mode})")
    if result.unresolved:
        for u in result.unresolved[:10]:
            print(f"  unresolved: {u['table']}.{u['column']}: {u['note']}")
    return 0 if ok else 1


_HANDLERS = {
    "validate": _cmd_validate,
    "stitch": _cmd_stitch,
    "init": _cmd_init,
    "infer": _cmd_infer,
    "maintain": _cmd_maintain,
    "review": _cmd_review,
    "probe": _cmd_probe,
    "drift": _cmd_drift,
    "lint": _cmd_lint,
    "mcp": _cmd_mcp,
    "telemetry": _cmd_telemetry,
}


_TELEMETRY_NOTICE = """\
semlayer now sends anonymous usage statistics: command, duration, outcome,
warehouse type, table/column counts, version, OS, and which MCP client
connects and uses. Never names, SQL, or data.
  See exactly what is sent:  semlayer telemetry --show
  Turn it off permanently:   semlayer telemetry --off
Details: https://github.com/neocatalystlabs/semlayer/blob/main/docs/telemetry.md
"""


def _note_run(report: dict, doc: dict | None = None, llm=None) -> None:
    """Hand what the pipeline learned to telemetry.

    The report already carries all of this -- it is what `infer --json`
    prints -- so nothing new is computed and nothing new is measured. The
    values pass through the whitelist on the way out, where the counts are
    bucketed, so an exact table count cannot escape from here either.
    """
    from semlayer import telemetry

    counts = report.get("counts") or {}
    # `schemas` is not in the report, but every table's `source` is
    # "schema.table", so the COUNT is derivable. Only the count leaves --
    # the names are discarded here, and the count is bucketed on the way out.
    schemas = None
    if doc:
        sl = doc.get("semantic_layer", doc)      # the document is wrapped
        names = {str(t.get("source", "")).rsplit(".", 1)[0]
                 for t in (sl.get("tables") or [])
                 if "." in str(t.get("source", ""))}
        schemas = len(names) or None
    telemetry.note(
        tables=counts.get("tables"),
        columns=counts.get("columns"),
        schemas=schemas,
        stages={k: int(v) for k, v in (report.get("stages") or {}).items()},
        llm=_llm_class(llm),
    )


def _llm_class(llm) -> str:
    """Provider class only -- never the model, the key, or the endpoint."""
    if llm is None:
        return "none"
    name = type(llm).__name__.lower()
    if "anthropic" in name:
        return "anthropic"
    if "openai" in name:
        return "openai"
    return "other"


def _note_invocation(args: argparse.Namespace) -> None:
    """Facts knowable before the work: which warehouse, which flags.

    Recorded up front so they survive a failure -- a run that dies on
    connection is exactly the one where knowing the connector matters.
    """
    from semlayer import telemetry

    # Known aliases only. Anything else falls through to the whitelist and
    # is DROPPED, which is deliberate: this takes everything before the first
    # colon, so `semlayer infer mydb.duckdb` yields "mydb.duckdb" -- a
    # filename. A strict set with no "other" is what stops that becoming a
    # recorded field.
    aliases = {"postgresql": "postgres", "pg": "postgres",
               "bq": "bigquery", "sf": "snowflake"}
    scheme = None
    for attr in ("source", "warehouse"):
        value = getattr(args, attr, None)
        if isinstance(value, str) and value:
            raw = value.split(":", 1)[0].split("+", 1)[0].lower()
            scheme = aliases.get(raw, raw)
            break
    telemetry.note(
        connector=scheme,
        flags=[f for f in ("no_llm", "no_sample_egress",
                           "no_declared_constraints", "apply", "json", "share")
               if getattr(args, f, False)],
    )


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

    # Printed before the work, not after, so a long `infer` cannot send its
    # first event before the user has been told that sending now happens.
    #
    # Never for `telemetry` itself. That command is the inspection and
    # control surface, and marking the notice TRUNCATES the spool -- so a
    # user whose first command after upgrading is `telemetry --show` would
    # be shown the notice, have the spool wiped, and then be shown nothing.
    # They asked to see what would be sent and we would have deleted it.
    if (args.command != "telemetry"
            and telemetry.enabled() and telemetry.notice_pending()):
        print(_TELEMETRY_NOTICE, file=sys.stderr)
        telemetry.mark_notice_shown()

    # Recorded in `finally`, not here. duration_s, outcome and error_class
    # do not exist until the work is over, and recording up front is why the
    # event could previously carry nothing but a command name. `t` is still
    # the start, so a run is attributed to the minute it began -- only the
    # write waits.
    _note_invocation(args)
    started_wall, started = int(time.time()), time.monotonic()
    outcome, error_class = "ok", None
    try:
        rc = _HANDLERS[args.command](args)
        if rc != 0:
            # A handler can fail without raising: an invalid document, a
            # missing path reported as FAIL, drift found. Exit code 1 is the
            # document-domain outcome, so it is the input that was wrong,
            # not the environment and not us. Left unclassified, these were
            # the only error rows with a null class -- indistinguishable
            # from a crash, which is the one thing the class exists to tell.
            outcome, error_class = "error", "invalid_input"
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
            # Last thing, after the event exists. Bounded by its own
            # deadline, so a dead network costs 1.5s once rather than
            # hanging the command.
            telemetry.flush()


if __name__ == "__main__":
    sys.exit(main())
