"""Heuristic semantic typing + entity-role classification (Stage 1, rule tier).

Deterministic name/pattern/statistics rules — the pre-LLM baseline and the
`--no-sample-egress` floor. Each classification carries confidence + the
signal that produced it. The LLM tier (M3+) escalates low-confidence columns;
these rules never call out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from semlayer.profile.stats import ColumnStats


@dataclass
class Typing:
    """One column's inferred semantic type + entity role, with confidence and provenance."""

    semantic_type: str
    entity_role: str
    confidence: float
    signal: str
    detail: str


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")
_URL_RE = re.compile(r"^https?://")
_PHONE_RE = re.compile(r"^\+?[\d\s().-]{7,20}$")

_NAME_RULES: list[tuple[re.Pattern, str, float]] = [
    (re.compile(r"(^|_)(email|e_mail)($|_)"), "pii_email", 0.9),
    (re.compile(r"(^|_)(phone|mobile|fax)($|_)"), "pii_phone", 0.85),
    (re.compile(r"(^|_)(ssn|tax_id|national_id|passport)($|_)"), "pii_national_id", 0.9),
    (
        re.compile(r"(^|_)(first|last|full|cust|customer|user|contact)_?(nm|name)($|_)"),
        "pii_name", 0.75,
    ),
    (
        re.compile(r"(^|_)(street|address|addr_ln|address_line|addr1|addr2)($|_)"),
        "pii_address", 0.75,
    ),
    (re.compile(r"(^|_)(country|ctry)(_cd|_code)?($|_)"), "geo_country", 0.85),
    (re.compile(r"(^|_)(state|province|region|rgn)(_cd|_code)?($|_)"), "geo_region", 0.8),
    (re.compile(r"(^|_)city($|_)"), "geo_city", 0.85),
    (re.compile(r"(^|_)(zip|postal)(_cd|_code)?($|_)"), "geo_postal", 0.85),
    (re.compile(r"(^|_)(lat|latitude|lon|lng|longitude)($|_)"), "geo_coordinates", 0.85),
    (
        re.compile(
            r"(^|_)(amt|amount|price|cost|total|revenue|rev|sales|paid|balance|bal|mrr|fee"
            r"|charge)($|_)"
        ),
        "monetary_value", 0.8,
    ),
    (re.compile(r"(^|_)(qty|quantity|units|cnt|count|num)($|_)"), "quantity", 0.75),
    (re.compile(r"(^|_)(rate|ratio)($|_)"), "rate", 0.75),
    (re.compile(r"(^|_)(pct|percent|percentage)($|_)"), "percentage", 0.8),
    (re.compile(r"(^|_)(sts|status|state)(_cd|_code)?($|_)"), "status_code", 0.7),
    (
        re.compile(
            r"(^|_)(typ|type|cat|category|class|seg|segment|tier|mthd|method|chnl|channel"
            r"|rsn|reason)(_cd|_code)?($|_)"
        ),
        "code", 0.6,
    ),
    (re.compile(r"(^|_)(is|has|flg|flag|active|enabled|deleted)($|_)"), "flag", 0.7),
    (re.compile(r"(^|_)(url|link|href)($|_)"), "url", 0.85),
    (re.compile(r"(^|_)(desc|description|comment|notes?|txt|text|title)($|_)"), "free_text", 0.7),
    (re.compile(r"(^|_)(nm|name)$"), "name", 0.65),
]

_ID_NAME_RE = re.compile(r"(^|_)(id|key|sk|pk)$|^id($|_)")
_WEAK_ID_NAME_RE = re.compile(r"(^|_)(no|nbr|number)$")
_METADATA_NAME_RE = re.compile(
    r"(^|_)(crt|created|updt|updated|upd|load|etl|ingest|src_sys|source_system|batch|audit)(_|$)|_(at|ts)$"
)
_DATE_NAME_RE = re.compile(r"(^|_)(dt|date)($|_)")


def _is_numeric(sql_type: str) -> bool:
    t = sql_type.upper()
    return any(k in t for k in ("INT", "DECIMAL", "NUMERIC", "DOUBLE", "FLOAT", "REAL", "HUGEINT"))


def _is_temporal(sql_type: str) -> bool:
    t = sql_type.upper()
    return "DATE" in t or "TIME" in t


def _values_match(stats: ColumnStats, pattern: re.Pattern, min_frac: float = 0.8) -> bool:
    vals = [v for v, _ in stats.top_values]
    if not vals:
        return False
    hits = sum(1 for v in vals if pattern.match(v))
    return hits / len(vals) >= min_frac


def _rule_value_patterns(
    stats: ColumnStats, name: str, t: str, no_sample_values: bool
) -> Typing | None:
    # skipped in no-egress-equivalent mode is NOT needed: these run locally;
    # the flag exists for symmetry with tests
    if not no_sample_values and stats.top_values:
        if _values_match(stats, _EMAIL_RE):
            return Typing("pii_email", "dimension", 0.95, "statistic", "value pattern: email")
        if _values_match(stats, _URL_RE):
            return Typing("url", "metadata", 0.9, "statistic", "value pattern: url")
    return None


def _rule_bool(stats: ColumnStats, name: str, t: str, no_sample_values: bool) -> Typing | None:
    if "BOOL" in t:
        return Typing("flag", "dimension", 0.9, "statistic", "boolean type")
    return None


