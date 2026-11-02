# SPDX-License-Identifier: FSL-1.1-ALv2
"""Answering a question: the context to answer it from, and running the SQL.

Three things an agent needs that the document alone cannot give it, and one
it must not be given.

What it needs: the part of the layer that bears on a question
(`context_for_question`), the claims underneath that nobody has confirmed
(`assumptions`), and a way to actually run the SQL it wrote (`run`) -- a
semantic layer that can only describe a warehouse leaves the user to go and
find a second MCP server before they get an answer.

What it must not be given: a way to change anything. Every statement is
parsed before it leaves this module and refused unless it is a SELECT. The
check is a parse, not a pattern: `"delete"` appears in plenty of harmless
column names, and `--` comments and nested statements defeat string
matching, which is why no amount of substring checking is used here.

The SQL is never rewritten either. Required filters are REPORTED and left
undone, the row cap is applied by fetching fewer rows rather than by
appending a LIMIT, and a failing query comes back with the error. What ran
is what the agent wrote and what the user can be shown.
"""

from __future__ import annotations

from semlayer.source import RUN_SQL_ROWS, RUN_SQL_TIMEOUT_S

# Dialect per connector family, for both the linter and the SELECT-only
# parse. Getting this wrong is not cosmetic: Snowflake's `QUALIFY` and
# BigQuery's backticks are parse errors in the default dialect, and an agent
# told its valid SQL does not parse will rewrite working SQL into something
# worse.
DIALECTS = {"duckdb": "duckdb", "postgres": "postgres",
            "bigquery": "bigquery", "snowflake": "snowflake"}


def dialect_for(family: str | None) -> str:
    """The sqlglot dialect for a connector family, defaulting to DuckDB."""
    return DIALECTS.get(family or "", "duckdb")


# Statement types that are a question rather than a change. Everything else
# -- INSERT, UPDATE, DELETE, MERGE, CREATE, DROP, ALTER, GRANT, COPY, CALL,
# SET, and anything sqlglot cannot classify -- is refused.
def _is_select(expression) -> bool:
    """Whether this statement only asks.

    `exp.Query` is sqlglot's own base for everything that returns rows --
    SELECT, the set operations, and a parenthesised or subqueried form of
    either. Asking it is better than listing the node types, which is how
    the first version of this refused a perfectly ordinary `(select 1)`.
    Older sqlglot has no `Query`, so the node list is the fallback.
    """
    from sqlglot import exp

    base = getattr(exp, "Query", None)
    if base is not None:
        return isinstance(expression, base)
    node = expression
    while isinstance(node, (exp.Subquery, exp.Paren)):
        node = node.this
    return isinstance(node, (exp.Select, exp.Union, exp.Intersect, exp.Except))


def _statement_label(statement) -> str:
    """What to call the statement in a refusal, in SQL's own words.

    sqlglot's node name is a concatenation -- `TruncateTable` -- and
    "TRUNCATETABLE is not allowed" reads like a typo rather than a rule. A
    statement it could not parse at all arrives as `Command`, where the
    keyword the user actually wrote is the first token.
    """
    import re
    name = type(statement).__name__
    if name == "Command":
        word = (statement.name or str(statement.this or "")).strip().split()
        return (word[0].upper() if word else "this statement")
    return " ".join(re.findall(r"[A-Z][a-z]*", name)).upper() or name.upper()


def select_only(sql: str, dialect: str = "duckdb") -> str | None:
    """None if `sql` is one read-only SELECT; otherwise why it was refused.

    Three refusals, and the middle one is the one a string check would miss:
    a statement that is not a SELECT, SEVERAL statements where the first is
    innocent, and a SELECT ... INTO, which writes a table.
    """
    import sqlglot
    from sqlglot import exp

    try:
        statements = sqlglot.parse(sql, read=dialect)
    except sqlglot.errors.ParseError as e:
        return f"this does not parse as {dialect} SQL: {str(e)[:200]}"
    real = [st for st in statements if st is not None]
    if not real:
        return "there is no SQL here to run"
    if len(real) > 1:
        return (f"{len(real)} statements were given and only one is allowed: "
                "run_sql executes a single SELECT")
    statement = real[0]
    if not _is_select(statement):
        return (f"{_statement_label(statement)} is not allowed: run_sql only ever "
                "runs a SELECT. Nothing here can change the warehouse -- if the "
                "user wants data written, that is their own client's job")
    if statement.find(exp.Into) is not None:
        return "SELECT ... INTO writes a table, so it is not allowed"
    return None


