# SPDX-License-Identifier: FSL-1.1-ALv2
"""Recording a verdict from a conversation, and who it was that decided.

The engine side of review was finished long before this: grouped questions,
accept/reject/set, sticky verdicts, `human` or `agent` on every entry
(`semlayer.review`). What this module adds is the part a chat needs -- the
same questions in a shape an agent can read, and a gate on who may answer
which.

The gate is the whole point, and it is not security: we cannot verify who
typed anything. What we can do is refuse to let an agent record, as its own
accept, a verdict that CHANGES what the layer does. Those kinds
(`review.GATING_KINDS`) unlock behaviour -- a decode guess a metric's filter
is written against, an aggregate table becoming routable -- so the agent
proposes with its reasoning and a person confirms. For kinds where accepting
changes nothing an agent may sign its own, because making a person confirm a
label nobody will ever read is how review queues die.

Rejecting and setting are always a person's, because both assert something
the engine never claimed.

Everything written says which: `signal: agent` with the model's name, or
`signal: human` with the person's. The file, the decision list and the next
answer's assumptions all carry it, so an agent's call and a person's are
never confused even though neither can be proved.
"""

from __future__ import annotations

from semlayer import review

# What an agent may do alone, in one place so the refusals and the docs agree.
AGENT_MAY = "accept a claim whose accept changes no behaviour"
PERSON_ONLY = ("reject, set, and accepting anything that unlocks behaviour: "
               f"{', '.join(sorted(review.GATING_KINDS))}")


def _signal(decided_by: str) -> str:
    """`decided_by` is the tool's word; `signal` is the file's. Translate once.

    The format defines two signals, `human` and `agent`, and the tool takes
    `person` or `agent`. Writing that mapping at each call site is how a
    note once reached the file signed `signal: person` -- a third value
    nothing reads -- so every site goes through here.
    """
    return "human" if decided_by == "person" else "agent"


def _open_questions(doc: dict) -> int:
    """How many QUESTIONS are still open, not how many objects they cover.

    `review.collect` returns items -- one per object -- and a single
    question can cover twenty of them. Reporting that count as "questions
    left" had the agent telling the user a number four times too big.
    `status.layer.undecided_review_items` is the item count and is named
    for it.
    """
    asked, _ = review.group(review.collect(doc), doc)
    return len(asked)


def _find(doc: dict, key: str):
    """The question with this key, and the full queue it came from."""
    asked, not_asked = review.group(review.collect(doc), doc)
    for q in [*asked, *not_asked]:
        if q.key == key:
            return q, asked
    return None, asked


def _as_dict(q, doc: dict) -> dict:
    """One question, written for an agent rather than for a terminal."""
    return {
        "key": q.key,
        "kind": q.kind,
        "claim": q.what,
        "engine_saw": q.reason,
        "confidence": q.confidence,
        "objects": q.n or len(q.items),
        "gates_behaviour": q.kind in review.GATING_KINDS,
        "decidable": q.decidable,
        "on_accept": q.on_accept,
        "on_reject": q.on_reject,
        "set_takes": review.set_shape(q),
        "who_may_decide": ("a person: accepting this changes what the layer does"
                           if q.kind in review.GATING_KINDS else
                           "you may accept this yourself; reject and set are the "
                           "person's"),
        "notes": [{k: v for k, v in n.items() if k != "about"} for n in q.notes],
    }


def questions(doc: dict, tables: list[str] | None = None, limit: int = 20) -> dict:
    """The open questions, newest concern first, optionally scoped to tables.

    A queue the agent PULLS, never one pushed at it. The product's answer to
    "what is uncertain in my model?" -- not a wall between the user and
    their first answer, which is why nothing calls this on the way in.
    """
    _q, asked = _find(doc, "")
    wanted = {t.lower() for t in tables} if tables else None
    out = []
    for q in asked:
        if wanted is not None and not ({(i.table or "").lower() for i in q.items}
                                       & wanted):
            continue
        out.append(_as_dict(q, doc))
    gating = [q for q in out if q["gates_behaviour"]]
    return {
        "open": len(out),
        "gating": len(gating),
        "questions": out[:max(1, limit)],
        "truncated": len(out) > limit,
        "you_may": AGENT_MAY,
        "person_only": PERSON_ONLY,
        "next": ("Read these to the user only if they asked what is uncertain. "
                 "Otherwise answer their question and raise the ones the answer "
                 "rested on -- `run_sql` returns those as `assumptions`."),
    }


def question(doc: dict, key: str) -> dict:
    """One question with every object it covers, and what a verdict would do."""
    q, _asked = _find(doc, key)
    if q is None:
        return {"error": f"no open question has the key {key!r}",
                "next": ("Call review_questions to see the keys. A question "
                         "that has been answered is gone from the queue, "
                         "which may be what happened.")}
    out = _as_dict(q, doc)
    out["objects_covered"] = [
        {"where": it.location, "claim": it.claim, "evidence": it.evidence}
        for it in q.items
    ]
    out["next"] = ("Decide it if you may, or put it to the user in one "
                   "sentence using `claim` and `engine_saw`. `exclude` takes "
                   "the `where` of any object the verdict should skip.")
    return out


