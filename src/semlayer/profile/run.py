# SPDX-License-Identifier: FSL-1.1-ALv2
"""Profile stage runner: source -> draft semantic layer document.

Output is a spec-conformant document with lifecycle: inferred and
confidence + provenance on every inferred element (dogfooding the format
from the first stage). Aggregation blocks are attached to measure columns;
PK candidates come from full-column uniqueness + id-naming.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import version

from semlayer.errors import SemlayerError
from semlayer.profile.stats import TableStats, profile_table
from semlayer.profile.typing_rules import ALWAYS_ESCALATE, classify
from semlayer.tier import infer_tier, is_informational

logger = logging.getLogger(__name__)

# Derived, never restated: a hardcoded copy silently drifts from the released
# version, and SPEC 3 rule 4 makes engine version its own drift class — a
# document stamped with the wrong engine hides a re-inference it should trigger.
ENGINE_VERSION = version("semlayer")


ESCALATE_BELOW = 0.7  # tier-1 confidence under this goes to the LLM (if provided)


def _native_comments(source) -> dict:
    """Read the warehouse's own COMMENT text, if the connector exposes any.

    A human comment is better evidence than any naming heuristic we could
    write, and we ignored them entirely while nine separate naming
    vocabularies were failing on schemas we did not author. Optional by
    design: a source without comments returns {} and nothing changes.
    """
    getter = getattr(source, "list_comments", None)
    if getter is None:
        return {}
    try:
        return getter() or {}
    except Exception:   # a catalog we cannot read must not fail the run
        return {}


def _apply_dbt_docs(doc: dict, ts, lineage) -> int:
    """Seed descriptions from the dbt project's own documentation.

    Same standing as a native warehouse COMMENT: a sentence a human wrote beats
    anything we generate. And it is free -- 447 column descriptions sat in the
    dbt_synthea manifest, a file we already open and parse for the DAG, and we
    threw every one of them away.

    Applied AFTER native comments and only where nothing is there yet, which
    keeps the existing precedence rule intact: never overwrite a description a
    human wrote, fill the gaps, flag the conflicts. Where both exist and differ
    they are recorded as a conflict rather than silently resolved -- two humans
    documenting the same column differently is worth a review, not a coin toss.
    """
    if lineage is None:
        return 0
    if not getattr(lineage, "table_descriptions", None) and \
            not getattr(lineage, "column_descriptions", None):
        return 0
    table = ts.table.name
    applied = 0
    td = lineage.table_descriptions.get(table)
    if td:
        if not doc.get("description"):
            doc["description"] = td
            applied += 1
        elif doc["description"].strip() != td.strip():
            doc.setdefault("conflicts", []).append({
                "field": "description",
                "detail": "the warehouse COMMENT and the dbt description differ",
            })
        doc.setdefault("provenance", []).append({
            "signal": "catalog_prior",
            "detail": "table description read from the dbt project",
        })
    for col in doc["columns"]:
        cd = lineage.column_descriptions.get((table, col["name"]))
        if not cd:
            continue
        if not col.get("description"):
            col["description"] = cd[:200]
            applied += 1
        col.setdefault("provenance", []).append({
            "signal": "catalog_prior",
            "detail": "column description read from the dbt project",
        })
    return applied


def _apply_comments(doc: dict, ts, comments: dict) -> int:
    """Seed descriptions from native comments. Returns how many were applied."""
    if not comments:
        return 0
    schema, table = ts.table.schema, ts.table.name
    applied = 0
    tc = comments.get((schema, table, None))
    if tc:
        doc["description"] = tc
        doc.setdefault("provenance", []).append({
            "signal": "catalog_prior",
            "detail": "table COMMENT read from the warehouse catalog",
        })
        applied += 1
    for col in doc["columns"]:
        cc = comments.get((schema, table, col["name"]))
        if not cc:
            continue
        col["description"] = cc[:200]
        col.setdefault("provenance", []).append({
            "signal": "catalog_prior",
            "detail": "column COMMENT read from the warehouse catalog",
        })
        applied += 1
    return applied


def profile_source(source, no_sample_values: bool = False, llm=None,
                   only: set[str] | None = None) -> dict:
    """Run the profile stage and return the draft semantic-layer document (stats discarded)."""
    doc, _ = profile_with_stats(source, no_sample_values=no_sample_values, llm=llm, only=only)
    return doc


def profile_with_stats(source, no_sample_values: bool = False, llm=None, context=None,  # noqa: PLR0913, PLR0917
                       only: set[str] | None = None, state=None, lineage=None,
                       llm_workers: int = 1):
    """Returns (document, stats_by_table) — Link consumes both.

    `context` (v0.2): a semlayer.context.ContextBundle; relevant prose excerpts
    join the typing-escalation evidence as priors.
    `llm_workers`: concurrent typing-escalation calls. One call per table from
    that table's own statistics, so the document is identical at any count.

    `only`: profile just these table names. The filter is applied AFTER
    `list_tables()` so no connector changes: metadata enumeration is one
    information_schema query, while per-column profiling is the expensive part
    and the only thing worth scoping.

    This is the targeted half of incremental re-inference. `drift.py` already
    computes which tables changed (`diff_snapshots`) and what depends on them
    (`blast_radius`); this is what lets that answer be acted on instead of
    re-profiling the warehouse. Merging a subset back INTO an existing layer is
    deliberately not here — it has to interact with drift's orphaning state
    machine, which governs what may disappear.
    """
    tables_meta = source.list_tables()
    comments = _native_comments(source)
    if state is not None:
        tables_meta = _drop_partition_children(source, tables_meta, state)
    n_comments = 0
    if only is not None:
        tables_meta = _apply_only(tables_meta, only)
    # Duplicate copies are skipped unless the caller named tables explicitly:
    # --only IS the scope, and silently dropping one of two tables someone
    # asked for by name would be wrong. 67% of an analytics warehouse's tables
    # are versioned or partitioned copies (ga360 100%, ebi_chembl 94%) against
    # ~0% of an application schema, so this is the largest single saving
    # available and it costs nothing on the schemas where it finds nothing.
    coverage: dict[str, object] = {"tables_seen": len(tables_meta)}
    if only is None:
        tables_meta, skipped = _dedup(tables_meta, source)
        if skipped:
            coverage["skipped"] = skipped

    partitions = _partitions_or_empty(source) if state is not None else {}

    out_tables = []
    stats_by_table: dict = {}
    reused_total = 0
    profiled_total = 0
    pending: list[tuple[TableStats, dict]] = []   # escalation, after the scan
    for t in sorted(tables_meta, key=lambda x: (x.schema, x.name)):
        parts = partitions.get((t.schema, t.name)) if partitions else None
        inc = None
        if parts:
            from semlayer.incremental import apply_cardinality, profile_partitioned
            from semlayer.source import source_identity

            inc = profile_partitioned(source, t, parts, state, source_identity(source))
            ts = inc.stats
            reused_total += len(inc.reused)
            profiled_total += len(inc.profiled)
        else:
            ts = profile_table(source, t)
        stats_by_table[t.name] = ts
        doc = _table_doc(ts, no_sample_values)
        if inc is not None:
            # The merged bounds and the method that produced them, replacing the
            # "exact" a whole-table profile would have claimed.
            apply_cardinality(doc, inc)
        n_comments += _apply_comments(doc, ts, comments)
        n_comments += _apply_dbt_docs(doc, ts, lineage)
        pending.append((ts, doc))
        out_tables.append(doc)
    _escalate_all(llm, pending, no_sample_values, context, max(1, llm_workers))
    model = "none/heuristic" if llm is None else f"{llm.model}/escalation"
    out: dict = {
        "semantic_layer": {
            "spec_version": "0.5.0",
            "name": "profiled",
            "generated_by": {"engine": ENGINE_VERSION, "model": model},
            "tables": out_tables,
        }
    }
    coverage["tables_modelled"] = len(out_tables)
    out["semantic_layer"]["coverage"] = coverage
    if partitions:
        # Stripped before serialisation, like _link_audit. Without it a user who
        # passed --state has no way to see whether it saved them anything.
        out["_partition_audit"] = {"profiled": profiled_total, "reused": reused_total}
    return out, stats_by_table


def _apply_only(tables_meta: list, only: set[str]) -> list:
    """Narrow the run to the named tables, or say which names did not exist.

    Silence is the wrong behaviour here: a typo in a table name is the
    likeliest reason a targeted re-profile comes back thinner than expected,
    and it looks exactly like the table having had nothing in it.
    """
    wanted = {n.lower() for n in only}
    matched = {n for t in tables_meta for n in (t.name.lower(),
                                                f"{t.schema}.{t.name}".lower())} & wanted
    missing = sorted(wanted - matched)
    if missing:
        raise SemlayerError(
            f"--only named {len(missing)} table(s) not in this warehouse: "
            f"{', '.join(missing)}",
            hint="check spelling and schema qualification; "
                 "run without --only to list what is there")
    return [t for t in tables_meta
            if t.name.lower() in wanted or f"{t.schema}.{t.name}".lower() in wanted]


def _dedup(tables_meta: list, source) -> tuple[list, list[dict]]:
    """Keep one table per duplicate group; report the rest as coverage.

    The skipped copies are not lost: each is recorded with `same_shape_as`
    pointing at the member that WAS profiled, so a consumer asked about one
    can answer that it exists and shares that shape -- which is the thing a
    bare omission cannot distinguish from "no such table".

    Declared partitions are exempt, and the distinction is not pedantic. A
    partition child looks exactly like a copy -- Postgres lists `evt_2024_01`
    beside `evt` with an identical signature and a shared stem -- but the
    parent's rows are the UNION of the children's, so `same_shape_as` would
    assert the one thing that is false about them, that their rows might not
    agree. They have their own mechanism (`_drop_partition_children` plus
    partition-wise profiling) with the right semantics, and the default path
    must keep profiling them as ordinary tables.
    """
    from semlayer.probe import duplicate_groups, representative

    groups = duplicate_groups(tables_meta)
    if not groups:
        return tables_meta, []
    # Queried only when a group was found, so a warehouse with no duplicates
    # issues no extra catalogue call and the default path is unchanged.
    children = {(p.schema, p.name)
                for ps in _partitions_or_empty(source).values() for p in ps}
    if children:
        groups = [g for g in ([t for t in grp
                               if (t.schema, t.name) not in children]
                              for grp in groups) if len(g) > 1]
        if not groups:
            return tables_meta, []
    drop: dict = {}
    skipped: list[dict] = []
    for g in groups:
        keep = representative(g)
        for t in g:
            if t is keep:
                continue
            drop[(t.schema, t.name)] = True
            skipped.append({
                "name": _qualified(t),
                "reason": "duplicate",
                "same_shape_as": _qualified(keep),
            })
    kept = [t for t in tables_meta if (t.schema, t.name) not in drop]
    skipped.sort(key=lambda s: s["name"])
    return kept, skipped


def _qualified(t) -> str:
    return f"{t.schema}.{t.name}" if getattr(t, "schema", None) else t.name


def _rule_detail(col: dict) -> str | None:
    prov = col.get("provenance") or []
    return prov[0].get("detail") if prov else None


def _escalation(ts: TableStats, doc: dict, context=None) -> tuple[list[dict], set[str]] | None:
    """Which of this table's columns go to the LLM: (batch, promoted names), or None."""
    low = [
        c
        for c in doc["columns"]
        if c["confidence"] < ESCALATE_BELOW or _rule_detail(c) in ALWAYS_ESCALATE
    ]
    promoted = _doc_promoted(context, ts.table.name, doc["columns"], low)
    if not low and not promoted:
        return None
    return low + promoted, {c["name"] for c in promoted}


