"""BIRD dev: 11 real SQLite databases with a human-written data dictionary.

Track I. CC BY-SA 4.0 -- share-alike.

READ THIS BEFORE QUOTING ANY NUMBER FROM THIS DATASET.

Share-alike means anything derived from it carries the same licence. A gold
file we hand-author for these databases is plausibly derivative, which would
put a CC BY-SA island inside an Apache-2.0 repo and could pass an obligation
to users of our output that they never asked for. So this dataset is
INTERNAL ONLY: run the engine against it, learn from it, and publish nothing
derived from it -- not in docs/, not in the README, not in a benchmark table,
not in a blog post, not with a caveat.

Why acquire it anyway: it is the only corpus we know of that ships
human-written column documentation for real, dirty schemas. Each database has
a `database_description/` directory, one CSV per table, with
original_column_name, column_name, column_description, data_format and
value_description -- including decoded value meanings. That is gold for the
describe stage, authored by someone other than us, and we have never scored
against any such thing.

Its schemas are small: 11 databases, 75 tables, 6.8 tables per database,
max 13. It is a description-and-SQL corpus, not a warehouse corpus, and should
not be treated as a replacement for a warehouse fixture.

    python fixtures/external/fetch_bird_dev.py
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _fetch import banner, download, extract

NAME = "bird_dev"
TRACK = "I"
LICENCE = "CC BY-SA 4.0 (share-alike — internal use only)"
SOURCE = "https://bird-bench.github.io/"
ARCHIVE = "https://bird-bench.oss-cn-beijing.aliyuncs.com/dev.zip"


def main() -> int:
    d = banner(NAME, TRACK, LICENCE, SOURCE)
    archive = download(ARCHIVE, d / "dev.zip")
    raw = extract(archive, d / "raw")

    # the outer zip nests the databases in a second archive
    for inner in raw.rglob("dev_databases.zip"):
        extract(inner, inner.parent / "dev_databases")

    dbs = sorted(p for p in raw.rglob("*.sqlite"))
    dicts = sorted(p for p in raw.rglob("database_description/*.csv"))
    questions = next(raw.rglob("dev.json"), None)
    n_q = len(json.loads(questions.read_text())) if questions else 0

    print(f"\n  {len(dbs)} SQLite databases")
    print(f"  {len(dicts)} column-description CSVs (the describe-stage answer key)")
    print(f"  {n_q} questions with gold SQL")
    print("\n  TRACK I. Run the engine against these; publish nothing derived "
          "from them.\n  Hand-authored gold for these belongs in "
          "fixtures/golds/internal/, which is gitignored.")
    return 0 if dbs else 1


if __name__ == "__main__":
    raise SystemExit(main())
