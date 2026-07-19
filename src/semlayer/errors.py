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
