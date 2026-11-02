# SPDX-License-Identifier: FSL-1.1-ALv2
"""The session: which connection, which layer, and what to do next.

`semlayer mcp` used to demand a layer path and do nothing else, which made
the documented order -- infer first, then serve -- the only one that worked
at all. The observed order is the opposite: people add the MCP server to
Claude Desktop, Claude Code or Cursor first, and expect it to be useful
before they have inferred anything. Telemetry says that is the common path
by a wide margin, and that most of those installs never run an inference.

So the server has to answer three questions with no layer present: what do
you have, what can you see, and what should I do next. This module owns the
state behind those answers -- which connection, which layer, where a layer
is cached -- so that the MCP server, the CLI and (later) the browser page
are thin adapters over one object instead of three places that each decide.

The pure query functions in `mcp_server` are deliberately untouched: they
take a document and they still take a document. A session is what chooses
the document, not a new way to query one.

Nothing here opens a warehouse connection. `discover()` reads file names,
section names and environment variable NAMES; it never reads a credential,
never reports a value that could be one, and never connects. That is what
makes it cheap enough to run on every `status` call, and safe enough to run
before the user has agreed to anything.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from semlayer import env_file

# Credentials, by whatever name a URL spells them. Stripped before the cache
# key is hashed, so the same warehouse reached with and without a password in
# the URL is one cache entry rather than two.
_SECRET_QUERY_KEYS = frozenset({
    "password", "passwd", "pwd", "token", "secret", "passphrase",
    "private_key", "private_key_path", "private_key_file", "key", "credentials",
})

# The URI schemes we can open, mapped to the connector family that opens them.
# `postgresql` and `postgres` are the two spellings of one DSN.
_FAMILIES = {"duckdb": "duckdb", "postgres": "postgres", "postgresql": "postgres",
             "bigquery": "bigquery", "snowflake": "snowflake"}


def home() -> Path:
    """Where semlayer keeps per-machine state, honouring `SEMLAYER_HOME`.

    Read on every call rather than captured at import, which is what
    `telemetry._DIR` does and then cannot be changed by a test without
    reloading the module.
    """
    return Path(os.environ.get("SEMLAYER_HOME", Path.home() / ".semlayer"))


def env_path() -> Path:
    """The `.env` file the agent prescribes lines in. Re-read on every call.

    Inside `SEMLAYER_HOME` rather than the working directory: an MCP client
    starts the server wherever it likes, and a credential file that is found
    only from one directory is a credential file that silently stops working.
    """
    return home() / ".env"


def environment() -> Mapping[str, str]:
    """The process environment with `$SEMLAYER_HOME/.env` layered on top.

    Every read of a warehouse variable in this module goes through a mapping
    rather than `os.environ`, which is what makes discovery a pure function
    of (file, environment) and lets a line added mid-conversation take effect
    on the next call with no restart -- and a line removed stop working.
    """
    return env_file.environment(env_path())


def _env_hint(keys: str = "") -> str:
    """Where to put a variable, written for an agent that has no shell.

    Every note in a discovery row is read out by the agent to the user, so a
    note saying "export it" prescribes the one thing that cannot work: a
    server an MCP client launched never sees a shell. The file is the answer
    everywhere, and saying so here keeps the advice identical across sources.
    """
    where = _tilde(env_path())
    what = f"add {keys} to {where}" if keys else f"add the missing lines to {where}"
    return (f"{what} and call status again -- a shell export is invisible to a "
            "server your MCP client launched, so that file is the place")


def _where(env: Mapping[str, str], var: str) -> str:
    """Where a variable was seen: the process, or the file we read.

    Not cosmetic. A variable exported in a shell is invisible to a server
    that Claude Desktop launched, so an agent telling the user "it is
    already set" would be wrong in the one case that matters -- and the
    advice for fixing it is different for each.
    """
    if var in getattr(env, "from_file", ()):
        return f"{_tilde(env_path())}: {var}"
    return f"environment: {var}"


def family(connection: str) -> str | None:
    """The connector family a connection string names, or None if unknown.

    Shared by the cache key and by discovery, so one spelling table decides
    both and `postgres` cannot mean two things in two places.
    """
    scheme = connection.split("://", 1)[0].split(":", 1)[0].strip().lower()
    return _FAMILIES.get(scheme)


def _identity(connection: str) -> str:
    """The connection string with every credential removed.

    Two properties matter, and the second is why this is not just a trim:

      * No secret is hashed, so the key cannot be reversed into one and the
        cache directory listing discloses nothing.
      * A URL carrying a password hashes the SAME as the URL without it. A
        user who exports the password in one shell and not in another is
        looking at one warehouse, and must not get two cached layers.

    A duckdb path is resolved to an absolute one: `duckdb:warehouse.duckdb`
    names different files from different working directories, and a cache key
    that cannot tell them apart would serve one warehouse's layer for
    another's. Resolution is lexical (`Path.resolve` without `strict`), so a
    file that does not exist yet still gets a stable key.
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    raw = connection.strip()
    if family(raw) == "duckdb" and "://" not in raw:
        rest = raw.split(":", 1)[1] if ":" in raw else ""
        return f"duckdb:{Path(rest).expanduser().resolve()}" if rest else "duckdb:"
    if "://" not in raw:
        return raw
    u = urlsplit(raw)
    netloc = u.hostname or ""
    if u.username:
        netloc = f"{u.username}@{netloc}"
    if u.port:
        netloc = f"{netloc}:{u.port}"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(u.query)
                             if k.lower() not in _SECRET_QUERY_KEYS))
    return urlunsplit((u.scheme, netloc, u.path, query, ""))


def cache_key(connection: str) -> str:
    """A filesystem-safe stem naming the cached layer for `connection`.

    The family is kept legible and the rest is a digest: `~/.semlayer/layers`
    should be readable by the person who owns it without disclosing anything
    to someone who can list it.
    """
    digest = hashlib.sha256(_identity(connection).encode()).hexdigest()[:12]
    return f"{family(connection) or 'layer'}-{digest}"


def layer_cache_path(connection: str) -> Path:
    """Where the inferred layer for `connection` is cached.

    DECIDED (Raj, 2026-11-02): the home directory, never the repository.
    Inference writes this file without being asked, and writing into
    somebody's repository unasked is not ours to do -- it would also force a
    `.gitignore` decision on them, and put a machine-local artefact in a
    place that gets committed by accident.

    The repository copy is the explicit one: `save_layer(path)` puts it
    there when the user wants their team to see it, and a repo copy WINS
    over a cache copy of the same warehouse in `visible_layers`, because it
    is the one carrying the team's review decisions. So the cache is the
    cold-start fallback -- which is the whole answer for a client like
    Claude Desktop, with no useful working directory at all.

    Still ONE function, so the decision stays reversible in one place.
    """
    return home() / "layers" / f"{cache_key(connection)}.yaml"


def load_document(path: str | Path) -> dict:
    """Load and shallow-validate a semantic layer document, resolving a set.

    Moved here from the CLI, unchanged, because "which layer is loaded" is
    this module's business and the CLI is now one of three callers.
    """
    import yaml

    from semlayer.errors import SemlayerError, SpecError
    from semlayer.validate import load_yaml

    try:
        doc = load_yaml(Path(path).read_text())
    except FileNotFoundError as e:
        raise SpecError(f"document not found: {path}",
                        hint="run `semlayer infer` first, or check the path") from e
    except yaml.YAMLError as e:
        raise SpecError(f"document is not valid YAML: {path} ({e})") from e
    if not isinstance(doc, dict) or "semantic_layer" not in doc:
        raise SemlayerError(f"not a semantic layer document: {path}",
                            hint="expected a top-level `semantic_layer:` mapping")
    from semlayer.setdoc import is_hub, load_set
    if is_hub(doc):
        # a hub and its members become one document; every command below
        # then works on the set exactly as it does on one file
        return load_set(str(path)).doc
    return doc


# --- what this build can open, in the agent's vocabulary -------------------
#
# The agent has never heard of semlayer. It does not know that Redshift is
# reached as postgres, that a DuckDB connection is a file path, or that we
# cannot open Databricks at all -- and it will guess if we do not say. So the
# vocabulary ships in the payload rather than in a doc nobody reads.

# Import name -> what pip installs it. `semlayer[warehouses]` installs all
# three optional ones; duckdb is a hard dependency and is always there.
_DRIVERS = {
    "duckdb": ("duckdb", "duckdb"),
    "postgres": ("psycopg", "psycopg[binary]"),
    "bigquery": ("google.cloud.bigquery", "google-cloud-bigquery"),
    "snowflake": ("snowflake.connector", "snowflake-connector-python"),
}

# One entry per connector family: the URI forms an agent may pass, the other
# names for the same thing, and the `.env` line to prescribe when a credential
# is missing. The credential lines are placeholders with the KEY NAME exact:
# an agent that improvises `POSTGRES_URL=` sends the user to edit a file that
# will never be read.
_KINDS: tuple[dict, ...] = (
    {
        "kind": "duckdb",
        "uri_forms": ["duckdb:<path-to-file>"],
        "aliases": [],
        "credentials": None,
        "note": "a file on this machine; there is no credential to find",
    },
    {
        "kind": "postgres",
        "uri_forms": ["postgresql://user@host:5432/dbname", "postgres"],
        "aliases": ["redshift", "supabase", "neon", "timescale", "citus",
                    "aurora postgres", "anything speaking the Postgres protocol"],
        "credentials": "PG_READER_DSN=postgresql://reader@host:5432/dbname",
        "note": ("the bare word `postgres` reads the DSN from the environment; "
                 "a URI is taken as given"),
    },
    {
        "kind": "bigquery",
        "uri_forms": ["bigquery:<project>", "bigquery"],
        "aliases": [],
        "credentials": ("GCP_PROJECT=my-project  (plus application default "
                        "credentials, or "
                        "GOOGLE_APPLICATION_CREDENTIALS_READER=/path/key.json)"),
        "note": "`gcloud auth application-default login` is the usual credential",
    },
    {
        "kind": "snowflake",
        "uri_forms": ["snowflake", "snowflake:<connection-name>",
                      "snowflake://user@account/database?role=R&warehouse=W"],
        "aliases": [],
        "credentials": ("SNOWFLAKE_ACCOUNT=ORG-ACCOUNT\n"
                        "SNOWFLAKE_READER_USER=semlayer_reader_svc\n"
                        "SNOWFLAKE_READER_PRIVATE_KEY_PATH=/path/key.p8"),
        "note": ("a name from ~/.snowflake/connections.toml needs nothing "
                 "pasted; `semlayer init snowflake` prints the setup for a "
                 "key-pair reader"),
    },
)

