# Iceberg / lakehouse via the DuckDB bridge

semlayer's DuckDB connector doubles as a bridge to Apache Iceberg tables —
on S3 or local — without a warehouse in the middle. Two recipes, both ending
in a standard `semlayer infer duckdb:...` run.

## Recipe 1: REST catalog (Glue, S3 Tables, Nessie, Polaris) — ATTACH

DuckDB attaches an Iceberg REST catalog as a database; semlayer enumerates
and queries across attached catalogs natively (catalog-qualified names are
handled by the connector).

```sql
-- bridge.sql
INSTALL iceberg; LOAD iceberg;
CREATE SECRET glue_secret (TYPE S3, PROVIDER credential_chain);
ATTACH 'my_catalog' AS ice (TYPE iceberg, ENDPOINT_TYPE glue);
```

```bash
duckdb bridge.duckdb < bridge.sql
semlayer infer duckdb:bridge.duckdb -o layer.yaml
```

## Recipe 2: direct table paths — view mapping

No REST catalog? Map each table's metadata path to a view:

```sql
-- bridge.sql
INSTALL iceberg; LOAD iceberg;
CREATE VIEW orders    AS SELECT * FROM iceberg_scan('s3://lake/warehouse/orders');
CREATE VIEW customers AS SELECT * FROM iceberg_scan('s3://lake/warehouse/customers');
```

Then infer against the same file as above. Views profile exactly like
tables — types, statistics, FK discovery, decodes all work unchanged.

## Status

Verified end-to-end in our test suite against real pyiceberg-written tables
(enumeration, catalog-qualified profiling, full inference). The live
Glue/S3-Tables REST leg follows the documented DuckDB `iceberg` extension
paths; if you hit an edge there, tell us — you'd be among the first through
that door, and we'll pair on it.

Drift detection works via snapshot diffing (`semlayer drift` against the
same bridge file); Iceberg-native change feeds are roadmap.
