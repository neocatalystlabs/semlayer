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
- **`--no-llm`**: nothing leaves your environment at all.
- **Telemetry**: anonymous command counts spooled to a local file only; no
  network transmission exists in this release. Opt out: `SEMLAYER_TELEMETRY=off`.
- The MCP server binds stdio (local) and serves the semantic layer document;
  it executes no warehouse queries.

## Supported versions

Beta: only the latest release receives fixes.
