"""`semlayer init` — generate the minimal-grant setup for a warehouse.

The grant scripts are the PRODUCT's security story: the reader
persona gets USAGE + SELECT and nothing else; every extra grant is a
documented opt-in. These templates are live-verified by the nightly
connector tests (reader-cannot-write proofs).
"""

from __future__ import annotations

SNOWFLAKE_TEMPLATE = """-- semlayer minimal-grant setup (Snowflake). Run as ACCOUNTADMIN.
-- Reader = the ONLY credential semlayer needs for inference.
USE ROLE ACCOUNTADMIN;

CREATE ROLE IF NOT EXISTS {prefix}_reader;
GRANT USAGE ON DATABASE {database} TO ROLE {prefix}_reader;
GRANT USAGE ON ALL SCHEMAS IN DATABASE {database} TO ROLE {prefix}_reader;
GRANT USAGE ON FUTURE SCHEMAS IN DATABASE {database} TO ROLE {prefix}_reader;
GRANT SELECT ON ALL TABLES IN DATABASE {database} TO ROLE {prefix}_reader;
GRANT SELECT ON FUTURE TABLES IN DATABASE {database} TO ROLE {prefix}_reader;
GRANT USAGE ON WAREHOUSE {warehouse} TO ROLE {prefix}_reader;

-- OPT-IN (drift watching only; EXCEEDS the minimal grant — see docs):
-- GRANT IMPORTED PRIVILEGES ON DATABASE snowflake TO ROLE {prefix}_reader;

CREATE USER IF NOT EXISTS {prefix}_reader_svc PASSWORD = '<set-a-strong-password>'
  DEFAULT_ROLE = {prefix}_reader DEFAULT_WAREHOUSE = {warehouse} MUST_CHANGE_PASSWORD = FALSE;
GRANT ROLE {prefix}_reader TO USER {prefix}_reader_svc;

-- Then set these before running `semlayer infer snowflake`:
--
--   export SNOWFLAKE_ACCOUNT=<your-account>
--   export SNOWFLAKE_READER_USER={prefix}_reader_svc
--   export SNOWFLAKE_READER_PASSWORD=<the-password-above>
--   export SNOWFLAKE_READER_ROLE={prefix}_reader
--
-- The ROLE is required and is not optional. semlayer passes it as the session
-- role rather than relying on the user's DEFAULT_ROLE, because a minimal-grant
-- claim is about the role actually in effect -- and if you run semlayer as your
-- own account instead of this service user, an inherited DEFAULT_ROLE would
-- silently profile under every privilege you hold. The role in effect is
-- recorded in the run report.
"""

BIGQUERY_TEMPLATE = """# semlayer minimal-grant setup (BigQuery). Requires project owner/IAM admin.
PROJECT={project}

gcloud iam service-accounts create {prefix}-reader --project=$PROJECT
gcloud projects add-iam-policy-binding $PROJECT \\
  --member="serviceAccount:{prefix}-reader@$PROJECT.iam.gserviceaccount.com" \\
  --role="roles/bigquery.dataViewer"
gcloud projects add-iam-policy-binding $PROJECT \\
  --member="serviceAccount:{prefix}-reader@$PROJECT.iam.gserviceaccount.com" \\
  --role="roles/bigquery.jobUser"

# OPT-IN (drift watching via INFORMATION_SCHEMA.JOBS; EXCEEDS minimal — see docs):
# gcloud projects add-iam-policy-binding $PROJECT \\
#   --member="serviceAccount:{prefix}-reader@$PROJECT.iam.gserviceaccount.com" \\
#   --role="roles/bigquery.resourceViewer"

gcloud iam service-accounts keys create {prefix}-reader.json \\
  --iam-account="{prefix}-reader@$PROJECT.iam.gserviceaccount.com"
"""


POSTGRES_TEMPLATE = """-- semlayer minimal-grant setup (PostgreSQL).
-- Run as a superuser or as the owner of the database.
-- Also applies to Redshift and other Postgres-protocol engines, though their
-- catalogs differ in places and are not covered by our live tests.
CREATE ROLE {prefix}_reader LOGIN PASSWORD '<set-a-strong-password>';

GRANT CONNECT ON DATABASE {database} TO {prefix}_reader;
GRANT USAGE ON SCHEMA {schema} TO {prefix}_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {prefix}_reader;
-- Future tables: Postgres has no FUTURE GRANTS, so default privileges are the
-- equivalent. They apply only to tables created by the role that runs this
-- statement, so re-run it per owner if your ETL creates tables as someone else.
ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT SELECT ON TABLES TO {prefix}_reader;

-- Repeat the three GRANTs and the ALTER for each schema you want inferred.

-- No further grants are needed, and in particular NOT pg_read_all_stats:
-- pg_stats filters its rows to tables the caller can already SELECT, so the
-- grants above are sufficient to read cardinality and null rates from the
-- planner's statistics instead of scanning. pg_class.reltuples is readable by
-- any role. Verified on PostgreSQL 16 -- see docs/calibration.md.
-- The same role cannot INSERT, UPDATE, DELETE, or CREATE in any schema.

-- Then set these before running `semlayer infer postgres`:
--
--   export PG_READER_DSN=postgresql://{prefix}_reader:<the-password-above>@host:5432/{database}
--
-- Or pass a connection string directly, which skips the environment entirely:
--
--   semlayer infer postgresql://... -o layer.yaml
--
-- PG_READER_ROLE is optional and usually unnecessary: unlike Snowflake, this
-- role has no privileges to inherit beyond the ones granted above. Set it if
-- you run inference as a human account that belongs to several groups, and
-- semlayer will SET ROLE and record the role in effect in the run report.
"""


def render(warehouse: str, database: str = "<your_database>",
           project: str = "<your_project>", warehouse_name: str = "<your_warehouse>",
           schema: str = "<your_schema>", prefix: str = "semlayer") -> str:
    """Render the minimal-grant setup script for `warehouse` (snowflake|bigquery|postgres)."""
    if warehouse == "snowflake":
        return SNOWFLAKE_TEMPLATE.format(prefix=prefix, database=database, warehouse=warehouse_name)
    if warehouse == "bigquery":
        return BIGQUERY_TEMPLATE.format(prefix=prefix, project=project)
    if warehouse == "postgres":
        return POSTGRES_TEMPLATE.format(prefix=prefix, database=database, schema=schema)
    raise ValueError(f"unsupported warehouse: {warehouse} (snowflake|bigquery|postgres)")