def _escalate_all(llm, pending: list[tuple[TableStats, dict]], no_sample_values: bool,
                  context, workers: int) -> None:
    """Typing escalation for every profiled table, one LLM call per table.

    Runs after the scan rather than inside it, so the calls can be in flight
    together. Each call reads only its own table's statistics and sibling
    names, so no prompt depends on another table's verdict and the document
    is identical at any worker count. Workers build, call and parse; the
    verdicts are applied here, in table order, on the calling thread --
    nothing mutable is shared. A worker that raises fails the run.
    """
    if llm is None:
        return
    jobs = [j for j in (_escalation_job(ts, doc, context) for ts, doc in pending
                        if doc.get("modeling") != "inventory_only") if j]

    def one(job) -> dict[str, dict]:
        from semlayer.profile.llm_typing import escalate_table
        ts, batch, _promoted, excerpts = job
        return escalate_table(
            llm, ts.table.name, [c.name for c in ts.table.columns],
            [ts.columns[c["name"]] for c in batch],
            no_sample_egress=no_sample_values, doc_excerpts=excerpts,
        )

    if workers <= 1 or len(jobs) <= 1:
        results = [one(j) for j in jobs]
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as ex:
            results = list(ex.map(one, jobs))

    for (_ts, batch, promoted_names, _ex), verdicts in zip(jobs, results, strict=True):
        _apply_verdicts(batch, promoted_names, verdicts)


