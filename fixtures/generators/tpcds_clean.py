"""tpcds_clean fixture: TPC-DS via DuckDB's tpcds extension.

24 tables (7 fact, 17 dimension) with clean, nameable keys — the flattering
baseline. Default scale factor is small for CI speed; nightly runs use SF1
(env TPCDS_SF=1). Never quote clean-fixture numbers as headline results.
"""

from __future__ import annotations

import os

DEFAULT_SF = 0.01  # ~10MB, seconds to generate; CI-friendly


def build(con) -> None:
    sf = float(os.environ.get("TPCDS_SF", DEFAULT_SF))
    con.execute("INSTALL tpcds; LOAD tpcds;")
    con.execute(f"CALL dsdgen(sf={sf})")


NAME = "tpcds_clean"
