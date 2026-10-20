# Security

## Reporting

Email help@neocatalystlabs.com (or use GitHub private vulnerability
reporting on this repo). We aim to acknowledge within 72 hours. Please do not
open public issues for vulnerabilities.

## Data-flow summary (what leaves your environment)

- The CLI runs **in your environment** with **your** warehouse credentials
  (read-only minimal grant; `semlayer init` generates the script — drift-feed
  grants are commented opt-in) and **your** LLM API key.
- **Default mode**: column statistics and up to 10 sample values per
  low-cardinality column are included in LLM prompts sent to your configured
  LLM provider. Nothing else leaves.
- **`--no-sample-egress`**: cell values never enter prompts (statistics only).
- **`--no-llm`**: no schema, column names, or sample values are sent to any
  LLM. Your data never reaches a model provider. This is a statement about
  where your schema and data go, not about all network traffic -- see
  Telemetry below.
- **Telemetry**: anonymous usage statistics are sent to
  `telemetry.semlayer.dev`, infrastructure we run — command, duration,
  outcome, warehouse type, bucketed counts, version, OS. **Never** table or
  column names, schema names, connection URIs, credentials, SQL, or cell
  values; the event is assembled on your machine and nothing is derived from
  the request. Inspect before it is sent: `semlayer telemetry --show`. Opt
  out: `semlayer telemetry --off` (permanent) or `SEMLAYER_TELEMETRY=off`
  (this shell). Full field list: [docs/telemetry.md](docs/telemetry.md).
- The MCP server binds stdio (local) and serves the semantic layer document;
  it executes no warehouse queries.

## Supported versions

Beta: only the latest release receives fixes.
