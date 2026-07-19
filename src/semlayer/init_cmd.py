"""`semlayer init` — generate the minimal-grant setup for a warehouse.

The grant scripts are the PRODUCT's security story (PRD §11): the reader
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


def render(warehouse: str, database: str = "<your_database>",
           project: str = "<your_project>", warehouse_name: str = "<your_warehouse>",
           prefix: str = "semlayer") -> str:
    """Render the minimal-grant setup script for `warehouse` (snowflake|bigquery)."""
    if warehouse == "snowflake":
        return SNOWFLAKE_TEMPLATE.format(prefix=prefix, database=database, warehouse=warehouse_name)
    if warehouse == "bigquery":
        return BIGQUERY_TEMPLATE.format(prefix=prefix, project=project)
    raise ValueError(f"unsupported warehouse: {warehouse} (snowflake|bigquery)")
