# SPDX-License-Identifier: FSL-1.1-ALv2
"""Snowflake connector: MetadataProvider + QueryExecutor over one database.

Runs under the customer's minimal-grant read-only service account (USAGE on
db/schemas/warehouse + SELECT on tables — exactly what `semlayer init`
generates). Anything requiring more is a documented opt-in, not an assumption.
"""

from __future__ import annotations

import logging
import os
import pathlib

from semlayer.errors import SourceError
from semlayer.source import ColumnMeta, TableMeta

logger = logging.getLogger(__name__)

_HOST_SUFFIX = ".snowflakecomputing.com"
# Where the driver (and Snowflake's own CLI) keep named connections.
_CONNECTIONS_TOML = "connections.toml"


def _prefix(persona: str) -> str:
    return "SNOWFLAKE_READER" if persona == "reader" else "SNOWFLAKE_ADMIN"


def _account_id(raw: str) -> str:
    """Accept the account spellings people actually paste.

    The Snowflake UI hands out several spellings of one thing: ORG-ACCOUNT, a
    bare locator, and a full hostname (`abc12345.us-east-1.snowflakecomputing
    .com`, often copied with the scheme and a trailing slash). The driver wants
    the identifier, so strip the URL clothing instead of failing on it — a
    stranger pasting what the console showed them is the first run we lost.
    """
    a = raw.strip().strip("/")
    if "://" in a:
        a = a.split("://", 1)[1]
    a = a.split("/", 1)[0]
    if a.lower().endswith(_HOST_SUFFIX):
        a = a[: -len(_HOST_SUFFIX)]
    return a


def _asked_role(prefix: str, given: str | None = None) -> str | None:
    """The role the caller asked for, or None to run as whoever they are.

    `given` comes from a URL or a named connection and wins over the
    environment.

    This used to refuse to connect without a role, on the grounds that a
    minimal-grant claim is about the role actually in effect and an inherited
    DEFAULT_ROLE would profile under every privilege the account holds. The
    reasoning was right and the remedy was wrong: refusing made a first run
    impossible for anyone who had not already created a role, while the run
    report still recorded the role we ASKED for rather than the one in
    effect -- so the claim it was protecting was never actually checked.

    `_effective_role` checks it instead, by asking the session. That makes
    running as yourself safe to allow: whatever privileges you hold, the
    report names them, and a divergence between asked-for and in-effect is
    reported rather than assumed away.
    """
    return given or os.environ.get(f"{prefix}_ROLE") or os.environ.get("SNOWFLAKE_ROLE")


def _effective_role(con, asked: str | None, prefix: str) -> str:
    """The role the session is ACTUALLY running as, from Snowflake itself.

    One extra metadata query per run, and the only thing that makes the run
    report's `access.session_role` mean anything: before this it recorded the
    caller's own input, which cannot disagree with itself.

    Three outcomes, deliberately different:

      * no role in effect -- raised. Snowflake returns no current role when
        the user has no default and none was passed, and nothing downstream
        works without one, so this is the old refusal kept for the case that
        actually warranted it.
      * asked for one and got another -- warned, not raised. Snowflake has
        applied a secondary role or an ownership rule, and the run is still
        valid; what matters is that the report names what ran, and that the
        difference is visible rather than silent.
      * could not ask -- warned, and we fall back to what was asked for or
        say "unknown". A failed metadata query is a poor reason to abandon a
        connection that otherwise works, and "unknown" is honest.
    """
    try:
        cur = con.cursor()
        try:
            cur.execute("select current_role()")
            row = cur.fetchone()
        finally:
            cur.close()
        effective = (row[0] if row else "") or ""
    except Exception as e:                      # a stub, or a driver quirk
        logger.warning("could not read CURRENT_ROLE(): %s", e)
        return asked or "unknown"

    effective = effective.strip()
    if not effective or effective.upper() == "NULL":
        raise SourceError(
            "no Snowflake role is in effect for this session",
            hint=f"the account has no DEFAULT_ROLE, so pass one: export "
                 f"{prefix}_ROLE (or SNOWFLAKE_ROLE), add ?role=<role> to a "
                 "URL, or `role = ...` in connections.toml. "
                 "`semlayer init snowflake` generates a minimal reader role.",
        )
    if asked and effective.upper() != asked.upper():
        # Not an error: Snowflake decides what a session runs as, and the
        # report is about what ran.
        logger.warning(
            "asked for role %s but the session is running as %s; the run "
            "report records %s", asked, effective, effective)
    return effective


