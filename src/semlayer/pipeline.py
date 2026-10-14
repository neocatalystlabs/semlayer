"""Full pipeline orchestration for `semlayer infer`."""

from __future__ import annotations

import logging
import time

from semlayer.describe import describe_source
from semlayer.enrich import enrich_source
from semlayer.link import link_source
from semlayer.profile.run import _scale_warning, profile_with_stats

logger = logging.getLogger(__name__)


# PLR0913: eight orthogonal knobs is the limit, and this is at it. The next one
# should become an InferOptions dataclass rather than a ninth parameter -- the
# lint is flagging a real smell, not a false positive.
def infer(source, llm=None, no_sample_egress: bool = False, context=None,  # noqa: PLR0913
          only: set[str] | None = None, lineage=None,
          use_declared: bool = True, state=None) -> dict:
    """Run Profile -> Link -> Describe (if llm given) -> Enrich and return the document."""
    doc, _report = infer_with_report(source, llm=llm, no_sample_egress=no_sample_egress,
                                     context=context, only=only, lineage=lineage,
                                     use_declared=use_declared, state=state)
    return doc


def infer_with_report(source, llm=None, no_sample_egress: bool = False,  # noqa: PLR0913
                      context=None, only: set[str] | None = None,
                      lineage=None, use_declared: bool = True,
                      state=None) -> tuple[dict, dict]:
    """`infer` plus the per-run observability report (BETA Q2).

    The report is the CLI's metrics surface: stage wall-clock, table/column
    counts, and LLM spend — content-free, safe to attach to beta feedback.

    `context` (v0.2): a semlayer.context.ContextBundle of knowledge-doc priors.
    `lineage`: a semlayer.lineage.LineageBundle read from an ETL manifest. Link
    consults it only to break ties statistics cannot.
    Prose excerpts ride into typing/describe evidence; CSV dictionaries seed
    descriptions; doc decode claims reconcile post-enrich (conflicts on
    contradiction — priors, never truth).
    """
    report: dict = {"stages": {}, "counts": {}}
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
        state=state, lineage=lineage))
    if context is not None and context.dictionary:
        from semlayer.context import apply_dictionary
        apply_dictionary(doc, context.dictionary)
    _timed("link", lambda: link_source(source, doc, stats, llm=llm, lineage=lineage,
                                       use_declared=use_declared))
    if llm is not None:
        _timed("describe", lambda: describe_source(
            doc, stats, llm, no_samples=no_sample_egress, context=context))
    _timed("enrich", lambda: enrich_source(source, doc, stats))
    if context is not None and context.chunks:
        from semlayer.context import apply_doc_decodes, apply_doc_ratios
        apply_doc_decodes(doc, context.chunks)
        apply_doc_ratios(doc, context.chunks)
    part = doc.pop("_partition_audit", None)
    if part:
        report["partitions"] = part
    doc.pop("_link_audit", None)
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
    raise ValueError(f"unsupported source uri: {uri}")