# Said once, in the payload, so the agent can tell the user the truth instead
# of either promising a connector we do not have or going quiet.
UNSUPPORTED = (
    "semlayer cannot open this warehouse directly. Say which warehouse it is "
    "rather than implying it will work. Two routes reach most of them: a dbt "
    "project that targets the store gives table names, columns and lineage "
    "with no warehouse connection at all; and DuckDB can attach some stores "
    "(an Iceberg REST catalog, Parquet or CSV files in object storage), which "
    "semlayer then models as DuckDB."
)


def _driver_present(module: str) -> bool:
    """Whether a connector's driver can be imported in this process.

    Guarded twice over: `find_spec` imports parent packages (so
    `google.cloud.bigquery` imports `google.cloud`), and a broken
    half-installed distribution can raise from that import. Neither is a
    reason for `status` to fail -- the answer is just "no".
    """
    import importlib.util
    try:
        return importlib.util.find_spec(module) is not None
    except Exception:
        return False


def supported() -> list[dict]:
    """The connection kinds this build can open, as the agent needs them.

    `driver_installed` is here because the drivers are optional extras: a
    server that finds a Postgres DSN it cannot open has moved the wall
    rather than removed it, and an agent that can see the driver is missing
    can say "reinstall with semlayer[warehouses]" instead of letting the
    user discover it minutes later at connect time.
    """
    out = []
    for kind in _KINDS:
        module, package = _DRIVERS[kind["kind"]]
        entry = dict(kind)
        entry["driver"] = module
        entry["driver_installed"] = _driver_present(module)
        if not entry["driver_installed"]:
            entry["note"] = (f"{entry['note']}; the {package} driver is NOT "
                             "installed -- reinstall as "
                             'semlayer[warehouses] to open this kind')
        out.append(entry)
    return out


# --- a warehouse we cannot open -------------------------------------------
#
# Names people say, mapped to what we can actually do about them. Kept
# separate from `_FAMILIES` because these are not schemes we accept; they are
# things a person says out loud that an agent must answer honestly.

_DBT_ROUTE = ("A dbt project that targets this store gives table names, "
              "columns and lineage without a warehouse connection: point "
              "semlayer at the project instead.")

_ROUTES = {
    "iceberg": ("DuckDB can attach an Iceberg REST catalog, so if DuckDB is "
                "available, model the store through it. Otherwise: "
                + _DBT_ROUTE),
    "databricks": _DBT_ROUTE,
    "trino": _DBT_ROUTE,
    "athena": _DBT_ROUTE,
    "mssql": _DBT_ROUTE,
    "oracle": _DBT_ROUTE,
    "clickhouse": _DBT_ROUTE,
    "spark": _DBT_ROUTE,
}

# What a person says -> the kind that actually opens it. The Postgres
# protocol is the reason most of this list exists: four vendors, one
# connector, and an agent that does not know that asks the user to wait for
# a connector we already have.
_SPOKEN_ALIASES = {
    "postgres": "postgres", "postgresql": "postgres", "pg": "postgres",
    "redshift": "postgres", "supabase": "postgres", "neon": "postgres",
    "timescale": "postgres", "timescaledb": "postgres", "citus": "postgres",
    "aurora": "postgres", "rds": "postgres", "cockroach": "postgres",
    "cockroachdb": "postgres", "yugabyte": "postgres", "alloydb": "postgres",
    "duckdb": "duckdb", "motherduck": "duckdb",
    "bigquery": "bigquery", "bq": "bigquery",
    "snowflake": "snowflake",
}

# Words that name one of the routes above. Checked as substrings, longest
# first, so "aws athena" and "spark sql" land somewhere.
_ROUTE_WORDS = {
    "iceberg": "iceberg", "databricks": "databricks", "unity catalog": "databricks",
    "delta lake": "databricks", "trino": "trino", "presto": "trino",
    "athena": "athena", "sql server": "mssql", "mssql": "mssql",
    "synapse": "mssql", "oracle": "oracle", "clickhouse": "clickhouse",
    "spark": "spark", "glue": "athena", "s3": "iceberg", "parquet": "iceberg",
}