def _escalation_job(ts: TableStats, doc: dict, context) -> tuple | None:
    """One table's escalation: (stats, batch, promoted names, doc excerpts), or None."""
    plan = _escalation(ts, doc, context)
    if plan is None:
        return None
    batch, promoted_names = plan
    excerpts = None
    if context is not None and context.chunks:
        from semlayer.context import relevant_excerpts
        excerpts = relevant_excerpts(
            context.chunks, ts.table.name, [c.name for c in ts.table.columns]) or None
    return ts, batch, promoted_names, excerpts


def _apply_verdicts(batch: list[dict], promoted_names: set[str], verdicts: dict) -> None:
    for c in batch:
        v = verdicts.get(c["name"])
        if not v:
            continue
        if c["name"] in promoted_names:
            _apply_promoted_verdict(c, v)
        else:
            _apply_verdict(c, v)


def _doc_promoted(context, table_name: str, cols: list[dict], low: list[dict]) -> list[dict]:
    """Columns whose confident heuristic answer gets a doc-prompted second look.

    Docs can flag what statistics can't. Requires column AND table tokens in
    the same chunk — the same anti-cross-talk scoping as decode claims.
    """
    if context is None or not context.chunks:
        return []
    low_names = {c["name"] for c in low}
    tn = table_name.lower()
    out = []
    for c in cols:
        if c["name"] in low_names:
            continue
        if any(c["name"].lower() in ch.tokens and tn in ch.tokens
               for ch in context.chunks):
            out.append(c)
    return out