def _auth(prefix: str, password: str | None = None) -> dict:
    """Pick the credential. `password` overrides the environment (URL form).

    Password was the ONLY accepted credential, which locked out every account
    on SSO — the common case at any company large enough to have a messy
    warehouse, and the exact person who wants to try the CLI on their own
    grants. `authenticator` covers SSO (externalbrowser), Okta (a URL), and
    OAuth; a private key covers key-pair auth, which is what most service
    accounts are moving to.
    """
    auth: dict = {}
    authenticator = os.environ.get(f"{prefix}_AUTHENTICATOR") or os.environ.get(
        "SNOWFLAKE_AUTHENTICATOR")
    key_path = os.environ.get(f"{prefix}_PRIVATE_KEY_PATH") or os.environ.get(
        "SNOWFLAKE_PRIVATE_KEY_PATH")
    password = password or os.environ.get(f"{prefix}_PASSWORD")
    if key_path:
        auth["private_key_file"] = key_path
        passphrase = os.environ.get(f"{prefix}_PRIVATE_KEY_PASSPHRASE") or \
            os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
        if passphrase:
            auth["private_key_file_pwd"] = passphrase
    elif authenticator:
        auth["authenticator"] = authenticator
        # externalbrowser prompts the user and takes no password; Okta and
        # oauth variants still need one, so pass it when it is set.
        if password and authenticator.lower() != "externalbrowser":
            auth["password"] = password
    elif password:
        auth["password"] = password
    else:
        raise SourceError(
            f"no Snowflake credential: set {prefix}_PASSWORD, "
            f"{prefix}_PRIVATE_KEY_PATH, or {prefix}_AUTHENTICATOR="
            "externalbrowser for SSO",
            hint="`semlayer init snowflake` prints the minimal grant; SSO and "
                 "key-pair let you run as yourself without a new password",
        )
    return auth


def _required_env(name: str, hint: str) -> str:
    """A missing setting is the user's to fix, so it must not read as our bug.

    `os.environ[...]` raised KeyError here, which cli._error_class maps by
    TYPE to `internal` — the bucket that is supposed to mean a semlayer defect.
    """
    value = os.environ.get(name)
    if not value:
        raise SourceError(f"{name} is not set", hint=hint)
    return value


def _connect(**kw):
    """One place where the driver is imported and its failures are translated.

    Both matter for the first run. The import is an optional extra, so a
    missing one must say `pip install` rather than `No module named`; and the
    driver's own refusals (bad account, wrong password, no such warehouse,
    expired SSO) used to reach the user raw and telemetry as `internal`.
    """
    try:
        import snowflake.connector as driver
    except ImportError as e:
        raise SourceError(
            "the Snowflake driver is not installed",
            hint="pip install 'semlayer[warehouses]'",
        ) from e
    try:
        from snowflake.connector.errors import Error as _DriverError
    except Exception:            # a stub or a driver without the module
        _DriverError = ()        # an empty tuple catches nothing
    try:
        return driver.connect(**kw)
    except _DriverError as e:
        # The driver's message names the account, the user and what it
        # objected to, which is exactly what the person needs; it becomes the
        # hint rather than being swallowed.
        raise SourceError("Snowflake refused the connection", hint=str(e)) from e


def _connections_toml() -> pathlib.Path:
    """The file the driver and Snowflake's CLI both read."""
    home = os.environ.get("SNOWFLAKE_HOME")
    return (pathlib.Path(home) if home else pathlib.Path.home() / ".snowflake") / _CONNECTIONS_TOML


