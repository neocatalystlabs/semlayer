"""wide_* fixtures: object-count scale, and the naming convention as a variable.

Two warehouses are generated from one description, identical in every way
except what the tables and columns are CALLED:

    wide_conventional   stg_sales__customer / int__customer / customer
    wide_opaque         T_0041 / CUST_MASTER_NEW / CUSTNO, CUST_NUM, C_ID

That difference is the point. Three independent disambiguation mechanisms in
the engine -- the ETL-layer rule, the pre-test FK prior, and margin stopping --
all read table and column NAMES. Each was measured on `dbt_synthea`, a tidy dbt
project, and each silently does nothing where no convention is followed. No
fixture represented that population, so "how much does the naming convention
buy us" could not be asked. With these two it is a controlled experiment: run
the same measurement on both and the difference is attributable to naming
alone, because nothing else differs.

WHAT IS DELIBERATELY HARD HERE

1. **Staging copies hold identical key sets.** Every published entity has an
   `int__` and a `stg_` ancestor carrying the same ids, so inclusion testing
   cannot separate them and neither can any statistic computed from key values
   -- in-degree included. Only the name, or an ETL manifest, can.
2. **Opaque mode is inconsistent, not random.** Real legacy warehouses do not
   name columns `x7f3a`; they call the same entity `CUST_NO` here and
   `CUSTOMER_NUMBER` there and `C_ID` in the table somebody wrote in 2009.
   Random strings would be unrealistically easy to rule out and
   unrealistically hard to match. Inconsistent abbreviation is the real case.
3. **Half the child columns have no parent at all.** Status codes, flags,
   free-text and amounts. Measured on dbt_synthea, children that match nothing
   spend 57% of the entire foreign-key budget discovering it, so a fixture
   without them would misrepresent where the cost is.
4. **Dead tables.** A tenth of the warehouse is referenced by nothing and
   queried by nobody -- the population a scope planner is supposed to skip and
   a naive run profiles in full.

WHAT A WRONG ENGINE DOES

- Resolves `order.customer_id` to `stg_sales__customer` (or in opaque mode, to
  whichever copy the dict yielded first) instead of the published table.
- Reports the same confidence on both fixtures, which would mean its
  confidence is not tracking the evidence it actually has.
- Spends its whole budget before reaching the tables anyone queries.

SIZE. Defaults to 2,000 tables, which is the documented support ceiling
(docs/scale.md). Row counts are small on purpose: this fixture exists to scale
the number of OBJECTS, because per-table and per-column costs are what bite at
2,000 tables, not data volume. Set SEMLAYER_WIDE_TABLES to build a smaller one.
"""

from __future__ import annotations

import os
import random

try:
    from generators._bulk import bulk_insert
except ImportError:
    from _bulk import bulk_insert

SEED = 20261016
N_TABLES = int(os.environ.get("SEMLAYER_WIDE_TABLES", "2000"))

# The entities the warehouse is about. Each gets a published table, an
# intermediate and a staging copy, plus facts that reference it.
ENTITIES = [
    "customer", "product", "store", "employee", "supplier", "campaign",
    "channel", "region", "warehouse", "carrier", "contract", "account",
    "invoice", "shipment", "ticket", "subscription", "device", "location",
]

# How the same entity gets referred to when nobody is enforcing a convention.
OPAQUE_FORMS = {
    "customer": ["CUST_NO", "CUSTOMER_NUMBER", "C_ID", "CUSTNUM", "CUST_KEY"],
    "product": ["PROD_CD", "PRODUCT_NO", "P_ID", "ITEM_CODE", "SKU_NUM"],
    "store": ["STR_NO", "STORE_CD", "S_ID", "LOC_NUM", "SITE_CODE"],
    "employee": ["EMP_NO", "EMPLOYEE_ID", "E_ID", "EMPNUM", "STAFF_CD"],
}
_FALLBACK_FORMS = ["{0}_NO", "{0}_CD", "{0}_NUM", "{0}_KEY", "{0}_ID"]

