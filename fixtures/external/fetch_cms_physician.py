"""Medicare Physician & Other Practitioners: a real detail-plus-summary pair.

Track P. US Government Works, read from the CMS catalog's own per-distribution
`license` field (all 6,615 distributions carry it).

Why this dataset specifically. Our central claim is that the engine recovers
rules nobody wrote down, and the only mechanism that needs no human to
interview is reconciliation: a published SUMMARY compared against the DETAIL it
was built from, reporting the filter under which the two agree. That requires a
warehouse shipping both levels, and almost no open dataset does. Neither
tpcds_clean nor AdventureWorksDW has a summary table at all, so neither can
carry the claim however real they are.

CMS publishes both, from the same source, for the same providers and year:

  by Provider and Service  -- one row per provider x HCPCS code x place
  by Provider              -- one row per provider, with CMS's own totals

And they do NOT reconcile, for a documented reason that is invisible in the
schema: **service lines covering fewer than 11 beneficiaries are suppressed
from the detail file but are still counted in the provider totals.** That is a
privacy rule, written in a PDF data dictionary, which no amount of profiling
the column types would reveal -- exactly the shape of the cancelled-orders rule
in messy_mart, but in a real warehouse nobody at this company built.

Sampling is by WHOLE PROVIDER, never by row. Taking an arbitrary slice of the
detail would make the gap meaningless, because then the detail would be
incomplete for reasons of our own and the recovered rule would be an artefact
of our sampling. Every provider in the sample has all of its detail rows and
its summary row.

Note on content: this is real physicians' practice names and addresses, which
CMS publishes deliberately as public professional data. It is still PII-shaped,
and the engine's PII detection is a legitimate thing to exercise on it. We ship
the fetch script and never the data, as for every acquired dataset.

    python fixtures/external/fetch_cms_physician.py [--providers N] [--year YYYY]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _fetch import banner, require_duckdb, summarise

NAME = "cms_physician"
TRACK = "P"
LICENCE = "US Government Works (https://www.usa.gov/government-works)"
SOURCE = "https://data.cms.gov"

# Dataset UUIDs for the 2023 release, confirmed by title in data.cms.gov/data.json.
DATASETS = {
    2023: {
        "detail": "0e9f2f2b-7bf9-451a-912c-e02e654dd725",
        "summary": "8ba584c6-a43a-4b0b-a35a-eb9a59e3a571",
    },
}
API = "https://data.cms.gov/data-api/v1/dataset/{uuid}/data"
PAGE = 5000
DEFAULT_PROVIDERS = 400


def _get(url: str, attempts: int = 4) -> list[dict]:
    """One API page, with backoff. The endpoint rate-limits under load."""
    for i in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            if i == attempts - 1:
                raise SystemExit(f"giving up on {url}\n  {type(e).__name__}: {e}") from e
            time.sleep(2 ** i)
    return []


def _page_all(uuid: str, filters: dict | None = None, limit: int | None = None) -> list[dict]:
    """Walk the API until it runs out of rows or we hit `limit`."""
    rows: list[dict] = []
    offset = 0
    while True:
        q = {"size": PAGE, "offset": offset}
        q.update(filters or {})
        batch = _get(f"{API.format(uuid=uuid)}?{urllib.parse.urlencode(q)}")
        rows.extend(batch)
        if len(batch) < PAGE or (limit and len(rows) >= limit):
            break
        offset += PAGE
        print(f"    {len(rows):,} rows", end="\r", file=sys.stderr)
    return rows[:limit] if limit else rows


def pick_providers(summary: list[dict], n: int) -> list[str]:
    """A deterministic, spread-out provider sample.

    Hash-ordered rather than head-of-file, because the API returns NPIs in
    ascending order and the first N would all be from the same NPI block --
    one issuing region, one era of registration.
    """
    npis = sorted({r["Rndrng_NPI"] for r in summary})
    return sorted(npis, key=lambda x: hashlib.sha256(x.encode()).hexdigest())[:n]


def _type_numerics(con, src: str, dest: str) -> None:
    """Rebuild `src` as `dest`, casting to DOUBLE every column that fully casts.

    The API returns every value as a JSON string, including the amounts. CMS's
    own published CSVs are typed, so leaving them as VARCHAR would misrepresent
    the warehouse -- and it hid a real crash: `semantic_type` is inferred partly
    from the name, so a text column called `Tot_Mdcr_Pymt_Amt` was typed
    `monetary_value`, reconciliation emitted SUM() over it, and `infer` died.

    A column is numeric only if EVERY non-null value casts. Anything else stays
    text, so zip codes and NPIs keep their leading zeros and stay identifiers
    rather than becoming quantities.
    """
    cols = [(r[0], r[1]) for r in con.sql(
        f"SELECT column_name, data_type FROM information_schema.columns "
        f"WHERE table_name = '{src}' ORDER BY ordinal_position").fetchall()]
    keep_text = ("NPI", "Zip", "FIPS", "Cd", "_Cd", "Code")
    picked = []
    for name, dtype in cols:
        if "VARCHAR" not in dtype.upper() or any(k in name for k in keep_text):
            picked.append(f'"{name}"')
            continue
        bad = con.sql(f'SELECT count(*) FROM "{src}" WHERE "{name}" IS NOT NULL '
                      f'AND TRY_CAST("{name}" AS DOUBLE) IS NULL').fetchone()[0]
        nonnull = con.sql(f'SELECT count("{name}") FROM "{src}"').fetchone()[0]
        picked.append(f'CAST("{name}" AS DOUBLE) AS "{name}"'
                      if bad == 0 and nonnull > 0 else f'"{name}"')
    con.sql(f'CREATE TABLE "{dest}" AS SELECT {", ".join(picked)} FROM "{src}"')


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--providers", type=int, default=DEFAULT_PROVIDERS,
                   help=f"whole providers to sample (default {DEFAULT_PROVIDERS})")
    p.add_argument("--year", type=int, default=2023, choices=sorted(DATASETS))
    args = p.parse_args(argv)

    d = banner(NAME, TRACK, LICENCE, SOURCE)
    ids = DATASETS[args.year]

    # A summary page big enough to sample from; the full file is ~1M providers.
    print("  fetching provider summaries to sample from")
    pool = _page_all(ids["summary"], limit=max(args.providers * 25, 20000))
    npis = pick_providers(pool, args.providers)
    print(f"  sampled {len(npis)} providers from {len(pool):,} summary rows")

    summary = [r for r in pool if r["Rndrng_NPI"] in set(npis)]
    print(f"  {len(summary)} summary rows for the sample")

    print("  fetching every detail row for those providers "
          "(whole providers only, or the reconciliation is meaningless)")
    detail: list[dict] = []
    for i, npi in enumerate(npis, 1):
        detail.extend(_page_all(ids["detail"], {"filter[Rndrng_NPI]": npi}))
        if i % 25 == 0:
            print(f"    {i}/{len(npis)} providers, {len(detail):,} detail rows", file=sys.stderr)
    print(f"  {len(detail):,} detail rows")

    if not detail or not summary:
        print("  nothing fetched; the API shape may have changed", file=sys.stderr)
        return 1

    duckdb = require_duckdb()
    db = d / f"{NAME}.duckdb"
    if db.exists():
        db.unlink()
    con = duckdb.connect(str(db))
    try:
        for table, rows in (("mup_phy_prvdr", summary), ("mup_phy_prvdr_svc", detail)):
            path = d / f"{table}.json"
            path.write_text(json.dumps(rows))
            con.sql(f'CREATE TABLE "raw_{table}" AS '
                    f"SELECT * FROM read_json_auto('{path}', maximum_object_size=1000000000)")
            path.unlink()
            _type_numerics(con, f"raw_{table}", table)
            con.sql(f'DROP TABLE "raw_{table}"')
    finally:
        con.close()

    summarise(db)
    print(f"\n  track {TRACK}: numbers may be published, citing CMS.")
    print("  next: python fixtures/external/probe.py "
          f"fixtures/external/data/{NAME}/{NAME}.duckdb")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