def tables_in(sql: str, dialect: str = "duckdb") -> list[str]:
    """The table names a statement reads, lowercased, unqualified last part.

    For attaching assumptions to an answer: what the agent relied on is
    whatever it selected from. A name we cannot resolve is dropped rather
    than guessed at -- a wrong assumption attached to an answer is worse
    than a missing one, because the user would be reassured by it.
    """
    import sqlglot
    from sqlglot import exp

    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.ParseError:
        return []
    out: list[str] = []
    for table in tree.find_all(exp.Table):
        name = (table.name or "").lower()
        if name and name not in out:
            out.append(name)
    return out


def _question_tables(question) -> set[str]:
    """Every table one review question touches, lowercased."""
    return {(it.table or "").lower() for it in question.items if it.table}


def open_questions(doc: dict, tables: list[str] | None = None) -> list[dict]:
    """The claims under these tables that nobody has confirmed.

    Written for an agent to render under an answer, so each one carries the
    claim in words, what the engine saw, and whether accepting it would
    CHANGE anything -- a decode guess that a metric's filter depends on is a
    different kind of uncertainty from a type label nobody has blessed.
    """
    from semlayer import review

    asked, _ = review.group(review.collect(doc), doc)
    wanted = {t.lower() for t in tables} if tables else None
    out = []
    for q in asked:
        if wanted is not None and not (_question_tables(q) & wanted):
            continue
        out.append({
            "key": q.key,
            "kind": q.kind,
            "claim": q.what,
            "engine_saw": q.reason,
            "confidence": q.confidence,
            "objects": q.n or len(q.items),
            "gates_behaviour": q.kind in review.GATING_KINDS,
            "on_accept": q.on_accept,
            "decided_by": "nobody",
        })
    return out


def assumptions(doc: dict, tables: list[str]) -> list[dict]:
    """What an answer over `tables` is resting on, and who stands behind it.

    Two sources, and the distinction is the point: claims nobody has looked
    at (`decided_by: nobody`), and claims an AGENT accepted (`agent`) --
    which are decided, but not by a person, and so are still assumptions. A
    claim a person confirmed is not an assumption at all and is left out; it
    is a decision, and `review_decisions` is where those are read.
    """
    from semlayer import review

    out = open_questions(doc, tables)
    wanted = {t.lower() for t in tables}
    for d in review.decisions(doc):
        if d.signal != "agent":
            continue
        where = d.where.split(".")[0].lower()
        if wanted and where not in wanted:
            continue
        out.append({"key": f"{d.verdict}:{d.where}", "kind": "agent_verdict",
                    "claim": d.what, "engine_saw": "", "confidence": None,
                    "objects": 1, "gates_behaviour": False,
                    "on_accept": "", "decided_by": "agent", "by": d.by,
                    "date": d.date})
    return out


def context_for_question(doc: dict, question: str) -> dict:
    """The part of the layer that bears on one question, plus its caveats.

    The context string is `cq.answer.semantic_context`, which is the same
    rendering the published benchmark measured -- search, then routing, then
    full detail for the tables that matter, then a compact index of
    everything else. Reusing it means the number we quote is a number about
    what the server actually serves.
    """
    from semlayer.cq.answer import _select_tables, semantic_context

    detailed, _index = _select_tables(doc, question)
    return {
        "question": question,
        "tables": detailed,
        "context": semantic_context(doc, question),
        "open_questions": open_questions(doc, detailed),
        "next": ("Write ONE SELECT from the context above, apply every "
                 "required filter it names, call check_sql on it, fix what "
                 "check_sql reports, then run_sql. Answer the user first and "
                 "put the assumptions under the answer, offering to record "
                 "a correction."),
    }