# Business processes a warehouse actually records. The first draft named every
# fact `event{i}`, which made 90% of the tables near-identical -- exactly the
# monotony that makes `multi_tenant` parameterised to 2,000 unsuitable for
# measuring a scope planner. A fixture has to be varied where the thing under
# test reads variety.
PROCESSES = [
    "order", "return", "payment", "refund", "shipment", "receipt", "adjustment",
    "booking", "cancellation", "claim", "quote", "renewal", "visit", "session",
    "click", "impression", "signup", "churn", "ticket", "escalation", "delivery",
    "pickup", "transfer", "reconciliation", "settlement", "chargeback", "audit",
    "inspection", "maintenance", "incident", "alert", "forecast", "allocation",
    "replenishment", "markdown", "promotion_exposure", "loyalty_accrual",
    "redemption", "enrolment", "assessment",
]
GRAINS = ["line", "header", "daily", "weekly", "snapshot", "hist", "detail", "summary"]

MEASURES = ["amount", "qty", "net_value", "tax", "discount", "weight"]
# Child columns that will never have a parent: the 57% case.
ORPHAN_COLS = ["status_cd", "flag_active", "note_txt", "created_by", "src_sys"]


def _forms(entity: str, rng: random.Random) -> list[str]:
    forms = OPAQUE_FORMS.get(entity)
    if forms:
        return forms
    stem = entity[:4].upper()
    return [f.format(stem) for f in _FALLBACK_FORMS]


class Naming:
    """Table and column names, in one of two houses."""

    def __init__(self, opaque: bool, rng: random.Random):
        self.opaque = opaque
        self.rng = rng
        self._opaque_table: dict[str, str] = {}
        self._used: set[str] = set()
        self._n = 0

    def table(self, stage: str, entity: str, kind: str = "dim") -> str:
        key = f"{stage}:{entity}:{kind}"
        if not self.opaque:
            if stage == "published":
                return entity if kind == "dim" else f"{entity}_{kind}"
            return f"{stage}__{entity}" if kind == "dim" else f"{stage}__{entity}_{kind}"
        if key not in self._opaque_table:
            self._n += 1
            # a real legacy warehouse mixes house styles; so does this. Names
            # must still be unique -- `event12` and `event34` both stem to
            # EVEN, which collided on the first build.
            stem = entity[:4].upper()
            style = self.rng.randrange(3)
            if style == 0:
                cand = f"T_{self._n:04d}"
            elif style == 1:
                suffix = self.rng.choice(["_NEW", "_FINAL", "_V2", "_MASTER", ""])
                cand = f"{stem}_MSTR{suffix}"
            else:
                cand = f"{stem}{self._n:03d}"
            if cand in self._used:
                cand = f"{cand}_{self._n:04d}"
            self._used.add(cand)
            self._opaque_table[key] = cand
        return self._opaque_table[key]

    def key(self, entity: str) -> str:
        """The PK column name on the entity's own table."""
        if not self.opaque:
            return f"{entity}_id"
        return _forms(entity, self.rng)[0]

    def fk(self, entity: str) -> str:
        """What a CHILD table calls the same key -- inconsistently, if opaque."""
        if not self.opaque:
            return f"{entity}_id"
        return self.rng.choice(_forms(entity, self.rng))


