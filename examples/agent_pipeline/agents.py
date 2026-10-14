"""Three agents in a chain, each a real model call, over one warehouse.

The chain is deliberately ordinary: pull a number, summarise it, act on it.
The third agent never sees any SQL -- only the number handed to it -- which is
the point. An error introduced in step 1 arrives at the action as a fact.

Every model turn goes through `semlayer.llm.AnthropicProvider`, so the whole
pipeline replays from committed cassettes with no API key and no spend. The
tool loop is a text protocol (`CALL <tool> <json>`) rather than the provider's
native tool API: that keeps each turn a single (system, user) pair, which is
what the cassette key is built from. Tool CALLS are executed for real -- in
layer mode over MCP stdio, against the server `semlayer mcp` serves.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

MAX_TURNS = 12
_VERB_RE = re.compile(r"^\s*(CALL|ANSWER)\b[ \t]*([a-z_]*)", re.MULTILINE)

_PROTOCOL = """
You work by emitting exactly one line per turn, and nothing else:

CALL <tool_name> {"arg": "value"}      -- to use a tool
ANSWER {"field": value}                -- when you are done

Emit ONE line and then stop. No prose, no explanation, no markdown fences.

Never write a RESULT yourself, and never guess what a tool would have
returned. Emit the CALL and stop; the real result will be given back to you
and you will be asked again. A number you did not get from run_sql is a
fabrication and will be discarded.
"""


def _parse(reply: str) -> tuple[str, str, dict] | None:
    """Read the FIRST protocol line in a reply and ignore everything after it.

    Models under this protocol will happily emit a CALL, invent the RESULT,
    and carry on to an ANSWER in a single turn. Taking only the first verb is
    what keeps the loop honest: a fabricated result never reaches the caller,
    because the turn ends at the call.
    """
    m = _VERB_RE.search(reply)
    if not m:
        return None
    verb, tool = m.group(1), m.group(2)
    args, start = {}, reply.find("{", m.end())
    if start != -1 and (blob := _balanced(reply, start)) is not None:
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            return (verb, tool, {"__malformed__": blob})
        if isinstance(parsed, dict):
            args = parsed
    elif verb == "ANSWER":
        return (verb, tool, {"__malformed__": reply})
    return (verb, tool, args)


def _balanced(text: str, start: int) -> str | None:
    """Slice one balanced JSON object out of `text`, respecting string escapes."""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


@dataclass
class Turn:
    """One step of an agent's loop, kept for the transcript."""

    kind: str  # "call" | "answer" | "malformed"
    tool: str = ""
    args: dict = field(default_factory=dict)
    result: str = ""
    raw: str = ""


@dataclass
class AgentRun:
    """What one agent did and what it handed on."""

    name: str
    answer: dict
    turns: list[Turn]

    @property
    def tool_calls(self) -> list[str]:
        """The tools this agent actually invoked, in order."""
        return [t.tool for t in self.turns if t.kind == "call"]


def run_agent(name: str, llm, system: str, task: str, tools) -> AgentRun:
    """Drive one agent's loop until it answers, runs out of turns, or breaks.

    `tools` is a callable (tool_name, args) -> str. It is the only way the
    agent touches the world; nothing here inspects or rewrites what it asks
    for.
    """
    transcript = [f"TASK: {task}"]
    turns: list[Turn] = []
    for _ in range(MAX_TURNS):
        reply = llm.complete(system + _PROTOCOL, "\n\n".join(transcript)).strip()
        parsed = _parse(reply)
        if parsed is None:
            turns.append(Turn("malformed", raw=reply))
            transcript.append("ERROR: emit exactly one CALL or ANSWER line and nothing else.")
            continue
        verb, tool, args = parsed
        if "__malformed__" in args:
            turns.append(Turn("malformed", raw=reply))
            transcript.append("ERROR: the arguments were not a valid JSON object. Try again.")
            continue
        if verb == "ANSWER":
            turns.append(Turn("answer", args=args, raw=reply))
            return AgentRun(name, args, turns)
        result = tools(tool, args)
        turns.append(Turn("call", tool=tool, args=args, result=result, raw=reply))
        # Only the call we executed goes back, never the model's own
        # continuation after it -- otherwise an invented RESULT becomes context.
        transcript.append(f"CALL {tool} {json.dumps(args)}\n\nRESULT: {result}")
    return AgentRun(name, {}, turns)