def _refuse(why: str, what_instead: str, extra: dict | None = None) -> dict:
    """A refusal an agent can act on, rather than an error it can only report."""
    out = {"recorded": False, "why": why, "next": what_instead}
    out.update(extra or {})
    return out


def _check_shape(q, verdict: str, decided_by: str) -> dict | None:
    """Whether this is a verdict at all, before asking who may give it.

    Order matters: an agent told "a person must do this" about a verdict
    that was malformed anyway would go and fix the wrong thing.
    """
    if decided_by not in ("agent", "person"):
        return _refuse(
            f"decided_by must be 'agent' or 'person', not {decided_by!r}",
            "Say which it was. An agent's verdict and a person's carry "
            "different weight and the file records both, so this is never "
            "defaulted for you.")
    if not q.decidable:
        return _refuse(
            f"{q.key} is shown but not asked: {q.on_reject}",
            "Tell the user what it says and move on; there is nothing to "
            "record here.")
    if verdict.split(":", 1)[0] not in ("accept", "reject", "set"):
        return _refuse(
            f"verdict must be accept, reject or set:<value>, not {verdict!r}",
            f"On this question set takes {review.set_shape(q)!r}."
            if review.set_shape(q) else "This question takes accept or reject.")
    return None


def _check(q, verdict: str, decided_by: str, doc: dict) -> dict | None:
    """Why this verdict may not be recorded this way, or None if it may."""
    shape = _check_shape(q, verdict, decided_by)
    if shape is not None:
        return shape
    base = verdict.split(":", 1)[0]
    if base in ("reject", "set") and decided_by != "person":
        return _refuse(
            f"{base} is always the person's call",
            f"Put it to the user in one sentence -- {q.what} -- and pass "
            "their answer with decided_by='person'. You may accept this "
            "yourself if they agree with the engine.")
    if base == "accept" and decided_by == "agent" and q.kind in review.GATING_KINDS:
        return _refuse(
            f"accepting a {q.kind} question changes what the layer does, so a "
            "person confirms it",
            f"Say what you think and why, offer it in one sentence -- {q.what} "
            f"({q.on_accept}) -- and record their yes with "
            "decided_by='person'. Pass `note` to leave your reasoning on the "
            "question in the meantime.",
            {"propose_instead": True})
    if base == "set":
        why = review.check_set(q, verdict.split(":", 1)[1], doc)
        if why:
            return _refuse(why, f"set takes {review.set_shape(q)!r} here.")
    return None


def answer(doc: dict, key: str, verdict: str, decided_by: str,
           by: str | None = None, exclude: list[str] | None = None,
           note: str | None = None) -> dict:
    """Record one verdict over every object the question covers.

    Returns what it changed and what is left, never a bare error: a refusal
    carries what to do instead, because the agent's next move is the whole
    point of refusing.
    """
    q, _asked = _find(doc, key)
    if q is None:
        return _refuse(f"no open question has the key {key!r}",
                       "Call review_questions for the current keys.")
    refusal = _check(q, verdict, decided_by, doc)
    if refusal is not None:
        if note:
            review.set_author(by or None, signal=_signal(decided_by))
            try:
                entry = review.note(doc, q, note)
            finally:
                review.set_author(None)
            refusal["note_recorded"] = entry
            refusal["next"] = (f"{refusal['next']} Your note is on the "
                               "question and shows up in review_decisions.")
        return refusal

    if not by:
        return _refuse(
            "a verdict has to say who made it, and this one has no name",
            "For a person: ask their name once and pass it as `by` -- never "
            "guess it, and never use your client's name. For yourself: pass "
            "something a human would recognise, like 'claude-code/opus'.")

    review.set_author(by, signal=_signal(decided_by))
    try:
        n = review.answer(doc, q, verdict, set(exclude) if exclude else None)
    finally:
        review.set_author(None)
    if note:
        review.set_author(by, signal=_signal(decided_by))
        try:
            review.note(doc, q, note)
        finally:
            review.set_author(None)
    left = _open_questions(doc)
    return {
        "recorded": True,
        "key": key,
        "verdict": verdict,
        "objects_changed": n,
        "signal": _signal(decided_by),
        "by": by,
        "open_questions_left": left,
        "next": ("It is in the document and in effect now -- this claim stops "
                 "appearing as an assumption on later answers. Tell the user "
                 "in one short sentence what was recorded and in whose name. "
                 "Use save_layer if they want their team to have it."),
    }


def decisions(doc: dict) -> dict:
    """Every verdict in the document, and who stands behind each one.

    The point of reading this is the `signal` column. We cannot prove who
    decided anything -- the CLI's `--by` cannot either -- so what we do
    instead is make it visible everywhere it could matter.
    """
    rows = [{"verdict": d.verdict, "where": d.where, "what": d.what,
             "date": d.date, "by": d.by, "signal": d.signal}
            for d in review.decisions(doc)]
    notes = (doc.get("semantic_layer") or {}).get("review_notes") or []
    by_agent = sum(1 for r in rows if r["signal"] == "agent")
    return {
        "decided": len(rows),
        "by_agent": by_agent,
        "by_person": len(rows) - by_agent,
        "open_questions": _open_questions(doc),
        "decisions": rows,
        "notes": notes,
        "next": ("`signal` is the column that matters: `agent` means nobody "
                 "has confirmed it, so say so when an answer rests on one."),
    }
