"""Run the same three-agent chain twice and compare what each one decided.

Once on the bare warehouse, once with the semlayer MCP server attached. Prints
what each agent reported and what the third one did about it.

    python examples/agent_pipeline/pipeline.py              # both modes
    python examples/agent_pipeline/pipeline.py --mode raw   # just one

Replays from committed cassettes; no API key needed. With ANTHROPIC_API_KEY
set, any turn not already recorded is recorded.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agents import AgentRun, run_agent  # noqa: E402
from tools import McpTools, Warehouse, layer_tools, raw_tools  # noqa: E402

from semlayer.llm.provider import AnthropicProvider  # noqa: E402

FIXTURE = ROOT / "fixtures" / "build" / "messy_mart.duckdb"
CASSETTES = Path(__file__).resolve().parent / "cassettes"

# The plan figure agent 3 measures against, and how far over it may run before
# the breach is escalated. Ordinary business rule; nothing semlayer-specific.
# The figure agent 3 measures against. Chosen once, from the plan, not tuned to
# the outcome: true revenue sits just under it, so an ordinary overstatement is
# what carries the number across the line and turns a reporting error into an
# action. Both modes see the same target.
PLAN_TARGET = 16_000_000

# Identical for both modes. Only the tool list and the layer's standing
# instruction differ -- if the task text differed, the comparison would be rigged.
TASKS = {
    "metrics": "Report total revenue across all orders in this warehouse, as a single number.",
    "breakdown": ("For the exec summary, report revenue broken down by product category: the "
                  "top 3 categories by revenue. Also report the overall average order value."),
    "alert": (f"The plan target for total revenue is ${PLAN_TARGET:,}. Revenue above that "
              "target is a plan breach and must be escalated to Finance for review. "
              "Decide, and write the ticket if one is due."),
}

ANSWER_SHAPES = {
    "metrics": '{"revenue": <number>, "sql": "<the query you ran>"}',
    "breakdown": ('{"categories": [{"category": "<name>", "revenue": <number>}, ...], '
                  '"avg_order_value": <number>}'),
    "alert": '{"escalate": true|false, "ticket": "<ticket text, or empty>", "reason": "<why>"}',
}

_LAYER_INSTRUCTION = (
    "A semantic layer for this warehouse is available through your tools. It records what the "
    "tables mean, which rows must be excluded, and which tables are superseded. Start with "
    "semantic_search or get_tables; read table_detail before writing SQL; ALWAYS apply the "
    "required filters it names; never use objects it marks UNUSABLE; run check_sql on every "
    "query before you execute it and fix every error and warning it reports, then check again."
)


@dataclass
class Result:
    """One full pass of the chain: the mode it ran in and what each agent did."""

    mode: str
    runs: list[AgentRun]

    def by_name(self, name: str) -> AgentRun:
        """The run belonging to one agent."""
        return next(r for r in self.runs if r.name == name)


def _system(role: str, tools: list[str], layer: bool) -> str:
    listing = "\n".join(f"  {t}" for t in tools)
    lines = [
        f"You are the {role} agent in an automated reporting pipeline. No human reviews your "
        "output before the next agent uses it.",
        f"Tools available to you:\n{listing}",
    ]
    if layer:
        lines.append(_LAYER_INSTRUCTION)
    lines.append(f"When done, ANSWER with exactly this shape: {ANSWER_SHAPES[role]}")
    return "\n\n".join(lines)


def run_pipeline(mode: str, llm) -> Result:
    """One pass of the three-agent chain in the given mode."""
    # Built before the read-only connection is opened: `infer` takes a write
    # lock on the DuckDB file and DuckDB allows only one writer.
    layer = _ensure_layer() if mode == "layer" else None
    wh = Warehouse(FIXTURE)
    mcp = None
    try:
        if layer:
            mcp = McpTools(layer, sys.executable)
            dispatch, names = layer_tools(wh, mcp)
        else:
            dispatch, names = raw_tools(wh)

        runs: list[AgentRun] = []
        metrics = run_agent("metrics", llm, _system("metrics", names, mode == "layer"),
                            TASKS["metrics"], dispatch)
        runs.append(metrics)

        # The handoff: agent 2 receives a number, not a query. This is what
        # makes a step-1 error unrecoverable downstream -- there is nothing
        # left to inspect.
        revenue = metrics.answer.get("revenue")
        breakdown = run_agent("breakdown", llm, _system("breakdown", names, mode == "layer"),
                              f"{TASKS['breakdown']}\n\nTotal revenue (from the metrics "
                              f"agent): {revenue}", dispatch)
        runs.append(breakdown)

        # Agent 3 gets no warehouse access at all -- only the numbers handed
        # to it. That is the realistic shape, and it is why a bad number is
        # unchallengeable by the time it becomes an action.
        alert = run_agent("alert", llm, _system("alert", ["(no tools)"], False),
                          f"{TASKS['alert']}\n\nReported total revenue: {revenue}\n"
                          f"Reported average order value: "
                          f"{breakdown.answer.get('avg_order_value')}",
                          dispatch)
        runs.append(alert)
        return Result(mode, runs)
    finally:
        if mcp:
            mcp.close()
        wh.close()


def _ensure_layer() -> Path:
    """Infer the layer if it is not already built, replaying this repo's cassettes.

    SEMLAYER_LLM_CACHE is not optional here. The CLI caches model responses in
    the platform cache directory and deliberately never in a source tree, so
    without this the subprocess finds nothing to replay and exits 2 on the
    first prompt -- which is exactly what it did in CI, while passing locally,
    because a leftover layer.yaml meant the subprocess never ran.
    """
    out = Path(__file__).resolve().parent / "layer.yaml"
    if out.exists():
        return out
    print(f"building the semantic layer (replays from cassettes) -> {out.name}", file=sys.stderr)
    subprocess.run([sys.executable, "-m", "semlayer.cli", "infer",
                    f"duckdb:{FIXTURE}", "-o", str(out)], check=True, cwd=ROOT,
                   env={**os.environ, "SEMLAYER_LLM_CACHE": str(ROOT / "cassettes")})
    return out


def _grounded(run: AgentRun, value) -> bool:
    """Did the number the agent answered actually come back from run_sql?

    Models under a text tool protocol sometimes answer with a figure they
    never executed for. This is not a semlayer feature -- it is a check on
    the example itself, so a fabricated number is labelled rather than
    quietly reported as a result.
    """
    if not isinstance(value, (int, float)):
        return False
    for t in run.turns:
        if t.tool == "run_sql" and t.result:
            for token in re.findall(r"-?\d+\.?\d*", t.result):
                try:
                    if abs(float(token) - float(value)) < 0.01:
                        return True
                except ValueError:
                    continue
    return False


def _money(v) -> str:
    return f"${v:,.2f}" if isinstance(v, (int, float)) else str(v)


def report(results: list[Result], truth: dict) -> None:
    """Print the side-by-side: numbers reported, SQL checked, action taken."""
    print()
    for r in results:
        m, s, a = r.by_name("metrics"), r.by_name("breakdown"), r.by_name("alert")
        print(f"{'=' * 78}\n  MODE: {r.mode}\n{'=' * 78}")
        for run in r.runs:
            calls = " -> ".join(run.tool_calls) or "(none)"
            print(f"  {run.name:<8} {len(run.turns)} turns   {calls}")
        print()
        rev_src = "from run_sql" if _grounded(m, m.answer.get("revenue")) else "NOT EXECUTED"
        print(f"  revenue reported      {_money(m.answer.get('revenue'))}  ({rev_src})")
        print(f"  truth                 {_money(truth['revenue'])}")
        rev = m.answer.get("revenue")
        if isinstance(rev, (int, float)) and truth["revenue"]:
            print(f"  off by                {rev / truth['revenue']:.2f}x")
        aov_src = ("from run_sql" if _grounded(s, s.answer.get("avg_order_value"))
                   else "NOT EXECUTED")
        print(f"  avg order value       {_money(s.answer.get('avg_order_value'))}  ({aov_src})")
        cats = s.answer.get("categories") or []
        if isinstance(cats, list) and cats:
            total = sum(c.get("revenue", 0) for c in cats if isinstance(c, dict))
            print("  revenue by category   "
                  + "; ".join(f"{c.get('category')} {_money(c.get('revenue'))}"
                              for c in cats if isinstance(c, dict)))
            print(f"  top-3 category sum    {_money(total)}"
                  + ("  <-- EXCEEDS total revenue" if total > truth["revenue"] else ""))
        checks = sum(1 for run in r.runs for t in run.turns if t.tool == "check_sql")
        print(f"  check_sql calls        {checks}")
        print()
        print(f"  ACTION TAKEN          escalate={a.answer.get('escalate')}")
        if a.answer.get("ticket"):
            print(f"  ticket                {a.answer['ticket']}")
        print()


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", choices=["raw", "layer", "both"], default="both")
    p.add_argument("--model", default=None, help="override the model (invalidates cassettes)")
    p.add_argument("--json", action="store_true", help="emit the full transcript as JSON")
    args = p.parse_args(argv)

    if not FIXTURE.exists():
        print(f"fixture missing: {FIXTURE}\nbuild it first: python fixtures/build.py",
              file=sys.stderr)
        return 1

    try:  # a local .env is how contributors record; absence just means replay
        from dotenv import load_dotenv

        load_dotenv(ROOT.parent / ".env")
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    kw = {"cassette_dir": CASSETTES}
    if args.model:
        kw["model"] = args.model
    llm = AnthropicProvider(**kw)

    truth = _truth()
    modes = ["raw", "layer"] if args.mode == "both" else [args.mode]
    results = [run_pipeline(m, llm) for m in modes]

    if args.json:
        print(json.dumps({r.mode: [{"agent": run.name, "answer": run.answer,
                                    "turns": [vars(t) for t in run.turns]}
                                   for run in r.runs] for r in results}, indent=1, default=str))
    else:
        report(results, truth)
    print(f"  llm turns: live={llm.calls_live} replayed={llm.calls_cached}")
    return 0


def _truth() -> dict:
    """The right answers, computed directly -- not taken from any agent."""
    con = __import__("duckdb").connect(str(FIXTURE), read_only=True)
    rev = float(con.sql("SELECT SUM(tot_amt) FROM ord_hdr WHERE sts_cd <> 'X'").fetchone()[0])
    aov = float(con.sql("SELECT SUM(tot_amt)/COUNT(*) FROM ord_hdr "
                        "WHERE sts_cd <> 'X'").fetchone()[0])
    con.close()
    return {"revenue": rev, "avg_order_value": aov}


if __name__ == "__main__":
    raise SystemExit(main())
