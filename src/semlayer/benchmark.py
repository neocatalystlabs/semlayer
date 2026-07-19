"""Reproducible benchmark runner (M6): the ablation, hardened for publication.

Reproducibility contract (adversarial B5): results are exact under pinned
engine + prompt versions + models via committed cassettes; cross-model
variance is REPORTED, never hidden. `run_benchmark` returns structured
results; report generation renders docs/benchmark.md.
"""

from __future__ import annotations

from collections import defaultdict

CONDITIONS = ["schema_only", "semantic", "semantic+ontology"]


def run_fixture(fixture: str, answer_model: str | None = None,
                conditions: list[str] | None = None) -> dict:
    """Run one fixture's CQ suite under each ablation condition and score results."""
    import importlib
    import sys
    from pathlib import Path

    import duckdb
    import yaml

    oss = Path(__file__).resolve().parent.parent.parent
    sys.path.insert(0, str(oss / "fixtures"))

    from semlayer.cq.answer import (
        answer_with_repair,
        ontology_context,
        schema_only_context,
        score_answer,
        semantic_context,
    )
    from semlayer.llm.provider import AnthropicProvider
    from semlayer.ontology import build_base_graph
    from semlayer.pipeline import infer
    from semlayer.source import DuckDBSource

    mod = importlib.import_module(f"generators.{fixture}")
    con = duckdb.connect(":memory:")
    mod.build(con)
    doc = infer(DuckDBSource(con), llm=AnthropicProvider())
    graph = build_base_graph(doc)
    cq_path = oss / "fixtures" / "cqs" / f"{fixture}.yaml"
    suite = yaml.safe_load(cq_path.read_text())["cq_suite"]["questions"]

    out: dict = {"fixture": fixture, "n_cqs": len(suite), "conditions": {}}
    for cond in conditions or CONDITIONS:
        llm = AnthropicProvider(model=answer_model) if answer_model else AnthropicProvider()
        passed = 0
        fails: list[dict] = []
        by_cat: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for cq in suite:
            q = cq["question"]
            ctx = (schema_only_context(doc) if cond == "schema_only"
                   else semantic_context(doc, q) if cond == "semantic"
                   else ontology_context(doc, graph, q))
            ok, detail = score_answer(con, cq, answer_with_repair(llm, con, ctx, q))
            is_meta_kind = cq["expected_kind"] in ("refusal", "clarification")
            cat = cq.get("trap") or (cq["expected_kind"] if is_meta_kind else cq["complexity"])
            by_cat[cat][0] += ok
            by_cat[cat][1] += 1
            passed += ok
            if not ok:
                fails.append({"id": cq["id"], "category": cat, "detail": detail[:60]})
        out["conditions"][cond] = {
            "pass_rate": round(passed / len(suite), 3),
            "passed": passed,
            "by_category": {k: {"passed": v[0], "total": v[1]} for k, v in sorted(by_cat.items())},
            "fails": fails,
            "spend": llm.spend_summary,
            "model": llm.model,
        }
    con.close()
    return out


def render_report(results: list[dict], engine_version: str, notes: list[str]) -> str:
    """Render the ablation results table + failure breakdown as a markdown report."""
    lines = [
        "# Benchmark: text-to-SQL accuracy — raw schema vs. inferred semantic layer",
        "",
        f"Engine {engine_version}; answers execution-scored against gold CQ suites",
        "(fixed questions, human-verified expected SQL, executed live). Reproducible",
        "under pinned engine+model via committed cassettes; re-run with:",
        "`python -m semlayer.benchmark`.",
        "",
        "| Fixture | CQs | Answerer | schema_only | semantic | semantic+ontology |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        conds = r["conditions"]
        model = next(iter(conds.values()))["model"].split("-2")[0]
        row = [r["fixture"], str(r["n_cqs"]), model]
        for c in CONDITIONS:
            row.append(f"{conds[c]['pass_rate']:.2f}" if c in conds else "—")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append("## Failure breakdown by category (semantic condition)")
    lines.append("")
    lines.append("| Fixture | Category | Passed/Total |")
    lines.append("|---|---|---|")
    for r in results:
        sem = r["conditions"].get("semantic")
        if not sem:
            continue
        for cat, v in sem["by_category"].items():
            lines.append(f"| {r['fixture']} | {cat} | {v['passed']}/{v['total']} |")
    lines.append("")
    lines.append("## Notes")
    for n in notes:
        lines.append(f"- {n}")
    return "\n".join(lines) + "\n"
