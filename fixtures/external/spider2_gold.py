"""Derive Spider 2.0's foreign-key gold from its gold SQL, and survey its naming.

    python fixtures/external/spider2_gold.py            # survey + FK gold
    python fixtures/external/spider2_gold.py --pairs    # just the TSV

Nothing in this file is a judgement call about what a join means. Each of the
376 gold queries was written by a person against a real warehouse and confirmed
by an evaluator to return the right answer, so every `a.x = b.y` inside a join
is a join a practitioner believed in. That is not a declared constraint, but it
is the thing declared constraints are meant to approximate, and the thing Link
exists to find.

Two mechanical decisions do all the work, and both were learned by getting them
wrong first.

**Qualifiers must be resolved, not grepped.** A naive extraction yields 626
"pairs" that are mostly not foreign keys: `a.es_lat = b.do_lat` joins two CTEs
and `tab_a.fullvisitorid` is a subquery alias. Every qualifier is resolved
through its own query's FROM/JOIN aliases and any side landing on a CTE or
subquery is dropped. Of 221 resolvable occurrences, 73 were CTE-sided.

**Pairs are deduplicated per DATABASE, not per query.** The same join appears
in several queries, and one schema's role-playing columns would otherwise carry
the headline on their own: `EU_soccer.match` has 22 edges into `player`
(`home_player_1..11`, `away_player_1..11`). Per-pair and per-database rates
answer different questions, so both are printed.

RECALL ONLY. The queries cover the joins their questions needed, not every join
each schema has, so a pair absent from this corpus is NOT a negative and no
precision figure may be computed from it.
"""

from __future__ import annotations

import collections
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

ROOT = pathlib.Path(__file__).resolve().parent / "data" / "spider2" / "Spider2"
QUESTIONS = ("spider2-lite/spider2-lite.jsonl", "spider2-snow/spider2-snow.jsonl")
GOLD = (("spider2-lite/evaluation_suite/gold/sql", "bigquery"),
        ("spider2-snow/evaluation_suite/gold/sql", "snowflake"))
SCHEMAS = {
    "bigquery": "spider2-lite/resource/databases/bigquery",
    "snowflake(lite)": "spider2-lite/resource/databases/snowflake",
    "sqlite": "spider2-lite/resource/databases/sqlite",
    "snowflake(snow)": "spider2-snow/resource/databases",
}


def _require() -> None:
    if not ROOT.is_dir():
        raise SystemExit(
            f"{ROOT} is missing.\n  run: python fixtures/external/fetch_spider2.py")


def instance_to_db() -> dict[str, str]:
    """`lite` calls it `db`, `snow` calls it `db_id`. Accept both."""
    out = {}
    for rel in QUESTIONS:
        f = ROOT / rel
        if not f.is_file():
            continue
        for line in f.read_text().splitlines():
            if line.strip():
                d = json.loads(line)
                out[d["instance_id"]] = d.get("db") or d["db_id"]
    return out


def fk_pairs() -> dict[str, set[tuple[str, str]]]:
    """{database: {(child_or_parent, the_other)}} -- orientation unknown."""
    import sqlglot
    from sqlglot import exp

    _require()
    inst2db = instance_to_db()
    by_db: dict[str, set] = collections.defaultdict(set)
    for rel, dialect in GOLD:
        base = ROOT / rel
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*.sql")):
            db = inst2db.get(f.stem)
            if db is None:
                continue
            try:
                trees = sqlglot.parse(f.read_text(errors="replace"), dialect=dialect)
            except Exception:
                continue
            for tree in trees:
                if tree is None:
                    continue
                ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
                amap: dict[str, str] = {}
                for tb in tree.find_all(exp.Table):
                    nm = tb.name.lower()
                    if nm in ctes:
                        continue
                    for k in {tb.alias_or_name.lower(), nm}:
                        if k:
                            amap[k] = nm
                for eq in tree.find_all(exp.EQ):
                    lhs, rhs = eq.this, eq.expression
                    if not (isinstance(lhs, exp.Column)
                            and isinstance(rhs, exp.Column)):
                        continue
                    lt, rt = lhs.table.lower(), rhs.table.lower()
                    if not lt or not rt or lt in ctes or rt in ctes:
                        continue
                    if lt not in amap or rt not in amap or amap[lt] == amap[rt]:
                        continue
                    a, b = sorted([f"{amap[lt]}.{lhs.name.lower()}",
                                   f"{amap[rt]}.{rhs.name.lower()}"])
                    by_db[db].add((a, b))
    return dict(by_db)