def _rule_temporal(
    stats: ColumnStats, name: str, t: str, no_sample_values: bool
) -> Typing | None:
    if not _is_temporal(t):
        return None
    if _METADATA_NAME_RE.search(name):
        return Typing("timestamp_event", "metadata", 0.75, "naming", "audit/etl timestamp name")
    if "TIMESTAMP" in t:
        return Typing("timestamp_event", "dimension", 0.7, "statistic", "timestamp type")
    return Typing("date", "dimension", 0.8, "statistic", "date type")


def _rule_nested(stats: ColumnStats, name: str, t: str, no_sample_values: bool) -> Typing | None:
    if "STRUCT" in t or "JSON" in t or "MAP" in t or t.endswith("[]"):
        return Typing("json_object", "metadata", 0.85, "statistic", "nested type")
    return None


def _rule_weak_id(stats: ColumnStats, name: str, t: str, no_sample_values: bool) -> Typing | None:
    # weak id suffixes (no/nbr/number) count only when the data looks like ids
    if _WEAK_ID_NAME_RE.search(name) and (stats.is_unique or stats.cardinality_ratio > 0.5):
        role = "primary_key" if stats.is_unique else "foreign_key"
        return Typing(
            "identifier", role, 0.6, "statistic",
            f"number-suffixed and high-cardinality (ratio {stats.cardinality_ratio:.2f})",
        )
    return None


def _rule_id_name(stats: ColumnStats, name: str, t: str, no_sample_values: bool) -> Typing | None:
    if not _ID_NAME_RE.search(name):
        return None
    role = "primary_key" if stats.is_unique else "foreign_key"
    conf = 0.85 if stats.is_unique else 0.7
    if not _is_numeric(t) and stats.cardinality_ratio < 0.01 and stats.n_distinct < 50:
        # id-suffixed but tiny domain: likely a code, not an identifier
        return Typing("code", "dimension", 0.55, "statistic", "id-named but low cardinality")
    return Typing("identifier", role, conf, "naming", f"id-like name, unique={stats.is_unique}")


def _rule_name_table(
    stats: ColumnStats, name: str, t: str, no_sample_values: bool
) -> Typing | None:
    for pattern, stype, conf in _NAME_RULES:
        if pattern.search(name):
            role = _role_for(stype, stats)
            # enum-ish confirmation: coded columns with tiny domains
            if stype in ("status_code", "code") and stats.n_distinct <= 30:
                conf = min(0.9, conf + 0.15)
            return Typing(stype, role, conf, "naming", f"name rule -> {stype}")
    return None


def _rule_metadata_name(
    stats: ColumnStats, name: str, t: str, no_sample_values: bool
) -> Typing | None:
    if _METADATA_NAME_RE.search(name):
        return Typing("unknown", "metadata", 0.6, "naming", "audit/etl name")
    return None


def _rule_numeric_fallback(
    stats: ColumnStats, name: str, t: str, no_sample_values: bool
) -> Typing | None:
    if not _is_numeric(t):
        return None
    if stats.cardinality_ratio > 0.9 and stats.is_unique:
        return Typing("identifier", "primary_key", 0.6, "statistic", "unique numeric")
    if "DECIMAL" in t or "DOUBLE" in t or "FLOAT" in t or "NUMERIC" in t:
        return Typing("monetary_value", "measure", 0.4, "statistic", "decimal fallback")
    if stats.n_distinct <= 30:
        return Typing("code", "dimension", 0.45, "statistic", "small int domain")
    return Typing("quantity", "measure", 0.35, "statistic", "integer fallback")


def _rule_text_fallback(
    stats: ColumnStats, name: str, t: str, no_sample_values: bool
) -> Typing | None:
    if stats.avg_len is not None and stats.avg_len > 40:
        return Typing("free_text", "metadata", 0.6, "statistic", f"avg length {stats.avg_len:.0f}")
    if stats.n_distinct <= 30 and stats.row_count > 100:
        return Typing("enum", "dimension", 0.6, "statistic", f"{stats.n_distinct} distinct values")
    if _DATE_NAME_RE.search(name):
        return Typing("date", "dimension", 0.5, "naming", "date-like name, non-temporal type")
    return None


# Ordered rule pipeline: first non-None wins. Order encodes precedence
# (e.g. structural type checks before name heuristics before statistical
# fallbacks) and is proven by test_profile's confidence-floor fixtures —
# reordering silently changes classification outcomes.
_RULES = [
    _rule_value_patterns,
    _rule_bool,
    _rule_temporal,
    _rule_nested,
    _rule_weak_id,
    _rule_id_name,
    _rule_name_table,
    _rule_metadata_name,
    _rule_numeric_fallback,
    _rule_text_fallback,
]


def classify(stats: ColumnStats, no_sample_values: bool = False) -> Typing:
    """Infer a column's semantic type + entity role by walking the ordered rule pipeline."""
    name = stats.name.lower()
    t = stats.sql_type.upper()
    for rule in _RULES:
        result = rule(stats, name, t, no_sample_values)
        if result is not None:
            return result
    return Typing("unknown", "dimension", 0.2, "statistic", "no rule matched")


def _role_for(stype: str, stats: ColumnStats) -> str:
    if stype in ("monetary_value", "quantity", "rate", "percentage"):
        return "measure"
    if stype in ("free_text", "url"):
        return "metadata"
    return "dimension"
