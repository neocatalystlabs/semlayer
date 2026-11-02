# SPDX-License-Identifier: FSL-1.1-ALv2
"""The `.env` file semlayer reads, and the only way its values reach a run.

An MCP server cannot ask the user to export a variable. Claude Desktop on
macOS launches it with a minimal environment, so a shell `export` is
invisible to it, and the MCP config's own `env` block needs a client
restart to take effect. What is left -- and what the agent is told to
prescribe -- is a file: `$SEMLAYER_HOME/.env`, default `~/.semlayer/.env`.

Two decisions here, both deliberate:

  * **Read on every call, never cached.** The loop we want is: the agent
    says which line to add, the user adds it, the user says "done", the
    agent calls `status` again. That only works if the file is re-read, and
    it is the reason a line REMOVED from the file stops working on the next
    call rather than outliving it in `os.environ`.
  * **Values never persist in `os.environ`.** Reads take a mapping (see
    `environment`), so discovery is a pure function of the file plus the
    real environment. `applied()` exists for the one case that cannot work
    that way -- a connector that reads `os.environ` itself when a
    connection is opened -- and it puts the values back afterwards.

A `.env` here is the user's own file in their own home directory, so it is
trusted with one exception: keys that shape how this process runs rather
than which warehouse it opens are refused, because a `PATH` or a
`LD_PRELOAD` line in a file whose stated purpose is credentials would be a
surprise with consequences.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

# Keys we refuse to apply: the process's own shape, not a warehouse's.
# `SEMLAYER_HOME` is in here because the file lives inside it -- honouring a
# line that moves the home directory would mean the file could relocate the
# directory it was read from.
DENIED = frozenset({
    "PATH", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "LD_PRELOAD",
    "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH",
    "HOME", "SEMLAYER_HOME", "IFS", "BASH_ENV", "ENV",
})

# A credential file that is megabytes long is not a credential file. Read
# with a cap so a mistaken path -- a layer, a log, a database -- costs a
# bounded read on a call that is supposed to be instant.
MAX_BYTES = 64 * 1024
MAX_VALUE = 8 * 1024

_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# `applied` mutates process-global state, and MCP tool calls can overlap.
# Without this, two overlapping opens could restore each other's values.
_LOCK = threading.RLock()


def _unquote(value: str) -> str:
    r"""Strip one layer of matching quotes, honouring escapes in double ones.

    Single quotes are literal, as in every shell: a password containing a
    backslash survives. Double quotes get the four escapes people actually
    write, so a multi-line private key pasted as `"-----BEGIN...\\n..."`
    arrives as the newlines it means.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        if value[0] == "'":
            return inner
        for a, b in (("\\n", "\n"), ("\\r", "\r"), ("\\t", "\t"),
                     ("\\\"", "\""), ("\\\\", "\\")):
            inner = inner.replace(a, b)
        return inner
    # Unquoted: an inline comment needs the space, so a `#` inside a DSN or
    # a password is kept. Quote the value if that is not what you meant.
    return value.split(" #", 1)[0].strip()


def parse(text: str) -> dict[str, str]:
    """`KEY=value` lines into a mapping. Comments, blanks and `export` ignored.

    Unparseable lines are skipped rather than raising: this file is edited
    by hand, mid-conversation, by someone who has been told one line to add.
    A typo three lines down must not take the whole file -- and therefore
    the whole `status` call -- with it.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _KEY.match(key) or key in DENIED:
            continue
        resolved = _unquote(value.strip())
        if len(resolved) <= MAX_VALUE:
            out[key] = resolved
    return out


def read(path: str | Path) -> dict[str, str]:
    """Parse the `.env` file at `path`, or return nothing at all.

    Never raises. A missing file is the normal case (most people never need
    one), and an unreadable one is a reason for `status` to report what it
    can rather than to fail -- `status` is the tool that is supposed to work
    when nothing else does.
    """
    try:
        p = Path(path)
        if p.stat().st_size > MAX_BYTES:
            return {}
        return parse(p.read_text(errors="replace"))
    except OSError:
        return {}


class Environment(dict):
    """A merged environment that remembers which keys came from the file.

    Discovery reports where it saw a connection, and "your shell" and "the
    file semlayer reads" need different advice: a variable set in a shell is
    invisible to a server an MCP client launches, which is the trap this
    whole module exists for. A dict subclass so a test can pass a plain one.
    """

    def __init__(self, values: Mapping[str, str], from_file: frozenset[str]):
        super().__init__(values)
        self.from_file = from_file


def environment(env_path: str | Path) -> Environment:
    """The real environment with the `.env` file layered on top.

    The file WINS over the process environment, which looks backwards until
    you look at what the alternatives are. The values a server inherits came
    from the MCP config's `env` block or from whatever launched the client,
    both of which need a restart to change. The file is the one the user can
    fix while we are talking to them. If an inherited value beat it, the
    advice "add this line and tell me" would silently fail for exactly the
    person who already has a wrong value set.
    """
    from_file = read(env_path)
    return Environment({**os.environ, **from_file}, frozenset(from_file))


@contextmanager
def applied(values: Mapping[str, str]) -> Iterator[None]:
    """Put `values` in `os.environ` for the duration of the block, then undo.

    For the connectors, which read `os.environ` themselves when they open a
    connection (`SnowflakeSource.from_env` and friends). The scope is one
    open, so nothing a `.env` says outlives the call it was read for, and
    nothing it says is visible to telemetry, which is assembled elsewhere.

    Held under a lock because MCP tool calls overlap and `os.environ` is
    process-global: two overlapping opens restoring each other's values is
    the kind of bug that only appears under load and then never reproduces.
    """
    with _LOCK:
        saved: dict[str, str | None] = {}
        try:
            for key, value in values.items():
                if key in DENIED:
                    continue
                saved[key] = os.environ.get(key)
                os.environ[key] = value
            yield
        finally:
            for key, old in saved.items():
                if old is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old
