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
from collections.abc import Callable
from pathlib import Path

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

    ONE function, deliberately. Whether the cache belongs in the user's home
    directory or in their repository -- where a teammate would see it, and
    where a `.gitignore` decision would have to be made -- is not settled.
    The home directory is the default until it is, and moving it is this
    function and nothing else.
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


# How many connections the `next` sentence names before it starts counting.
_NAMED_IN_NEXT = 6


def next_step(state: str, connections: list[dict]) -> str:
    """What the agent should do next, in one sentence, written to be acted on.

    ONE place, because this sentence is a promise about what the server can
    do and it changes the moment that changes: when inference becomes a tool
    here, the `no_layer` branch stops telling the agent to send its user to a
    terminal and starts naming the tool. Keeping it inline in `status()`
    would mean finding every copy.
    """
    if state == "ready":
        return ("A semantic layer is loaded: call semantic_search or get_domains "
                "to find your way around, and table_detail before you write any "
                "SQL.")
    usable = [c["uri"] for c in connections if c.get("supported")]
    if usable:
        # Named rather than counted, because "ask the user which one" is not
        # actionable without the names -- but capped, because a fixtures
        # directory with twenty databases in it would put twenty paths in one
        # sentence. The full list is in `connections` either way.
        named = ", ".join(usable[:_NAMED_IN_NEXT])
        if len(usable) > _NAMED_IN_NEXT:
            named += f", and {len(usable) - _NAMED_IN_NEXT} more (see `connections`)"
        return ("No semantic layer is loaded yet. These warehouse connections are "
                f"visible from here: {named}. Ask the user which one to "
                "model, then ask them to run `semlayer infer <connection> -o "
                "layer.yaml` in a terminal and restart this server with that file "
                "-- inference is not yet available as a tool.")
    return ("No semantic layer is loaded and no warehouse connection is visible "
            "from here. Ask the user how they reach their warehouse (a DuckDB "
            "file, a Postgres DSN, a BigQuery project, or a Snowflake account), "
            "then ask them to run `semlayer infer <connection> -o layer.yaml` in "
            "a terminal and restart this server with that file -- inference is "
            "not yet available as a tool.")


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
        """`no_layer` or `ready`."""
        return "ready" if self.doc is not None else "no_layer"

    def load(self, path: str | Path) -> None:
        """Load the layer at `path`. An explicit path wins over everything.

        Raises, as the CLI always has: someone who named a file wants that
        file, and silently serving an empty session instead would be a
        mystery rather than an error.
        """
        self.doc = load_document(path)
        self.layer_path = Path(path)

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

    def select(self, connection: str) -> bool:
        """Select `connection`, loading its cached layer if one exists."""
        self.connection = connection
        return self.load_cached(connection)

    def discover(self) -> list[dict]:
        """Warehouse connections visible from here. Names only; opens nothing."""
        return discover()

    def status(self) -> dict:
        """One object, written for an agent to act on rather than to read.

        Every other tool answers with THIS when the state is wrong for it, so
        an agent that calls `get_tables` before a layer exists is told what to
        do instead of handed an error. That is the whole contract: a tool call
        never dead-ends.
        """
        connections = self.discover()
        return {"state": self.state,
                "connection": self.connection,
                "layer": self._layer(),
                "connections": connections,
                "next": next_step(self.state, connections)}

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
            "inferred_at": generated.get("generated_at") or self._mtime(),
            "engine": generated.get("engine"),
            "model": generated.get("model"),
            "undecided_review_items": self._undecided(),
        }

    def _mtime(self) -> str | None:
        """The layer file's modification time, as a fallback for `inferred_at`.

        The spec defines `generated_by.generated_at` and the producer does not
        yet write it, so for every document in existence today the honest
        answer to "how old is this" is the file's own timestamp. Noted in the
        PR rather than fixed here: writing the field changes what `infer`
        emits, which is a different change from this one.
        """
        if self.layer_path is None:
            return None
        from datetime import datetime, timezone
        try:
            ts = self.layer_path.stat().st_mtime
        except OSError:
            return None
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")

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


def _duckdb_in_cwd() -> list[dict]:
    """DuckDB files sitting in the working directory.

    `.db` is included because that is what plenty of people call a DuckDB
    file, and noted because it is also what SQLite files are called -- we
    cannot tell which without opening it, and discovery opens nothing.
    """
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


