# SPDX-License-Identifier: FSL-1.1-ALv2
"""What a telemetry event may contain, and the only way to build one.

This module is the honesty contract in executable form. `record()` used to
write whatever keyword arguments it was handed; now every field passes
through `serialize()`, which drops anything not declared here and reshapes
what is.

Two properties follow from that, and neither depends on anyone remembering:

  * A caller cannot send a field nobody declared. Not "should not" -- the
    serializer has no path that writes an unknown key.
  * A caller cannot send an exact table or column count, because the buckets
    are applied HERE rather than at call sites. An exact count is a weak
    fingerprint of a particular warehouse; a range is not.

Bumping SCHEMA: any change to the fields below. Old events keep their old
number and stay readable, because the receiver stores what it recognises and
parks the rest rather than rejecting a batch it does not fully understand.
"""

from __future__ import annotations

SCHEMA = 4

# (lower_bound, label), highest first. A value is labelled by the first bound
# it reaches.
_TABLE_BUCKETS = [(1000, "1000_plus"), (201, "201_1000"), (51, "51_200"),
                  (21, "21_50"), (6, "6_20"), (1, "1_5"), (0, "0")]
_COLUMN_BUCKETS = [(10000, "10000_plus"), (2001, "2001_10000"), (501, "501_2000"),
                   (201, "201_500"), (51, "51_200"), (1, "1_50"), (0, "0")]
_SCHEMA_BUCKETS = [(6, "6_plus"), (2, "2_5"), (1, "1")]

EVENTS = {"cli", "infer", "probe", "drift", "validate", "export", "mcp",
          "init", "review", "lint", "telemetry"}
CONNECTORS = {"snowflake", "bigquery", "postgres", "duckdb", "none"}
OUTCOMES = {"ok", "error", "interrupted"}
ERROR_CLASSES = {"connection", "auth", "permission", "llm", "timeout",
                 "invalid_input", "internal", "other"}
LLMS = {"anthropic", "openai", "other", "none"}
OSES = {"linux", "darwin", "windows", "other"}
ARCHES = {"x86_64", "arm64", "other"}
FLAGS = {"no_llm", "no_sample_egress", "no_declared_constraints", "apply",
         "json", "share"}

# Which MCP tools a session actually called. These are OUR names -- the ones
# registered in `mcp_server.build_server` -- so unlike HOSTS this is a
# pass-through vocabulary, not a classifier over someone else's text.
#
# "other" is here, and unknown names map to it rather than being dropped,
# which is the one place this differs from FLAGS. A dropped name would make
# "a tool we forgot to declare" and "no tool call at all" the same row, and
# those mean opposite things: the first is our bookkeeping falling behind a
# renamed tool, the second is the probe-versus-person question this field
# exists to answer.
TOOLS = {"semantic_search", "get_domains", "get_tables", "table_detail",
         "get_metrics", "route_intent", "check_sql", "compile_metric",
         # The two that work with no layer loaded. Worth telling apart from
         # the rest: a session that called `status` and nothing else is one
         # that asked what we had and was told "nothing yet", which is the
         # onboarding failure we are trying to measure.
         "status", "discover_connections",
         "other"}

# Which front door an `infer` came through: the CLI, or the MCP server's own
# infer tool. A two-value enum rather than a boolean because a third door is
# already foreseeable (the browser page), and because `via=cli` reads as a
# fact where `from_mcp=false` reads as an absence.
#
# This is the measurement that decides whether MCP-first onboarding worked.
# Today every install that launches the server and stops is indistinguishable
# from one that was never going to infer anything; once inference can start
# from inside a session, `via` is what separates "they found it" from "they
# went to a terminal and read the docs".
VIA = {"cli", "mcp"}

# Which MCP client launched the server. NOT a pass-through enum like the
# others: a client name is arbitrary vendor text, so `_host` CLASSIFIES it
# and can only ever return one of these labels -- the raw string has no path
# to an event even if a client calls itself "claude-desktop".
#
# "sdk" is the bare Python/TypeScript SDK default, which is what a script or
# an automated probe presents. It is a separate label from "other" because
# telling a scripted launch from an unrecognised real client is the whole
# reason this field exists -- a launch count alone cannot separate a person
# in an editor from a script in a container, and folding the two into
# "other" would merge exactly what the field is for.
HOSTS = {"claude-desktop", "claude-code", "cursor", "vscode", "inspector",
         "sdk", "other"}

# Checked in order: the first match wins. "claude-code" before "claude"
# because the former contains the latter; "cursor" before "vscode" because
# Cursor identifies itself as "cursor-vscode"; "inspector" before the SDK
# rules because the inspector ships as an mcp-* package.
_HOST_RULES = [
    (("claude-code", "claude code"),            "claude-code"),
    (("claude",),                               "claude-desktop"),
    (("cursor",),                               "cursor"),
    (("inspector",),                            "inspector"),
    (("vscode", "visual studio code", "code"),  "vscode"),
    (("mcp", "sdk"),                            "sdk"),
]


def _host(v: object) -> str | None:
    """Classify an MCP client name into HOSTS, never echoing the name.

    Returns None for a non-string or an empty one, so a client that sends no
    clientInfo is absent from the event rather than guessed at.
    """
    if not isinstance(v, str):
        return None
    name = v.strip().lower()
    if not name:
        return None
    for needles, label in _HOST_RULES:
        if any(n in name for n in needles):
            return label
    return "other"


