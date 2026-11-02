"""Live BigQuery connector tests (nightly tier; auto-skip without credentials).

Same proofs as the Snowflake tier: profiler end-to-end under the minimal-grant
reader; reader cannot write; score parity vs the DuckDB baseline.
"""

import os
from pathlib import Path

import pytest
import yaml

OSS = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
    load_dotenv(OSS.parent / ".env")
except ImportError:
    pass

pytestmark = pytest.mark.skipif(
    not os.environ.get("GCP_PROJECT") or not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_READER"),
    reason="BigQuery credentials not configured (.env)",
)


@pytest.fixture(scope="module")
def reader():
    from semlayer.connectors.bigquery import BigQuerySource
    src = BigQuerySource.from_env(persona="reader")
    yield src
    src.close()


def test_reader_lists_fan_trap_tables(reader):
    tables = {t.name for t in reader.list_tables() if t.schema == "fan_trap"}
    if not tables:
        pytest.skip("fan_trap not mirrored yet (run mirror_bigquery.py fan_trap)")
    assert {"orders", "customers", "payments", "shipments"} <= tables


def test_reader_cannot_write(reader):
    from google.api_core.exceptions import Forbidden
    with pytest.raises(Forbidden):
        reader.query(
            f"INSERT INTO `{reader.project}.fan_trap.customers` VALUES (99999, 'x', 'north')"
        )


def test_profile_fan_trap_on_bigquery_meets_floor(reader):
    from semlayer.profile import profile_source
    from semlayer.scoring import score_types
    from semlayer.validate import validate_document

    tables = [t for t in reader.list_tables() if t.schema == "fan_trap"]
    if not tables:
        pytest.skip("fan_trap not mirrored yet")

    class _Scoped:
        def list_tables(self):
            return tables
        def __getattr__(self, name):  # delegate query/qualify/quote_ident
            return getattr(reader, name)

    draft = profile_source(_Scoped())
    assert validate_document(draft).ok

    gold = yaml.safe_load((OSS / "fixtures" / "golds" / "fan_trap.yaml").read_text())
    s = score_types(draft, gold)
    print(f"\n[bigquery:fan_trap] type_acc={s.type_accuracy:.3f} role_acc={s.role_accuracy:.3f} pk_r={s.pk_recall:.3f}")
    assert s.type_accuracy >= 0.85
    assert s.pk_recall >= 0.99


def test_the_whole_deterministic_pipeline_runs_on_bigquery(reader):
    """PROFILE then LINK then ENRICH, which is what the CLI actually does.

    This is the test whose absence let `semlayer infer` ship broken against
    BigQuery for as long as the connector has existed. Every other test in
    this file stops after PROFILE -- and PROFILE was the one stage that
    already quoted identifiers through the source, so it was the one stage
    that worked. LINK, ENRICH and probe built their SQL with literal double
    quotes, which on BigQuery delimit a STRING rather than an identifier, so
    `sum("amount")` asked BigQuery to total the text "amount":

        No matching signature for aggregate function SUM
          Argument 1: Unable to coerce type STRING to expected type FLOAT64

    No stub can catch that. DuckDB and Postgres accept the broken form
    because double quotes really are their identifier quote, so the only
    thing that fails is a real BigQuery, and the only thing that reaches a
    real BigQuery is this file.

    Deterministic (`llm=None`), so it costs nothing beyond the queries and
    can run on every nightly. It asserts the pipeline COMPLETES and the
    document validates; the accuracy floors stay in the test above.
    """
    from semlayer.pipeline import InferOptions, infer_with_report
    from semlayer.validate import validate_document

    tables = [t for t in reader.list_tables() if t.schema == "fan_trap"]
    if not tables:
        pytest.skip("fan_trap not mirrored yet")

    doc, report = infer_with_report(
        reader, llm=None, options=InferOptions(schema={"fan_trap"}))

    assert validate_document(doc).ok
    assert set(report["stages"]) == {"profile", "link", "enrich"}, (
        f"a stage did not run: {sorted(report['stages'])}")
    assert report["counts"]["tables"] == 4

    sl = doc["semantic_layer"]
    orders = next(t for t in sl["tables"] if t["name"].endswith("orders"))
    cid = next(c for c in orders["columns"] if c["name"] == "customer_id")
    assert cid.get("foreign_key"), "orders.customer_id should resolve to customers"

    # The enum decode reads rows out of the parent table under a row cap, so
    # its presence means a LINK result AND an ENRICH query that succeeded --
    # and that query is one of the ones that used to be unrunnable here.
    #
    # NOT asserting the ORDER, though it should be assertable: the decode
    # query has no ORDER BY, so BigQuery returns these in whatever sequence
    # it likes and DuckDB returns insertion order. Fixing that changes the
    # layer, which changes the agent-pipeline prompts, which needs a paid
    # cassette re-record. See HANDOFF.
    assert cid.get("enum_values"), "the capped decode query should have run"