def route_for(requested: str) -> dict:
    """What to tell the user about a warehouse they named.

    Three answers, and the middle one is the one worth having: a name we
    already open under a different word (Redshift is postgres), a name we
    cannot open but can route around, and a name we know nothing about.
    Never a bare "unsupported" -- that sends the agent to guess.
    """
    said = " ".join(requested.lower().split())
    bare = said.replace("-", " ").strip()
    for word, kind in sorted(_SPOKEN_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if word in bare.split() or bare == word:
            entry = next(k for k in supported() if k["kind"] == kind)
            return {
                "requested": requested[:64],
                "supported": True,
                "kind": kind,
                "uri_forms": entry["uri_forms"],
                "note": (f"this is the {kind} connection: {entry['note']}"),
                "next": (f"Tell the user semlayer opens this as {kind}, then "
                         "ask for the connection details named in "
                         "`uri_forms` -- never for a password in chat."),
            }
    for word, engine in sorted(_ROUTE_WORDS.items(), key=lambda kv: -len(kv[0])):
        if word in bare:
            return {
                "requested": requested[:64],
                "supported": False,
                "engine": engine,
                "route": _ROUTES[engine],
                "next": ("Tell the user semlayer cannot open this directly, "
                         "offer the route, and ask whether to proceed that "
                         "way."),
            }
    return {
        "requested": requested[:64],
        "supported": False,
        "engine": None,
        "route": UNSUPPORTED,
        "next": ("Tell the user semlayer cannot open this directly, offer the "
                 "route, and ask whether to proceed that way."),
    }


# --- layers already on this machine ---------------------------------------
#
# The cache survives the agent, the client and the host; what did not survive
# the first build of this module was the server KNOWING about it. A second
# session that starts empty while yesterday's layer sits on disk is the same
# wall as the first session had.

# Names worth opening before any glob: what `semlayer infer -o` suggests and
# what the docs use.
_LAYER_NAMES = ("layer.yaml", "semlayer.yaml", "semantic_layer.yaml")
_LAYER_GLOBS = (".semlayer/*.yaml", "*.yaml")

# Opening files is the one thing in `status` that scales with someone else's
# directory, so it is capped. A repo with hundreds of YAML files gets the
# known names plus this many others, not a directory walk.
_MAX_LAYER_CANDIDATES = 20

# Read enough of a document to identify it, never enough to be slow.
_MAX_SCAN_LINES = 200_000


def _git_root(start: Path) -> Path | None:
    """The nearest ancestor holding a `.git`, or None.

    Claude Code and Cursor start the server in the project; a layer file
    committed at the repo root is then one directory up from wherever the
    terminal happened to be.
    """
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return None


def _search_dirs() -> list[Path]:
    """Where a layer file would be: the working directory, then the repo root."""
    cwd = Path.cwd()
    out = [cwd]
    root = _git_root(cwd)
    if root is not None and root != cwd:
        out.append(root)
    return out


def _add(out: list[Path], path: Path) -> None:
    """Append a real file we have not already got."""
    if path.is_file() and path not in out:
        out.append(path)


def _layer_candidates() -> list[Path]:
    """Files that might be layer documents, nearest and best-named first.

    Capped: the known names always get looked at, and the glob fills the
    rest. Order matters because the cap is a cliff -- `layer.yaml` must
    never be the file we ran out of room for.
    """
    dirs = _search_dirs()
    out: list[Path] = []
    for d in dirs:
        for name in _LAYER_NAMES:
            _add(out, d / name)
    for d in dirs:
        for glob in _LAYER_GLOBS:
            for path in sorted(d.glob(glob)):
                _add(out, path)
                if len(out) >= _MAX_LAYER_CANDIDATES:
                    return out
    return out


# A document may open with a licence header, a `---`, or a wall of comments
# before it says what it is -- every hand-authored gold in this repo does, and
# a layer somebody has edited by hand usually will. Bounded, because the
# alternative is reading all of a file that is not a layer at all.
_MAX_PREAMBLE_LINES = 50


def _opens_a_layer(fh: object) -> bool:
    """Whether a file's first real line is `semantic_layer:`.

    Comments, blanks and a YAML document marker are skipped. The engine's own
    output starts with the key, which is exactly why this needed a test
    against a hand-authored file: the first version of this function demanded
    line one, and every gold in fixtures/golds was invisible to it.
    """
    for _ in range(_MAX_PREAMBLE_LINES):
        line = fh.readline()                        # type: ignore[attr-defined]
        if not line:
            return False
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped == "---":
            continue
        return stripped.startswith("semantic_layer:")
    return False


def _scalar(value: str) -> str | None:
    """A YAML scalar as written on one line, unquoted. Nothing fancier."""
    return value.strip().strip("'\"") or None


def _scan_key(stripped: str, indent: int, state: dict, out: dict) -> None:
    """One of `semantic_layer`'s own keys: note the section, keep what we want."""
    key, _, value = stripped.partition(":")
    state["top"] = indent
    state["section"] = key
    state["item"] = None
    if key in ("name", "warehouse"):
        out[key] = _scalar(value)
    elif key == "tables":
        out["tables"] = 0


def _scan_line(line: str, state: dict, out: dict) -> None:
    """Fold one line of a document's head into `out`.

    Two indents carry what we want, and neither is fixed: a key of
    `semantic_layer` sits at one level, and the entries of `tables:` at
    another. Both 2-space and 4-space list indentation are legal YAML and
    both are in this repo -- the engine emits the first, every hand-authored
    gold uses the second. So the item indent is LEARNED from the first entry
    rather than assumed, and only entries at that same indent are counted;
    a column's `- name:`, nested deeper, is not a table.
    """
    stripped = line.lstrip()
    indent = len(line) - len(stripped)
    if not stripped.strip():
        return
    if indent <= state["top"] and not stripped.startswith("- "):
        _scan_key(stripped, indent, state, out)
        return
    if state["section"] == "tables" and stripped.startswith("- name:"):
        if state["item"] is None:
            state["item"] = indent
        if indent == state["item"]:
            out["tables"] += 1
    elif state["section"] == "generated_by" and indent > state["top"]:
        key, _, value = stripped.partition(":")
        if key == "engine":
            out["engine"] = _scalar(value)
        elif key == "generated_at":
            out["inferred_at"] = _scalar(value)


def scan_layer(path: Path) -> dict | None:
    """Identify a layer document by reading it, not by parsing it.

    `status` may list twenty candidates and is called repeatedly, so parsing
    each one as YAML (tens of milliseconds for a real layer) is the wrong
    shape for a tool that must answer instantly. This reads the head for the
    name and the warehouse, and counts table entries by their indentation.

    Returns None for a file that is not a semantic layer at all, which is
    how a repo full of CI config stays out of the list.

    The count is exact for documents this engine writes and for the fixtures.
    A document in flow style, or indented differently, yields `tables: null`
    rather than a wrong number -- the loaded layer's count always comes from
    the parsed document instead.
    """
    out: dict = {"path": str(path), "name": None, "warehouse": None,
                 "engine": None, "tables": None, "inferred_at": None}
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            if not _opens_a_layer(fh):
                return None
            # `top` is the indent of `semantic_layer`'s own keys, learned
            # from the first one rather than assumed to be two spaces.
            state = {"section": "", "top": 10 ** 6, "item": None}
            for n, line in enumerate(fh):
                if n > _MAX_SCAN_LINES:
                    break
                if line.strip() and not line.startswith((" ", "\t")):
                    break                       # left the semantic_layer block
                _scan_line(line, state, out)
    except OSError:
        return None
    if out["inferred_at"] is None:
        out["inferred_at"] = _mtime(path)
    return out


def _mtime(path: Path) -> str | None:
    """A file's modification time as an ISO string, for an undated document.

    The spec defines `generated_by.generated_at` and the producer does not
    write it yet, so for most documents in existence the honest answer to
    "how old is this" is the file's own timestamp.
    """
    from datetime import datetime, timezone
    try:
        ts = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _last_used_path() -> Path:
    """Where the last layer this session loaded is remembered."""
    return home() / "last-used"


def _mark_used(ref: str) -> None:
    """Remember `ref` as the layer most recently loaded here.

    So that "load the usual one" has an answer when several are visible.
    Best-effort: a read-only home directory costs the tiebreak, not the
    session.
    """
    try:
        path = _last_used_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{ref}\n")
    except OSError:
        pass


def _last_used() -> str | None:
    """The ref remembered by `_mark_used`, if any."""
    try:
        return _last_used_path().read_text().strip() or None
    except OSError:
        return None


def cached_layers() -> list[Path]:
    """Every layer document in `$SEMLAYER_HOME/layers/`."""
    try:
        return sorted(p for p in (home() / "layers").glob("*.yaml") if p.is_file())
    except OSError:
        return []


def visible_layers() -> list[dict]:
    """Layers already on this machine: in the project, then in the cache.

    A repo copy WINS over a cache copy of the same warehouse. The repo is
    where a team's review decisions live and where a teammate can see them;
    the cache is the cold-start fallback for a client (Claude Desktop) that
    has no useful working directory at all.

    Each entry carries `ref`, which is what `load_layer` takes.
    """
    last = _last_used()
    out: list[dict] = []
    seen: set[str] = set()
    for source, paths in (("repo", _layer_candidates()), ("cache", cached_layers())):
        for path in paths:
            info = scan_layer(path)
            if info is None:
                continue
            warehouse = info["warehouse"]
            if warehouse and warehouse in seen:
                continue                 # the repo copy claimed this warehouse
            if warehouse:
                seen.add(warehouse)
            entry = {"ref": info["path"], "source": source,
                     "connection": warehouse, "name": info["name"],
                     "tables": info["tables"], "inferred_at": info["inferred_at"],
                     "engine": info["engine"]}
            if last and last in (info["path"], warehouse):
                entry["last_used"] = True
            out.append(entry)
    return out


# What `generated_by.model` says when a run had no model: statistics, naming
# rules and inclusion tests, and no sentence written by anything.
_NO_MODEL = {"", "none", "none/heuristic"}


def built_without_model(doc: dict) -> bool | None:
    """Whether THIS engine built the layer with no model. None if unknown.

    Three states, and the third is why this is not a boolean. A document we
    produced records `generated_by.model`, so "none/heuristic" is a fact. A
    document with no `generated_by` at all was not written by us -- every
    hand-authored gold in fixtures is one -- and we know nothing about how it
    was made. Claiming "no descriptions" there would tell someone their
    hand-written layer was statistics-only, and counting descriptions does
    not save us: the messy_mart gold has none at table level either.
    """
    generated = (doc.get("semantic_layer") or {}).get("generated_by")
    if not generated:
        return None
    return str(generated.get("model") or "").strip().lower() in _NO_MODEL


def save_document(path: str | Path, doc: dict) -> None:
    """Write a semantic layer document. The sibling of `load_document`.

    Here rather than in the CLI because the infer tool writes the cache copy
    and the CLI writes the user's file, and one spelling of "how a layer is
    serialised" is the difference between those two files being the same
    document or merely similar ones.
    """
    import yaml

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, sort_keys=False, width=100))


# How many connections the `next` sentence names before it starts counting.
_NAMED_IN_NEXT = 6


def _next_ready(payload: dict) -> str:
    """What to do with a layer in hand -- and, on day 2, what to admit first."""
    layer = payload.get("layer") or {}
    asked = payload.get("pending_question")
    if asked:
        # The reminder the whole wait-and-return design needs: the layer
        # finished after the turn that asked for it, so the agent is told
        # what it was for rather than expected to remember.
        return (f"The layer is ready, and the user's question is still "
                f"unanswered: {asked!r}. Answer it now -- start with "
                "context_for_question, check_sql the SQL you write, then "
                "run_sql. Say which warehouse the answer came from.")
    if payload.get("autoloaded"):
        where = layer.get("warehouse") or layer.get("path") or "this machine"
        return (f"A semantic layer for {where} was already on this machine and has "
                f"been loaded (inferred {layer.get('inferred_at')}). Name that "
                "warehouse to the user before you answer anything, so they can say "
                "if it is the wrong one -- call status again after load_layer if it "
                "is. Then start with semantic_search or get_domains, and "
                "table_detail before you write any SQL.")
    return ("A semantic layer is loaded: start with semantic_search or get_domains "
            "to find your way around, and table_detail before you write any SQL. "
            "Apply every required_filter, and run check_sql before any SQL runs.")


def _describe(layer: dict) -> str:
    """One layer in words a person would recognise.

    The `ref` of a cached layer is a path with a hash in it, which is the
    right thing for `load_layer` to take and the wrong thing to read out to
    a user. So the sentence names the layer and its warehouse, and the agent
    takes the ref from the list.
    """
    bits = [layer.get("name") or Path(layer["ref"]).name]
    if layer.get("connection"):
        bits.append(str(layer["connection"]))
    if layer.get("tables"):
        bits.append(f"{layer['tables']} tables")
    bits.append("in your project" if layer.get("source") == "repo" else "cached")
    return f"{bits[0]} ({', '.join(bits[1:])})"


def _name_some(values: list[str]) -> str:
    """Name a few things, then count the rest. The full list is in the payload."""
    named = ", ".join(values[:_NAMED_IN_NEXT])
    if len(values) > _NAMED_IN_NEXT:
        named += f", and {len(values) - _NAMED_IN_NEXT} more (see the payload)"
    return named


def _env_file_report() -> dict:
    """Where credentials go, and which keys that file currently sets.

    Key NAMES only, never values -- the same rule discovery follows. The
    names are the useful half anyway: an agent that prescribed a line can
    see whether it landed, which is what turns "add this line and tell me"
    into a loop that closes without a restart.
    """
    path = env_path()
    return {"path": str(path),
            "exists": path.is_file(),
            "keys": sorted(env_file.read(path)),
            "note": ("add a line here and call status again -- this file is "
                     "re-read on every call, so no restart is needed. Never "
                     "ask the user for a secret in chat.")}


