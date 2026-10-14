"""Pre-flight: is this warehouse worth acquiring, and what can it validate?

Run before authoring any gold for a candidate dataset. Gold is the expensive
part, so the point is to find out cheaply what a warehouse can and cannot
demonstrate.

    python fixtures/external/probe.py fixtures/external/data/<name>/<name>.duckdb

The question that matters most is the last one. Our central claim is that the
engine recovers business rules nobody wrote down -- and the mechanism needs no
human to interview, because it reconciles a published SUMMARY table against the
DETAIL it was built from and reports the filter under which they agree.

So a warehouse can only demonstrate that claim if it ships both levels. Neither
`tpcds_clean` nor AdventureWorksDW does, which is why neither can carry it:
AdventureWorksDW returns 0 aggregate tables and 0 required filters. It is a
clean vendor warehouse, and on clean warehouses we publish that we make things
slightly worse.

Vocabulary diversity is a different axis and finds different bugs. Acquiring
AdventureWorksDW exposed three real engine defects within an hour without
having a single recoverable rule in it.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]


def probe(db: pathlib.Path) -> dict:
    """Infer deterministically and report what the warehouse can exercise."""
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / "layer.yaml"
        subprocess.run(
            [sys.executable, "-m", "semlayer.cli", "infer", f"duckdb:{db}",
             "--no-llm", "-o", str(out)],
            check=True, cwd=ROOT, stdout=subprocess.DEVNULL)
        sl = yaml.safe_load(out.read_text())["semantic_layer"]

    filters = [t["name"] for t in sl["tables"]
               if (t.get("knowledge") or {}).get("required_filters")]
    rels = sl.get("relationships") or []
    pairs: dict[tuple, int] = {}
    for r in rels:
        k = (r.get("from", {}).get("table"), r.get("to", {}).get("table"))
        pairs[k] = pairs.get(k, 0) + 1
    hier = {}
    for h in sl.get("hierarchies") or []:
        hier[h.get("kind")] = hier.get(h.get("kind"), 0) + 1

    return {
        "tables": len(sl["tables"]),
        "columns": sum(len(t.get("columns") or []) for t in sl["tables"]),
        "relationships": len(rels),
        # the key is `table`, not `name` -- printing None is how this was found
        "aggregate_tables": [a.get("table") for a in (sl.get("aggregate_tables") or [])],
        "required_filters": filters,
        "role_playing_pairs": sum(1 for n in pairs.values() if n > 1),
        "hierarchies": hier,
        "metrics": len(sl.get("metrics") or []),
        "deprecated": [t["name"] for t in sl["tables"] if t.get("lifecycle") == "deprecated"],
        "fiscal_columns": sorted({c["name"] for t in sl["tables"] for c in t["columns"]
                                  if "fy" in c["name"].lower()
                                  or "fiscal" in c["name"].lower()}),
    }


def report(name: str, r: dict) -> None:
    print(f"\n{name}: {r['tables']} tables, {r['columns']} columns, "
          f"{r['relationships']} relationships\n")
    print("  CAN IT DEMONSTRATE RULE RECOVERY?")
    if r["aggregate_tables"] and r["required_filters"]:
        print(f"    YES — summary tables {r['aggregate_tables']} reconcile with their "
              f"detail only under a filter, recorded on {r['required_filters']}")
    elif r["aggregate_tables"]:
        print(f"    PARTLY — summary tables found ({r['aggregate_tables']}) but they "
              "reconcile with no filter needed: a tidy warehouse")
    else:
        print("    NO — nothing reconciled. Note this cannot distinguish "
              '"no summary table exists" from "a summary exists and does not')
        print("    reconcile additively\" — since aggregate detection became "
              "shape-based, candidates are")
        print("    examined and attempted silently. CMS is the second case: its "
              "detail carries AVERAGES")
        print("    (Avg_Mdcr_Pymt_Amt), so its totals reconcile as "
              "SUM(avg * count), which the engine")
        print("    cannot express — 0 of 387 providers match on a plain SUM.")
    print("\n  OTHER SHAPES IT EXERCISES")
    for label, v in (("role-playing dimension pairs", r["role_playing_pairs"]),
                     ("hierarchies by kind", r["hierarchies"] or "none"),
                     ("metrics proposed", r["metrics"]),
                     ("deprecated tables", r["deprecated"] or "none"),
                     ("fiscal columns", r["fiscal_columns"] or "none")):
        print(f"    {label:30}{v}")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    db = pathlib.Path(argv[0])
    if not db.exists():
        print(f"no such database: {db}", file=sys.stderr)
        return 1
    report(db.stem, probe(db))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
