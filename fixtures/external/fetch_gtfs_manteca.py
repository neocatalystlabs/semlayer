"""A GTFS transit feed: a real schema with no ETL convention and no ambiguity.

Track P. CC BY 4.0, stated verbatim on the publisher's page:

    "The City of Manteca's GTFS data is licensed under the CC BY 4.0 license."
    https://www.manteca.gov/384/GTFS-Data

Why this dataset, and why now. `semlayer probe` grades a warehouse on the
evidence it offers and caps the confidence any inference may claim. It is
accurate on `dbt_synthea` -- predicted 0.78 against a measured 0.785, and 0.90
against 0.908 with a manifest -- and it is **wrong by 0.62 on
`adventureworks_dw`**, where it reports `weak` (cap 0.38) for a warehouse
whose foreign keys score 1.000.

The reason is a gap tier 1 cannot close. A warehouse with no stage prefixes
might have no convention AND duplicate key sets, which is genuinely hard, or
no convention and NO duplicates, which is easy. Tier 1 sees 0% prefixes and
assumes the worst. Tier 2 exists to separate those, by asking whether several
tables hold the same key values.

AdventureWorks cannot settle it, because its surrogate keys are dense integer
sequences: `DimProduct` numbered 1..606 and an unrelated dimension numbered
1..606 hash to the same value set while sharing nothing. Tier 2 reports 24%
"shared key sets" on a clean star schema, which is an artefact of counting
from one rather than a finding.

GTFS has the properties that make the question answerable:

  - snake_case throughout (`stop_id`, `trip_id`), so key detection works
  - no ETL stage prefixes, so tier 1 grades it `weak` as it does AdventureWorks
  - STRING identifiers, not integer sequences, so coincidental collision is
    not a confound
  - one copy of each entity, so the honest answer is "nothing to disambiguate"
  - a real foreign-key structure including role-playing: `transfers` carries
    `from_stop_id` and `to_stop_id` into the same `stops` table
  - vocabulary maximally distant from retail, which no fixture here has

So it tests exactly the cell where probe is known to be broken.

Size is the trade-off accepted deliberately. This is a small municipal
operator, so the feed is small, and the questions above are structural rather
than statistical -- they are about which tables share key values, not about
how many rows each holds. A larger agency would add volume and no new
evidence, and most of them wrap their feeds in bespoke licence agreements that
a human would have to read first.

The repo ships this recipe and never the data.

    python fixtures/external/fetch_gtfs_manteca.py
"""

from __future__ import annotations

import csv
import io
import pathlib
import sys
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _fetch import banner, download, require_duckdb, summarise

NAME = "gtfs_manteca"
LICENCE = "CC BY 4.0"
SOURCE = "https://www.manteca.gov/384/GTFS-Data"
URL = "https://s3.amazonaws.com/etatransit.gtfs/mantecatransit.etaspot.net/gtfs.zip"

# Every column in a GTFS file is text on the wire. Loading them as VARCHAR is
# not laziness: `route_id` "0012" is not the integer 12, and a loader that
# silently narrows it breaks the join it was meant to preserve. The engine's
# own typing stage is one of the things under test here, so the fixture must
# not pre-empt it.
SKIP = {"__MACOSX"}


def main() -> int:
    d = banner(NAME, "P", LICENCE, SOURCE)
    zip_path = download(URL, d / "gtfs.zip")

    duckdb = require_duckdb()
    db = d / f"{NAME}.duckdb"
    if db.exists():
        db.unlink()
    con = duckdb.connect(str(db))
    try:
        with zipfile.ZipFile(zip_path) as z:
            members = [n for n in z.namelist()
                       if n.endswith(".txt") and not any(s in n for s in SKIP)]
            if not members:
                raise SystemExit("no .txt members in the archive — not a GTFS feed?")
            for member in sorted(members):
                table = pathlib.Path(member).stem
                raw = z.read(member).decode("utf-8-sig", errors="replace")
                rows = list(csv.reader(io.StringIO(raw)))
                if len(rows) < 1:
                    print(f"  skip {table}: empty")
                    continue
                header = [h.strip() for h in rows[0] if h.strip()]
                if not header:
                    print(f"  skip {table}: no header")
                    continue
                cols = ", ".join(f'"{h}" VARCHAR' for h in header)
                con.execute(f'CREATE TABLE "{table}" ({cols})')
                body = [r[:len(header)] + [None] * (len(header) - len(r))
                        for r in rows[1:] if any(c.strip() for c in r)]
                if body:
                    placeholders = ", ".join("?" * len(header))
                    con.executemany(
                        f'INSERT INTO "{table}" VALUES ({placeholders})', body)
                print(f"  {table:24s} {len(header):3d} cols  {len(body):7d} rows")
    finally:
        con.close()

    summarise(db)
    print(f"\n  {db}")
    print("  record the sha256 printed above in fixtures/external/registry.yaml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
