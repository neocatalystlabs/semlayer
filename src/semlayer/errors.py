# SPDX-License-Identifier: FSL-1.1-ALv2
"""Exception taxonomy (Q1): failures carry context and a next step.

Boundary rule: connector/LLM/spec internals raise these; the CLI catches
`SemlayerError` and prints `message` + `hint` — third-party exceptions never
reach users raw. Library consumers can catch the taxonomy precisely instead
of a generic Exception.
"""

from __future__ import annotations


class SemlayerError(Exception):
    """Base for all semlayer failures. `hint` tells the user what to do next."""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


class SourceError(SemlayerError):
    """Warehouse/source failures: bad URI, auth, missing grants, query errors."""


class SpecError(SemlayerError):
    """Semantic-layer document failures: unreadable, invalid, unresolvable refs."""


class LLMError(SemlayerError):
    """LLM tier failures: missing key/cassette, malformed responses, API errors."""


class DriftError(SemlayerError):
    """Drift-loop failures: snapshot/diff/apply problems."""


def error_class(e: BaseException) -> str:
    """Map an exception TYPE to the reported telemetry class. Never the message.

    The message is where warehouse names, hostnames and SQL live, so it is
    not read here at all. That is why this maps types, and why it is coarser
    than the enum allows: `auth` is declared in the schema but unreachable,
    because SourceError covers a bad URI, a refused login and a missing
    grant alike and telling them apart would mean parsing text.

    Builtins are mapped too. A missing file or an unreadable one is an
    ordinary user mistake, and calling it "internal" would make that bucket
    -- which should mean OUR bug -- mean nothing.

    Here rather than in the CLI because the MCP server now records its own
    `infer` events, and both front doors must classify a failure the same
    way or the two are not comparable -- which is the entire point of the
    `via` field they are separated by.
    """
    # Most specific first: FileNotFoundError and PermissionError are both
    # OSError, and SourceError/SpecError are both SemlayerError.
    for kinds, label in (
        ((SourceError,), "connection"),
        ((SpecError,), "invalid_input"),
        ((LLMError,), "llm"),
        ((DriftError, SemlayerError), "other"),
        ((PermissionError,), "permission"),
        ((FileNotFoundError, IsADirectoryError, NotADirectoryError), "invalid_input"),
        ((TimeoutError,), "timeout"),
        ((ConnectionError,), "connection"),
    ):
        if isinstance(e, kinds):
            return label
    return "internal"
