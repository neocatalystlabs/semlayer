# SPDX-License-Identifier: FSL-1.1-ALv2
"""Two-stage validation for semantic layer documents.

Stage 1: JSON Schema validation against the spec version declared in the document.
Stage 2: reference resolution — every table/column/hierarchy/metric reference must
resolve within the document (SPEC.md §4), plus contract lints the schema can't express.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema
import yaml

# packaged copy (works in installed wheels); the repo-root spec/ is the
# public-facing canonical source — a test asserts the two never drift
_SPEC_DIR = Path(__file__).resolve().parent / "spec"


@dataclass
class ValidationResult:
    """Accumulated errors/warnings from validating one semantic layer document."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """A document is valid iff it produced no errors (warnings are non-blocking)."""
        return not self.errors


def _load_schema() -> dict:
    """Load the JSON Schema for the current spec version from disk."""
    with open(_SPEC_DIR / "semantic-layer.schema.json") as f:
        return json.load(f)


# PyYAML's pure-Python SafeLoader parsed a 6 MB layer (2,000 tables) in
# 44 s; the C loader, same grammar and same result, in 7 s. Every command
# that opens a layer went through the slow one, so a 42 MB layer (15,000
# tables) took about five minutes to open. The C extension ships in the
# PyYAML wheels for every platform we run on; the fallback is for a source
# build without libyaml.
_SAFE_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def load_yaml(text: str) -> dict:
    """yaml.safe_load, with the C loader when PyYAML has one."""
    return yaml.load(text, Loader=_SAFE_LOADER)  # a safe loader, C-accelerated


def load_document(path: str | Path) -> dict:
    """Parse a semantic layer YAML document into a plain dict."""
    return load_yaml(Path(path).read_text())


@dataclass
class _Ctx:
    """Reference indexes + the result collector, shared by all per-section checks.

    Built once per document so each check function stays a flat loop over its own
    section instead of re-deriving tables/columns/hierarchies/metrics locally.
    """

    sl: dict
    tables: dict
    columns: dict
    hierarchies: dict
    metrics: dict
    result: ValidationResult

    def check_table(self, ref: str, ctx: str) -> None:
        """Record an error if `ref` is not a declared table name."""
        if ref not in self.tables:
            self.result.errors.append(f"ref: {ctx}: unknown table '{ref}'")

    def check_column(self, ref: str, ctx: str) -> None:
        """Record an error if `ref` is not a declared 'table.column' reference."""
        if ref not in self.columns:
            self.result.errors.append(
                f"ref: {ctx}: unknown column reference '{ref}' (expected 'table.column')"
            )

    def check_dimension(self, ref: str, ctx: str) -> None:
        """A metric dimension: 'table.column', 'hierarchy.level', or 'hierarchy.*'."""
        if ref in self.columns:
            return
        head, _, tail = ref.partition(".")
        if head in self.hierarchies:
            h = self.hierarchies[head]
            if tail == "*":
                return
            level_names = {lv["name"] for lv in h.get("levels", [])}
            aliases = set(h.get("level_aliases", {}))
            if tail in level_names or tail in aliases:
                return
            self.result.errors.append(
                f"ref: {ctx}: '{tail}' is not a declared level of hierarchy '{head}' "
                f"(closed vocabulary; declared: {sorted(level_names)})"
            )
            return
        self.result.errors.append(
            f"ref: {ctx}: '{ref}' resolves to neither a column nor a hierarchy level"
        )

    def check_any_ref(self, ref: str, ctx: str) -> None:
        """A deprecation replacement may target any table/column/metric/hierarchy."""
        known = (
            self.tables.keys() | self.columns.keys()
            | self.metrics.keys() | self.hierarchies.keys()
        )
        if ref in known:
            return
        self.result.errors.append(f"ref: {ctx}: deprecation replacement '{ref}' does not resolve")


def _check_foreign_keys(c: _Ctx) -> None:
    for cref, col in c.columns.items():
        fk = col.get("foreign_key")
        if fk:
            c.check_column(fk["references"], f"column {cref} foreign_key")


def _check_relationships(c: _Ctx) -> None:
    for rel in c.sl.get("relationships", []):
        if rel.get("lifecycle") == "orphaned":
            continue        # an end that no longer exists is what orphaned records (SPEC.md)
        for side in ("from", "to"):
            tref = rel[side]["table"]
            c.check_table(tref, f"relationship {rel.get('name')} {side}")
            if tref in c.tables:
                for col in rel[side]["columns"]:
                    c.check_column(f"{tref}.{col}", f"relationship {rel.get('name')} {side}")


def _check_hierarchies(c: _Ctx) -> None:
    for hname, h in c.hierarchies.items():
        dt = h.get("dimension_table")
        if dt:
            c.check_table(dt, f"hierarchy {hname}")
        for lv in h.get("levels", []):
            col = lv.get("column")
            if col and dt and dt in c.tables:
                c.check_column(f"{dt}.{col}", f"hierarchy {hname} level {lv['name']}")
        for alias, target in h.get("level_aliases", {}).items():
            if target not in {lv["name"] for lv in h.get("levels", [])}:
                c.result.errors.append(
                    f"ref: hierarchy {hname}: alias '{alias}' targets unknown level '{target}'"
                )
        if h.get("kind") == "time" and not h.get("base_column"):
            c.result.warnings.append(f"hierarchy {hname}: time hierarchy without base_column")


def _check_aggregates(c: _Ctx) -> None:
    for agg in c.sl.get("aggregate_tables", []):
        c.check_table(agg["table"], f"aggregate_table {agg['table']}")
        c.check_table(agg["aggregates"], f"aggregate_table {agg['table']} base")
        is_heuristic = agg.get("mapping_source") == "heuristic"
        is_verified = agg.get("routing", {}).get("status") == "verified"
        if is_heuristic and is_verified:
            c.result.errors.append(
                f"contract: aggregate_table {agg['table']}: heuristic mappings cannot have "
                f"verified routing (SPEC.md §2.7)"
            )


def _check_simple_metric(c: _Ctx, mname: str, m: dict) -> None:
    if not m.get("measure"):
        c.result.errors.append(f"metric {mname}: simple metric requires 'measure'")
        return
    c.check_column(m["measure"], f"metric {mname} measure")
    col = c.columns.get(m["measure"])
    if col is not None and "aggregations" not in col:
        c.result.errors.append(
            f"contract: metric {mname}: measure '{m['measure']}' has no aggregations "
            f"block (metrics may only reference modeled measures)"
        )


def _check_ratio_metric(c: _Ctx, mname: str, m: dict) -> None:
    for part in ("numerator", "denominator"):
        if not m.get(part):
            c.result.errors.append(f"metric {mname}: ratio metric requires '{part}'")
        elif m[part] not in c.metrics and m[part] not in c.columns:
            c.result.errors.append(
                f"ref: metric {mname}: {part} '{m[part]}' is neither a declared metric "
                f"nor a modeled 'table.column' measure"
            )


def _check_derived_metric(c: _Ctx, mname: str, m: dict) -> None:
    for im in m.get("input_metrics", []):
        if im not in c.metrics:
            c.result.errors.append(f"ref: metric {mname}: input metric '{im}' is not declared")


_METRIC_CHECKS = {
    "simple": _check_simple_metric,
    "ratio": _check_ratio_metric,
    "derived": _check_derived_metric,
}


def _check_metrics(c: _Ctx) -> None:
    for mname, m in c.metrics.items():
        check = _METRIC_CHECKS.get(m["type"])
        if check is not None:
            check(c, mname, m)
        for d in m.get("dimensions", []):
            c.check_dimension(d, f"metric {mname} dimension")


def _check_deprecations(c: _Ctx) -> None:
    for tname, t in c.tables.items():
        if t.get("lifecycle") == "deprecated" and t.get("deprecation", {}).get("replacement"):
            c.check_any_ref(t["deprecation"]["replacement"], f"table {tname}")
        for col in t.get("columns", []):
            replacement = col.get("deprecation", {}).get("replacement")
            if col.get("lifecycle") == "deprecated" and replacement:
                c.check_any_ref(replacement, f"column {tname}.{col['name']}")
            # contract: metric filters cannot cite llm_guess-only enum decodes — checked at
            # compile time; here we lint that guessed decodes exist alongside a warning.
            for ev in col.get("enum_values", []) or []:
                if ev.get("decode_source") == "llm_guess":
                    c.result.warnings.append(
                        f"column {tname}.{col['name']}: enum value '{ev['value']}' decode is an "
                        f"LLM guess — review-gated; metric filters may not reference it "
                        f"(SPEC.md §2.8)"
                    )


def _check_repo_knowledge(c: _Ctx) -> None:
    rk = c.sl.get("repo_knowledge", {})
    for entry in rk.get("routing", []):
        for tref in entry.get("use", []):
            c.check_table(tref, f"routing intent '{entry['intent']}' use")
        for av in entry.get("avoid", []):
            c.check_table(av["table"], f"routing intent '{entry['intent']}' avoid")
    for dom in rk.get("domains", []):
        for tref in dom.get("tables", []):
            c.check_table(tref, f"domain '{dom['name']}'")


def validate_document(doc: dict) -> ValidationResult:
    """Validate a parsed semantic layer document against schema + reference contracts."""
    result = ValidationResult()
    schema = _load_schema()
    validator = jsonschema.Draft202012Validator(schema)
    for err in sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path)):
        loc = "/".join(str(p) for p in err.absolute_path) or "<root>"
        result.errors.append(f"schema: {loc}: {err.message}")
    if result.errors:
        return result  # reference checks assume a structurally valid doc

    sl = doc["semantic_layer"]
    tables = {t["name"]: t for t in sl.get("tables", [])}
    columns = {
        f"{tname}.{col['name']}": col
        for tname, t in tables.items()
        for col in t.get("columns", [])
    }
    hierarchies = {h["name"]: h for h in sl.get("hierarchies", [])}
    metrics = {m["name"]: m for m in sl.get("metrics", [])}
    c = _Ctx(
        sl=sl, tables=tables, columns=columns,
        hierarchies=hierarchies, metrics=metrics, result=result,
    )

    _check_foreign_keys(c)
    _check_relationships(c)
    _check_hierarchies(c)
    _check_aggregates(c)
    _check_metrics(c)
    _check_deprecations(c)
    _check_repo_knowledge(c)

    return result


def _validate_hub(path: str | Path) -> ValidationResult:
    """Resolve the hub with its members and validate the set."""
    from semlayer.setdoc import SetError, load_set, set_checks
    try:
        rs = load_set(path)
    except SetError as e:
        r = ValidationResult()
        r.errors.append(f"set: {e}")
        return r
    result = validate_document(rs.doc)
    result.errors.extend(set_checks(rs))
    return result


def validate_file(path: str | Path) -> ValidationResult:
    """Load and validate a document from a YAML file path; a hub is validated as a set.

    A hub (a document with `members`) is resolved with its members into one
    document first, so cross-member references are checked, then the set's
    own rules are applied (names, shared sources, warehouses). A member on
    its own validates as any document does: it knows nothing of the set.
    """
    from semlayer.setdoc import is_hub
    try:
        doc = load_document(path)
    except yaml.YAMLError as e:
        return ValidationResult(errors=[f"yaml: {e}"])
    except FileNotFoundError:
        # Reported, not raised: `validate` takes several paths and should
        # say which one is missing and carry on, the same as it does for a
        # file that parses but fails. Previously this escaped as a
        # traceback, so a mistyped filename looked like a crash.
        return ValidationResult(errors=[f"file not found: {path}"])
    except IsADirectoryError:
        return ValidationResult(errors=[f"not a file: {path}"])
    except PermissionError:
        return ValidationResult(errors=[f"cannot read: {path}"])
    if not isinstance(doc, dict):
        return ValidationResult(errors=["document is not a mapping"])
    return _validate_hub(path) if is_hub(doc) else validate_document(doc)