def _next_inferring(payload: dict) -> str:
    """A run is going. Say where, and do not promise to come back."""
    job = payload.get("job") or {}
    asked = job.get("question")
    where = job.get("doing") or job.get("stage") or "starting"
    percent = round((job.get("fraction") or 0) * 100)
    eta = job.get("eta_s")
    # "roughly", always: the estimate is an extrapolation from how fast this
    # warehouse has answered so far, and a number to the second invites
    # somebody to hold us to it.
    left = (f", roughly {eta // 60} min left" if eta and eta >= 90 else
            f", roughly {eta}s left" if eta else "")
    sentence = (f"A semantic layer is being built for {job.get('connection')} -- "
                f"about {percent}%, {where}, {job.get('elapsed_s')}s so far"
                f"{left}. Tell the user that, in those terms, and in words "
                "rather than as a number alone. NOTHING WILL WAKE YOU when it "
                "finishes: call status again when the user next says anything, "
                "or if your client lets you, poll it.")
    if asked:
        sentence += (f" They asked: {asked!r} -- answer that as soon as the "
                     "layer is ready.")
    if payload.get("layer"):
        sentence += (" The previous layer is still loaded and still answerable "
                     "-- say that it is the previous one. But run_sql is "
                     "refused until the run finishes: the warehouse connection "
                     "is not shared between the two.")
    return sentence


def next_step(payload: dict) -> str:
    """What the agent should do next, in one sentence, written to be acted on.

    ONE place, because this sentence is a promise about what the server can
    do. The hardest one to write is still the common one: no layer, because
    the thing the agent has to tell the user is not "I could not load a file"
    but "this does not exist yet and has to be built".
    """
    if payload.get("state") == "inferring":
        return _next_inferring(payload)
    if payload.get("state") == "ready":
        return _next_ready(payload)
    layers = payload.get("layers") or []
    if layers:
        named = _name_some([_describe(x) for x in layers])
        usual = next((_describe(x) for x in layers if x.get("last_used")), None)
        sentence = ("No layer is loaded, but these are already on this machine: "
                    f"{named}. Ask the user which one they mean, then call "
                    "load_layer with that layer's `ref` from the list above.")
        if usual:
            sentence += f" They used {usual} last."
        return sentence
    usable = [c["uri"] for c in (payload.get("connections") or [])
              if c.get("supported")]
    if usable:
        return ("No semantic layer exists for this machine yet, and one has to be "
                "BUILT before any question about the data can be answered -- there "
                "is nothing to search or query until then. These warehouse "
                f"connections are visible from here: {_name_some(usable)}. Ask the "
                "user which one to model -- or whether their warehouse is somewhere "
                "else -- and then call infer with that uri and their question. "
                "Inference reads their warehouse and takes minutes on a real one, "
                "so ask before starting it.")
    return ("No semantic layer exists on this machine, and none can be built until "
            "you know where the data is -- tell the user that, then ask where it "
            "lives. For Postgres, Redshift, Supabase or Neon ask for a DSN without "
            "the password, or the name of a dbt profile. For BigQuery ask for the "
            "project id. For DuckDB ask for the file path. For Snowflake ask for "
            "the connection name from their connections file, or the account and "
            "user. If it is none of these, call discover_connections with "
            "`requested` set to what they said. Then call infer with the uri and "
            "their question.")


# --- a credential someone pasted into the chat ----------------------------
#
# The agent is told never to ask for one, and people paste them anyway. The
# choice (Raj, 2026-11-01) is to accept it for the session rather than refuse:
# refusing teaches nothing and strands the user, while accepting with a clear
# "here is the durable place" moves them to the right place next time.
#
# The whole value of that choice depends on the secret going nowhere. It lives
# in one attribute, in memory; `connection` -- which is what the status payload
# reports and what the cache key hashes -- is the redacted form.


def redact(connection: str) -> str:
    """The connection string with any secret replaced, its shape intact.

    Distinct from `_identity`, which is for hashing and may normalise
    whatever it likes. This is for a human and an agent to read, so the host,
    the database and the user survive and only the secret is replaced: a
    redaction that destroys the rest is one the user cannot recognise as
    their own connection.
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    raw = connection.strip()
    if "://" not in raw:
        return raw
    u = urlsplit(raw)
    netloc = u.hostname or ""
    if u.username:
        netloc = f"{u.username}:***@{netloc}" if u.password else f"{u.username}@{netloc}"
    elif u.password:
        netloc = f"***@{netloc}"
    if u.port:
        netloc = f"{netloc}:{u.port}"
    # `safe="*"`, or the redaction marker itself comes back percent-encoded
    # as %2A%2A%2A and the user cannot read their own connection string.
    query = urlencode([(k, "***" if k.lower() in _SECRET_QUERY_KEYS else v)
                       for k, v in parse_qsl(u.query, keep_blank_values=True)],
                      safe="*")
    return urlunsplit((u.scheme, netloc, u.path, query, u.fragment))


def carries_secret(connection: str) -> bool:
    """Whether this connection string has a credential embedded in it."""
    return redact(connection) != connection.strip()


# What to tell the agent when it worked anyway. Three things, in this order,
# because the first is what the user asked and the last is what we want them
# to do: it worked, it dies with the server, here is the durable line.
def pasted_credential_reply(connection: str) -> dict:
    """The answer to a secret in a connection string: accepted, and transient."""
    return {
        "credential": "accepted for this session only",
        "held": "in memory, for as long as this server runs",
        "never": ("written to disk, logged, sent in telemetry, recorded in the "
                  "layer or the run report, or used in the cache key"),
        "gone_when": "this server restarts, which your MCP client does often",
        "durable_instead": redact(connection),
        "next": ("Tell the user it worked, that the credential is gone when the "
                 "server restarts, and that the durable place is a line in "
                 f"{_tilde(env_path())} -- the connection above with the secret "
                 "taken out, plus the credential on its own line. If they are "
                 "not on a read-only role, `semlayer init <kind>` prints one. "
                 "Say once that rotating a credential pasted into a chat is the "
                 "careful thing to do."),
    }


# --- the inference job ----------------------------------------------------
#
# MCP has no way to wake a model when a background job finishes, and nothing
# here may pretend otherwise. So the shape is: start it, wait a little in case
# it is quick, and if it is not, hand back `inferring` and let the next
# `status` carry the news. The agent is reminded by the payload rather than
# expected to remember.

# How long a tool call may block. A DuckDB file with no LLM finishes in
# seconds, which is the case worth optimising: discovered, inferred and
# answered in one turn, with no polling at all. Beyond this the call returns
# and the job keeps running.
INFER_WAIT_CAP_S = 60
INFER_WAIT_DEFAULT_S = 25


# What share of a run each stage takes, for the progress fraction. MEASURED,
# because the four stages are nowhere near equal and a bar built on equal
# weights would stall for minutes and then jump.
#
# With a model: the live 116-table dbt_synthea run in docs/scale.md
# (2026-10-28, 8 workers, all four LLM loops pooled) -- profile 53 s, link
# 142 s, describe 159 s of 354 s. Enrich did not register against those, so
# it gets a small tail rather than zero: a bar that reaches 100% while the
# run is still going is worse than one that ends at 97%.
#
# Without a model: the six built fixtures, 2026-11-02. The split depends on
# size -- a 4-table fixture is nearly all fixed cost -- so these come from
# the three big enough for a bar to matter at all: messy_mart 10/73/16,
# tpcds_clean 21/75/4, collision_heavy 20/79/1.
STAGE_WEIGHTS_LLM = {"profile": 0.15, "link": 0.39, "describe": 0.43,
                     "enrich": 0.03}
STAGE_WEIGHTS_NO_LLM = {"profile": 0.17, "link": 0.76, "enrich": 0.07}

# What each stage is doing, in words a person would use. The agent reads
# this out, and "enrich" is our word for it, not theirs.
STAGE_WORDS = {
    "profile": "measuring every column",
    "link": "finding the keys and joins nobody declared",
    "describe": "writing descriptions",
    "enrich": "metrics, hierarchies and routing knowledge",
}

# Below this, an estimate is guesswork dressed up as information.
_ETA_MIN_FRACTION = 0.05
_ETA_MIN_ELAPSED_S = 5
# And below this there is nothing useful to say: "roughly 10 seconds left"
# on a run that is about to finish is noise, and on a short run the floor
# was reported for the whole second half of it.
_ETA_MIN_REMAINING_S = 20
# Rounded to this, because "roughly" is the honest word and a number to the
# second invites someone to hold us to it.
_ETA_ROUND_S = 10

# A running job never reports 100%. The last stage finishing is not the run
# finishing -- the document still has to be written and swapped in -- and a
# bar that sits at 100% while the thing is still going is the single most
# common way a progress bar lies.
_MAX_RUNNING_FRACTION = 0.99


class Cancelled(Exception):
    """Raised inside the pipeline's progress callback to stop a run."""


