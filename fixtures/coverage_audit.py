"""Audit which features have data behind them, and which only have unit tests.

Run from the repo root:

    python fixtures/coverage_audit.py

Reads the gold files and the built databases -- never a design document.
Coverage claims rot: three of the four gap claims in the 2026-10-03 fixture
proposal were false when checked against this, so the audit is a script.

Three columns that are easy to conflate and must stay separate:

- IN DATA        the column or shape physically exists in the warehouse
- IN GOLD        a human asserted it, so the engine can be SCORED on it
- EMITTED        the engine actually produces it

"in data but not in gold" is an annotation job. "in gold but not emitted" is a
detector waiting to be written, already scoreable. Only "in neither" argues for
acquiring or synthesising anything.
"""

from __future__ import annotations

import collections
import json
import pathlib
import sys

import duckdb
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
GOLDS = ROOT / "fixtures" / "golds"
BUILD = ROOT / "fixtures" / "build"
sys.path.insert(0, str(ROOT / "src"))

from semlayer.profile.stats import SAMPLE_THRESHOLD  # noqa: E402


def gold(name: str) -> dict:
    return yaml.safe_load((GOLDS / f"{name}.yaml").read_text())["semantic_layer"]


def role_playing(doc: dict) -> int:
    """Count (fact, dim) pairs joined more than once -- the role-playing shape.

    Relationships are a TOP-LEVEL list. Scanning columns for a `foreign_key`
    field finds nothing and reads as a total gap; it is not one.
    """
    pairs = collections.Counter(
        (r.get("from", {}).get("table"), r.get("to", {}).get("table"))
        for r in doc.get("relationships") or []
    )
    return sum(1 for n in pairs.values() if n > 1)


def fiscal_in_gold(doc: dict) -> bool:
    """A calendar block carrying only timezone/week_start asserts no fiscal year."""
    cal = doc.get("calendar") or {}
    return any(k not in ("timezone", "week_start") for k in cal)


# Not every fixture lives in `main`: multi_tenant has 20 tenant_NNN schemas and
# no main at all, so hardcoding the schema silently reported it as empty.
_USER_SCHEMAS = ("information_schema", "pg_catalog", "main_temp", "temp", "system")


