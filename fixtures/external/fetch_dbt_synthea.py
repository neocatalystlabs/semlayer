"""OHDSI dbt-synthea: the first fixture where the ETL that built it is readable.

Track P. Apache-2.0, read from the repository's own LICENSE file.

Why this dataset specifically. Every other fixture we have is a warehouse with
no authoring history -- we see the tables and must infer backwards. This one
ships the dbt project that BUILDS the tables, so `target/manifest.json` carries
the compiled SQL, the model DAG, and 319 human-declared foreign keys. That is
the input the ETL-reading stage needs, and nothing else we hold has it:

  fixtures (9)              0 views, no ETL source      178 tables
  adventureworks_dw         install script, no models    29 tables
  cms_physician             published extracts            2 tables

What it can validate that nothing else can:

  319 declared foreign keys   194 `relationships` + 125 `relationships_where`.
                              The conditional kind -- an FK that holds only
                              under a filter -- appears in no fixture we wrote.
  421 declared column types   `expect_column_values_to_be_in_type_list`, a
                              free answer key for the typing stage.
  127 join equalities         parsed from the compiled SQL, independent of the
                              declared tests, so the two can corroborate.

Honest limits, so nobody mistakes this for a general-purpose fixture:

  - It is SMALL: 27 patients, 604 visits, 59 conditions. It exercises reading
    the ETL; it is a poor test of statistical inference, and cross-checking an
    ETL-derived claim against observed data is thin on 59 rows.
  - The FK topology is CONCENTRATED: 319 edges but 18 distinct parents, and
    215 of them point at `concept`. Hub-and-spoke, not a diverse star.
  - Declared FKs are abundant here because OMOP CDM is a PUBLISHED data model.
    Corporate warehouses measured alongside this one have almost none (Cal-ITP
    19 enabled across 624 models; Tuva Health 0 across 353). So this dataset
    validates the declared-constraint path, NOT the common enterprise case
    where joins must be parsed out of the SQL.
  - Domain overlaps cms_physician at the word level, but OMOP is clinical
    research rather than billing, and the self-referencing `concept` graph is a
    shape neither of them has.

Reproducibility. The upstream repo is pinned to a COMMIT, and git is
content-addressed, so verifying the commit hash is a stronger integrity check
than a checksum over a tarball (GitHub's generated tarballs are not
byte-stable). dbt package versions are pinned by writing our own
package-lock.yml: upstream's packages.yml leaves dbt_date as a RANGE
(">=0.13.0", "<0.14.0"), which would let a fixture drift under us.

    python fixtures/external/fetch_dbt_synthea.py [--force]

Requires dbt-core and dbt-duckdb, which are NOT semlayer dependencies:

    pip install dbt-core dbt-duckdb
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _fetch import banner, summarise

NAME = "dbt_synthea"
REPO = "https://github.com/OHDSI/dbt-synthea.git"
# Pinned commit = the integrity anchor. Re-date licence_verified in
# registry.yaml if you ever move this.
COMMIT = "33fdbfa9b354f00def5ee6e45525cb2da80e0ddf"
LICENCE = "Apache-2.0"
SOURCE = "https://github.com/OHDSI/dbt-synthea"
PROFILE = "synthea_omop_etl"

# Upstream leaves dbt_date as a range; pin all three so the fixture cannot drift.
PACKAGE_LOCK = """packages:
  - package: dbt-labs/dbt_utils
    version: 1.3.0
  - package: metaplane/dbt_expectations
    version: 0.10.8
  - package: godatadriven/dbt_date
    version: 0.13.0
sha1_hash: ca340920b0b988893f453cb1db79e69529200e03
"""


def _profiles(db: pathlib.Path) -> str:
    return f"""{PROFILE}:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: '{db}'
      threads: 8
"""


def _dbt(args: list[str], repo: pathlib.Path, profiles_dir: pathlib.Path) -> None:
    """Run dbt against the checkout.

    Our configuration lives OUTSIDE the checkout and is passed in by path:
    downloaded trees are untrusted, so we do not write into them beyond the
    lock file dbt itself requires. This checkout was inspected before being
    added here -- it declares no hooks, no on-run operations and no Python
    models, so dbt executes only its own code plus Jinja in the model files.
    """
    cmd = ["dbt", *args, "--project-dir", str(repo), "--profiles-dir", str(profiles_dir)]
    print("  $ " + " ".join(cmd[:3]) + " ...")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-15:])
        raise SystemExit(f"dbt {args[0]} failed:\n{tail}")


def fetch(force: bool = False) -> pathlib.Path:
    d = banner(NAME, "P", LICENCE, SOURCE)
    db = d / f"{NAME}.duckdb"
    manifest = d / "manifest.json"

    if db.exists() and manifest.exists() and not force:
        print(f"  have {db.name} and {manifest.name}")
        summarise(db)
        return db

    if shutil.which("dbt") is None:
        raise SystemExit(
            "dbt not found. This fixture is built by its own ETL, so it needs:\n"
            "    pip install dbt-core dbt-duckdb"
        )

    repo = d / "repo"
    if not (repo / ".git").exists():
        print(f"  cloning {REPO}")
        subprocess.run(["git", "clone", "--quiet", REPO, str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "fetch", "--quiet", "origin", COMMIT], check=False)
    subprocess.run(["git", "-C", str(repo), "checkout", "--quiet", COMMIT], check=True)

    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    if head != COMMIT:
        raise SystemExit(f"integrity check failed: HEAD is {head}, expected {COMMIT}")
    print(f"  commit {head[:12]} verified")

    (repo / "package-lock.yml").write_text(PACKAGE_LOCK)
    (d / "profiles.yml").write_text(_profiles(db))

    db.unlink(missing_ok=True)
    _dbt(["deps"], repo, d)
    # SEEDS MUST RUN FIRST, AND THIS IS NOT COSMETIC. The vocabulary staging
    # models read source() rather than ref(), so dbt has no dependency edge
    # from them to the seeds. With threads > 1 the model is compiled before the
    # seed lands, dbt_utils column introspection returns NOTHING, and the
    # compiled SQL becomes `SELECT` with an empty select list. A single
    # `dbt build` yields 29 errors and 1,412 skipped nodes -- and it fails
    # loudly, which is the only good thing about it.
    _dbt(["seed"], repo, d)
    _dbt(["build"], repo, d)

    shutil.copy2(repo / "target" / "manifest.json", manifest)
    print(f"  wrote {manifest.name} (the point of this fixture: the authoring ETL)")
    summarise(db)
    return db


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true", help="rebuild even if present")
    fetch(**vars(ap.parse_args()))