def _bucket(buckets: list[tuple[int, str]], v: object) -> str | None:
    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
        return None
    for lo, label in buckets:
        if v >= lo:
            return label
    return None


def _enum(allowed: set[str], v: object) -> str | None:
    """Unknown members become "other" where that exists, else are dropped.

    Dropping is the safe default: a value we did not anticipate is exactly
    the kind of thing that turns out to be a hostname or a database name.
    """
    if not isinstance(v, str):
        return None
    if v in allowed:
        return v
    return "other" if "other" in allowed else None


def _int(v: object) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _bool(v: object) -> bool | None:
    return v if isinstance(v, bool) else None


def _version(v: object) -> str | None:
    """A version string, length-capped. Never a path, never an interpreter."""
    return v if isinstance(v, str) and 0 < len(v) <= 32 else None


def _flags(v: object) -> list[str] | None:
    if not isinstance(v, (list, tuple, set)):
        return None
    out = sorted({f for f in v if isinstance(f, str) and f in FLAGS})
    return out or None


def _tools(v: object) -> list[str] | None:
    """Which MCP tools were called: deduped, sorted, never their arguments.

    A set, not a sequence of calls: the order and the repetition are the
    shape of someone's session, and we only need to know whether the server
    was used at all and for what. Absent rather than empty when nothing was
    called, so "launched and did nothing" is the absence of a key rather
    than a value that has to be spelled the same way everywhere.
    """
    if not isinstance(v, (list, tuple, set)):
        return None
    out = sorted({t if t in TOOLS else "other"
                  for t in v if isinstance(t, str) and t})
    return out or None


def _stage_times(v: object) -> dict[str, int] | None:
    """Stage name -> whole seconds.

    Stage names are data, not an enum, so renaming or splitting a pipeline
    stage does not bump SCHEMA. Values must be ints; anything else is
    dropped rather than coerced, because a float here would most likely be
    a measurement we did not mean to send.
    """
    if not isinstance(v, dict):
        return None
    out = {str(k)[:32]: x for k, x in v.items()
           if isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 86400}
    return out or None


# Probe reports counts as well as ratios, and they were going out exact --
# straight past the bucketing applied to the very same quantities at the top
# level. Bucketing `tables` because an exact pair fingerprints a warehouse,
# while shipping that pair inside a nested object, is not a policy.
_PROBE_BUCKETED = {"tables": _TABLE_BUCKETS, "columns": _COLUMN_BUCKETS,
                   "schemas": _SCHEMA_BUCKETS}


def _probe(v: object) -> dict | None:
    """Probe.telemetry() output: grades and ratios, never names.

    Counts are bucketed to the same granularity as the top-level fields.
    Ratios stay exact -- they are already fractions and disclose nothing
    about size.
    """
    if not isinstance(v, dict):
        return None
    out: dict[str, object] = {}     # bucket labels are str, ratios are float
    for k, x in v.items():
        if not isinstance(k, str):
            continue
        if k in _PROBE_BUCKETED:
            b = _bucket(_PROBE_BUCKETED[k], x)
            if b is not None:
                out[k] = b
        elif isinstance(x, (int, float, str, bool)) and not (
                isinstance(x, str) and len(x) > 32):
            out[k] = x
    return out or None


# field -> transform. The whole whitelist; nothing else is ever written.
FIELDS = {
    "event":       lambda v: _enum(EVENTS, v),
    "command":     lambda v: _enum(EVENTS, v),
    "connector":   lambda v: _enum(CONNECTORS, v),
    "outcome":     lambda v: _enum(OUTCOMES, v),
    "error_class": lambda v: _enum(ERROR_CLASSES, v),
    "llm":         lambda v: _enum(LLMS, v),
    "os":          lambda v: _enum(OSES, v),
    "arch":        lambda v: _enum(ARCHES, v),
    "version":     _version,
    "python":      _version,
    "duration_s":  _int,
    "ci":          _bool,
    "flags":       _flags,
    "via":         lambda v: _enum(VIA, v),
    "host":        _host,
    "tools":       _tools,
    "stages":      _stage_times,
    "probe":       _probe,
    "tables":      lambda v: _bucket(_TABLE_BUCKETS, v),
    "columns":     lambda v: _bucket(_COLUMN_BUCKETS, v),
    "schemas":     lambda v: _bucket(_SCHEMA_BUCKETS, v),
}

# Set by record(), not by callers, and so deliberately absent from FIELDS:
# schema, t, anon, event_id.
#
# `event_id` is what makes a re-sent batch harmless. The spool is truncated
# only after a 2xx, so a response that arrives after the sender's deadline
# leaves events spooled that the receiver already committed, and every later
# command re-posts them. An id per event lets the receiver recognise the
# replay instead of inserting it again -- the client stays simple and keeps
# retrying, and the count stops being wrong.
RESERVED = {"schema", "t", "anon", "event_id"}


def serialize(props: dict) -> dict:
    """Keep what is declared, reshaped. Drop everything else, silently.

    Silently because the alternative is worse: raising would let a telemetry
    mistake break a user's actual command, and warning would print noise
    about a subsystem they did not ask about.
    """
    out = {}
    for name, transform in FIELDS.items():
        if name in props:
            value = transform(props[name])
            if value is not None:
                out[name] = value
    return out