class SnowflakeSource:
    """MetadataProvider + QueryExecutor over one Snowflake database."""

    def __init__(self, con, database: str):
        self.con = con
        self.database = database
        # recorded in the run report so the layer says which privileges
        # produced it; "unknown" when a caller built the source directly
        self.session_role: str = "unknown"

    @classmethod
    def from_uri(cls, uri: str, persona: str = "reader") -> SnowflakeSource:
        """Open whatever the person pasted after `semlayer infer`.

        Three forms, because people hold a Snowflake connection in three
        shapes and the CLI accepted only the first:

          snowflake                        the environment (what `init` sets up)
          snowflake:<name>                 a named connection from connections.toml
          snowflake://user@account/db       a URL, with ?role= and ?warehouse=

        Telemetry for the ten days after the sender went live recorded one
        person outside the team trying a real warehouse: 14 Snowflake runs, 14
        `connection` errors, no successes. The only Snowflake path that could
        produce that class was the "unsupported source uri" refusal, so what
        they pasted was almost certainly one of the last two forms — and they
        were told to try "snowflake, bigquery, duckdb:<path>".
        """
        body = uri[len("snowflake"):].lstrip(":").lstrip("/").rstrip("/")
        if not body:
            return cls.from_env(persona=persona)
        # A bare token is a connection NAME; anything with URL structure in it
        # is a URL. `snowflake://acct/db` has no user and is still a URL.
        if any(ch in body for ch in "@/?"):
            return cls.from_url(uri, persona=persona)
        return cls.from_connection_name(body, persona=persona)

    @classmethod
    def from_env(cls, persona: str = "reader") -> SnowflakeSource:
        """persona: 'reader' (the product) or 'admin' (fixture loading only).

        The session role is passed when one is set and inherited when it is
        not -- see `_asked_role` and `_effective_role`. Config is validated
        BEFORE the driver import,
        deliberately: the role is a cheap environment check and the driver is
        an optional extra, so importing first meant an install without
        `[warehouses]` reported `No module named 'snowflake'` for what is
        actually a missing setting -- and it made the test for this rule
        unrunnable anywhere the extra is absent, which is CI.
        """
        prefix = _prefix(persona)
        asked = _asked_role(prefix)
        auth = _auth(prefix)
        export_hint = (
            f"export SNOWFLAKE_ACCOUNT=<org-account> {prefix}_USER=<user> "
            f"SNOWFLAKE_DATABASE=<db>; {prefix}_ROLE is optional -- without "
            "it the run uses your own default role and reports which one. "
            "`semlayer init snowflake` prints the whole block"
        )
        account = _account_id(_required_env("SNOWFLAKE_ACCOUNT", export_hint))
        user = _required_env(f"{prefix}_USER", export_hint)
        database = os.environ.get("SNOWFLAKE_DATABASE", "SEMLAYER_TEST")
        con = _connect(
            account=account,
            user=user,
            # Omitted entirely when unset: passing role=None makes the driver
            # send a null role rather than leaving the session default alone.
            **({"role": asked} if asked else {}),
            warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "SEMLAYER_WH"),
            database=database,
            **auth,
        )
        src = cls(con, database)
        src.session_role = _effective_role(con, asked, prefix)
        return src

    @classmethod
    def from_connection_name(cls, name: str, persona: str = "reader") -> SnowflakeSource:
        """Open the named connection from `connections.toml`.

        The file is the driver's own, shared with Snowflake's CLI, so someone
        who already runs `snow` has their credentials here and should not have
        to restate them as environment variables. It is read rather than left
        entirely to the driver for one reason: the role in effect and the
        database are recorded in the run report, and `connection_name=` alone
        would leave both unknown to us.
        """
        prefix = _prefix(persona)
        path = _connections_toml()
        if not path.exists():
            raise SourceError(
                f"no Snowflake connection file at {path}",
                hint="`snow connection add` writes it, or set SNOWFLAKE_HOME; or pass "
                     "a URL (snowflake://user@account/db?role=..) or use the "
                     "environment (`semlayer init snowflake`)",
            )
        import tomllib
        try:
            doc = tomllib.loads(path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError) as e:
            raise SourceError(f"could not read {path}", hint=str(e)) from e
        section = doc.get(name)
        if not isinstance(section, dict):
            known = ", ".join(sorted(k for k, v in doc.items() if isinstance(v, dict)))
            raise SourceError(
                f"no connection named {name!r} in {path}",
                hint=f"connections in that file: {known}" if known
                     else "that file defines no connections",
            )
        asked = _asked_role(prefix, section.get("role"))
        database = section.get("database") or os.environ.get("SNOWFLAKE_DATABASE")
        if not database:
            raise SourceError(
                f"connection {name!r} names no database",
                hint="add `database = \"<db>\"` to it in "
                     f"{path}, or export SNOWFLAKE_DATABASE",
            )
        # The driver merges connection_name with explicit kwargs, and the
        # explicit ones win: the role we recorded is the role in effect.
        con = _connect(connection_name=name, database=database,
                       **({"role": asked} if asked else {}))
        src = cls(con, database)
        src.session_role = _effective_role(con, asked, prefix)
        return src

    @classmethod
    def from_url(cls, url: str, persona: str = "reader") -> SnowflakeSource:
        """Open `snowflake://user[:password]@account/db[/schema][?role=&warehouse=]`.

        The same shape SQLAlchemy's Snowflake dialect uses, so it is a form
        people already hold rather than a surface we invented. Parts the URL
        omits fall back to the environment, which is what lets a URL carry the
        account and database while the credential stays out of shell history.
        """
        from urllib.parse import parse_qs, unquote, urlparse

        prefix = _prefix(persona)
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items() if v}
        account = _account_id(unquote(u.hostname or "")) or _account_id(
            os.environ.get("SNOWFLAKE_ACCOUNT", ""))
        segments = [seg for seg in (u.path or "").split("/") if seg]
        database = (unquote(segments[0]) if segments
                    else os.environ.get("SNOWFLAKE_DATABASE", ""))
        user = unquote(u.username or "") or os.environ.get(f"{prefix}_USER", "")
        password = unquote(u.password) if u.password else None
        example = ("snowflake://<user>@<org-account>/<database>"
                   "?role=<role>&warehouse=<wh>")
        if not account:
            raise SourceError(f"no Snowflake account in {url}",
                              hint=f"expected {example}")
        if not database:
            raise SourceError(
                f"no database in {url}",
                hint=f"expected {example}, or export SNOWFLAKE_DATABASE")
        if not user:
            raise SourceError(
                f"no user in {url}",
                hint=f"expected {example}, or export {prefix}_USER")
        asked = _asked_role(prefix, q.get("role"))
        auth = _auth(prefix, password)
        if password:
            # Said once, where it is actionable, and not again at every run.
            logger.warning(
                "the password came from the URL, so it is in your shell history: "
                "%s_PASSWORD or connections.toml keeps it out", prefix)
        kw: dict = {}
        if len(segments) > 1:
            # The driver's own session schema. Inference scope is --schema.
            kw["schema"] = unquote(segments[1])
        con = _connect(
            account=account,
            user=user,
            **({"role": asked} if asked else {}),
            warehouse=q.get("warehouse") or os.environ.get(
                "SNOWFLAKE_WAREHOUSE", "SEMLAYER_WH"),
            database=database,
            **kw,
            **auth,
        )
        src = cls(con, database)
        src.session_role = _effective_role(con, asked, prefix)
        return src

    def list_comments(self) -> dict[tuple[str, str, str | None], str]:
        """Native COMMENT text from information_schema.

        Snowflake surfaces comments as a plain `comment` column on both
        information_schema.tables and information_schema.columns, so this
        needs no extra grants beyond the USAGE the reader role already has.

        NOT covered by the nightly live connector test, which has no commented
        objects to read -- `_native_comments` degrades to no comments if the
        query fails, so a wrong assumption here costs priors rather than a run.
        """
        out: dict[tuple[str, str, str | None], str] = {}
        for sql, has_col in (
            (f"""SELECT table_schema, table_name, NULL, comment
                 FROM {self.database}.information_schema.tables
                 WHERE comment IS NOT NULL
                   AND table_schema <> 'INFORMATION_SCHEMA'""", False),
            (f"""SELECT table_schema, table_name, column_name, comment
                 FROM {self.database}.information_schema.columns
                 WHERE comment IS NOT NULL
                   AND table_schema <> 'INFORMATION_SCHEMA'""", True),
        ):
            for schema, table, col, comment in self.query(sql):
                if comment:
                    out[(schema, table, col if has_col else None)] = comment
        return out

    def list_tables(self) -> list[TableMeta]:
        """Enumerate the database's tables/columns via information_schema."""
        rows = self.query(
            f"""
            SELECT table_schema, table_name, column_name, data_type
            FROM {self.database}.information_schema.columns
            WHERE table_schema NOT IN ('INFORMATION_SCHEMA')
            ORDER BY table_schema, table_name, ordinal_position
            """
        )
        tables: dict[tuple[str, str], list[ColumnMeta]] = {}
        for schema, table, col, dtype in rows:
            tables.setdefault((schema, table), []).append(ColumnMeta(col, dtype))
        return [
            TableMeta(schema, table, tuple(cols))
            for (schema, table), cols in tables.items()
        ]

    def query(self, sql: str) -> list[tuple]:
        """Run sql and return plain row tuples."""
        cur = self.con.cursor()
        try:
            cur.execute(sql)
            return cur.fetchall()
        finally:
            cur.close()

    def for_worker(self):
        """Per-worker executor: the shared connection.

        It may be used from several threads; `query` already opens a cursor
        per call.
        """
        return self

    def close(self) -> None:
        """Close the Snowflake connection."""
        self.con.close()

    def ddl_events_since(self, minutes: int = 120) -> list[tuple]:
        """DDL statements from ACCOUNT_USAGE.QUERY_HISTORY (drift watcher feed).

        OPT-IN grant required (IMPORTED PRIVILEGES ON DATABASE snowflake);
        latency is ~45min by design — the CI/CD hook is the fast path.
        """
        return self.query(f"""
            SELECT query_text, start_time
            FROM snowflake.account_usage.query_history
            WHERE query_type IN ('ALTER_TABLE_MODIFY_COLUMN','ALTER','ALTER_TABLE',
                                 'CREATE_TABLE','DROP','CREATE','RENAME_COLUMN',
                                 'ALTER_TABLE_ADD_COLUMN','ALTER_TABLE_DROP_COLUMN')
              AND start_time > DATEADD('minute', -{minutes}, CURRENT_TIMESTAMP())
            ORDER BY start_time DESC LIMIT 100
        """)