def _jsonable(value):
    """A warehouse value as JSON a model can read without parsing it back.

    Decimals become floats and dates become ISO strings. The alternative is
    `default=str` at the serialiser, which turns 13006346.06 into a STRING
    and leaves the agent doing arithmetic on text -- the agent example hit
    exactly this and coerced for the same reason.
    """
    import datetime
    from decimal import Decimal

    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    return value


def run(doc: dict, source, sql: str, *, dialect: str = "duckdb",
        limit: int = RUN_SQL_ROWS, timeout_s: float = RUN_SQL_TIMEOUT_S) -> dict:
    """Lint, then run one SELECT, and say exactly what happened.

    The order matters and is the product: `check_sql` runs FIRST and an
    error-level finding is returned rather than executed. A query that sums
    cancelled orders executes perfectly well, which is why "it ran" was
    never the standard.

    Warnings do not block. They are reported alongside the rows, because a
    fan-out warning on a query that is deliberately counting rows is noise,
    and an agent that cannot proceed past a warning will rewrite correct SQL
    to silence it.

    Nothing is persisted and nothing is rewritten. On failure the error comes
    back with the context an agent needs to repair it -- the same material
    `cq.answer.answer_with_repair` feeds back in the benchmark loop, which is
    where we learned that agents fix their own SQL when shown the error.
    """
    from semlayer.lint import lint_sql, render_findings

    refused = select_only(sql, dialect)
    if refused is not None:
        return {"ran": False, "refused": refused, "sql": sql,
                "next": ("Tell the user what you tried to run and why it was "
                         "refused. run_sql reads; it never writes.")}

    lint = lint_sql(doc, sql, dialect=dialect)
    touched = tables_in(sql, dialect)
    if lint["errors"]:
        return {"ran": False, "sql": sql, "lint": lint,
                "findings": render_findings(lint),
                "repair": {"sql": sql, "problems": render_findings(lint),
                           "rule": ("Required filters are verified business "
                                    "rules and take precedence over the "
                                    "question's wording: apply them and say "
                                    "so in your answer.")},
                "next": ("Not executed: check_sql found errors. Fix the SQL "
                         "against `findings` and call run_sql again. Do not "
                         "work around a required filter -- apply it.")}

    try:
        columns, rows, truncated = source.query_capped(sql, limit=limit,
                                                       timeout_s=timeout_s)
    except Exception as e:
        return {"ran": False, "sql": sql,
                "error": f"{type(e).__name__}: {str(e)[:400]}",
                "repair": {"sql": sql,
                           "problems": f"{type(e).__name__}: {str(e)[:400]}",
                           "rule": ("Fix the SQL. The schema in your context "
                                    "is the layer's view of the warehouse; if "
                                    "they disagree, trust the error.")},
                "next": ("The warehouse refused this query. Fix it from the "
                         "error and call run_sql again, or tell the user what "
                         "is wrong if it cannot be fixed.")}

    out: dict = {
        "ran": True,
        "sql": sql,
        "columns": columns,
        "rows": [[_jsonable(v) for v in r] for r in rows],
        "row_count": len(rows),
        "truncated": truncated,
        "limits": {"rows": limit, "timeout_s": timeout_s},
        "assumptions": assumptions(doc, touched),
    }
    if lint["warnings"]:
        out["warnings"] = render_findings(lint)
    out["next"] = ("Answer the user in words FIRST, from these rows. Then put "
                   "the assumptions under the answer, each as the claim it "
                   "rests on, and offer to record a correction in one word. "
                   + ("Say that the rows were truncated at the cap. "
                      if truncated else "")
                   + ("Repeat the warnings: they are about whether the number "
                      "means what the user thinks. " if lint["warnings"] else ""))
    return out
