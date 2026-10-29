# SPDX-License-Identifier: FSL-1.1-ALv2
"""Full pipeline orchestration for `semlayer infer`."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

from semlayer.describe import describe_source
from semlayer.enrich import enrich_source
from semlayer.errors import SemlayerError, SourceError
from semlayer.link import link_source
from semlayer.profile.run import _scale_warning, profile_with_stats

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InferOptions:
    """How a run behaves, as distinct from what it runs on.

    The things a run operates on -- source, LLM provider, context docs,
    lineage, state store -- stay as parameters. The settings that shape the
    run live here, so the next knob is one field with a default rather than
    four signature edits, and the run report can record what it ran under.
    Defaults reproduce the serial, sample-bearing behaviour every published
    number was measured with.
    """

    no_sample_egress: bool = False
    # Accepts any iterable of names; normalised to a frozenset in __post_init__.
    only: Iterable[str] | None = None
    use_declared: bool = True
    llm_workers: int = 1
    query_workers: int = 1

    def __post_init__(self) -> None:
        if self.only is not None and not isinstance(self.only, frozenset):
            object.__setattr__(self, "only", frozenset(self.only))
        for name in ("llm_workers", "query_workers"):
            n = getattr(self, name)
            if not isinstance(n, int) or isinstance(n, bool) or n < 1:
                raise SemlayerError(f"{name} must be a whole number of at least 1, got {n!r}",
                                    hint="1 is fully serial")

    def as_report(self) -> dict[str, Any]:
        """The options as the run report records them (sets become sorted lists)."""
        d = asdict(self)
        d["only"] = sorted(self.only) if self.only is not None else None
        return d


_SETTING_NAMES = frozenset(InferOptions.__dataclass_fields__)


def _resolve(options: InferOptions | None, settings: dict) -> InferOptions:
    """One InferOptions from either the object or the pre-dataclass keywords.

    The keywords still work so that every existing caller -- tests, bench
    scripts, the examples -- keeps running unchanged. Mixing the two is an
    error rather than a merge, because "which one wins" is not a question a
    caller should have to know the answer to.
    """
    unknown = set(settings) - _SETTING_NAMES
    if unknown:
        raise TypeError(f"infer() got unexpected keyword argument(s): {', '.join(sorted(unknown))}")
    if options is None:
        return InferOptions(**settings)
    if settings:
        raise TypeError("pass either options=InferOptions(...) or the individual settings "
                        f"({', '.join(sorted(settings))}), not both")
    return options


def infer(source, llm=None, options: InferOptions | None = None, *,
          context=None, lineage=None, state=None, **settings) -> dict:
    """Run Profile -> Link -> Describe (if llm given) -> Enrich and return the document."""
    doc, _report = infer_with_report(source, llm=llm, options=options, context=context,
                                     lineage=lineage, state=state, **settings)
    return doc


def infer_with_report(source, llm=None, options: InferOptions | None = None, *,
                      context=None, lineage=None, state=None,
                      **settings) -> tuple[dict, dict]:
    """`infer` plus the per-run observability report (BETA Q2).

    The report is the CLI's metrics surface: stage wall-clock, table/column
    counts, LLM spend and the options the run used — content-free, safe to
    attach to beta feedback.

    `options`: an InferOptions. The individual settings (`no_sample_egress`,
    `only`, `use_declared`, `llm_workers`, `query_workers`) are still accepted
    as keywords for existing callers; pass one form or the other.
    `context` (v0.2): a semlayer.context.ContextBundle of knowledge-doc priors.
    `lineage`: a semlayer.lineage.LineageBundle read from an ETL manifest. Link
    consults it only to break ties statistics cannot.
    `state`: a state store for partition-wise incremental profiling.
    Prose excerpts ride into typing/describe evidence; CSV dictionaries seed
    descriptions; doc decode claims reconcile post-enrich (conflicts on
    contradiction — priors, never truth).
    """
    opt = _resolve(options, settings)
    no_sample_egress = opt.no_sample_egress
    only = set(opt.only) if opt.only is not None else None
    use_declared, llm_workers, query_workers = (
        opt.use_declared, opt.llm_workers, opt.query_workers)
    report: dict = {"stages": {}, "counts": {}, "options": opt.as_report()}
    # Which privileges produced this layer. A minimal-grant claim is about the
    # role actually in effect, so the run must say what that was rather than
    # leaving it to whatever the account's DEFAULT_ROLE happened to be.
    session_role = getattr(source, "session_role", None)
    if session_role:
        report["access"] = {"session_role": session_role}

    def _timed(stage: str, fn):
        t0 = time.perf_counter()
        result = fn()
        dt = round(time.perf_counter() - t0, 2)
        report["stages"][stage] = dt
        logger.info("stage %s done in %.1fs", stage, dt)
        return result

    doc, stats = _timed("profile", lambda: profile_with_stats(
        source, no_sample_values=no_sample_egress, llm=llm, context=context, only=only,
        state=state, lineage=lineage, llm_workers=llm_workers))
    if context is not None and context.dictionary:
        from semlayer.context import apply_dictionary
        apply_dictionary(doc, context.dictionary)
    _timed("link", lambda: link_source(source, doc, stats, llm=llm, lineage=lineage,
                                       use_declared=use_declared,
                                       query_workers=query_workers,
                                       llm_workers=llm_workers))
    if llm is not None:
        _timed("describe", lambda: describe_source(
            doc, stats, llm, no_samples=no_sample_egress, context=context,
            llm_workers=llm_workers))
    _timed("enrich", lambda: enrich_source(source, doc, stats))
    if context is not None and context.chunks:
        from semlayer.context import apply_doc_decodes, apply_doc_ratios
        apply_doc_decodes(doc, context.chunks)
        apply_doc_ratios(doc, context.chunks)
    part = doc.pop("_partition_audit", None)
    if part:
        report["partitions"] = part
    doc.pop("_link_audit", None)
    from semlayer.link.candidates import last_timing
    if last_timing():
        report["link"] = last_timing()
    sl = doc["semantic_layer"]
    report["counts"] = {
        "tables": len(sl["tables"]),
        "columns": sum(len(t["columns"]) for t in sl["tables"]),
        "relationships": len(sl.get("relationships", [])),
        "metrics": len(sl.get("metrics", [])),
    }
    # Computed here rather than stashed in the document: the root carries
    # additionalProperties: false, so an extra key breaks spec conformance for
    # anyone validating profile output -- which is how 11 tests failed when this
    # was written into the doc. The pipeline already holds the counts.
    warn = _scale_warning(report["counts"]["tables"], report["counts"]["columns"])
    if warn:
        report["scale_warning"] = warn
        logger.warning("%s", warn)
    if llm is not None:
        report["llm"] = {"model": llm.model, "spend": llm.spend_summary}
    return doc, report


def open_source_from_uri(uri: str):
    """duckdb:<path> | postgresql://... | snowflake | bigquery | postgres (env-configured)."""
    if uri.startswith("duckdb:"):
        import duckdb

        from semlayer.source import DuckDBSource
        return DuckDBSource(duckdb.connect(uri.split(":", 1)[1]))
    if uri == "snowflake":
        from semlayer.connectors.snowflake import SnowflakeSource
        return SnowflakeSource.from_env(persona="reader")
    if uri == "bigquery":
        from semlayer.connectors.bigquery import BigQuerySource
        return BigQuerySource.from_env(persona="reader")
    # Postgres accepts a DSN inline as well as via the environment: unlike the
    # cloud warehouses, a connection string is the normal way people hold one.
    if uri.startswith(("postgresql://", "postgres://")):
        from semlayer.connectors.postgres import PostgresSource
        return PostgresSource.from_dsn(uri)
    if uri == "postgres":
        from semlayer.connectors.postgres import PostgresSource
        return PostgresSource.from_env(persona="reader")
    # A SourceError rather than a ValueError: this is a user typing a URI,
    # not a broken invariant, and it should read as advice rather than as a
    # crash. It is also what lets telemetry record `connection` instead of
    # `internal`, which should mean OUR bug.
    raise SourceError(
        f"unsupported source uri: {uri}",
        hint="expected one of: snowflake, bigquery, duckdb:<path>, "
             "postgres, postgresql://<dsn>")
