# Several layers, one surface: sets

Finance, Sales and Platform each run `semlayer infer` against their own
schema, in their own repo, and commit their own file:

```bash
semlayer infer snowflake --schema finance      # writes finance.yaml, name: finance
```

None of those files knows about the others. Where a key column points
outside the file, `fct_gl.cost_center_id` say, the engine records a
foreign-key role with no target and a review item, and that is all.

An analyst who wants to work across the three writes a **hub**: a layer
whose `members` list names the other files. It usually owns no tables of
its own, and its `relationships` are the **seam**, the only place where
cross-document knowledge lives.

```yaml
semantic_layer:
  spec_version: 0.6.0
  name: acme
  members:
    - {name: finance,  path: ./finance.yaml}
    - {name: sales,    path: ./sales.yaml}
    - {name: platform, path: ./platform.yaml}
  relationships:
    - name: gl_cost_center
      from: {table: finance/fct_gl, columns: [cost_center_id]}
      to:   {table: platform/dim_cost_center, columns: [cost_center_id]}
      cardinality: many_to_one
      confidence: 0.94
```

Inside a set every table, hierarchy and metric is `<document>/<name>`:
`finance/fct_gl`, `sales/net_revenue`, `platform/cost_center_geo.*`. Member
files are never edited; the same file can sit in several hubs.

## Every command takes the hub

| command | what it does with a hub |
|---|---|
| `semlayer validate acme.yaml` | resolves the members, checks every reference across them, then the set's own rules: member names match and are unique, no two members describe one `source` in the same warehouse, a relationship across warehouses needs `federation` |
| `semlayer mcp acme.yaml` | one endpoint; each member is a domain; `table_detail finance/fct_gl` shows the seam edges; `compile_metric sales/net_revenue` grouped by `platform/dim_region.region_name` joins across the seam |
| `semlayer lint acme.yaml --sql q.sql` | SQL that names `finance.fct_gl` and `platform.dim_cost_center` is checked against the set; a join no relationship supports is a finding |
| `semlayer review acme.yaml` | the seam edges below the review threshold and the hub's own claims; members keep their own queues |

## What compiled SQL looks like

Tables are emitted by their `source`, aliased by their set name:

```sql
SELECT platform_dim_region.region_name AS region_name,
       SUM(sales_fct_orders.net_revenue) AS sales_net_revenue
FROM sales.fct_orders AS sales_fct_orders
LEFT JOIN platform.dim_cost_center AS platform_dim_cost_center
  ON sales_fct_orders.cost_center_id = platform_dim_cost_center.cost_center_id
LEFT JOIN platform.dim_region AS platform_dim_region
  ON platform_dim_cost_center.region_id = platform_dim_region.region_id
GROUP BY platform_dim_region.region_name
```

A single-schema layer compiles exactly as before: a table in the engine's
default schema is emitted by its bare name.

## Two warehouses

Each document records the `warehouse` it was inferred from. When a join
path crosses tables whose warehouses differ, `compile_metric` refuses with
the two warehouses named and tells the caller to run the per-warehouse
queries and join on the key, because no single statement can. If an engine
that sees both exists, a DuckDB bridge with both attached for instance,
the hub declares it and the join compiles:

```yaml
  federation: {engine: duckdb, bridge: bridge.duckdb}
```

## Inferring the seam

Writing the seam by hand is rung one. `semlayer stitch`, which finds the
parents of every unresolved key across members, tests inclusion through
the analyst's own connection and writes the hub, is the next release.