class InferJob:
    """One inference, running on a thread the tool call does not wait for.

    Cancellation is cooperative and lands at a stage boundary: the progress
    callback raises `Cancelled`, which unwinds the pipeline. Killing a thread
    is not possible in Python and pretending otherwise would leave a half-run
    holding a warehouse connection.
    """

    def __init__(self, connection: str, question: str | None, scope: str | None,
                 with_llm: bool = True):
        self.connection = redact(connection)
        self.question = question or None
        self.scope = scope or None
        self.started_at = time.time()
        self.state = "running"          # running | done | failed | cancelled
        self.stage: str | None = None
        self.stages: dict[str, float] = {}
        self.weights = STAGE_WEIGHTS_LLM if with_llm else STAGE_WEIGHTS_NO_LLM
        self.label: str | None = None
        self.tables_done: int | None = None
        self.tables_total: int | None = None
        self._within = 0.0
        self.error: str | None = None
        self.exception: BaseException | None = None
        self.doc: dict | None = None
        self.report: dict | None = None
        self.model: str | None = None
        self._cancel = threading.Event()
        self.thread: threading.Thread | None = None

    @property
    def elapsed_s(self) -> int:
        """Whole seconds since the run started."""
        return int(time.time() - self.started_at)

    @property
    def running(self) -> bool:
        """Whether the run is still going."""
        return self.state == "running"

    def cancel(self) -> None:
        """Ask the run to stop at its next stage boundary."""
        self._cancel.set()

    @property
    def fraction(self) -> float:
        """How far through the whole run, 0 to 1, from the measured weights.

        Completed stages contribute their whole weight; the running one
        contributes its weight times how far through itself it says it is.
        Monotonic by construction, which matters more than being exactly
        right: a bar that goes backwards reads as a bug in the thing being
        watched.
        """
        done = sum(w for s, w in self.weights.items() if s in self.stages)
        current = self.weights.get(self.stage or "", 0.0)
        if self.stage in self.stages:
            current = 0.0           # already counted above
        at = min(1.0, done + current * self._within)
        if self.running:
            at = min(at, _MAX_RUNNING_FRACTION)
        return round(at, 3)

    @property
    def eta_s(self) -> int | None:
        """Roughly how many seconds are left, or None when it would be a guess.

        Extrapolated from the fraction, which is itself driven by per-unit
        rates inside the stage -- so it already reflects how fast THIS
        warehouse is answering, rather than any assumption about size. Not
        offered in the first seconds or the first few percent, and rounded,
        because the honest word is "roughly".
        """
        f, elapsed = self.fraction, self.elapsed_s
        if f < _ETA_MIN_FRACTION or elapsed < _ETA_MIN_ELAPSED_S or not self.running:
            return None
        remaining = elapsed * (1 - f) / f
        if remaining < _ETA_MIN_REMAINING_S:
            return None
        return int(round(remaining / _ETA_ROUND_S) * _ETA_ROUND_S)

    def progress(self, event: dict) -> None:
        """The pipeline's callback: record where it is, and stop if asked to."""
        if self._cancel.is_set():
            raise Cancelled("cancelled by the user")
        stage = str(event.get("stage") or "")
        kind = event.get("event")
        if kind == "done":
            self.stages[stage] = event.get("seconds") or 0
            self._within = 0.0
        elif kind == "unit":
            self.stage = stage
            self._within = float(event.get("within") or 0.0)
            self.label = event.get("label")
            if stage in ("profile", "describe"):
                # Tables are the unit a person understands. Link's units are
                # inclusion queries and model chunks, which are ours.
                self.tables_done = event.get("done")
                self.tables_total = event.get("total")
        else:
            # A stage boundary. Clear what belonged to the last stage: enrich
            # has no per-unit loop, so without this the label would still
            # read "checking inclusion: 79 of 79" while link was over -- and
            # that line is what the agent reads out to the user.
            self.stage = stage
            self._within = 0.0
            self.label = None
            self.tables_done = self.tables_total = None

    def as_dict(self) -> dict:
        """What `status` reports about this run. No credential, ever."""
        out = {
            "state": self.state,
            "connection": self.connection,
            "elapsed_s": self.elapsed_s,
            "stage": self.stage,
            "fraction": self.fraction,
            # The per-unit label while a hooked loop is running, else the
            # stage in words: enrich has no loop, and "doing: null" would
            # leave the agent with nothing to say.
            "doing": self.label or STAGE_WORDS.get(self.stage or ""),
            "stages_done": self.stages,
            "question": self.question,
        }
        if self.tables_total:
            out["tables_done"] = self.tables_done
            out["tables_total"] = self.tables_total
        eta = self.eta_s
        if eta is not None and self.running:
            out["eta_s"] = eta
        if self.model:
            out["model"] = self.model
        if self.error:
            out["error"] = self.error
        return out


