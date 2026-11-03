"""Recording a verdict from a chat: who may decide what, and what it says.

The engine's review logic is tested elsewhere. What is tested here is the
gate on top of it, and the gate is not security -- nobody can verify who
typed anything. It exists so that an agent cannot record, as its own
accept, a verdict that CHANGES what the layer does; and so that whatever
is recorded says which of the two it was.

Real layers throughout: the inferred one, because it is the only thing
with every question kind in it.
"""

from __future__ import annotations

import pytest

from semlayer import agent_review as ar
from semlayer import review


@pytest.fixture(scope="module")
def doc(_built):
    """A real statistics-only layer: 20 questions, 4 of them gating."""
    from semlayer.pipeline import InferOptions, infer_with_report, open_source_from_uri
    source = open_source_from_uri(f"duckdb:{_built}")
    out, _report = infer_with_report(source, llm=None, options=InferOptions(name="mart"))
    return out


@pytest.fixture(scope="module")
def _built():
    from pathlib import Path
    path = (Path(__file__).resolve().parents[1] / "fixtures" / "build"
            / "messy_mart.duckdb")
    if not path.is_file():
        pytest.skip("fixtures not built: python fixtures/build.py messy_mart")
    return path


@pytest.fixture
def fresh(doc):
    """A copy, because every test here writes verdicts into it."""
    import copy
    return copy.deepcopy(doc)


def _pick(doc, gating: bool):
    qs = ar.questions(doc, limit=100)["questions"]
    return next(q for q in qs if q["gates_behaviour"] is gating and q["decidable"])


# --- the queue is pulled, not pushed ------------------------------------

def test_the_queue_knows_what_gates_behaviour(fresh):
    out = ar.questions(fresh, limit=100)
    assert out["open"] > 0
    assert 0 < out["gating"] < out["open"], "the fixture needs both kinds"
    for q in out["questions"]:
        assert q["gates_behaviour"] == (q["kind"] in review.GATING_KINDS)


def test_the_queue_says_who_may_decide_each_one(fresh):
    for q in ar.questions(fresh, limit=100)["questions"]:
        assert q["who_may_decide"]
        if q["gates_behaviour"]:
            assert "a person" in q["who_may_decide"]


def test_the_queue_can_be_scoped_to_tables(fresh):
    everywhere = ar.questions(fresh, limit=100)["open"]
    scoped = ar.questions(fresh, ["cust_mstr"], limit=100)["open"]
    assert 0 < scoped < everywhere


def test_the_queue_tells_the_agent_not_to_recite_it(fresh):
    """No wall at ready time: the questions that matter are the ones an
    answer rested on, and run_sql hands those over as assumptions."""
    assert "only if they asked" in ar.questions(fresh)["next"]


def test_a_question_carries_every_object_and_its_evidence(fresh):
    key = _pick(fresh, gating=False)["key"]
    out = ar.question(fresh, key)
    assert out["objects_covered"]
    assert all(o["where"] for o in out["objects_covered"])
    assert out["on_accept"] and out["on_reject"]


def test_an_unknown_key_says_what_to_do(fresh):
    out = ar.question(fresh, "q-nope")
    assert "no open question" in out["error"]
    assert "review_questions" in out["next"]


# --- who may decide what -------------------------------------------------

def test_an_agent_may_accept_what_changes_no_behaviour(fresh):
    q = _pick(fresh, gating=False)
    out = ar.answer(fresh, q["key"], "accept", "agent", by="claude-code/opus")
    assert out["recorded"] is True
    assert out["signal"] == "agent"
    assert out["objects_changed"] > 0
    assert out["open_questions_left"] < ar.questions(fresh, limit=100)["open"] + 1


def test_an_agent_may_not_accept_what_unlocks_behaviour(fresh):
    """A decode guess a metric's filter is written against, or an aggregate
    becoming routable. The agent proposes; a person confirms."""
    q = _pick(fresh, gating=True)
    out = ar.answer(fresh, q["key"], "accept", "agent", by="claude-code/opus")
    assert out["recorded"] is False
    assert out["propose_instead"] is True
    assert q["kind"] in out["why"]
    assert "decided_by='person'" in out["next"]


def test_a_refusal_still_keeps_the_agent_s_reasoning(fresh):
    """Proposing IS leaving the note: the user sees why the agent thinks so,
    and `review_decisions` shows it."""
    q = _pick(fresh, gating=True)
    out = ar.answer(fresh, q["key"], "accept", "agent", by="claude-code/opus",
                    note="the codes match the dimension table")
    assert out["recorded"] is False
    assert out["note_recorded"]["note"] == "the codes match the dimension table"
    assert out["note_recorded"]["signal"] == "agent"
    assert any(n["note"] for n in ar.decisions(fresh)["notes"])


