# SPDX-License-Identifier: FSL-1.1-ALv2
"""What evidence does this warehouse offer, and what accuracy follows from it?

Every disambiguation mechanism the engine has reads NAMES. The ETL-layer rule
reads a stage prefix; the foreign-key prior reads name similarity; margin
stopping ranks by that prior. All three were measured on a tidy dbt project,
and all three do nothing at all on a warehouse that follows no convention --
silently, with no change in the confidence the output claims.

That silence is the problem this module exists to end. `probe` looks before
inference runs and says which signals are present, so a run can be planned
against the evidence that exists rather than the evidence we wish for, and so
the output can cap its confidence at what the evidence supports.

TWO TIERS, because they cost different things.

  Tier 1 is metadata only: information_schema, a manifest if one is passed,
  declared constraints. Seconds, no data read, nothing to consent to.

  Tier 2 samples DATA to answer one question tier 1 cannot: do several tables
  hold the same key values? That is the ambiguity which makes naming decisive
  in the first place, and it cannot be seen from a catalogue. It reads rows,
  so it is opt-in.

WHAT THIS IS NOT. It is not a scorer of your warehouse, and the grade is not a
verdict on your data modelling. It predicts how well THIS ENGINE will do, which
is a statement about our limitations, not yours.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from semlayer.lineage import naming_layer
from semlayer.source import row_cap

# A column name that looks like a key. The first version required an
# underscore or the start of the name before the suffix, which made it
# snake_case-only: `customer_id` matched and `CustomerKey` did not. Run on
# AdventureWorks -- PascalCase throughout -- it found so few key columns that
# tier 2 sampled ONE table and reported "0% share a key set" from it. A
# measurement from a sample of one is not a measurement.
_KEYISH = re.compile(
    r"(?:^|_|(?<=[a-z0-9]))(id|key|no|num|number|code|cd|sk|pk)$", re.IGNORECASE)

# Grades are about what the engine can do here, not about the warehouse.
GRADES = ("strong", "fair", "weak")

# Set from measurement, not judgement. The first version guessed
# 0.95/0.85/0.60 and the guess was wrong in the direction that matters: run on
# two 2,000-table warehouses differing only in naming, `fair` achieved 1.000
# precision against a cap of 0.85 (under-claiming, harmless) and `weak`
# achieved 0.394 against a cap of 0.60 -- telling a user to trust output half
# again as much as it deserves, which is the whole failure this module exists
# to prevent.
#
# The anchors, FK precision, all [V]:
#   with a manifest      dbt_synthea                      0.908
#   real dbt project     dbt_synthea, no manifest         0.785
#   no convention        wide_opaque, 2,000 tables        0.384
#
# `wide_conventional` scores 1.000 at every size tried (150, 300, 2,000) and is
# deliberately NOT an anchor: its names are perfectly regular, so once the
# layer rule can read the stage the answer is deterministic. That is a ceiling
# artifact of a fixture its author wrote, not a capability. The realistic
# upper bound for a well-named warehouse is dbt_synthea's 0.785.
#
# The no-convention figure degrades with scale and flattens -- 0.425 at 150
# tables, 0.394 at 300, 0.384 at 2,000 -- toward the arithmetic of choosing
# blindly among three identical-key copies (1/3), plus what name matching
# still recovers. So the failure saturates rather than compounding.
#
# Caps sit at or just below the weakest observation for the grade, because a
# cap that exceeds what was measured is a promise nothing supports. `weak` was
# 0.40 until the 2,000-table run measured 0.384 beneath it.
GRADE_CONFIDENCE_CAP = {"strong": 0.90, "fair": 0.78, "weak": 0.38}


@dataclass
class Probe:
    """What was found, in a form that is safe to print and safe to send."""

    tables: int = 0
    columns: int = 0
    schemas: int = 0
    key_like_columns: int = 0
    # evidence
    has_manifest: bool = False
    declared_pks: int = 0
    declared_fks: int = 0
    stage_prefix_ratio: float = 0.0
    distinct_stage_prefixes: int = 0
    # tier 2
    tier2_ran: bool = False
    sampled_tables: int = 0
    shared_key_set_ratio: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def grade(self) -> str:
        """How much the engine can disambiguate here.

        A manifest is decisive on its own: it states the ETL layout, which is
        the signal everything else approximates. **Declared FOREIGN keys are
        the other decisive signal; declared primary keys are not.** Running
        this on a 2,000-table fixture graded it `strong` on 54 declared PKs,
        which is wrong: a PK says a column identifies rows in its own table
        and says nothing about which of three copies a child meant. Warehouses
        routinely declare PKs and not FKs, so the two must be counted apart.
        Otherwise the question is
        whether names carry a convention, and -- if tier 2 ran -- whether
        there is any ambiguity to resolve in the first place. A warehouse with
        no convention but no duplicate key sets is not a hard case; it is an
        easy one with ugly names.
        """
        if self.has_manifest or self.declared_fks >= max(1, self.tables // 20):
            return "strong"
        if self.stage_prefix_ratio >= 0.25 or self.declared_fks:
            return "fair"
        if self.tier2_ran and self.shared_key_set_ratio < 0.05:
            return "fair"
        return "weak"

    @property
    def confidence_cap(self) -> float:
        """The most any disambiguation-dependent inference may claim here."""
        return GRADE_CONFIDENCE_CAP[self.grade]

    def telemetry(self) -> dict:
        """Counts, fractions and booleans. Never an identifier, in any form.

        Not hashed names either: a hash still carries the shape of a schema,
        and a user who allowed usage pings did not agree to that. This payload
        is printed before it is sent (see `semlayer probe --share`), so the
        claim is checkable rather than promised.
        """
        return {
            "tables": self.tables,
            "columns": self.columns,
            "schemas": self.schemas,
            "key_like_column_ratio": round(self.key_like_columns / max(1, self.columns), 3),
            "has_manifest": self.has_manifest,
            "declared_pk_ratio": round(self.declared_pks / max(1, self.tables), 3),
            "declared_fk_ratio": round(self.declared_fks / max(1, self.tables), 3),
            "stage_prefix_ratio": round(self.stage_prefix_ratio, 3),
            "distinct_stage_prefixes": self.distinct_stage_prefixes,
            "tier2_ran": self.tier2_ran,
            "shared_key_set_ratio": round(self.shared_key_set_ratio, 3),
            "grade": self.grade,
        }


def tier1(src, lineage=None) -> Probe:
    """Catalogue only. No rows are read."""
    p = Probe(has_manifest=bool(lineage))
    tables = src.list_tables()
    p.tables = len(tables)
    p.schemas = len({t.schema for t in tables})

    prefixed = 0
    prefixes: set[str] = set()
    for t in tables:
        layer = naming_layer(t.name)
        if layer:
            prefixed += 1
            prefixes.add(layer)
        for col in t.columns:
            p.columns += 1
            if _KEYISH.search(col.name):
                p.key_like_columns += 1

    p.stage_prefix_ratio = prefixed / max(1, p.tables)
    p.distinct_stage_prefixes = len(prefixes)

    try:
        dc = src.list_constraints()
        p.declared_pks = len(dc.primary_keys)
        p.declared_fks = len(dc.foreign_keys)
    except Exception as e:                       # a catalog that will not answer
        p.notes.append(f"declared constraints unreadable: {type(e).__name__}")

    if p.declared_pks and not p.declared_fks:
        p.notes.append(
            "Primary keys are declared but foreign keys are not. A primary key "
            "does not say which table a child column meant, so join targets are "
            "still inferred."
        )
    if p.grade == "weak":
        # Tier 1 cannot tell "no convention AND duplicate key sets", which is
        # genuinely hard, from "no convention but nothing duplicated", which
        # is easy. It graded AdventureWorks weak -- a warehouse whose foreign
        # keys score 1.000 -- on 0% stage prefixes alone. Only tier 2 can
        # separate them, so a weak grade without it is provisional and must
        # say so rather than publishing a confident cap.
        p.notes.append(
            "This grade is PROVISIONAL. Without tier 2 it cannot tell a "
            "warehouse with nothing to disambiguate from one with several "
            "copies of every entity, and those deserve opposite answers. Run "
            "with --sample before trusting the cap."
        )
        p.notes.append(
            "No manifest, and table names carry no ETL stage convention. The "
            "engine's join disambiguation reads names, so where several tables "
            "hold the same key values it cannot tell them apart: expect "
            "low-confidence foreign keys and review them."
        )
    if p.has_manifest:
        p.notes.append("ETL manifest supplied: join targets use it directly.")
    return p


# Tier 2 samples rather than scans. A full key-set comparison is one query per
# table and a pairwise intersection, which at 2,000 tables is both slower than
# the inference it is meant to inform and a bigger read than a reconnaissance
# pass has any business taking.
TIER2_TABLE_SAMPLE = 200
TIER2_VALUE_SAMPLE = 500


def _dense_sequence(values: list) -> bool:
    """Is this value set just the integers counted from 0 or 1?

    Surrogate keys usually are, and two tables that both count from one hold
    identical value sets while sharing no meaning at all.
    """
    try:
        nums = sorted(int(v) for v in values)
    except (TypeError, ValueError):
        return False
    if len(nums) < 2:
        return False
    return nums[0] in (0, 1) and nums[-1] - nums[0] == len(nums) - 1


def tier2(src, p: Probe, table_sample: int = TIER2_TABLE_SAMPLE) -> Probe:
    """Do several tables hold the SAME key values? Reads rows; opt-in.

    This is the one question the catalogue cannot answer and the one that
    decides whether naming matters at all. A warehouse with ugly names and no
    duplicate key sets is an easy case; a warehouse with duplicates and no
    convention is the case nothing in the engine can resolve.

    Identity is by sampled value set, not by name, so it sees a staging copy
    whatever it is called -- which is the entire point.

    Two rules decide what counts, and both were learned by running this on
    real warehouses rather than on the fixtures it was written against:

    1. **The key must be UNIQUE in every table of the pair.** A first version
       asked only whether tables shared key values, and on a GTFS feed it
       flagged `routes.route_id == trips.route_id` -- which is a foreign key,
       not an ambiguity. Every FK child shares values with its parent by
       definition, so sharing is normal in any normalised schema. What is
       dangerous is several tables that could each be the PARENT, and that
       means the key identifies rows in all of them.

    2. **A dense integer sequence is not evidence.** AdventureWorks numbers
       its surrogate keys 1..N, so `DimProduct` at 1..606 and an unrelated
       dimension at 1..606 hash identically while sharing nothing. That
       produced "24% of tables share a key set" on a clean star schema. Two
       tables counting from one is arithmetic, not a finding.
    """
    import hashlib

    tables = src.list_tables()
    step = max(1, len(tables) // table_sample)
    sampled = tables[::step][:table_sample]

    buckets: dict[str, int] = {}
    looked = 0
    for t in sampled:
        keyish = [c for c in t.columns if _KEYISH.search(c.name)]
        if not keyish:
            continue
        col = keyish[0].name
        try:
            counts = src.query(
                f'SELECT COUNT("{col}"), COUNT(DISTINCT "{col}") '
                f'FROM "{t.schema}"."{t.name}"'
            )
            rows = src.query(
                f'SELECT DISTINCT "{col}" FROM "{t.schema}"."{t.name}" '
                f"WHERE \"{col}\" IS NOT NULL "
                f"ORDER BY 1{row_cap(src, TIER2_VALUE_SAMPLE)}"
            )
        except Exception:
            continue                      # a view we cannot read is not a finding
        looked += 1
        if not rows or not counts:
            continue
        n, distinct = counts[0]
        if not n or distinct != n:
            continue                      # rule 1: not a candidate parent here
        if _dense_sequence([r[0] for r in rows]):
            continue                      # rule 2: counting from one is not evidence
        digest = hashlib.sha256(
            "|".join(str(r[0]) for r in rows).encode()
        ).hexdigest()
        buckets[digest] = buckets.get(digest, 0) + 1

    shared = sum(n for n in buckets.values() if n > 1)
    p.tier2_ran = True
    p.sampled_tables = looked
    p.shared_key_set_ratio = shared / max(1, looked)
    if p.shared_key_set_ratio >= 0.2:
        p.notes.append(
            f"{p.shared_key_set_ratio:.0%} of sampled tables share a key set with "
            "another table. Inclusion testing cannot tell those apart, so the "
            "join target is decided by names or by an ETL manifest -- whichever "
            "this warehouse has."
        )
    return p


def render(p: Probe) -> str:
    """The report a person reads. Says what is missing as plainly as what is not."""
    pct = f"{p.stage_prefix_ratio:.0%}"
    out = [
        "# Probe",
        "",
        f"{p.tables} tables, {p.columns} columns, {p.schemas} schema(s)",
        "",
        "## Evidence",
        f"- ETL manifest              : {'yes' if p.has_manifest else 'NO'}",
        f"- declared primary keys     : {p.declared_pks or 'NONE'}",
        f"- declared foreign keys     : {p.declared_fks or 'NONE'}"
        + ("   <- the decisive one" if p.declared_fks else "   <- none, so joins are inferred"),
        f"- tables with a stage prefix: {pct} ({p.distinct_stage_prefixes} distinct)",
        f"- key-like columns          : {p.key_like_columns}",
    ]
    if p.tier2_ran:
        out.append(f"- tables sharing a key set  : {p.shared_key_set_ratio:.0%} "
                   f"(sampled {p.sampled_tables})")
    else:
        out.append("- tables sharing a key set  : not measured (tier 2 is opt-in)")
    out += [
        "",
        f"## Evidence grade: {p.grade.upper()}"
        + ("  (PROVISIONAL — tier 2 not run)"
           if p.grade == "weak" and not p.tier2_ran else ""),
        f"Confidence will be capped at {p.confidence_cap:.2f} for inferences that "
        "depend on disambiguation.",
    ]
    for n in p.notes:
        out += ["", n]
    return "\n".join(out)
