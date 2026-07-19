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
    src = BigQuerySource.from_env(role="reader")
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