def _apply_verdict(c: dict, v: dict) -> None:
    prior_signal = c["provenance"][0]["signal"] if c["provenance"] else "statistic"
    if v["semantic_type"] != c["semantic_type"] and prior_signal == "naming":
        # LLM overriding a name-rule answer: record the disagreement so
        # reviewers see both readings (conflicts envelope, SPEC.md)
        c.setdefault("conflicts", []).append({
            "between": ["naming", "llm"],
            "detail": f"naming said {c['semantic_type']}, llm said {v['semantic_type']}",
        })
    _apply_core(c, v)


def _apply_core(c: dict, v: dict) -> None:
    c["semantic_type"] = v["semantic_type"]
    c["entity_role"] = v["entity_role"]
    c["confidence"] = round(v["confidence"], 2)
    c["provenance"].append({"signal": "llm", "detail": v["rationale"]})
    if v["semantic_type"].startswith("pii_"):
        c["pii"] = True
    if v["entity_role"] == "measure" and "aggregations" not in c:
        c["aggregations"] = {"allowed": ["sum", "avg", "min", "max", "count"], "default": "sum"}
    if v.get("enum_decodes"):
        c["enum_values"] = [
            {"value": k, "meaning": m, "decode_source": "llm_guess"}
            for k, m in v["enum_decodes"].items()
        ]