class Session:
    """Which connection, which layer, and the state that follows from both.

    State is derived, never assigned: a session is `ready` when it holds a
    document and `no_layer` when it does not. There is no way to be told you
    are ready and not be. `inferring` arrives with the infer tool, which is
    the next slice -- the running job will live here too.
    """

    def __init__(self, connection: str | None = None) -> None:
        self.connection = connection
        self.layer_path: Path | None = None
        self.doc: dict | None = None
        # Reserved for the infer tool: the question the user asked before the
        # layer existed, so the status payload can remind the agent to answer
        # it once the job finishes. Always null in this build, and in the
        # payload now so the next slice is a fill-in, not a shape change.
        self.pending_question: str | None = None
        # Whether the loaded layer was found rather than asked for. The agent
        # has to say so before answering: a layer we chose on their behalf is
        # a layer they have not agreed to.
        self.autoloaded = False
        # A credential pasted into the chat, if one was. The full connection
        # string lives HERE and nowhere else: `connection` above is the
        # redacted form, which is what the payload reports and what the cache
        # key hashes. Never written, never logged, never sent.
        self._credential: str | None = None
        # The open warehouse connection, reused across infer and run_sql, and
        # the running job if there is one.
        self._source = None
        self.job: InferJob | None = None
        # Held for as long as one operation is using that connection. The
        # connectors are not built to be shared: DuckDB forbids two threads
        # on one connection object (its own docstring says so, and
        # `for_worker` exists because of it), and an agent's query arriving
        # mid-inference would be a second user of the same handle.
        self._in_use = threading.Lock()
        # Whether a question has been answered yet, and whether the one
        # sentence about the model key has been said. Both exist so that
        # sentence lands AFTER the first answer and exactly once: a stranger
        # has no key, so day 1 has to work keyless and the suggestion must
        # never read as a precondition.
        self.answered_once = False
        self.told_about_key = False

    @classmethod
    def of(cls, layer: Session | dict) -> Session:
        """A session from either a session or a bare document.

        `build_server(doc)` is how every in-process test and the agent example
        build a server, and those callers have a document and no connection.
        Accepting both here means the server takes a session without a flag day
        for the callers that do not have one.
        """
        if isinstance(layer, Session):
            return layer
        s = cls()
        s.doc = layer
        return s

    @property
    def state(self) -> str:
        """`no_layer`, `inferring`, or `ready`.

        A running job wins over a loaded layer, because "a new layer is being
        built" is the more actionable fact and the agent should caveat what it
        answers from the old one. The tools still answer from the document
        that is loaded: `inferring` describes the session, not the document.
        """
        if self.job is not None and self.job.running:
            return "inferring"
        return "ready" if self.doc is not None else "no_layer"

    def load(self, path: str | Path) -> None:
        """Load the layer at `path`. An explicit path wins over everything.

        Raises, as the CLI always has: someone who named a file wants that
        file, and silently serving an empty session instead would be a
        mystery rather than an error.
        """
        # Built first, swapped after: a tool call running concurrently sees
        # either the old document or the new one, never a half-built dict.
        # `load_document` returns a fresh object and nothing here mutates the
        # one already loaded, which is the property the infer job needs when
        # it swaps a layer in under a live conversation.
        doc = load_document(path)
        self.doc, self.layer_path = doc, Path(path)

    def load_cached(self, connection: str) -> bool:
        """Load the cached layer for `connection`, if there is a readable one.

        Returns whether it loaded, and never raises. A cache entry is ours,
        not the user's: if it is half-written or was produced by a version
        whose output this one cannot read, the right answer is to start empty
        and say so, not to refuse to start the server at all.
        """
        from semlayer.errors import SemlayerError

        path = layer_cache_path(connection)
        try:
            doc = load_document(path)
        except (SemlayerError, OSError):
            return False
        self.doc, self.layer_path = doc, path
        return True

    def load_layer(self, ref: str) -> dict:
        """Load one of the layers `status` listed, by its `ref`.

        Takes a ref as listed, a connection string one of them is for, or a
        path on disk -- an agent relaying what a user typed should not have
        to care which of the three it has. Returns the new status, so the
        agent sees the state it just produced instead of calling again.

        Unlike `load`, this NEVER raises. `load` is the CLI's contract:
        someone who typed a path wants that path, and an error is the honest
        answer. Here the caller is an agent repeating something a person
        said, and a raise reaches it as a bare protocol error with nowhere to
        go. So a ref we cannot load comes back as the status object with an
        `error` on it -- which still lists `layers`, so the agent's next move
        is in its hands.
        """
        from semlayer.errors import SemlayerError

        wanted = ref.strip()
        match = next((x for x in visible_layers()
                      if wanted in (x["ref"], x["connection"])), None)
        path = Path(match["ref"]) if match else Path(wanted).expanduser()
        try:
            self.load(path)
        except (SemlayerError, OSError) as e:
            out = self.status()
            out["error"] = (f"{wanted[:200]!r} is not a layer this server can "
                            f"load ({e}). The `layers` list above is what is "
                            "actually here; ask the user which of those they "
                            "mean, or have them build one.")
            return out
        if match and match["connection"]:
            self.connection = match["connection"]
        elif self.doc is not None:
            self.connection = (self.doc.get("semantic_layer", {}).get("warehouse")
                               or self.connection)
        self.autoloaded = False
        _mark_used(str(path))
        return self.status()

    def autoload(self) -> bool:
        """Load the layer already on this machine, when there is exactly one.

        Day 2 is a new chat, and often a new host: the person who inferred
        yesterday should not have to say anything today. One visible layer is
        an unambiguous answer, so it is loaded and `next` names the warehouse
        for them to object to. Two or more is a question, not a default.
        """
        if self.doc is not None or self.connection:
            return False
        layers = visible_layers()
        if len(layers) != 1:
            return False
        try:
            self.load(layers[0]["ref"])
        except Exception:
            # Bare, like `load_cached`: a layer we FOUND rather than one the
            # user named must never be the reason the server fails to start.
            return False
        self.connection = layers[0]["connection"] or self.connection
        self.autoloaded = True
        _mark_used(layers[0]["ref"])
        return True

    def select(self, connection: str) -> bool:
        """Select `connection`, loading its cached layer if one exists.

        A secret in the string is split off here and nowhere else: what the
        session reports from now on is the redacted form, and the full string
        survives only in `_credential`, in memory.
        """
        self.close_source()
        self._credential = connection if carries_secret(connection) else None
        self.connection = redact(connection)
        return self.load_cached(connection)

    def target(self) -> str | None:
        """The connection string to actually open -- with the secret, if any."""
        return self._credential or self.connection

    def open_source(self):
        """Open the warehouse this session is about, once, and keep it.

        One connection serves inference and the agent's own queries: the
        alternative is a fresh login per tool call, which on Snowflake is
        seconds of latency inside a chat turn.

        The `.env` file is applied for the duration of the open and taken
        back afterwards, because the connectors read `os.environ` themselves
        at that moment. Nothing it says outlives the call.
        """
        from semlayer.errors import SourceError

        if self._source is not None:
            return self._source
        target = self.target()
        if not target:
            raise SourceError(
                "no warehouse connection is selected for this session",
                hint="call status to see what is visible, then pass a uri")
        from semlayer.pipeline import open_source_from_uri
        with env_file.applied(env_file.read(env_path())):
            self._source = open_source_from_uri(target)
        return self._source

    def close_source(self) -> None:
        """Release the warehouse connection, if one is open."""
        source, self._source = self._source, None
        close = getattr(source, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass            # a connection already dead needs no closing

    def start_infer(self, connection: str | None = None, *,
                    question: str | None = None, scope: str | None = None,
                    wait_seconds: float = INFER_WAIT_DEFAULT_S,
                    use_llm: bool = True) -> dict:
        """Build a layer for `connection`, waiting only a little for it.

        The wait is the whole design. A DuckDB file with no LLM finishes in
        seconds, and waiting for it means the user is discovered, inferred
        and ANSWERED in one turn with no polling. A real warehouse will not
        finish, so the call returns `inferring` and the next `status` carries
        the news -- MCP cannot wake a model when a job ends, and nothing here
        pretends it can.

        `question` is what the user actually asked. It is kept so that the
        status payload can remind the agent to answer it once the layer
        exists, rather than relying on the agent to remember across turns.
        """
        if self.job is not None and self.job.running:
            out = self.status()
            out["error"] = ("an inference is already running for "
                            f"{self.job.connection}; call status to watch it, "
                            "or cancel_infer to stop it")
            return out
        if connection and redact(connection) != self.connection:
            self.select(connection)
        elif connection and carries_secret(connection):
            # The same warehouse with a new secret -- a rotated password, or
            # the user pasting it again. Only ever REPLACE a credential with
            # another one: the agent sees the redacted form in `status`, so
            # the redacted form is what it passes back, and treating that as
            # "no secret given" wiped the held credential and sent the
            # driver a literal `***`.
            self._credential = connection
        if question:
            self.pending_question = question
        target = self.target()
        if not target:
            out = self.status()
            out["error"] = ("no connection to infer from: pass one, or ask the "
                            "user which of `connections` to model")
            return out
        job = InferJob(target, question, scope,
                       with_llm=use_llm and self._api_key() is not None)
        self.job = job
        job.thread = threading.Thread(
            target=self._run_infer, args=(job, use_llm), daemon=True,
            name="semlayer-infer")
        job.thread.start()
        if wait_seconds:
            job.thread.join(timeout=max(0.0, min(wait_seconds, INFER_WAIT_CAP_S)))
        return self.after_infer(job, target)

    def after_infer(self, job: InferJob, target: str) -> dict:
        """The status to answer an `infer` call with, once it has waited.

        Separate from `start_infer` because the MCP tool does its own
        waiting: it has to stay on the event loop to stream progress to the
        client, so it cannot use a blocking join.
        """
        out = self.status()
        if job.state == "failed":
            out["error"] = job.error
        if carries_secret(target):
            out["credential"] = pasted_credential_reply(target)
        return out

    @contextmanager
    def _exclusive(self, job: InferJob | None = None) -> Iterator[None]:
        """Take the warehouse connection for one operation.

        A run WAITS for a query rather than refusing, because a query is
        seconds and a run is minutes -- but a query may now hold the
        connection for as long as its own time limit, so the wait is
        reported: `status` says the run is waiting for the connection
        instead of claiming it is still starting.
        """
        if not self._in_use.acquire(blocking=False):
            if job is not None:
                job.stage = "waiting for the warehouse connection"
            self._in_use.acquire()
        try:
            yield
        finally:
            self._in_use.release()

    def _run_infer(self, job: InferJob, use_llm: bool) -> None:
        """The job body. Runs on its own thread; never raises out of it."""
        import time as _time
        started = _time.time()
        try:
            # Held for the whole run: an agent query arriving mid-inference
            # is refused rather than allowed onto the same connection.
            with self._exclusive(job):
                doc = self._infer_once(job, use_llm)
            path = layer_cache_path(self.target() or "")
            save_document(path, doc)
            job.doc, job.state = doc, "done"
            # The swap: a fresh document, assigned over the old reference.
            # A tool call in flight reads one or the other, never a dict
            # being filled in.
            self.doc, self.layer_path = doc, path
            self.autoloaded = False
            _mark_used(str(path))
        except Cancelled:
            job.state = "cancelled"
        except BaseException as e:
            # Anything at all: this is a thread, and an exception escaping it
            # would be printed to stderr and lost rather than reported to the
            # agent that asked for the run.
            job.state, job.error = "failed", f"{type(e).__name__}: {e}"[:400]
            # Kept for the telemetry class, which maps the TYPE and never
            # reads the message -- that is where table names and SQL live.
            job.exception = e
        finally:
            self._note_infer(job, started)

    def _infer_once(self, job: InferJob, use_llm: bool) -> dict:
        """Open, infer, return the document. The part that can fail."""
        from semlayer.pipeline import InferOptions, infer_with_report
        from semlayer.source import document_name

        source = self.open_source()
        llm = self._provider() if use_llm else None
        job.model = getattr(llm, "model", None) if llm is not None else "none/heuristic"
        schema = {x.strip() for x in (job.scope or "").split(",") if x.strip()} or None
        options = InferOptions(schema=schema,
                               name=document_name(None, schema, source))
        doc, report = infer_with_report(source, llm=llm, options=options,
                                        progress=job.progress)
        job.report = report
        return doc

    def _api_key(self) -> str | None:
        """The model key, from the `.env` file or the environment.

        Separate from `_provider` because the progress weights need to know
        whether describe will run at all, and constructing a provider to
        find that out would be wasteful and surprising.
        """
        return (env_file.read(env_path()).get("ANTHROPIC_API_KEY")
                or os.environ.get("ANTHROPIC_API_KEY") or None)

    def _provider(self):
        """The LLM provider for an inference, or None if there is no key.

        Mirrors the CLI: descriptions are what the LLM is for, and a run
        without a key still produces a layer -- statistics, keys, joins,
        aggregates -- so a missing key degrades the result rather than
        failing the call. `repo_cache=False` per #73: a server has no
        business writing cassettes into someone's repository.
        """
        key = self._api_key()
        if not key:
            return None
        from semlayer.llm.provider import AnthropicProvider
        with env_file.applied({"ANTHROPIC_API_KEY": key}):
            return AnthropicProvider(repo_cache=False)

    def _note_infer(self, job: InferJob, started: float) -> None:
        """Record the `infer` event for a run started from an agent.

        A separate event rather than fields on the session's own event,
        because one session may infer more than once and because this must be
        comparable with the CLI's infer events -- same event name, same
        command, `via` the only difference. Fields are passed explicitly, not
        through `telemetry.note`, which is process-scoped and would smear
        this run's counts across the session event written at exit.
        """
        import time as _time
        try:
            from semlayer import telemetry
            from semlayer.errors import error_class
            sl = (job.doc or {}).get("semantic_layer", {})
            tables = sl.get("tables") or []
            props: dict = {
                "command": "infer",
                "via": "mcp",
                "connector": family(job.connection) or "none",
                "duration_s": int(_time.time() - started),
                "outcome": {"done": "ok", "cancelled": "interrupted"}.get(
                    job.state, "error"),
                "llm": "anthropic" if job.model not in (None, "none/heuristic") else "none",
            }
            if tables:
                props["tables"] = len(tables)
                props["columns"] = sum(len(t.get("columns") or []) for t in tables)
            if job.report and job.report.get("stages"):
                props["stages"] = {k: int(v) for k, v in job.report["stages"].items()}
            if job.state == "failed":
                props["error_class"] = error_class(job.exception or Exception())
            telemetry.record("cli", t=int(started), **props)
        except Exception:
            pass        # telemetry may never break a run, let alone a thread

    def discover(self) -> list[dict]:
        """Warehouse connections visible from here. Names only; opens nothing."""
        return discover()

    def route(self, requested: str) -> dict:
        """What to tell a user who named a warehouse that was not listed."""
        return route_for(requested)

    def run_query(self, doc: dict, sql: str) -> dict:
        """Run the agent's SQL, if the warehouse connection is free.

        Refused rather than queued while an inference is running, and the
        asymmetry is deliberate: a query is seconds and the agent can simply
        ask again, while a run is minutes and must not be thrown away or
        interfered with. The inference waits for a query; a query does not
        wait for an inference.

        Sharing the connection instead is not an option. DuckDB forbids two
        threads on one connection object, and on every engine the profiling
        queries and the agent's query would be interleaving on one handle.
        """
        from semlayer import agent_query

        if not self._in_use.acquire(blocking=False):
            out = self.status()
            out["ran"] = False
            out["error"] = ("the warehouse connection is busy with the "
                            "inference that is running, and semlayer will not "
                            "share one connection between the two")
            out["next"] = ("Tell the user the query has to wait for the run to "
                           "finish, say roughly where the run is, and call "
                           "run_sql again when status says `ready`.")
            return out
        try:
            dialect = agent_query.dialect_for(family(self.connection or ""))
            source = self.open_source()
            out = agent_query.run(doc, source, sql, dialect=dialect)
        finally:
            self._in_use.release()
        if out.get("ran"):
            self.answered_once = True
            note = self._model_key_note(doc)
            if note:
                out["next"] = f"{out['next']} {note}"
        return out

    def _model_key_note(self, doc: dict) -> str | None:
        """The one sentence about what a model key would add, said once.

        After the first answer, never before it: suggesting a key as a
        precondition turns a keyless day 1 -- which works, and is what a
        stranger has -- into a setup step. And once only, because the second
        time it is nagging.
        """
        if self.told_about_key or not built_without_model(doc):
            return None
        self.told_about_key = True
        if self._api_key():
            # They have a key now; the layer predates it. Telling them to
            # add one would be wrong, and re-inferring is the actual fix.
            return ("Say once, after your answer: this layer was built from "
                    "statistics only, so nothing in it has a written "
                    "description -- `infer` again and it will add them, now "
                    "that a model key is set.")
        return (f"Say once, after your answer: this layer was built from "
                f"statistics only -- measured columns, inferred keys, no "
                f"written descriptions. A model key in {_tilde(env_path())} "
                "adds them next time it is built. Do not make it sound "
                "required; everything here works without one.")

    def cancel_infer(self) -> dict:
        """Ask a running inference to stop, and say what happened.

        Cooperative, and it lands at the next stage boundary: a thread cannot
        be killed, and a run abandoned mid-stage would still hold a warehouse
        connection and still be charging for queries. So this returns
        immediately with `cancelling` and the next `status` shows the end.
        """
        out = self.status()
        if self.job is None or not self.job.running:
            out["error"] = "no inference is running"
            return out
        self.job.cancel()
        out["cancelling"] = (f"asked the run to stop at the end of "
                             f"{self.job.stage or 'the current stage'}; call "
                             "status to see it finish")
        return out

    def status(self) -> dict:
        """One object, written for an agent to act on rather than to read.

        Every other tool answers with THIS when the state is wrong for it, so
        an agent that calls `get_tables` before a layer exists is told what to
        do instead of handed an error. That is the whole contract: a tool call
        never dead-ends.
        """
        payload = {
            "state": self.state,
            "connection": self.connection,
            "job": self.job.as_dict() if self.job is not None else None,
            "layer": self._layer(),
            "layers": visible_layers(),
            "connections": discover(),
            "supported": supported(),
            "unsupported": UNSUPPORTED,
            "env_file": _env_file_report(),
            "pending_question": self.pending_question,
            "autoloaded": self.autoloaded,
        }
        payload["next"] = next_step(payload)
        return payload

    def _layer(self) -> dict | None:
        """What is loaded: size, provenance, and how much of it nobody has read."""
        if self.doc is None:
            return None
        sl = self.doc.get("semantic_layer", {})
        tables = sl.get("tables") or []
        generated = sl.get("generated_by") or {}
        return {
            "path": str(self.layer_path) if self.layer_path else None,
            "name": sl.get("name"),
            "warehouse": sl.get("warehouse"),
            "tables": len(tables),
            "columns": sum(len(t.get("columns") or []) for t in tables),
            "inferred_at": (generated.get("generated_at")
                            or (_mtime(self.layer_path) if self.layer_path else None)),
            "engine": generated.get("engine"),
            "model": generated.get("model"),
            # Unmistakable rather than a string the agent has to recognise:
            # `model: "none/heuristic"` is us saying it, not them reading it.
            "built_without_model": built_without_model(self.doc),
            "undecided_review_items": self._undecided(),
        }

    def _undecided(self) -> int | None:
        """How many inferred claims are still waiting for a human verdict.

        Guarded: the review queue walks the whole document, and a document
        this version cannot walk is a reason to omit a number, not a reason
        for `status` -- the tool that exists to work when nothing else does --
        to fail.
        """
        from semlayer.review import collect
        try:
            return len(collect(self.doc or {}))
        except Exception:
            return None


# --- discovery: one function per source, in the order they are reported ----
#
# Order is precedence: the first entry to claim a URI keeps it. It runs
# cheapest-and-most-local first (a file in front of you) and most-ambient
# last (a credential file in your home directory), because that is the order
# in which someone would expect to be asked about them.


# DuckDB files in the working directory. First because it is the most local
# thing there is: a file you can see, needing no credential and no network.
_DUCKDB_GLOBS = ("*.duckdb", "*.db")

# Non-recursive and capped. A directory listing is cheap; a recursive walk of
# someone's home directory is not, and twenty names is already more than an
# agent should be asking a person to choose between.
_MAX_DUCKDB_FILES = 20


def _duckdb_in_cwd(env: Mapping[str, str]) -> list[dict]:
    """DuckDB files sitting in the working directory.

    `.db` is included because that is what plenty of people call a DuckDB
    file, and noted because it is also what SQLite files are called -- we
    cannot tell which without opening it, and discovery opens nothing.
    """
    del env           # a directory listing, not a variable; one shape
    cwd = Path.cwd()
    found = sorted({p for glob in _DUCKDB_GLOBS for p in cwd.glob(glob)
                    if p.is_file()})
    out = []
    for path in found[:_MAX_DUCKDB_FILES]:
        sqlite = path.suffix == ".db"
        out.append({
            "uri": f"duckdb:{path.name}",
            "kind": "duckdb",
            "source": "working directory",
            "supported": True,
            "note": ("a .db file may be SQLite rather than DuckDB; semlayer "
                     "will say so if it is" if sqlite else None),
        })
    if len(found) > _MAX_DUCKDB_FILES:
        out.append({
            "uri": f"duckdb:{cwd}",
            "kind": "duckdb",
            "source": "working directory",
            "supported": False,
            "note": (f"{len(found)} database files here, more than the "
                     f"{_MAX_DUCKDB_FILES} listed: name the file you want"),
        })
    return out


# Postgres, from the environment. `PG_READER_DSN` is what `semlayer init
# postgres` tells you to export; `PG_DSN` is the fallback the connector also
# honours, so discovery honours it too rather than reporting nothing for a
# machine that is in fact configured.
_PG_DSN_VARS = ("PG_READER_DSN", "PG_DSN")


def _postgres_in_env(env: Mapping[str, str]) -> list[dict]:
    """A Postgres DSN in the environment, reported by variable name only.

    The variable's VALUE is a DSN, which is to say a hostname, a database
    name and usually a password. None of it is read here and none of it is
    reported: the URI is the bare word `postgres`, which is what the
    connector takes when it reads the environment itself.
    """
    for var in _PG_DSN_VARS:
        if env.get(var):
            return [{
                "uri": "postgres",
                "kind": "postgres",
                "source": _where(env, var),
                "supported": True,
                "note": (f"semlayer reads the DSN from {var} itself; its value "
                         "is never reported here"),
            }]
    return []


# BigQuery, from the environment. Four ways a machine can be ready for it,
# in the order `BigQuerySource.from_env` consults them: the reader key file,
# then whatever Application Default Credentials finds -- a key file named by
# the standard variable, or the file `gcloud auth application-default login`
# writes.
_GCP_KEY_VARS = ("GOOGLE_APPLICATION_CREDENTIALS_READER",
                 "GOOGLE_APPLICATION_CREDENTIALS")
_ADC_FILE = "application_default_credentials.json"


def _adc_file(env: Mapping[str, str]) -> Path | None:
    """The file `gcloud auth application-default login` writes, if it is there.

    `CLOUDSDK_CONFIG` wins because gcloud itself honours it, and the Windows
    location is different -- a path that is right on one platform and silently
    absent on another would make BigQuery invisible to half the people who
    have it configured.
    """
    config = env.get("CLOUDSDK_CONFIG")
    if config:
        base = Path(config)
    elif os.name == "nt":
        appdata = env.get("APPDATA")
        if not appdata:
            return None
        base = Path(appdata) / "gcloud"
    else:
        base = Path.home() / ".config" / "gcloud"
    path = base / _ADC_FILE
    return path if path.is_file() else None


def _bigquery_in_env(env: Mapping[str, str]) -> list[dict]:
    """A BigQuery project or credential in the environment.

    The project id IS reported, in the URI and in the note: it is the only
    identifier BigQuery has, an agent cannot assemble `bigquery:<project>`
    without it, and a project id is not a credential. The credential itself
    is named by variable only, and the key FILE's path is not reported at
    all -- knowing a key file exists is the useful part; knowing where it
    lives is not ours to pass on.
    """
    project = env.get("GCP_PROJECT")
    key_var = next((v for v in _GCP_KEY_VARS if env.get(v)), None)
    adc = _adc_file(env)
    if not (project or key_var or adc):
        return []
    credential = (f"a key file named by {key_var}" if key_var
                  else f"application default credentials ({_ADC_FILE})" if adc
                  else "no credential found -- `gcloud auth "
                       "application-default login` provides one")
    note = (f"project {project}, from GCP_PROJECT; {credential}" if project
            else f"{credential}; no GCP_PROJECT is set, so the project comes "
                 "from the credential's own quota project -- pass "
                 "bigquery:<project> to be sure of it, or "
                 + _env_hint("GCP_PROJECT=<project>"))
    return [{
        "uri": f"bigquery:{project}" if project else "bigquery",
        "kind": "bigquery",
        "source": _where(env, key_var) if key_var
                  else _where(env, "GCP_PROJECT") if project and not adc
                  else f"gcloud: {_ADC_FILE}",
        "supported": True,
        "note": note,
    }]


# dbt's `profiles.yml`: the one place a data team has already written down
# every warehouse it talks to. Looked for where dbt itself looks, and in
# dbt's own order of precedence -- DBT_PROFILES_DIR, then the project
# directory, then ~/.dbt.
_DBT_DIRS = ("DBT_PROFILES_DIR",)
_DBT_FILE = "profiles.yml"


def _tilde(path: Path) -> str:
    """A path with the home directory written as `~`.

    Cosmetic in the common case and not in all of them: the absolute form
    carries the account name, and this output goes to an agent.
    """
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def _dbt_files(env: Mapping[str, str]) -> list[Path]:
    """Every `profiles.yml` worth reading, in dbt's order of precedence."""
    dirs = [Path(env[v]) for v in _DBT_DIRS if env.get(v)]
    dirs += [Path.cwd(), Path.home() / ".dbt"]
    out = []
    for d in dirs:
        path = d / _DBT_FILE
        if path.is_file() and path not in out:
            out.append(path)
    return out


def _templated(*values: object) -> bool:
    """Whether any value is a dbt template we would have to render to read.

    `{{ env_var('SNOWFLAKE_ACCOUNT') }}` is extremely common in a checked-in
    profile. Rendering it means implementing dbt's Jinja context, so the
    honest answer is to report the target, say we cannot resolve it, and let
    the agent ask the user.
    """
    return any("{{" in str(v) for v in values if v is not None)


def _dbt_postgres(cfg: dict) -> tuple[str | None, str | None]:
    host, db = cfg.get("host"), cfg.get("dbname") or cfg.get("database")
    user, port = cfg.get("user"), cfg.get("port") or 5432
    if _templated(host, db, user, port):
        return None, "the profile builds this target from dbt templates we do not render"
    if not (host and db):
        return None, "the profile names no host or no database for this target"
    from urllib.parse import quote
    credential = f"{quote(str(user), safe='')}@" if user else ""
    return f"postgresql://{credential}{host}:{port}/{quote(str(db), safe='')}", None


def _dbt_bigquery(cfg: dict) -> tuple[str | None, str | None]:
    project = cfg.get("project") or cfg.get("database")
    if _templated(project):
        return None, "the profile builds this target from dbt templates we do not render"
    if not project:
        return None, "the profile names no project for this target"
    return f"bigquery:{project}", None


def _dbt_snowflake(cfg: dict) -> tuple[str | None, str | None]:
    from urllib.parse import quote, urlencode
    account, db = cfg.get("account"), cfg.get("database")
    user, role, wh = cfg.get("user"), cfg.get("role"), cfg.get("warehouse")
    if _templated(account, db, user, role, wh):
        return None, "the profile builds this target from dbt templates we do not render"
    if not (account and db):
        return None, "the profile names no account or no database for this target"
    query = urlencode([(k, v) for k, v in (("role", role), ("warehouse", wh)) if v])
    credential = f"{quote(str(user), safe='')}@" if user else ""
    return (f"snowflake://{credential}{account}/{quote(str(db), safe='')}"
            f"{'?' + query if query else ''}", None)


_DBT_ADAPTERS = {"postgres": _dbt_postgres, "bigquery": _dbt_bigquery,
                 "snowflake": _dbt_snowflake}


def _dbt_target(profile: str, target: str, cfg: dict, default: object,
                path: Path) -> dict:
    """One profile/target pair as a connection, mappable or not.

    An unmappable target is still reported, as `dbt:<profile>.<target>` with
    `supported: false` and the reason. It is the answer to "I have dbt, why
    can you not see my warehouse" -- and an adapter we do not support is a
    fact about us, which the agent should be able to tell the user rather
    than guess at.

    No credential is read. dbt profiles hold passwords and key paths inline,
    and the mapped URIs carry the user name at most -- the connector gets the
    secret from the environment, the same as every other path into it.
    """
    kind = str(cfg.get("type") or "unknown").lower()[:32]
    adapter = _DBT_ADAPTERS.get(kind)
    uri, why = adapter(cfg) if adapter else (
        None, f"semlayer has no connector for dbt's {kind!r} adapter")
    which = f"dbt profile {profile!r}, target {target!r}"
    if target == default:
        which += " (the profile's default)"
    return {
        "uri": uri or f"dbt:{profile}.{target}",
        "kind": kind,
        "source": f"dbt: {_tilde(path)}",
        "supported": uri is not None,
        "note": (f"{which}; no credential is read from the profile"
                 if uri else f"{which}: {why}"),
    }


def _dbt_profiles(env: Mapping[str, str]) -> list[dict]:
    """Every warehouse dbt knows about, from every profiles.yml we can see."""
    import yaml

    out = []
    for path in _dbt_files(env):
        try:
            doc = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError):
            # One unreadable profile costs its own rows, never the answer.
            continue
        for profile, body in (doc or {}).items():
            if not isinstance(body, dict):
                continue            # `config:` is a sibling of the profiles
            outputs = body.get("outputs")
            for target, cfg in (outputs or {}).items():
                if isinstance(cfg, dict):
                    out.append(_dbt_target(str(profile), str(target), cfg,
                                           body.get("target"), path))
    return out


