"""scale_N: a warehouse of N tables in an enterprise tier mix, N programmable.

    python fixtures/generators/scale.py --tables 5000 --out /tmp/scale_5000.duckdb

`wide.py` is a controlled experiment pinned at 2,000 tables. This generator
exists to walk past it -- 2,000, 5,000, 10,000, 20,000 -- and see which stage
of the engine curves upward and what fails first. Row counts are tiny on
purpose; the number of OBJECTS is the dial, because per-table and per-column
costs are what bite at scale, not data volume.

THE TIER MIX

A real enterprise warehouse is mostly not the part anybody models against.
The hypothesis this fixture is built to test is that only 10-20% of tables
are useful and the rest should be recorded as informational only. So the mix
is deliberately noise-heavy, after the shape of large dbt + raw-zone estates:

    core      8%   fct_ / dim_ / agg_ star schemas, some SCD2 dimensions
    staging  22%   stg_<src>__ and int__ copies of the core entities
    raw      45%   raw_ / landing_<YYYYMMDD> / cdc_ / snapshot_ / hist_ copies
    scratch  25%   tmp_ / wrk_ / bak_ / test_ / dev_ / junk_ leftovers

Every table's tier is written to a sidecar `<out>.meta.json` with the gold
foreign-key edges, so a run's output can be scored per tier: what did the
engine do with the 70% that is noise?

WHAT IS DELIBERATELY HARD (inherited from wide.py)

- Every copy of an entity carries the SAME key domain, so inclusion testing
  cannot separate `dim_customer`, `stg_crm__customer`, `raw_crm__customer`
  and `bak_customer_20250301`. Only the name can.
- Half the child columns have no parent at all (status codes, flags, text).
- Raw shards multiply: one entity may have a dozen `landing_<ent>_<date>`
  tables, which is what makes candidate parents numerous.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

try:
    from generators.wide import ENTITIES, GRAINS, MEASURES, ORPHAN_COLS, PROCESSES
except ImportError:
    from wide import ENTITIES, GRAINS, MEASURES, ORPHAN_COLS, PROCESSES

SEED = 20261028
TIERS = (("core", 0.08), ("staging", 0.22), ("raw", 0.45), ("scratch", 0.25))

DOMAINS = [
    "orders", "billing", "payments", "customer_success", "inventory", "marketing",
    "sales", "finance", "hr", "logistics", "procurement", "support", "product",
    "pricing", "loyalty", "risk", "compliance", "fulfilment", "returns", "web",
    "mobile", "ads", "partner", "wholesale", "retail", "subscriptions", "tax",
    "treasury", "legal", "facilities", "fleet", "field_service", "warranty",
    "quality", "planning", "merchandising", "ecommerce", "crm", "erp", "iot",
]
SOURCES = ["crm", "erp", "web", "pos", "sap", "sfdc", "stripe", "zendesk", "ga4", "hubspot"]
DATES = ["20240101", "20240401", "20240701", "20241001", "20250101", "20250401",
         "20250701", "20251001", "20250115", "20250215", "20250315", "20250515"]
ATTRS = ["segment", "tier", "region", "category", "channel", "status", "type"]


def _lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def insert_rows(con, table: str, rows: list[tuple]) -> None:
    """One INSERT ... VALUES per table. Rows here are 5-120 long, and a
    literal statement beats both executemany (~2 ms/row) and the CSV COPY
    round trip (a temp file per table) at that size: 300 tables built in
    8 s instead of 17."""
    if not rows:
        return
    vals = ",".join("(" + ",".join(_lit(v) for v in r) + ")" for r in rows)
    con.execute(f"INSERT INTO {table} VALUES {vals}")


def _quota(n_tables: int) -> dict[str, int]:
    q = {name: int(n_tables * share) for name, share in TIERS}
    q["raw"] += n_tables - sum(q.values())     # rounding remainder goes to the noise
    return q


def plan(n_tables: int, rng: random.Random) -> list[dict]:
    """Decide the whole warehouse before writing any of it.

    Each table: name, tier, kind (dim|scd2|fact|agg|copy|raw|scratch|junk),
    entity, and for facts/aggs the entities they reference.
    """
    quota = _quota(n_tables)
    tables: list[dict] = []
    used: set[str] = set()

    def add(t: dict) -> bool:
        if t["name"] in used:
            return False
        used.add(t["name"])
        tables.append(t)
        return True

    # ---- core: domains of star schemas, ~10 tables each --------------------
    core_dims: list[dict] = []          # entity, key, scd2
    core_facts: list[dict] = []
    d = -1
    while len(tables) < quota["core"]:
        d += 1
        dom = DOMAINS[d % len(DOMAINS)] + ("" if d < len(DOMAINS) else f"_{d // len(DOMAINS) + 1}")
        dims = []
        for ent in rng.sample(ENTITIES, rng.randint(4, 7)):
            name = f"{dom}_{ent}" if rng.random() < 0.6 else ent
            scd2 = rng.random() < 0.3
            dim = {"entity": name, "scd2": scd2}
            if add({"name": f"dim_{name}", "tier": "core", "kind": "scd2" if scd2 else "dim",
                    "entity": name}):
                dims.append(dim)
                core_dims.append(dim)
            if len(tables) >= quota["core"]:
                break
        if not dims:
            continue
        for _ in range(rng.randint(1, 3)):
            proc = rng.choice(PROCESSES)
            grain = rng.choice(GRAINS)
            refs = rng.sample(dims, min(len(dims), rng.randint(2, 5)))
            fact = {"name": f"fct_{dom}_{proc}_{grain}", "tier": "core", "kind": "fact",
                    "entity": f"{dom}_{proc}_{grain}", "refs": [r["entity"] for r in refs]}
            if add(fact):
                core_facts.append(fact)
                if rng.random() < 0.5:
                    by = rng.choice(refs)["entity"]
                    add({"name": f"agg_{dom}_{proc}_monthly_by_{by}", "tier": "core",
                         "kind": "agg", "entity": f"{dom}_{proc}", "refs": [by]})
    core_n = len(tables)

    # ---- staging: stg_<src>__ and int__ copies of core dims and facts ------
    i = 0
    pool = core_dims + core_facts
    while len(tables) - core_n < quota["staging"] and pool:
        t = pool[i % len(pool)]
        i += 1
        ent = t["entity"]
        src = SOURCES[(i // len(pool)) % len(SOURCES)]
        pref = f"stg_{src}__" if (i // len(pool)) % 3 else "int__"
        suffix = "" if i // len(pool) < 3 else f"_v{i // len(pool) - 1}"
        add({"name": f"{pref}{ent}{suffix}", "tier": "staging", "kind": "copy",
             "entity": ent, "of": t})
        if i > quota["staging"] * 4:
            break
    staging_n = len(tables) - core_n

    # ---- raw: shards and change feeds of the dims ---------------------------
    kinds = [("raw_{src}__{ent}", "copy"), ("landing_{ent}_{date}", "copy"),
             ("cdc_{src}_{ent}", "cdc"), ("snapshot_{ent}_{date}", "copy"),
             ("hist_{ent}", "cdc"), ("src_{src}_{ent}_{date}", "copy"),
             ("audit_{ent}_{date}", "cdc")]
    i = 0
    while len(tables) - core_n - staging_n < quota["raw"] and core_dims:
        dim = core_dims[i % len(core_dims)]
        pat, kind = kinds[(i // len(core_dims)) % len(kinds)]
        round_ = i // (len(core_dims) * len(kinds))
        name = pat.format(src=SOURCES[(i + round_) % len(SOURCES)], ent=dim["entity"],
                          date=DATES[(i + round_) % len(DATES)])
        if round_:
            name = f"{name}_{round_}"
        add({"name": name, "tier": "raw", "kind": kind, "entity": dim["entity"], "of": dim})
        i += 1
        if i > quota["raw"] * 4:
            break

    # ---- scratch: the leftovers ---------------------------------------------
    pats = ["tmp_{ent}_{n}", "wrk_{proc}_{n}", "bak_{ent}_{date}", "test_{proc}",
            "dev_{ent}_{n}", "junk_{n}", "scratch_{proc}_{n}", "{ent}_copy", "{ent}_old",
            "backup_{ent}_{date}", "sandbox_{proc}_{n}", "{proc}_draft_{n}"]
    i = 0
    while len(tables) < n_tables:
        pat = pats[i % len(pats)]
        ent = (core_dims[i % len(core_dims)]["entity"] if core_dims else f"e{i}")
        name = pat.format(ent=ent, proc=PROCESSES[i % len(PROCESSES)],
                          date=DATES[i % len(DATES)], n=i)
        kind = "copy" if pat.startswith(("bak_", "backup_", "{ent}")) and rng.random() < 0.7 else "junk"
        add({"name": name, "tier": "scratch", "kind": kind, "entity": ent,
             "of": core_dims[i % len(core_dims)] if core_dims else None})
        i += 1
    return tables


def build(con, n_tables: int, seed: int = SEED) -> dict:
    """Build N tables into `con`. Returns the meta dict (tiers + gold edges)."""
    rng = random.Random(seed)
    tables = plan(n_tables, rng)
    names = {t["name"] for t in tables}
    domains: dict[str, list[int]] = {}
    gold: list[tuple[str, str, str, str]] = []
    dim_table: dict[str, str] = {}        # entity -> core dim table name

    def domain(ent: str) -> list[int]:
        if ent not in domains:
            base = 1000 * (len(domains) + 1)
            domains[ent] = list(range(base, base + rng.randint(40, 90)))
        return domains[ent]

    def dim_cols(ent: str, scd2: bool) -> tuple[list[str], list[tuple]]:
        keys = domain(ent)
        attrs = rng.sample(ATTRS, 2)
        cols = [f'"{ent}_id" INTEGER', f'"{ent}_name" VARCHAR',
                f'"{attrs[0]}" VARCHAR', f'"{ent}_{attrs[1]}" VARCHAR',
                '"is_active" BOOLEAN', '"created_at" TIMESTAMP']
        rows = []
        if scd2:
            cols = [f'"sk_{ent}" INTEGER', *cols, '"valid_from" DATE', '"valid_to" DATE',
                    '"is_current" BOOLEAN']
            sk = 1
            for k in keys:
                versions = rng.randint(1, 3)
                for v in range(versions):
                    cur = v == versions - 1
                    rows.append((sk, k, f"{ent}-{k}", rng.choice("ABC"), rng.choice("XYZ"),
                                 rng.random() < 0.9, "2024-01-01 00:00:00",
                                 f"2024-0{v + 1}-01", "9999-12-31" if cur else f"2024-0{v + 2}-01",
                                 cur))
                    sk += 1
        else:
            rows = [(k, f"{ent}-{k}", rng.choice("ABC"), rng.choice("XYZ"),
                     rng.random() < 0.9, "2024-01-01 00:00:00") for k in keys]
        return cols, rows

    def create(name: str, cols: list[str], rows: list[tuple]) -> None:
        con.execute(f'CREATE TABLE "{name}" ({", ".join(cols)})')
        insert_rows(con, f'"{name}"', rows)

    etl_cols = ['"etl_batch_id" INTEGER', '"dw_inserted_at" TIMESTAMP',
                '"src_system" VARCHAR', '"record_hash" VARCHAR']

    def etl_vals() -> tuple:
        return (rng.randint(1, 500), "2025-01-01 00:00:00", rng.choice(SOURCES),
                f"{rng.getrandbits(64):016x}")

    for t in tables:
        name, kind, ent = t["name"], t["kind"], t["entity"]
        if kind in ("dim", "scd2"):
            cols, rows = dim_cols(ent, kind == "scd2")
            create(name, cols, rows)
            dim_table[ent] = name
        elif kind in ("fact", "agg"):
            refs = t["refs"]
            cols = ['"event_id" INTEGER'] if kind == "fact" else ['"month" DATE']
            fkcols = []
            for e in refs:
                c = f"{e}_id"
                while c in fkcols:
                    c += "_x"
                fkcols.append(c)
                cols.append(f'"{c}" INTEGER')
                gold.append((name, c, dim_table.get(e, f"dim_{e}"), f"{e}_id"))
            meas = rng.sample(MEASURES, rng.randint(1, 3))
            mcols = ([f"amt_{m}" for m in meas] if kind == "fact"
                     else [f"total_{m}" for m in meas] + ["cnt_events"])
            cols += [f'"{m}" DOUBLE' for m in mcols]
            orph = rng.sample(ORPHAN_COLS, rng.randint(1, 2)) if kind == "fact" else []
            cols += [f'"{o}" VARCHAR' for o in orph]
            cols += ['"created_at" TIMESTAMP'] if kind == "fact" else []
            n = rng.randint(30, 120) if kind == "fact" else rng.randint(20, 60)
            rows = []
            for r in range(n):
                row: list = [r] if kind == "fact" else [f"2025-{(r % 12) + 1:02d}-01"]
                row += [rng.choice(domain(e)) for e in refs]
                row += [round(rng.uniform(1, 5000), 2) for _ in mcols]
                row += [rng.choice(["OPEN", "CLOSED", "X"]) for _ in orph]
                row += ["2025-01-01 00:00:00"] if kind == "fact" else []
                rows.append(tuple(row))
            create(name, cols, rows)
        elif kind in ("copy", "cdc"):
            of = t["of"]
            if of is not None and "refs" in of:          # a copy of a fact
                refs = of["refs"]
                cols = ['"event_id" INTEGER'] + [f'"{e}_id" INTEGER' for e in refs] + \
                       ['"amt_amount" DOUBLE', '"status_cd" VARCHAR'] + etl_cols
                parent_stage = t["name"].split("__")[0] + "__" if "__" in t["name"] else None
                for e in refs:
                    # the copy of the dim in the same stage, if it was planned;
                    # otherwise the core dim (same key domain either way)
                    parent = f"{parent_stage}{e}" if parent_stage else None
                    if parent not in names:
                        parent = dim_table.get(e, f"dim_{e}")
                    gold.append((name, f"{e}_id", parent, f"{e}_id"))
                rows = [(r, *[rng.choice(domain(e)) for e in refs],
                         round(rng.uniform(1, 500), 2), rng.choice(["OPEN", "CLOSED"]),
                         *etl_vals()) for r in range(rng.randint(20, 80))]
            else:
                keys = domain(ent)
                cols = [f'"{ent}_id" INTEGER', f'"{ent}_name" VARCHAR',
                        '"payload" VARCHAR', *etl_cols]
                if kind == "cdc":
                    cols += ['"op" VARCHAR', '"changed_at" TIMESTAMP']
                rows = []
                for k in rng.sample(keys, rng.randint(len(keys) // 2, len(keys))):
                    row = [k, f"{ent}-{k}", f'{{"id":{k}}}', *etl_vals()]
                    if kind == "cdc":
                        row += [rng.choice(["I", "U", "D"]), "2025-02-01 00:00:00"]
                    rows.append(tuple(row))
            create(name, cols, rows)
        else:   # junk
            ncols = rng.randint(2, 5)
            cols = ['"c1" INTEGER'] + [f'"col{j}" VARCHAR' for j in range(2, ncols + 1)]
            rows = [(r, *[f"x{r}{j}" for j in range(2, ncols + 1)])
                    for r in range(rng.randint(5, 30))]
            create(name, cols, rows)

    meta = {
        "n_tables": len(tables), "seed": seed,
        "tiers": {k: sum(1 for t in tables if t["tier"] == k) for k, _ in TIERS},
        "tables": {t["name"]: {"tier": t["tier"], "kind": t["kind"]} for t in tables},
        "gold_fks": gold,
    }
    return meta


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tables", type=int, required=True)
    ap.add_argument("--out", required=True, help="DuckDB file to create (replaced if present)")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--block-size", type=int, default=16384,
                    help="DuckDB block size; the 256 KB default costs ~280 KB per table "
                         "(85 MB per 300 tables), 16 KB is the minimum and gives ~24 KB")
    args = ap.parse_args(argv)
    import duckdb
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    con = duckdb.connect()
    con.execute(f"ATTACH '{out}' AS scale_db (BLOCK_SIZE {args.block_size})")
    con.execute("USE scale_db")
    try:
        meta = build(con, args.tables, args.seed)
        con.execute("CHECKPOINT")
    finally:
        con.close()
    Path(str(out) + ".meta.json").write_text(json.dumps(meta, indent=1))
    print(f"built {meta['n_tables']} tables -> {out}  tiers {meta['tiers']}  "
          f"gold fks {len(meta['gold_fks'])}  ({out.stat().st_size / 1e6:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