def _postgres_in_env() -> list[dict]:
    """A Postgres DSN in the environment, reported by variable name only.

    The variable's VALUE is a DSN, which is to say a hostname, a database
    name and usually a password. None of it is read here and none of it is
    reported: the URI is the bare word `postgres`, which is what the
    connector takes when it reads the environment itself.
    """
    for var in _PG_DSN_VARS:
        if os.environ.get(var):
            return [{
                "uri": "postgres",
                "kind": "postgres",
                "source": f"environment: {var}",
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


def _adc_file() -> Path | None:
    """The file `gcloud auth application-default login` writes, if it is there.

    `CLOUDSDK_CONFIG` wins because gcloud itself honours it, and the Windows
    location is different -- a path that is right on one platform and silently
    absent on another would make BigQuery invisible to half the people who
    have it configured.
    """
    config = os.environ.get("CLOUDSDK_CONFIG")
    if config:
        base = Path(config)
    elif os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        base = Path(appdata) / "gcloud"
    else:
        base = Path.home() / ".config" / "gcloud"
    path = base / _ADC_FILE
    return path if path.is_file() else None


def _bigquery_in_env() -> list[dict]:
    """A BigQuery project or credential in the environment.

    The project id IS reported, in the URI and in the note: it is the only
    identifier BigQuery has, an agent cannot assemble `bigquery:<project>`
    without it, and a project id is not a credential. The credential itself
    is named by variable only, and the key FILE's path is not reported at
    all -- knowing a key file exists is the useful part; knowing where it
    lives is not ours to pass on.
    """
    project = os.environ.get("GCP_PROJECT")
    key_var = next((v for v in _GCP_KEY_VARS if os.environ.get(v)), None)
    adc = _adc_file()
    if not (project or key_var or adc):
        return []
    credential = (f"a key file named by {key_var}" if key_var
                  else f"application default credentials ({_ADC_FILE})" if adc
                  else "no credential found -- `gcloud auth "
                       "application-default login` provides one")
    note = (f"project {project}, from GCP_PROJECT; {credential}" if project
            else f"{credential}; no GCP_PROJECT is set, so the project comes "
                 "from the credential's own quota project -- pass "
                 "bigquery:<project> to be sure of it")
    return [{
        "uri": f"bigquery:{project}" if project else "bigquery",
        "kind": "bigquery",
        "source": f"environment: {key_var}" if key_var
                  else "environment: GCP_PROJECT" if project and not adc
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


def _dbt_files() -> list[Path]:
    """Every `profiles.yml` worth reading, in dbt's order of precedence."""
    dirs = [Path(os.environ[v]) for v in _DBT_DIRS if os.environ.get(v)]
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


def _dbt_profiles() -> list[dict]:
    """Every warehouse dbt knows about, from every profiles.yml we can see."""
    import yaml

    out = []
    for path in _dbt_files():
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


def _snowflake_in_env() -> list[dict]:
    """A Snowflake account in the environment.

    `supported` is false without a user or without a credential, because
    `from_env` refuses without either -- and an agent that knows which
    variable is missing can tell the user, which is strictly better than
    letting them find out from a refusal several minutes later.

    Account identifiers are not reported. They are the organisation's name
    and locator; naming the variable is enough to say a connection is there.
    """
    if not os.environ.get("SNOWFLAKE_ACCOUNT"):
        return []
    missing = [v for v in (_SF_USER,) if not os.environ.get(v)]
    if not any(os.environ.get(v) for v in _SF_CREDENTIAL_VARS):
        missing.append(f"one of {', '.join(_SF_CREDENTIAL_VARS[:2])}")
    note = "account from SNOWFLAKE_ACCOUNT"
    if not os.environ.get("SNOWFLAKE_DATABASE"):
        note += "; SNOWFLAKE_DATABASE is unset, so the connector falls back to "
        note += "its test default -- export it"
    if missing:
        note += f"; not set: {', '.join(missing)}"
    return [{
        "uri": "snowflake",
        "kind": "snowflake",
        "source": "environment: SNOWFLAKE_ACCOUNT",
        "supported": not missing,
        "note": note,
    }]


def _snowflake_connections() -> list[dict]:
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


_SOURCES: tuple[Callable[[], list[dict]], ...] = (
    _duckdb_in_cwd,
    _postgres_in_env,
    _bigquery_in_env,
    _dbt_profiles,
    _snowflake_in_env,
    _snowflake_connections,
)


def discover() -> list[dict]:
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
    out: list[dict] = []
    seen: set[str] = set()
    for source in _SOURCES:
        try:
            found = source()
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