def test_a_refused_person_s_note_is_signed_human_not_person(fresh):
    """The file knows two signals, `human` and `agent`. `person` is the
    TOOL's word, and the refusal path wrote it straight through -- a third
    value nothing in the format reads. Only the agent case was covered,
    which is why it passed.

    A person's `set` IS refused when the value is not spec vocabulary, and a
    note can ride along with that refusal, so this path is reachable.
    """
    q = _pick(fresh, gating=False)
    out = ar.answer(fresh, q["key"], "set:not-a-real-semantic-type", "person",
                    by="Raj Devnath", note="pretty sure it is a currency")
    assert out["recorded"] is False
    assert out["note_recorded"]["signal"] == "human", out["note_recorded"]
    assert out["note_recorded"]["by"] == "Raj Devnath"


def test_a_refusal_does_not_leave_the_author_set(fresh):
    """set_author is process-global. The refusal path never reset it, so the
    next verdict written by anything in this process -- including the CLI --
    inherited the name of whoever was last refused.
    """
    from semlayer import review

    q = _pick(fresh, gating=True)
    ar.answer(fresh, q["key"], "accept", "agent", by="claude-code/opus",
              note="leaving my reasoning")
    assert review._AUTHOR is None
    assert review._SIGNAL == "human"


def test_a_person_may_accept_what_unlocks_behaviour(fresh):
    q = _pick(fresh, gating=True)
    out = ar.answer(fresh, q["key"], "accept", "person", by="Raj Devnath")
    assert out["recorded"] is True
    assert out["signal"] == "human"
    assert out["by"] == "Raj Devnath"


@pytest.mark.parametrize("verdict", ["reject", "set:code/dimension"])
def test_rejecting_and_setting_are_always_the_person_s(fresh, verdict):
    """Both assert something the engine never claimed."""
    q = _pick(fresh, gating=False)
    out = ar.answer(fresh, q["key"], verdict, "agent", by="claude-code/opus")
    assert out["recorded"] is False
    assert "the person's call" in out["why"]


def test_a_verdict_has_to_say_who_decided(fresh):
    q = _pick(fresh, gating=False)
    out = ar.answer(fresh, q["key"], "accept", "nobody", by="x")
    assert out["recorded"] is False
    assert "decided_by" in out["why"]
    assert "never defaulted" in out["next"]


def test_a_verdict_has_to_be_signed(fresh):
    """Never invented, and never the client's name: a file that says
    "claude-desktop" decided something is a file that lies."""
    q = _pick(fresh, gating=False)
    out = ar.answer(fresh, q["key"], "accept", "person")
    assert out["recorded"] is False
    assert "who made it" in out["why"]
    assert "never use your client's name" in out["next"]


def test_a_nonsense_verdict_is_refused_before_the_who_question(fresh):
    """An agent told "a person must do this" about a malformed verdict would
    go and fix the wrong thing."""
    q = _pick(fresh, gating=True)
    out = ar.answer(fresh, q["key"], "maybe", "agent", by="claude-code/opus")
    assert "verdict must be" in out["why"]
    assert "propose_instead" not in out


def test_an_illegal_set_value_names_the_shape(fresh):
    q = _pick(fresh, gating=False)
    out = ar.answer(fresh, q["key"], "set:not-a-real-type", "person", by="Raj")
    assert out["recorded"] is False
    assert out["why"]


def test_excluding_an_object_leaves_it_alone(fresh):
    q = ar.question(fresh, _pick(fresh, gating=False)["key"])
    assert len(q["objects_covered"]) > 1, "need a multi-object question"
    spared = q["objects_covered"][0]["where"]
    out = ar.answer(fresh, q["key"], "accept", "agent", by="claude-code/opus",
                    exclude=[spared])
    assert out["objects_changed"] == len(q["objects_covered"]) - 1


# --- what the record says ------------------------------------------------

def test_a_verdict_takes_effect_immediately(fresh):
    """The claim stops being an assumption on the next answer -- that is the
    whole reason for recording it in the conversation."""
    from semlayer import agent_query
    q = _pick(fresh, gating=False)
    before = {a["key"] for a in agent_query.open_questions(fresh)}
    assert q["key"] in before
    ar.answer(fresh, q["key"], "accept", "agent", by="claude-code/opus")
    assert q["key"] not in {a["key"] for a in agent_query.open_questions(fresh)}


def test_the_record_separates_an_agent_s_calls_from_a_person_s(fresh):
    ar.answer(fresh, _pick(fresh, gating=False)["key"], "accept", "agent",
              by="claude-code/opus")
    ar.answer(fresh, _pick(fresh, gating=True)["key"], "accept", "person",
              by="Raj Devnath")
    out = ar.decisions(fresh)
    assert out["by_agent"] > 0
    assert out["by_person"] > 0
    assert out["by_agent"] + out["by_person"] == out["decided"]
    assert {r["signal"] for r in out["decisions"]} == {"agent", "human"}
    assert "signal" in out["next"]


def test_the_counts_are_questions_not_objects(fresh):
    """`review.collect` returns one item per object and a question can cover
    twenty. Reporting that as "questions left" had the agent telling the
    user a number four times too big."""
    q = _pick(fresh, gating=False)
    covered = len(ar.question(fresh, q["key"])["objects_covered"])
    assert covered > 1
    before = ar.questions(fresh, limit=100)["open"]
    out = ar.answer(fresh, q["key"], "accept", "agent", by="claude-code/opus")
    assert out["objects_changed"] == covered
    assert out["open_questions_left"] == before - 1