def survey_naming() -> list[tuple]:
    """probe's tier-1 reading of every schema the corpus ships."""
    from semlayer.lineage import naming_layer
    from semlayer.probe import _KEYISH

    _require()
    rows = []
    for src, rel in SCHEMAS.items():
        base = ROOT / rel
        if not base.is_dir():
            continue
        for db in sorted(p for p in base.iterdir() if p.is_dir()):
            tables, cols, described, nested = [], [], 0, False
            for f in db.rglob("*.json"):
                try:
                    d = json.loads(f.read_text(errors="replace"))
                except Exception:
                    continue
                cn = d.get("column_names")
                if cn is None:
                    continue
                tables.append(str(d.get("table_name") or f.stem))
                cols += [str(c) for c in cn]
                ds = d.get("description") or []
                # `column_names` lists top-level columns; `description` covers
                # the FLATTENED tree, so on a nested BigQuery schema it is the
                # longer list. Dividing one by the other reported 1664% of
                # columns described on `ga360`, which is how this was noticed.
                if len(ds) > len(cn):
                    nested = True
                described += sum(1 for x in ds if x)
            if not tables:
                continue
            rows.append((
                src, db.name, len(tables), len(cols),
                sum(1 for t in tables if naming_layer(t)) / len(tables),
                sum(1 for c in cols if _KEYISH.search(c)) / max(1, len(cols)),
                None if nested else described / max(1, len(cols)),
            ))
    return rows


def _pct(xs, q: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(len(s) * q))] if s else 0.0


def main(argv: list[str]) -> int:
    pairs = fk_pairs()
    flat = sorted((db, a, b) for db, prs in pairs.items() for a, b in prs)
    if "--pairs" in argv:
        print("\n".join(f"{db}\t{a}\t{b}" for db, a, b in flat))
        return 0

    rows = survey_naming()
    tot = len(rows)
    prefixed = [r for r in rows if r[4] > 0]
    print(f"SCHEMA SURVEY  ({tot} databases, "
          f"{sum(r[2] for r in rows):,} tables, {sum(r[3] for r in rows):,} columns)")
    print(f"  graded weak by tier 1 (no stage convention): "
          f"{tot - sum(1 for r in rows if r[4] >= 0.25)}/{tot}")
    print(f"  carry ANY ETL stage prefix                 : {len(prefixed)}/{tot}")
    for r in prefixed:
        print(f"      {r[0]:16s} {r[1]:30s} {r[2]:4d} tables  {r[4]:5.1%}")
    flatsch = [r[6] for r in rows if r[6] is not None]
    print(f"  nested RECORD schemas                      : "
          f"{tot - len(flatsch)}/{tot}")
    print(f"  described-column share (flat schemas)      : "
          f"median {_pct(flatsch, .5):.0%}  p90 {_pct(flatsch, .9):.0%}")
    print(f"  flat schemas with ZERO descriptions        : "
          f"{sum(1 for x in flatsch if x == 0)}/{len(flatsch)}")

    from semlayer.link.candidates import name_similarity
    from semlayer.link.run import AUTO_NAME_THRESHOLD

    def best(a: str, b: str) -> float:
        """Orientation is unknown, so score both and keep the better. That is
        generous to us: Link only ever gets one of the two."""
        at, ac = a.split(".", 1)
        bt, bc = b.split(".", 1)
        return max(name_similarity(ac, at, bc, bt), name_similarity(bc, bt, ac, at))

    n = len(flat)
    scored = [(best(a, b), a, b, a.split(".", 1)[1] == b.split(".", 1)[1])
              for _, a, b in flat]
    below = [s for s in scored if s[0] < AUTO_NAME_THRESHOLD]
    cross = [s for s in scored if not s[3]]
    print(f"\nFK GOLD  ({n} distinct pairs over {len(pairs)} databases)")
    print(f"  same column name both sides : {n - len(cross)}")
    print(f"  cross-name                  : {len(cross)}")
    print(f"\nNAME SIGNAL vs AUTO_NAME_THRESHOLD ({AUTO_NAME_THRESHOLD})")
    print(f"  per pair, below threshold   : {len(below)}/{n} ({len(below)/n:.0%})")
    print(f"  per pair, zero signal       : "
          f"{sum(1 for s in scored if s[0] == 0)}/{n}")
    rates = sorted(
        ((sum(1 for a, b in prs if best(a, b) < AUTO_NAME_THRESHOLD) / len(prs),
          db, len(prs)) for db, prs in pairs.items()), reverse=True)
    m = len(rates)
    print(f"  per database, mean share    : {sum(r[0] for r in rates)/m:.0%}"
          f"   median {_pct([r[0] for r in rates], .5):.0%}")
    print(f"  databases missing >=1 FK    : "
          f"{sum(1 for r in rates if r[0] > 0)}/{m}")
    print("\n  worst schemas:")
    for r, db, k in rates[:8]:
        if r > 0:
            print(f"      {r:5.0%} of {k:3d} FKs below threshold   {db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
