"""Spider 2.0: ~300 real schemas and 376 human-written gold SQL queries.

Track P. MIT, stated in the repository's own LICENSE file:

    https://github.com/xlang-ai/Spider2/blob/main/LICENSE

What this buys that nothing else here does, and what it cannot buy.

Every other dataset in this registry is one warehouse. This is roughly three
hundred, in medicine, genomics, finance, sport, patents and government, and it
is the only corpus here large enough to answer "what do real schemas actually
look like" rather than "what does this one look like".

The second half is worth more than the first. The repository ships 376 gold
SQL queries, each written by a person against a real warehouse and confirmed
by an evaluator to return the right answer. Every `a.x = b.y` inside a join is
therefore a join a practitioner believed in -- not a declared constraint, but
the thing declared constraints are meant to approximate and the thing Link
exists to find. Nothing else here supplies foreign-key evidence on real naming
at this scale.

Three limits, all of which have to be stated wherever a number from this is
quoted:

1. **No data.** Each table ships three sample rows. Inclusion testing cannot
   run, so Link cannot be scored end-to-end here. What IS measurable is
   everything that reads names only -- which is also what ranks the 40 parents
   a child gets tested against, so a true parent the names rank 41st is never
   queried whatever the data would have said.

2. **Recall only, never precision.** The queries cover the joins their
   questions needed, not every join each schema has. A pair absent from the
   corpus is not a negative, so this corpus can show a miss and can never show
   a false positive.

3. **The join predicates need resolving, not grepping.** A naive extraction is
   mostly wrong: `a.es_lat = b.do_lat` joins two CTEs and `tab_a.fullvisitorid`
   is a subquery alias, and neither is a foreign key. Qualifiers must be
   resolved through each query's own FROM/JOIN aliases with CTE and subquery
   sides dropped. Of 221 resolvable predicate occurrences, 73 were CTE-sided.
   Counting those measures how people write SQL, not how schemas are shaped.

The repo ships this recipe and never the data. The checkout is ~1.3 GB of
schema JSON, which is why it is sparse: the methods, baselines and dbt
directories are not fetched.

    python fixtures/external/fetch_spider2.py
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _fetch import banner

NAME = "spider2"
LICENCE = "MIT"
SOURCE = "https://github.com/xlang-ai/Spider2"
URL = "https://github.com/xlang-ai/Spider2.git"

# Pinned, because every number derived from this corpus is a number about a
# specific set of schemas. The corpus is still being edited upstream -- the
# `snow` question file exists in two dated versions -- so a floating clone
# would silently change what a published figure refers to.
COMMIT = "cafb867313aab4e674652054198f383cf4018943"

# Schema JSON, the question files, the gold SQL, the licence. Not `methods`
# (baseline agent implementations), not `baselines`, not `spider2-dbt`.
SPARSE = [
    "spider2-lite/resource/databases",
    "spider2-snow/resource/databases",
    "spider2-lite/evaluation_suite/gold/sql",
    "spider2-snow/evaluation_suite/gold/sql",
    "spider2-lite/spider2-lite.jsonl",
    "spider2-snow/spider2-snow.jsonl",
    "LICENSE",
]


def _git(d: pathlib.Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=d, check=True,
                   stdout=subprocess.DEVNULL)


def main() -> int:
    d = banner(NAME, "P", LICENCE, SOURCE)
    repo = d / "Spider2"
    if (repo / ".git").is_dir():
        print(f"  have {repo.name}")
    else:
        repo.mkdir(parents=True, exist_ok=True)
        print(f"  cloning {URL} (blobless, sparse)")
        _git(repo, "init", "-q")
        _git(repo, "remote", "add", "origin", URL)
        # --filter=blob:none fetches no file contents until checkout, and
        # --depth 1 no history, which is what keeps a 165 MB repository with
        # 1.3 GB of checkout from costing both.
        _git(repo, "fetch", "--depth", "1", "--filter=blob:none", "origin", COMMIT)
        _git(repo, "sparse-checkout", "init", "--cone")
        # --skip-checks: the list names files as well as directories, which
        # cone mode rejects by default.
        _git(repo, "sparse-checkout", "set", "--skip-checks", *SPARSE)
        _git(repo, "checkout", "-q", COMMIT)

    head = subprocess.run(("git", "rev-parse", "HEAD"), cwd=repo,
                          check=True, capture_output=True, text=True).stdout.strip()
    if head != COMMIT:
        raise SystemExit(f"expected {COMMIT}, checked out {head}")

    dbs = sum(1 for base in ("spider2-lite/resource/databases/bigquery",
                             "spider2-lite/resource/databases/snowflake",
                             "spider2-lite/resource/databases/sqlite",
                             "spider2-snow/resource/databases")
              for _ in (repo / base).iterdir() if (repo / base).is_dir())
    gold = sum(1 for p in repo.rglob("evaluation_suite/gold/sql/**/*.sql"))
    print(f"\n  commit {head}")
    print(f"  schema databases : {dbs}")
    print(f"  gold SQL queries : {gold}")
    print(f"\n  {repo}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
