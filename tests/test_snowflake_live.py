"""Live Snowflake connector tests (nightly tier; auto-skip without credentials).

Proves the two things that matter:
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

# The role is NOT part of this guard any more. It used to be: the connector
# refused to connect without an explicit session role, so an unset role raised
# rather than skipped and turned three tests into ERRORs in every suite run.
# #78 made the role optional -- the session is asked what it is actually
# running as, instead of being told -- so gating on the role now skips the one
# tier that could exercise that code for want of a variable the product no
# longer wants. A run with no role set is a case worth covering, not a reason
# to cover nothing.
# The credential may be a key pair rather than a password, and after the init
# template change it usually will be: `init` now creates a SERVICE user, which
# cannot hold a password at all. Gating on READER_PASSWORD alone would skip
# this whole tier for anyone who set their account up the way the quickstart
# now tells them to.
pytestmark = pytest.mark.skipif(
    not os.environ.get("SNOWFLAKE_ACCOUNT")
    or not (os.environ.get("SNOWFLAKE_READER_PASSWORD")
            or os.environ.get("SNOWFLAKE_READER_PRIVATE_KEY_PATH")
            or os.environ.get("SNOWFLAKE_READER_AUTHENTICATOR")),
    reason="Snowflake credentials not configured (.env: ACCOUNT, plus a "
           "READER_PASSWORD, READER_PRIVATE_KEY_PATH or READER_AUTHENTICATOR)",
)


@pytest.fixture(scope="module")
def reader():
    """The reader persona, or a SKIP if the account cannot be reached.

    Credentials being PRESENT is not the same as the account being usable: a
    Snowflake trial that has expired suspends every warehouse, and the
    connection then fails with "Your free trial has ended". That turned three
    tests into ERRORs in every suite run, which reads as a regression and is
    not one -- the tests are unrunnable, not failing.

    The discriminator used to be `isinstance(e, SemlayerError)`: a raw driver
    exception meant unreachable, one of ours meant misconfigured. #78 wrapped
    `driver.connect` so that a refusal records as `connection` rather than as
    our bug -- and in doing so made the refusal a SourceError, i.e. one of
    ours, so this skip became unreachable and the expired trial went back to
    erroring. The fix is to discriminate on what SourceError now MEANS: the
    source refused us. A problem in our own arguments is a SpecError or a bare
    SemlayerError, and those still fail loudly.

    The skip reason carries the driver's own hint rather than a summary.
    A tier that skips without saying why is how a tier stays skipped.
    """
    from semlayer.connectors.snowflake import SnowflakeSource
    from semlayer.errors import SourceError
    try:
        src = SnowflakeSource.from_env(persona="reader")
    except SourceError as e:
        detail = (getattr(e, "hint", "") or str(e)).strip()
        pytest.skip(f"Snowflake not reachable: {e} -- {detail[:200]}")
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