def _apply_promoted_verdict(c: dict, v: dict) -> None:
    """Apply a verdict to a doc-promoted column (heuristic was CONFIDENT).

    A correction always lands in the conflicts envelope for review — a
    doc-prompted override of a decided answer is never silent. Agreement
    corroborates without confidence churn.
    """
    if v["semantic_type"] == c["semantic_type"]:
        c["provenance"].append(
            {"signal": "docs", "detail": "doc-prompted re-check corroborated the heuristic"})
        return
    prior_signal = c["provenance"][0]["signal"] if c["provenance"] else "statistic"
    prior = f"{c['semantic_type']} (conf {c['confidence']})"
    c.setdefault("conflicts", []).append({
        "between": [prior_signal, "llm"],
        "detail": f"doc-prompted re-escalation: heuristic said {prior}, "
                  f"llm+docs said {v['semantic_type']}",
    })
    c["provenance"].append(
        {"signal": "docs", "detail": "re-escalated: column named in context docs"})
    _apply_core(c, v)



# The scale line we hold, and why these two numbers rather than terabytes.
#
# Measured (docs/calibration.md): data volume does NOT force an architecture
# change -- it is linear and bounded once the large tables are partitioned.
# TABLE and COLUMN counts do, because they drive fixed per-object costs:
#
#   ~150 tables     Link's candidate space. 163 key-shaped parent columns at 116
#                   tables against MAX_PARENTS = 200, so the cap is already
#                   consuming its whole budget. Parent columns grow ~1.4 per
#                   table, so at 30,000 tables we would consider 0.5% of them.
#                   The fix is blocking, not a bigger cap.
#   ~2,000 tables   serial execution: ~8.8 s of fixed cost per table (round
#                   trips plus two LLM calls). Concurrency, not architecture.
#   ~30,000 columns the output document: 1,209 bytes per column measured, so
#                   450,000 columns is ~544 MB in ONE file. Nothing downstream
#                   loads that, and sharding is a FORMAT change (SPEC.md #4
#                   requires every reference to resolve within the document).
#
# COLUMNS is the better primary signal: three of the four limits scale with
# columns and only latency scales with tables. Both are reported.
SCALE_TABLES = 2_000
SCALE_COLUMNS = 30_000


def _scale_warning(n_tables: int, n_columns: int) -> str | None:
    """Say so when a warehouse is past the size this architecture is tested at.

    A warning, never a refusal: the run still completes, and a user who knows
    their warehouse may be right to proceed. What we must not do is produce a
    quietly degraded layer and let them believe it is complete -- Link's cap
    truncates silently, which is the failure mode this exists to surface.
    """
    over = []
    if n_tables > SCALE_TABLES:
        over.append(f"{n_tables:,} tables (tested to {SCALE_TABLES:,})")
    if n_columns > SCALE_COLUMNS:
        over.append(f"{n_columns:,} columns (tested to {SCALE_COLUMNS:,})")
    if not over:
        return None
    # The first wording also said the output document "becomes impractical
    # to load". Measured 2026-10-28 on a 15,000-table synthetic warehouse: the
    # 42 MB layer parses in 14 s and every MCP tool answers in milliseconds
    # after that, so that clause was retired. The truncation clause stands:
    # MAX_PARENTS caps the candidate list and the cap bites past ~2,900 tables.
    return (f"warehouse exceeds the tested scale: {', '.join(over)}. "
            f"Foreign-key discovery caps its candidate-parent list, so some keys "
            f"may go untested above this size. Narrow the run with --only or a "
            f"schema filter, or see docs/scale.md for what has been measured past it.")


def _partitions_or_empty(source) -> dict:
    """Physical partitions, or {} if the source cannot say.

    Partition-wise profiling is attempted only when a state store was supplied
    AND the source can enumerate partitions, so the default path stays
    byte-for-byte what it has always been.
    """
    lister = getattr(source, "list_partitions", None)
    if lister is None:
        return {}
    try:
        return lister() or {}
    except Exception as e:
        logger.warning("partitions unavailable, profiling whole tables: %s", e)
        return {}


