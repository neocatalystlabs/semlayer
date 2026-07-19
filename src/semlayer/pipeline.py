"""Full pipeline orchestration for `semlayer infer`."""

from __future__ import annotations

import logging
import time

from semlayer.describe import describe_source
from semlayer.enrich import enrich_source
from semlayer.link import link_source
from semlayer.profile.run import profile_with_stats

logger = logging.getLogger(__name__)


def infer(source, llm=None, no_sample_egress: bool = False) -> dict:
    """Run Profile -> Link -> Describe (if llm given) -> Enrich and return the document."""
    doc, _report = infer_with_report(source, llm=llm, no_sample_egress=no_sample_egress)
    return doc


def infer_with_report(source, llm=None, no_sample_egress: bool = False) -> tuple[dict, dict]:
    """`infer` plus the per-run observability report (BETA Q2).

    The report is the CLI's metrics surface: stage wall-clock, table/column
    counts, and LLM spend — content-free, safe to attach to beta feedback.
    """
    report: dict = {"stages": {}, "counts": {}}

    def _timed(stage: str, fn):
        t0 = time.perf_counter()
        result = fn()
        dt = round(time.perf_counter() - t0, 2)
        report["stages"][stage] = dt
        logger.info("stage %s done in %.1fs", stage, dt)
        return result

    doc, stats = _timed("profile", lambda: profile_with_stats(
        source, no_sample_values=no_sample_egress, llm=llm))
    _timed("link", lambda: link_source(source, doc, stats, llm=llm))
    if llm is not None:
        _timed("describe", lambda: describe_source(doc, stats, llm, no_samples=no_sample_egress))
    _timed("enrich", lambda: enrich_source(source, doc, stats))
    doc.pop("_link_audit", None)
    sl = doc["semantic_layer"]
    report["counts"] = {
        "tables": len(sl["tables"]),
        "columns": sum(len(t["columns"]) for t in sl["tables"]),
        "relationships": len(sl.get("relationships", [])),
        "metrics": len(sl.get("metrics", [])),
    }
    if llm is not None:
        report["llm"] = {"model": llm.model, "spend": llm.spend_summary}
    return doc, report


def open_source_from_uri(uri: str):
    """duckdb:<path> | snowflake | bigquery (env-configured)."""
    if uri.startswith("duckdb:"):
        import duckdb

        from semlayer.source import DuckDBSource
        return DuckDBSource(duckdb.connect(uri.split(":", 1)[1]))
    if uri == "snowflake":
        from semlayer.connectors.snowflake import SnowflakeSource
        return SnowflakeSource.from_env(role="reader")
    if uri == "bigquery":
        from semlayer.connectors.bigquery import BigQuerySource
        return BigQuerySource.from_env(role="reader")
    raise ValueError(f"unsupported source uri: {uri}")
