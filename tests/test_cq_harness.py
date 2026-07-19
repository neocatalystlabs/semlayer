"""M5: the ablation gate + CQ generation/skeptic quality gates.

Ablation floors (measured 2026-07-18, Haiku answerer + 1 repair round):
  messy_mart schema_only 0.34 | semantic 0.45 | semantic+ontology 0.45
Gates: semantic must beat schema_only by >=0.05 absolute; ontology must be
non-inferior to semantic (>= semantic - 0.05). The HONEST verdict stands in
docs/ablation-m5.md: base-graph ontology adds no measurable lift on this
workload -> enrichment stays internal (skeptic verdict upheld by data).
"""

import sys
from pathlib import Path

import duckdb
import pytest
import yaml

OSS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OSS / "fixtures"))

try:
    from dotenv import load_dotenv
    load_dotenv(OSS.parent / ".env")
except ImportError:
    pass

from semlayer.cq.answer import (answer_with_repair, ontology_context,  # noqa: E402
                                schema_only_context, score_answer, semantic_context)
from semlayer.cq.generate import generate, skeptic_verify  # noqa: E402
from semlayer.llm.provider import AnthropicProvider, CassetteMiss  # noqa: E402
from semlayer.ontology import build_base_graph, join_path  # noqa: E402
from semlayer.pipeline import infer  # noqa: E402
from semlayer.source import DuckDBSource  # noqa: E402

SEEDED_BAD_CQS = [
    {"question": "What is the vibe of our customers?", "complexity": "simple"},
    {"question": "How profitable were we?", "complexity": "simple"},  # no profit/cost modeled
    {"question": "What was employee headcount by office floor?", "complexity": "simple"},  # unmodeled
    {"question": "Is revenue good?", "complexity": "simple"},
    {"question": "Compare the thing with the other thing.", "complexity": "complex"},
    {"question": "What was churn last quarter?", "complexity": "complex"},  # churn undefined here
]


@pytest.fixture(scope="module")
def setup():
    import importlib
    mod = importlib.import_module("generators.messy_mart")
    con = duckdb.connect(":memory:")
    mod.build(con)
    try:
        doc = infer(DuckDBSource(con), llm=AnthropicProvider())
    except CassetteMiss as e:
        pytest.skip(str(e))
    return con, doc, build_base_graph(doc)


def _run_condition(con, doc, graph, suite, cond):
    llm = AnthropicProvider()
    passed = 0
    for cq in suite:
        q = cq["question"]
        ctx = (schema_only_context(doc) if cond == "schema_only"
               else semantic_context(doc, q) if cond == "semantic"
               else ontology_context(doc, graph, q))
        ok, _ = score_answer(con, cq, answer_with_repair(llm, con, ctx, q))
        passed += ok
    return passed / len(suite)


def test_ablation_gate(setup):
    con, doc, graph = setup
    suite = yaml.safe_load((OSS / "fixtures" / "cqs" / "messy_mart.yaml").read_text())["cq_suite"]["questions"]
    try:
        schema = _run_condition(con, doc, graph, suite, "schema_only")
        semantic = _run_condition(con, doc, graph, suite, "semantic")
        onto = _run_condition(con, doc, graph, suite, "semantic+ontology")
    except CassetteMiss as e:
        pytest.skip(str(e))
    print(f"\n[ablation] schema={schema:.2f} semantic={semantic:.2f} +ontology={onto:.2f}")
    assert semantic >= schema + 0.05, "semantic layer must materially beat raw schema"
    assert onto >= semantic - 0.05, "ontology must be non-inferior"


def test_skeptic_rejects_seeded_bad_cqs(setup):
    _, doc, _ = setup
    try:
        verdicts = skeptic_verify(AnthropicProvider(model="claude-sonnet-5"), doc, SEEDED_BAD_CQS)
    except CassetteMiss as e:
        pytest.skip(str(e))
    rejected = sum(1 for v in verdicts if v["verdict"] == "reject")
    rate = rejected / len(verdicts)
    print(f"\n[skeptic] seeded-bad rejection {rejected}/{len(verdicts)} = {rate:.2f}")
    assert rate >= 0.8, [v for v in verdicts if v["verdict"] == "accept"]


def test_generated_cqs_pass_skeptic_mostly(setup):
    _, doc, _ = setup
    try:
        gen = generate(AnthropicProvider(), doc, n=12)
        assert len(gen) >= 8, "generator must produce questions"
        verdicts = skeptic_verify(AnthropicProvider(model="claude-sonnet-5"), doc, gen)
    except CassetteMiss as e:
        pytest.skip(str(e))
    accepted = [v for v in verdicts if v["verdict"] == "accept"]
    rate = len(accepted) / len(verdicts)
    print(f"\n[generator] skeptic acceptance {len(accepted)}/{len(verdicts)} = {rate:.2f}")
    assert rate >= 0.5, "most generated CQs should survive the skeptic"


def test_base_graph_derivation_and_paths(setup):
    _, doc, graph = setup
    o = graph["ontology"]
    assert o["derivation"].startswith("deterministic")
    assert all("derived_from" in n for n in o["nodes"])
    assert all("derived_from" in e for e in o["edges"])
    p = join_path(graph, "ord_ln", "cust_mstr")
    assert p and p[0] == "ord_ln" and p[-1] == "cust_mstr" and len(p) <= 4