def _tables(con) -> list[tuple[str, str]]:
    """Every (schema, table) a fixture actually defines, whatever it calls them."""
    rows = con.sql(
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_type='BASE TABLE' ORDER BY 1, 2").fetchall()
    return [(s, t) for s, t in rows if s not in _USER_SCHEMAS]


def fiscal_in_data(db: pathlib.Path) -> bool:
    """Any column whose name suggests a fiscal period distinct from Gregorian."""
    if not db.exists():
        return False
    con = duckdb.connect(str(db), read_only=True)
    try:
        cols = [r[0].lower() for r in con.sql(
            "SELECT column_name FROM information_schema.columns").fetchall()]
    finally:
        con.close()
    return any("_fy_" in c or c.startswith("fy_") or "fiscal" in c for c in cols)


def row_stats(db: pathlib.Path) -> tuple[int, int]:
    """Total rows, and how many tables sit above the full-scan threshold."""
    if not db.exists():
        return 0, 0
    con = duckdb.connect(str(db), read_only=True)
    try:
        counts = [con.sql(f'SELECT count(*) FROM "{s}"."{t}"').fetchone()[0]
                  for s, t in _tables(con)]
    finally:
        con.close()
    return sum(counts), sum(1 for n in counts if n >= SAMPLE_THRESHOLD)


def audit() -> dict:
    per_fixture = {}
    for path in sorted(GOLDS.glob("*.yaml")):
        name = path.stem
        doc = gold(name)
        db = BUILD / f"{name}.duckdb"
        rows, big = row_stats(db)
        hier = collections.Counter(h.get("kind") for h in doc.get("hierarchies") or [])
        per_fixture[name] = {
            "tables": len(doc.get("tables") or []),
            "columns": sum(len(t.get("columns") or []) for t in doc.get("tables") or []),
            "rows": rows,
            "tables_over_threshold": big,
            "composite_pk": sum(1 for t in doc.get("tables") or []
                                if len(t.get("primary_key") or []) > 1),
            "role_playing_pairs": role_playing(doc),
            "hierarchies": dict(hier),
            "many_to_many": sum(1 for r in doc.get("relationships") or []
                                if r.get("cardinality") == "many_to_many"),
            "metrics": len(doc.get("metrics") or []),
            "aggregate_tables": len(doc.get("aggregate_tables") or []),
            "fiscal_in_gold": fiscal_in_gold(doc),
            "fiscal_in_data": fiscal_in_data(db),
            "built": db.exists(),
        }
    return per_fixture


def report(per_fixture: dict) -> None:
    print(f"corpus: {len(per_fixture)} fixtures, "
          f"{sum(f['columns'] for f in per_fixture.values())} gold columns, "
          f"{sum(f['tables'] for f in per_fixture.values())} tables")
    print(f"full-scan threshold: {SAMPLE_THRESHOLD:,} rows\n")

    head = f"{'fixture':17}{'tbl':>5}{'cols':>6}{'rows':>12}{'>1M':>5}{'cPK':>5}{'role':>6}{'m2m':>5}{'lvl':>5}{'fiscal':>8}"
    print(head)
    print("-" * len(head))
    for name, f in per_fixture.items():
        fiscal = ("gold" if f["fiscal_in_gold"] else
                  "DATA" if f["fiscal_in_data"] else "-")
        print(f"{name:17}{f['tables']:>5}{f['columns']:>6}{f['rows']:>12,}"
              f"{f['tables_over_threshold']:>5}{f['composite_pk']:>5}"
              f"{f['role_playing_pairs']:>6}{f['many_to_many']:>5}"
              f"{f['hierarchies'].get('level', 0):>5}{fiscal:>8}")

    tot = {k: sum(f[k] for f in per_fixture.values())
           for k in ("role_playing_pairs", "many_to_many", "composite_pk",
                     "tables_over_threshold")}
    levels = sum(f["hierarchies"].get("level", 0) for f in per_fixture.values())
    fiscal_gold = sum(1 for f in per_fixture.values() if f["fiscal_in_gold"])
    fiscal_data = sum(1 for f in per_fixture.values() if f["fiscal_in_data"])

    print("\nCORPUS-WIDE")
    print(f"  role-playing pairs asserted in gold   {tot['role_playing_pairs']}")
    print(f"  level hierarchies asserted in gold    {levels}")
    print(f"  many-to-many relationships            {tot['many_to_many']}")
    print(f"  composite primary keys                {tot['composite_pk']}")
    print(f"  tables over the sampling threshold    {tot['tables_over_threshold']}")
    print(f"  fixtures asserting a fiscal calendar  {fiscal_gold}"
          f"  (have fiscal columns in data: {fiscal_data})")

    print("\nGAPS IN THE FIXTURE CORPUS, with the cheapest route")
    if fiscal_data > fiscal_gold:
        print(f"  ANNOTATE   {fiscal_data - fiscal_gold} fixture(s) have fiscal columns "
              "in the data that gold does not assert -> free to make scoreable")
    if levels:
        print(f"  DETECTOR   gold asserts {levels} level hierarchies; nothing emits "
              "kind=level -> scoreable the moment it is written, no new data needed")
    # "Anywhere in the corpus" used to mean "in the synthetic golds", which is
    # how this line read SYNTHESISE while AdventureWorksDW shipped a textbook
    # bridge. Authored annotations on acquired warehouses count as gold too.
    annotated = _annotated_m2m()
    if not tot["many_to_many"] and not annotated:
        print("  SYNTHESISE no many-to-many relationship anywhere in the corpus")
    elif not tot["many_to_many"]:
        for ds, rel in annotated:
            print(f"  DETECTOR   {ds} asserts many_to_many "
                  f"({rel['from']} -> {rel['to']} via {rel['via']}); "
                  "link/run.py hardcodes many_to_one at both emission sites, so "
                  "nothing can emit it")
            if any(len(leg.get("columns") or []) > 1 for leg in rel.get("via_keys") or []):
                print("             ALSO blocked on composite foreign keys: a "
                      "bridge leg is multi-column and the engine does not "
                      "attempt those, so a detector would see half a bridge")
    if not tot["tables_over_threshold"]:
        print("  REGENERATE no table crosses the sampling threshold -> the sampling "
              "path has never been scored against gold")
    _external(levels)


def _annotated_m2m() -> list[tuple[str, dict]]:
    """Many-to-many relationships WE asserted about an acquired warehouse.

    Separate from `declared_keys.json`, which is the vendor's own and lives in
    a gitignored directory the fetch script regenerates.
    """
    out = []
    d = ROOT / "fixtures" / "external" / "annotations"
    for f in sorted(d.glob("*.yaml")) if d.is_dir() else []:
        try:
            doc = yaml.safe_load(f.read_text()) or {}
        except Exception:
            continue
        for rel in doc.get("relationships") or []:
            if rel.get("cardinality") == "many_to_many":
                out.append((doc.get("dataset", f.stem), rel))
    return out


def _external(level_hierarchies: int) -> None:
    """Report acquired real-world datasets, kept strictly apart from fixtures.

    Fixture numbers and real-schema numbers measure different things and are
    never averaged. Track-I datasets are listed so nobody forgets they exist,
    and flagged so nobody publishes a number from one.
    """
    reg = ROOT / "fixtures" / "external" / "registry.yaml"
    if not reg.exists():
        return
    rows = yaml.safe_load(reg.read_text()).get("datasets") or []
    # A private overlay, when the checkout has one. This prints to a terminal
    # and writes no tracked file, so merging it here cannot reach the published
    # tree -- unlike `render_warehouses.py`, whose output IS tracked and whose
    # overlay is therefore opt-in.
    overlay = ROOT.parent / "internal" / "fixtures" / "registry.yaml"
    if overlay.exists():
        rows = rows + ((yaml.safe_load(overlay.read_text()) or {})
                       .get("datasets") or [])
    print("\nACQUIRED REAL SCHEMAS (never averaged with fixture numbers)")
    if not rows:
        print("  none yet")
    domains = set()
    # A missing field used to raise a bare KeyError from inside the print,
    # which killed the whole audit and named neither the entry nor the field.
    # Five entries added on 2026-10-16 had no `domain` and the audit stopped
    # reporting at all. Say which entry and which field, then carry on.
    for r in rows:
        missing = [k for k in ("name", "track", "domain", "licence")
                   if not r.get(k)]
        if missing:
            print(f"  [?] {r.get('name', '<unnamed>')!r} INCOMPLETE REGISTRY "
                  f"ENTRY -- missing {', '.join(missing)}; not counted")
            continue
        fetched = (ROOT / "fixtures" / "external" / "data" / r["name"]).exists()
        flag = "PUBLISHABLE" if r["track"] == "P" else "INTERNAL ONLY"
        print(f"  [{r['track']}] {r['name']:20} {r['domain']:28} "
              f"{r['licence']:14} {'fetched' if fetched else 'not fetched'}  {flag}")
        print(f"      features: {', '.join(r.get('features') or [])}")
        domains.add(r["domain"])

    print("\nGAPS, with the cheapest route")
    print(f"  ACQUIRE    all 9 fixtures are retail/orders; acquired domains so "
          f"far: {', '.join(sorted(domains)) or 'none'}")


if __name__ == "__main__":
    data = audit()
    if "--json" in sys.argv:
        print(json.dumps(data, indent=1))
    else:
        report(data)
