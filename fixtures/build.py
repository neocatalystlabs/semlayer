#!/usr/bin/env python3
"""One-command fixture builder.

Usage:
    python fixtures/build.py            # build all fixtures into fixtures/build/
    python fixtures/build.py fan_trap   # build one

Each fixture is a module in fixtures/generators/ exposing NAME and build(con),
where con is a DuckDB connection. Generation is seeded and deterministic:
building twice produces identical databases.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import duckdb

FIXTURES_DIR = Path(__file__).resolve().parent
BUILD_DIR = FIXTURES_DIR / "build"
GENERATORS = [
    "fan_trap",
    "tpcds_clean",
    "self_ref",
    "multi_tenant",
    "collision_heavy",
    "messy_mart",
    "obt",
    "eav",
    "snapshot_noval",
]  # extended as fixtures land

# Built on request, not by default: 2,000 tables each, ~55 s apiece, and they
# exist to be measured against rather than to guard a behaviour.
#   python fixtures/build.py wide_conventional wide_opaque
WIDE = {"wide_conventional": "build", "wide_opaque": "build_opaque"}


def build_fixture(name: str) -> Path:
    # The wide pair share one generator module and differ only in the entry
    # point, because they must differ ONLY in naming -- two modules would
    # drift and the comparison would stop being controlled.
    module, entry = (("wide", WIDE[name]) if name in WIDE else (name, "build"))
    sys.path.insert(0, str(FIXTURES_DIR))
    try:
        mod = importlib.import_module(f"generators.{module}")
    finally:
        sys.path.pop(0)
    BUILD_DIR.mkdir(exist_ok=True)
    out = BUILD_DIR / f"{name}.duckdb"
    if out.exists():
        out.unlink()
    con = duckdb.connect(str(out))
    try:
        getattr(mod, entry)(con)
    finally:
        con.close()
    return out


def main(argv: list[str]) -> int:
    names = argv or GENERATORS
    for name in names:
        out = build_fixture(name)
        print(f"built {name} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
