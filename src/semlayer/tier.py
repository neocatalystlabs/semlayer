# SPDX-License-Identifier: FSL-1.1-ALv2
"""Which tier of the warehouse a table belongs to, from its name and columns.

Most of a large warehouse is not the part anyone models against. A 15,000-
table synthetic estate in a realistic mix was 8% core star schemas, 22%
staging copies, 45% raw shards and change feeds, 25% scratch. The engine had
no notion of any of that: it profiled, linked, reconciled and queued for
review every raw shard and every backup as if it were a business table.
Measured consequences at 2,000 tables: 76% of the review queue sat on raw
and scratch tables; every one of them received a full foreign-key budget;
and the raw copies competed as PARENTS, so 69% of statistical foreign keys
pointed at the wrong copy of a shared key.

The tier is a claim like any other: a value, a confidence, and the signals
that produced it. It is inferred from two things that need no query:

  1. The NAME. Prefix and suffix vocabularies for each tier, the way
     warehouses actually name things (raw_, landing_, cdc_, _20250101;
     tmp_, bak_, _old, _copy; stg_, int__; fct_, dim_, agg_). A table
     that matches nothing is CORE at low confidence: an unprefixed name is
     the canonical object, and the cost of wrongly demoting a real table
     is far higher than the cost of fully modelling a raw one.
  2. ETL METADATA COLUMNS (etl_batch_id, record_hash, _fivetran_synced, a
     CDC op/changed_at pair). Two or more mark a landing or change-feed
     table; one alone is not evidence, because staging and core tables
     carry a load timestamp too.

Precedence when signals disagree: the name wins. A `stg_` table with
`etl_batch_id` is staging, because the convention is a declaration and the
column is a habit. Version suffixes (`_v2`) carry no tier signal: they occur
in every tier.

What this is NOT: a judgement about the data. It predicts how the engine
should spend effort, and it is wrong in the direction of modelling too much,
never too little. Scored against gold tiers in tests/test_tier.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TIERS = ("core", "staging", "raw", "scratch")

# Name rules, in precedence order. Each is (tier, confidence, kind, regex).
# What is NOT here, and why: `_snapshot`, `_hist`, `_history`, `_log` and
# `_source` as suffixes, because they name real objects (`cust_snapshot` is an
# SCD2 table, `cdm_source` is OMOP, `etl_log` is operational and already typed
# so); `source_` as a prefix (`source_to_concept_map` is OMOP vocabulary);
# `_test` as a suffix (application schemas have test models). Each of those
# fired on a real fixture when it was tried. The prefix forms (`snapshot_`,
# `hist_`, `audit_`) stay: a prefix is a declaration, a suffix a description.
_SCRATCH_PREFIX = (r"^(?:tmp|temp|scratch|wrk|work|test|testing|dev|bak|backup|bkp|junk|"
                   r"sandbox|sbx|old|copy|delete|del|zz+|xx+)[_-]")
_SCRATCH_SUFFIX = (r"[_-](?:tmp|temp|scratch|wrk|work|bak|backup|bkp|copy|old|draft|"
                   r"delete|deleteme|donotuse|do_not_use)$")
_STAGING_PREFIX = (r"^(?:stg|stage|staging|base|int|intermediate|prep|prepared|clean|"
                   r"cleansed|conformed|xform|transform)[_-]")
_CORE_PREFIX = (r"^(?:fct|fact|dim|dm|mrt|mart|agg|aggr|sum|summary|rpt|report|rep|kpi|"
                r"metric|metrics|mv|cube)[_-]")
_RAW_PREFIX = (r"^(?:raw|ingest|ingestion|src|landing|land|lnd|ext|external|cdc|snap|"
               r"snapshot|hist|history|audit|archive|arch)[_-]")
_RAW_SUFFIX = r"[_-](?:raw|landing|cdc|archive)$"
# a date shard: orders_20250101, events_2025_01, ga_sessions_20161125_2
_DATE_SHARD = r"[_-](?:\d{8}|\d{6}|\d{4}[_-]\d{2}(?:[_-]\d{2})?)(?:[_-]\d+)?$"
_STAGING_SUFFIX = r"[_-](?:stg|stage|staging|int|prep|clean)$"
_CORE_SUFFIX = r"[_-](?:fact|facts|dim|dims|mart|agg|aggr|summary|mv|kpi|cube|rollup)$"

# Order matters. Scratch first: a backup of anything is scratch. Then the
# declared layers by prefix, which beat a raw-looking suffix (`fct_x_hist`
# is a core history fact, `int__x_hist` is staging). Then raw by prefix,
# suffix or date shard. Then the weaker suffix forms of staging and core.
# the whole name is a scratch word: `junk`, `tmp`, `test_3`, `scratch`
_SCRATCH_WHOLE = r"^(?:tmp|temp|scratch|junk|test|testing|sandbox|backup|bak|dummy|foo|bar|baz)$"

_NAME_RULES: tuple[tuple[str, float, str, re.Pattern[str]], ...] = tuple(
    (tier, conf, kind, re.compile(rx, re.IGNORECASE)) for tier, conf, kind, rx in (
        ("scratch", 0.9, "prefix", _SCRATCH_PREFIX),
        ("scratch", 0.9, "word", _SCRATCH_WHOLE),
        ("scratch", 0.85, "suffix", _SCRATCH_SUFFIX),
        ("staging", 0.9, "prefix", _STAGING_PREFIX),
        ("core", 0.9, "prefix", _CORE_PREFIX),
        ("raw", 0.9, "prefix", _RAW_PREFIX),
        ("raw", 0.85, "suffix", _RAW_SUFFIX),
        ("raw", 0.8, "date shard", _DATE_SHARD),
        ("staging", 0.85, "suffix", _STAGING_SUFFIX),
        ("core", 0.85, "suffix", _CORE_SUFFIX),
    ))
# `renewal_draft_11`, `tmp_orders_3`: a small trailing counter is not part of
# the name for suffix purposes. Date shards are matched on the full name.
_COUNTER = re.compile(r"[_-]\d{1,3}$")

# Columns that ETL tooling writes, not the business. Two or more mark a
# landing or change-feed table. Lowercased, exact.
ETL_METADATA_COLUMNS = frozenset({
    "etl_batch_id", "etl_batch", "batch_id", "dw_inserted_at", "dw_updated_at",
    "dw_load_ts", "dw_loaded_at", "src_system", "source_system", "record_hash",
    "row_hash", "hash_diff", "hashdiff", "_loaded_at", "loaded_at", "load_ts",
    "load_timestamp", "ingested_at", "ingestion_ts", "_ingested_at", "_extracted_at",
    "_fivetran_synced", "_fivetran_deleted", "_fivetran_id", "_airbyte_emitted_at",
    "_airbyte_ab_id", "_airbyte_raw_id", "_airbyte_extracted_at", "_sdc_extracted_at",
    "_sdc_batched_at", "_sdc_received_at", "_sdc_sequence", "_sdc_table_version",
    "_sling_loaded_at", "_dlt_load_id", "_dlt_id", "__hevo__loaded_at",
    "_etl_loaded_at", "etl_loaded_at", "etl_updated_at", "cdc_op", "op", "_op",
    "changed_at", "change_ts", "cdc_ts", "_cdc_timestamp", "lsn", "_lsn",
})
_ETL_MIN_COLUMNS = 2


@dataclass
class TierClaim:
    """A table's tier, how sure the engine is, and the signals that said so."""

    tier: str
    confidence: float
    signals: list[str] = field(default_factory=list)

    def provenance(self) -> list[dict]:
        """The signals as provenance entries for the layer document."""
        return [{"signal": "naming" if s.startswith("name") else "statistic", "detail": s}
                for s in self.signals]