def _drop_partition_children(source, tables_meta: list, state) -> list:
    """Remove physical partitions from the table list when profiling the parent.

    A partition is a table in its own right -- Postgres lists `evt_2024_01`
    alongside `evt` -- so without this the same rows are profiled twice and the
    layer gains 120 monthly "tables" nobody models against.
    """
    parts = _partitions_or_empty(source)
    children = {(p.schema, p.name) for ps in parts.values() for p in ps}
    if not children:
        return tables_meta
    return [t for t in tables_meta if (t.schema, t.name) not in children]


def _cardinality(cs) -> dict:
    """What we honestly know about this column's distinct count.

    A whole-table profile counts exactly, so the bounds coincide and `method`
    is "exact". A partition-wise profile may only have an interval, and
    `semlayer.incremental` overwrites this block with the merged bounds and the
    method that produced them -- "hll_sketch" when a sketch narrowed it,
    "bounds_only" when nothing did.

    Published so a consumer can decide whether a GROUP BY is sane or a join
    will fan out without issuing a query, and so the UNCERTAINTY travels with
    the number instead of being dropped.

    NO TIMESTAMP. This block used to carry `as_of: datetime.now()`, which made
    `infer` non-deterministic: two runs over an unchanged warehouse differed by
    198 lines on messy_mart, every one of them a timestamp. That breaks the
    workflow the product is built around -- inference output lands as a PR in
    the customer's repo, and a re-run that rewrites 198 lines for no reason
    makes the diff unreviewable and drift detection cry wolf. It also silently
    broke cassette replay for examples/agent_pipeline, because the layer feeds
    the agent prompt.

    Recording staleness per statistic is still worth doing -- it is what tells
    a consumer that an incremental run reused an old partition's numbers -- but
    it belongs in the state file next to `change_token`, which is the thing
    that knows what moved, and it must then surface only where it DIFFERS from
    the current run. See docs/calibration.md for the design and why the old
    field could not do that job.
    """
    return {"distinct": cs.n_distinct, "lower_bound": cs.n_distinct,
            "upper_bound": cs.n_distinct, "method": "exact",
            "null_count": cs.n_null}


def _table_doc(ts: TableStats, no_sample_values: bool) -> dict:
    cols = []
    pk_candidates = []
    for name, cs in ts.columns.items():
        typ = classify(cs, no_sample_values=no_sample_values)
        col: dict = {
            "name": name,
            "sql_type": cs.sql_type,
            "semantic_type": typ.semantic_type,
            "entity_role": typ.entity_role,
            "confidence": round(typ.confidence, 2),
            "lifecycle": "inferred",
            "provenance": [{"signal": typ.signal, "detail": typ.detail}],
        }
        col["cardinality"] = _cardinality(cs)
        if typ.semantic_type.startswith("pii_"):
            col["pii"] = True
        if typ.entity_role == "measure":
            col["aggregations"] = {
                "allowed": ["sum", "avg", "min", "max", "count"],
                "default": "sum",
            }
        if typ.entity_role == "primary_key":
            pk_candidates.append((name, typ.confidence))
        cols.append(col)

    tier = infer_tier(ts.table.name, list(ts.columns))
    doc: dict = {
        "name": ts.table.name,
        "source": ts.table.qualified,
        "table_type": "unknown",   # Link-stage decision
        "tier": {"value": tier.tier, "confidence": tier.confidence,
                 "provenance": tier.provenance()},
        # Raw and scratch tiers are listed with their shape and nothing more:
        # no escalation, no foreign-key budget, no descriptions, no metrics, no
        # review items. See semlayer.tier for why, and SPEC.md 2.13 for what a
        # consumer does with one.
        "modeling": "inventory_only" if is_informational(tier) else "full",
        "lifecycle": "inferred",
        "columns": cols,
    }
    if pk_candidates:
        best = max(pk_candidates, key=lambda x: x[1])
        doc["primary_key"] = [best[0]]
        # demote other unique id columns to unique_key
        for col in cols:
            if col["entity_role"] == "primary_key" and col["name"] != best[0]:
                col["entity_role"] = "unique_key"
    return doc