def _plan(rng: random.Random, naming: Naming) -> list[dict]:
    """Decide the whole warehouse before writing any of it."""
    tables: list[dict] = []
    # 1. three copies of every entity: published, intermediate, staging
    for ent in ENTITIES:
        for stage in ("published", "int", "stg_sales"):
            tables.append({"kind": "dim", "entity": ent, "stage": stage,
                           "name": naming.table(stage, ent)})
    # 2. facts referencing a handful of entities each, then pad to N_TABLES
    i = 0
    while len(tables) < int(N_TABLES * 0.9):
        ents = rng.sample(ENTITIES, rng.randint(2, 5))
        stage = rng.choices(["published", "int", "stg_sales"], weights=[5, 3, 2])[0]
        proc = PROCESSES[i % len(PROCESSES)]
        grain = GRAINS[(i // len(PROCESSES)) % len(GRAINS)]
        era = i // (len(PROCESSES) * len(GRAINS))
        ent = f"{proc}_{grain}" if era == 0 else f"{proc}_{grain}_v{era + 1}"
        tables.append({"kind": "fact", "entity": ent, "stage": stage,
                       "name": naming.table(stage, ent, "fact"),
                       "refs": ents})
        i += 1
    # 3. dead tables: referenced by nothing, queried by nobody
    while len(tables) < N_TABLES:
        tables.append({"kind": "dead", "entity": f"scratch{len(tables)}",
                       "stage": "stg_sales",
                       "name": naming.table("stg_sales", f"scratch{len(tables)}")})
    return tables


def _build(con, opaque: bool) -> list[tuple[str, str, str, str]]:
    """Build the warehouse and return its gold foreign-key edges.

    Gold is (child_table, child_column, parent_table, parent_column), and the
    parent is the copy of the entity **in the child's own ETL stage**, falling
    back to published where the child is published.

    The first version made it always the published copy, and that was wrong in
    a way the first run of the experiment exposed: it scores the engine's
    same-layer rule as an error on every fact that is not itself published.
    That rule is measured (+18.3 recall, +14.9 precision on dbt_synthea) and
    it is what the convention means -- `stg_synthea__allergies.encounter_id`
    is declared against `stg_synthea__encounters`, not the published table
    built from it. With ~50% of facts in non-published stages, published-only
    gold capped achievable precision near 0.5, which is what the run measured.
    """
    rng = random.Random(SEED)
    naming = Naming(opaque, rng)
    tables = _plan(rng, naming)

    # Entity key domains: every copy of an entity shares them exactly, which is
    # what makes inclusion testing unable to tell the copies apart.
    domains = {e: list(range(1000, 1000 + 40 + i * 3)) for i, e in enumerate(ENTITIES)}
    # (entity, stage) -> table name, so gold can resolve within a stage
    copies = {(t["entity"], t["stage"]): t["name"]
              for t in tables if t["kind"] == "dim"}
    pubkey: dict[str, str] = {}
    gold: list[tuple[str, str, str, str]] = []

    for t in tables:
        name, kind = t["name"], t["kind"]
        if kind == "dim":
            ent = t["entity"]
            kcol = naming.key(ent)
            pubkey.setdefault(ent, kcol)   # same column name in every copy
            con.execute(f'CREATE TABLE "{name}" ('
                        f'"{kcol}" INTEGER PRIMARY KEY, '
                        f'"{"nm" if opaque else "name"}" VARCHAR, '
                        f'"{ORPHAN_COLS[0]}" VARCHAR)')
            rows = [(k, f"{ent}-{k}", rng.choice(["A", "B", "C"]))
                    for k in domains[ent]]
            bulk_insert(con, f'"{name}"', rows)
        elif kind == "fact":
            refs = t["refs"]
            cols = [f'"{"id" if opaque else "event_id"}" INTEGER']
            fkcols = []
            for e in refs:
                c = naming.fk(e)
                while c in fkcols:                      # no duplicate columns
                    c = c + "_X"
                fkcols.append(c)
                cols.append(f'"{c}" INTEGER')
                parent = copies.get((e, t["stage"])) or copies[(e, "published")]
                gold.append((name, c, parent, pubkey[e]))
            meas = rng.sample(MEASURES, rng.randint(1, 3))
            cols += [f'"{m}" DOUBLE' for m in meas]
            orph = rng.sample(ORPHAN_COLS, rng.randint(1, 3))
            cols += [f'"{o}" VARCHAR' for o in orph]
            con.execute(f'CREATE TABLE "{name}" ({", ".join(cols)})')
            n = rng.randint(30, 120)
            rows = []
            for r in range(n):
                row: list = [r]
                row += [rng.choice(domains[e]) for e in refs]
                row += [round(rng.uniform(1, 500), 2) for _ in meas]
                row += [rng.choice(["OPEN", "CLOSED", "X"]) for _ in orph]
                rows.append(tuple(row))
            bulk_insert(con, f'"{name}"', rows)
        else:
            con.execute(f'CREATE TABLE "{name}" ('
                        f'"{"c1" if opaque else "scratch_id"}" INTEGER, '
                        f'"{"c2" if opaque else "payload"}" VARCHAR)')
            bulk_insert(con, f'"{name}"',
                        [(i, f"x{i}") for i in range(rng.randint(5, 30))])
    return gold


NAME = "wide_conventional"


def build(con) -> list[tuple[str, str, str, str]]:
    return _build(con, opaque=False)


def build_opaque(con) -> list[tuple[str, str, str, str]]:
    return _build(con, opaque=True)