# Snowflake, from the environment. The variables `semlayer init snowflake`
# tells you to export, and the credential variables `connectors.snowflake._auth`
# accepts -- password, key pair, or an authenticator for SSO.
_SF_USER = "SNOWFLAKE_READER_USER"
_SF_CREDENTIAL_VARS = ("SNOWFLAKE_READER_PASSWORD",
                       "SNOWFLAKE_READER_PRIVATE_KEY_PATH",
                       "SNOWFLAKE_PRIVATE_KEY_PATH",
                       "SNOWFLAKE_READER_AUTHENTICATOR",
                       "SNOWFLAKE_AUTHENTICATOR")


def _snowflake_in_env(env: Mapping[str, str]) -> list[dict]:
    """A Snowflake account in the environment.

    `supported` is false without a user or without a credential, because
    `from_env` refuses without either -- and an agent that knows which
    variable is missing can tell the user, which is strictly better than
    letting them find out from a refusal several minutes later.

    Account identifiers are not reported. They are the organisation's name
    and locator; naming the variable is enough to say a connection is there.
    """
    if not env.get("SNOWFLAKE_ACCOUNT"):
        return []
    # What is missing, as the LINES to add rather than the names of the
    # variables: an agent relaying "SNOWFLAKE_READER_USER is not set" leaves
    # the user to guess the spelling and the file. One hint at the end, once,
    # because the whole note is read out loud.
    needs, blocking = [], False
    if not env.get(_SF_USER):
        needs.append(f"{_SF_USER}=<user>")
        blocking = True
    if not any(env.get(v) for v in _SF_CREDENTIAL_VARS):
        needs.append("SNOWFLAKE_READER_PRIVATE_KEY_PATH=<path to the .p8 key> "
                     "(or SNOWFLAKE_READER_PASSWORD=<password>)")
        blocking = True
    note = "account from SNOWFLAKE_ACCOUNT"
    if not env.get("SNOWFLAKE_DATABASE"):
        note += ("; without SNOWFLAKE_DATABASE the connector falls back to its "
                 "test default (SEMLAYER_TEST)")
        needs.append("SNOWFLAKE_DATABASE=<database>")
    if needs:
        note += f"; {_env_hint('; '.join(needs))}"
    missing = blocking
    return [{
        "uri": "snowflake",
        "kind": "snowflake",
        "source": _where(env, "SNOWFLAKE_ACCOUNT"),
        "supported": not missing,
        "note": note,
    }]


