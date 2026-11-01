# Telemetry

semlayer sends anonymous usage statistics, so we can tell which warehouses
to support next, which commands are used, where runs fail, how long inference
takes at what scale, and whether an upgrade fixed the thing it was supposed
to fix. This page says exactly what is sent, and how to stop it.

**Turn it off, or back on:**

```sh
semlayer telemetry --off     # off permanently
semlayer telemetry --on      # on again
semlayer telemetry           # which of these is in effect, and where the spool is
```

`--off` writes a marker file in `~/.semlayer/`; `--on` removes it. The
decision survives new shells and upgrades until you change it.

For one shell only, without changing the stored setting:

```sh
export SEMLAYER_TELEMETRY=off
```

If that variable is set, `semlayer telemetry --on` cannot override it — an
environment variable is not ours to unset. `semlayer telemetry` says which
switch is in effect, so you are never left wondering why `--on` appeared to
do nothing.

**See precisely what would be sent, before it is sent:**

```sh
semlayer telemetry --show
```

That prints the local spool verbatim. Nothing is sent that you cannot read
there first.

## What is in an event

The event is assembled entirely on your machine, by
[`telemetry.py`](../src/semlayer/telemetry.py) and the whitelist in
[`telemetry_schema.py`](../src/semlayer/telemetry_schema.py). Nothing is
added, derived, or inferred from the request itself — the receiver never
reads your IP address, and no geolocation is performed.

| Field | Example | Notes |
|---|---|---|
| `schema` | `2` | version of this field set |
| `t` | `1760600000` | when the command started, whole seconds |
| `anon` | `3f9a1c…` | 32 random hex characters, generated once per machine in `~/.semlayer/anon_id`. Not derived from anything about you or your hardware — delete the file and you become a different machine |
| `version` | `0.6.0` | semlayer version |
| `python` | `3.12` | major.minor only |
| `os` | `linux` | one of linux, darwin, windows, other. No kernel release, no distribution |
| `arch` | `x86_64` | one of x86_64, arm64, other |
| `ci` | `false` | whether `CI` is set |
| `event`, `command` | `cli`, `infer` | which subcommand ran. Never its arguments |
| `connector` | `snowflake` | the warehouse type, from the URI scheme. **Never the URI**, never a host, account, database or credential |
| `duration_s` | `212` | wall clock, whole seconds |
| `outcome` | `ok` | ok, error, or interrupted |
| `error_class` | `connection` | the exception **type**, mapped to a fixed list. **Never the message**, because that is where names and SQL live |
| `tables` | `21_50` | a bucket, never an exact count |
| `columns` | `501_2000` | a bucket |
| `schemas` | `1` | a bucket |
| `llm` | `anthropic` | the provider class. Never the model, the key, or the endpoint |
| `flags` | `["no_sample_egress"]` | which boolean flags were set. Never their values |
| `probe` | `{"grade": "B", …}` | grades and ratios from `semlayer probe` |
| `stages` | `{"profile": 12, …}` | seconds per pipeline stage |
| `host` | `cursor` | on `mcp` events only: which MCP client connected, from the `initialize` handshake, classified into a fixed list — claude-desktop, claude-code, cursor, vscode, inspector, sdk, other. **Never the client's own name string** and never its version: the classifier cannot return its input, so a client calling itself anything at all still arrives as one of those seven words |

## What is never in an event

Table names, column names, schema names, database names, warehouse
identifiers, hostnames, connection URIs, credentials, SQL, cell values,
file paths, environment variables, email addresses, usernames, MCP client
name strings.

This is enforced, not promised. Every field passes through a whitelist, and
a caller cannot send an undeclared key because the serializer has no path
that writes one. The tests in
[`test_telemetry_schema.py`](../tests/test_telemetry_schema.py) are written
as attempts to leak a name, each asserting that it does not get out.

### Why counts are bucketed

An exact table and column count — say 47 and 611 — is close to unique, and
is therefore a weak fingerprint of one particular warehouse. "21–50 tables"
is not. The bucketing happens in the serializer rather than at call sites,
so an exact count cannot be sent even by mistake.

## When it is sent

Events are sent to `https://telemetry.semlayer.dev/v1/events`, infrastructure
we run — no third-party analytics service is involved. They are stored in the
United States and deleted after 13 months.

At the end of a command, in a background thread with a 1.5-second deadline.
If the endpoint is slow, down, or unreachable, the command is unaffected and
the events wait in the spool for next time.

**The spool is only cleared when the server confirms receipt.** A failure
never destroys pending events. Equally, events older than 30 days are
dropped rather than sent, and the spool is capped at 200 events — a machine
that is permanently offline never accumulates an unbounded file.

## What `--no-llm` does and does not mean

`--no-llm` means no schema, column names, or sample values are sent to any
model provider. It is a statement about where your *data* goes.

It is not a statement about all network traffic: telemetry still sends
unless you turn it off. The two are separate switches because they protect
different things, and conflating them would weaken the `--no-llm`
guarantee, which is the stronger of the two.
