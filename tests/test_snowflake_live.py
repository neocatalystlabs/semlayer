"""Live Snowflake connector tests (nightly tier; auto-skip without credentials).

Proves the two things that matter (PRD §11):
1. The profiler works end-to-end under the MINIMAL-GRANT reader persona.
2. The reader persona genuinely cannot write (the grant script is honest).

Requires .env with SNOWFLAKE_* and the fan_trap fixture mirrored:
    .venv/bin/python oss/fixtures/mirror_snowflake.py fan_trap
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
    not os.environ.get("SNOWFLAKE_ACCOUNT") or not os.environ.get("SNOWFLAKE_READER_PASSWORD"),
    reason="Snowflake credentials not configured (.env)",
)


@pytest.fixture(scope="module")
def reader():
    from semlayer.connectors.snowflake import SnowflakeSource
    src = SnowflakeSource.from_env(role="reader")
    yield src
    src.close()


def test_reader_lists_fan_trap_tables(reader):
    tables = {t.name.lower() for t in reader.list_tables() if t.schema.lower() == "fan_trap"}
    if not tables:
        pytest.skip("fan_trap not mirrored yet (run mirror_snowflake.py fan_trap)")
    assert {"orders", "customers", "payments", "shipments"} <= tables


def test_reader_cannot_write(reader):
    """The minimal-grant story must be real: reader INSERT must fail."""
    import snowflake.connector.errors as sferr
    with pytest.raises((sferr.ProgrammingError, sferr.DatabaseError)):
        reader.query('INSERT INTO "FAN_TRAP"."CUSTOMERS" VALUES (99999, \'x\', \'north\')')


def test_profile_fan_trap_on_snowflake_meets_floor(reader):
    """End-to-end: profile the mirrored fixture under the reader persona and
    score against the same gold — the cross-warehouse consistency check."""
    from semlayer.profile import profile_source
    from semlayer.scoring import score_types
    from semlayer.validate import validate_document

    tables = [t for t in reader.list_tables() if t.schema.lower() == "fan_trap"]
    if not tables:
        pytest.skip("fan_trap not mirrored yet")

    class _Scoped:
        def list_tables(self):
            return tables
        def query(self, sql):
            return reader.query(sql)

    draft = profile_source(_Scoped())
    # normalize snowflake-uppercased names to match gold
    for t in draft["semantic_layer"]["tables"]:
        t["name"] = t["name"].lower()
        for c in t["columns"]:
            c["name"] = c["name"].lower()
        t["primary_key"] = [c.lower() for c in t.get("primary_key", [])]
    assert validate_document(draft).ok

    gold = yaml.safe_load((OSS / "fixtures" / "golds" / "fan_trap.yaml").read_text())
    s = score_types(draft, gold)
    print(f"\n[snowflake:fan_trap] type_acc={s.type_accuracy:.3f} role_acc={s.role_accuracy:.3f} pk_r={s.pk_recall:.3f}")
    assert s.type_accuracy >= 0.85   # parity floor vs DuckDB's 0.929
    assert s.pk_recall >= 0.99
