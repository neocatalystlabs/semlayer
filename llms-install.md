# Installing semlayer as an MCP server

For an AI agent setting this up on a user's machine. Everything needed is on
this page; nothing here requires reading another file.

## What it is

semlayer reads a data warehouse and writes the semantic layer an agent needs
to query it correctly — table and column meanings, the foreign keys nobody
declared, which columns are measures, the filters that must always be
applied. It then serves that over MCP, and can run SQL against the warehouse.

## Prerequisite

`uvx` (from [uv](https://docs.astral.sh/uv/)). Check with `uvx --version`. If
absent: `curl -LsSf https://astral.sh/uv/install.sh | sh`.

## Configuration

Add to the client's MCP config:

```json
{
  "mcpServers": {
    "semlayer": {
      "command": "uvx",
      "args": ["--with", "semlayer[warehouses]", "semlayer", "mcp"]
    }
  }
}
```

Then restart the client.

**`--with semlayer[warehouses]` is not decoration.** The Snowflake, BigQuery
and Postgres drivers are optional dependencies. Without them the server can
*find* a warehouse and not open it, which fails on the first real request
rather than at startup — so a config missing that argument looks like it
worked.

## Optional environment

| Variable | Effect if absent |
|---|---|
| `ANTHROPIC_API_KEY` | Inference still runs on statistics alone. Descriptions and LLM-validated joins are skipped; the layer is thinner but valid. |

No key is required to start the server or to build a layer. Do not block
setup on it, and do not invent one.

## Verifying it worked

Ask the server for `status`. That is the tool to call first in any session:
it reports which warehouses are configured on the machine, whether a layer
has been built, and what to do next. A fresh install with no layer is a
normal, expected state — `status` will say so and name the next step.

If `status` answers, the installation is correct.

## What not to do

- Don't pass a layer file path unless the user has one. The server starts
  with nothing and guides the user through building it.
- Don't run `infer` unprompted. It reads the user's warehouse and, with a
  key set, spends money on LLM calls.
