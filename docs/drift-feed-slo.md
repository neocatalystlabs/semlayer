# Warehouse Change-Feed Latency (measured)

The drift loop's layering: the CI/CD hook is the only guaranteed-fast path;
warehouse change feeds are the COMPLETE path (catch ad hoc DDL regardless of
origin), with warehouse-dependent latency; scheduled snapshot diffing is the
universal fallback.

## Snowflake — ACCOUNT_USAGE.QUERY_HISTORY

- Documented worst case (Snowflake docs): up to ~45 min for QUERY_HISTORY;
  hours for some object-metadata views.
- **Measured 2026-07-18 (single live sample, XS trial account): CREATE TABLE
  visible to the reader persona after 2.2 minutes.**
- Claimed SLO remains conservative until the nightly measurement accumulates
  a distribution: "minutes to ~45 minutes, warehouse-dependent." The nightly
  connector suite appends one sample per run; revisit the claim at n>=30.
- Grant required: IMPORTED PRIVILEGES ON DATABASE snowflake — the documented
  OPT-IN beyond the minimal grant (commented in `semlayer init` output).

## BigQuery — INFORMATION_SCHEMA.JOBS

- Not yet measured live (reader has roles/bigquery.resourceViewer opt-in
  ready). Documented latency is lower than Snowflake's ACCOUNT_USAGE;
  measurement lands with the nightly suite.

## Fallback

`semlayer drift <doc> <source>` performs a full structural diff on every run;
a cron cadence of N minutes bounds detection latency at N regardless of feed
availability — no grants beyond minimal required.