def _snowflake_connections(env: Mapping[str, str]) -> list[dict]:
    """Named connections from `~/.snowflake/connections.toml`.

    The driver's own file, shared with Snowflake's CLI, so someone who runs
    `snow` already has their connections here. Parsed by the connector's
    `connection_sections`, not by a second copy of a TOML reader living here.

    Role and database are reported as PRESENT or ABSENT, never by value: the
    run report records both, so `from_connection_name` refuses a section with
    no database, and a section with no role runs under whatever the account's
    default happens to be. Both are worth knowing before a run starts.

    No other key is touched. A section's `password`, `private_key_path` and
    `token` are not read, and the names of the sections are the only thing
    this source discloses.
    """
    del env           # this source is a file, not a variable; one shape
    from semlayer.connectors.snowflake import connection_sections

    out = []
    for name, section in connection_sections().items():
        has = [k for k in ("role", "database") if section.get(k)]
        lacks = [k for k in ("role", "database") if not section.get(k)]
        note = f"from connections.toml; carries {', '.join(has)}" if has else \
               "from connections.toml"
        if lacks:
            note += (f"; no {', '.join(lacks)} -- the run report needs both, so "
                     "semlayer refuses a connection with no database and "
                     "records the account's default role when none is named")
        out.append({
            "uri": f"snowflake:{name}",
            "kind": "snowflake",
            "source": "~/.snowflake/connections.toml",
            "supported": bool(section.get("database")),
            "note": note,
        })
    return out


_SOURCES: tuple[Callable[[Mapping[str, str]], list[dict]], ...] = (
    _duckdb_in_cwd,
    _postgres_in_env,
    _bigquery_in_env,
    _dbt_profiles,
    _snowflake_in_env,
    _snowflake_connections,
)


def discover(env: Mapping[str, str] | None = None) -> list[dict]:
    """Every warehouse connection visible from here, in source order.

    Each entry is `{uri, kind, source, supported, note}`: `uri` is what you
    would pass to `semlayer infer`, `kind` is the connector family, `source`
    says where we saw it, `supported` says whether we can open it at all, and
    `note` is for the one thing an agent would otherwise have to guess.

    Deduplicated by URI with the first occurrence winning, so a profile that
    restates the environment does not double-report a warehouse.

    Guarded per source: a malformed profiles.yml or an unreadable directory
    costs its own row, never the whole answer.
    """
    env = environment() if env is None else env
    out: list[dict] = []
    seen: set[str] = set()
    for source in _SOURCES:
        try:
            found = source(env)
        except Exception:
            # Discovery is advisory. A source that cannot be read is a source
            # with nothing to say, and `status` must answer regardless.
            continue
        for entry in found:
            if entry["uri"] in seen:
                continue
            seen.add(entry["uri"])
            out.append(entry)
    return out