def _bare(table: str) -> str:
    return table.rsplit(".", 1)[-1].lower()


def _name_tier(name: str) -> tuple[str, float, str] | None:
    """(tier, confidence, signal) from the name rules, or None if nothing fires."""
    trimmed = _COUNTER.sub("", name)
    for tier, conf, kind, rx in _NAME_RULES:
        target = name if kind == "date shard" else trimmed
        m = rx.search(target) if kind != "prefix" else rx.match(target)
        if m:
            return tier, conf, f"name {kind}: {m.group(0)}"
    return None


def infer_tier(table: str, columns: list[str] | None = None) -> TierClaim:
    """Tier of `table` from its name and, if given, its column names.

    The name decides when it says anything. ETL metadata columns raise a raw
    verdict's confidence and, with no name signal at all, make a weak raw
    claim on their own. A table with neither is core at 0.5: unprefixed is
    canonical, and under-modelling a real table is the error we refuse.
    """
    name = _bare(table)
    cols = {c.lower() for c in (columns or [])}
    etl = sorted(cols & ETL_METADATA_COLUMNS)
    etl_signal = (f"etl metadata columns: {', '.join(etl[:4])}"
                  if len(etl) >= _ETL_MIN_COLUMNS else "")
    hit = _name_tier(name)
    if hit is not None:
        tier, conf, signal = hit
        signals = [signal]
        if tier == "raw" and etl_signal:
            signals.append(etl_signal)
            conf = min(0.95, conf + 0.05)
        return TierClaim(tier, conf, signals)
    if etl_signal:
        return TierClaim("raw", 0.7, [etl_signal])
    return TierClaim("core", 0.5, ["name: no tier convention; unprefixed tables are core"])


def is_informational(claim: TierClaim, threshold: float = 0.8) -> bool:
    """Should the engine treat this table as informational only?

    Raw and scratch tiers at or above `threshold`. Below it the name hinted
    but did not declare, and the table is modelled in full, because the
    error of under-modelling a real table is the one we refuse to make.
    """
    return claim.tier in ("raw", "scratch") and claim.confidence >= threshold
