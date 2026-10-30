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
files are never edited; the same file can sit in several hubs. A hub cannot
yet be a member of another hub: validation refuses it, so a regional hub
listed inside a global one fails loudly instead of contributing nothing.
List the teams' files directly. Document names never contain `/`.

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

```bash
semlayer stitch finance.yaml sales.yaml platform.yaml -o acme.yaml --via snowflake
```

`stitch` reads the members and finds every **unresolved reference**: a
foreign-key column with no target, which is how a member records a key
that points outside its own file. For each one it does what Link does
inside a layer, name-ranked candidate parents and one inclusion statement
per child, except that the parents come from the other members and the
statements run through `--via`, the analyst's own connection and grants.
It writes the hub and touches no member.

- A parent with name evidence is written at Link's confidence, with the
  inclusion ratio and name score as provenance.
- A parent found by statistics alone is written at 0.5, below the review
  threshold, so `semlayer review acme.yaml` shows it as a `seam_edge`
  rather than anyone assuming it.
- A key that found no parent in any member is listed under `unresolved`.
- Among equally-named parents, a `certified` table wins: the platform
  team's conformed dimension beats a staging copy of it.
- `--via` defaults to `snowflake` or `bigquery` when every member records
  that warehouse. With `--no-queries`, or when no connection can reach the
  members, edges are proposed from names and distinct counts only, at 0.3,
  marked as untested.

Statistics come from the member documents (`cardinality`, roles, types);
the inclusion tests are the only queries stitch issues.

## Reviewing the seam, and refreshing it

`semlayer review acme.yaml` shows the hub's queue: seam edges below the
threshold and `same_entity` pairs with no canonical copy. Accepting one
marks it `reviewed`; rejecting one marks it `deprecated` and leaves it in
the hub, so consumers skip it and the next refresh does not propose it
again. Verdicts are written to the hub file itself, never to the members.

When a member file changes, re-infer the seam from the members the hub
lists:

```bash
semlayer stitch --refresh acme.yaml --via snowflake
```

The rule is the same one `infer --into` applies to a layer. Edges you
reviewed, certified or rejected are kept as they are; `inferred` edges are
dropped and inferred again; a kept edge whose end no longer exists in its
member becomes `orphaned`, with the missing end named in its provenance,
and stays until you delete it. A fresh inference is dropped when a kept
edge already answers it: the same two ends whatever your verdict was, or
the same key column under an edge you accepted. A rejected edge blocks only
itself, so a different parent for that key is still proposed, and a key
whose only parent was the one you rejected is listed under `unresolved`
saying so. The hub's own tables, metrics and `federation` are untouched.

## Copies across members

Finance keeps its own `dim_customer`: the platform rows for finance's
customers, plus `credit_limit` and `owner_team` that only finance needs.
That is not a fact and its dimension, it is the same entity twice, and a
consumer that joined the two as if they were a key and its parent would be
wrong. Stitch looks for it: two tables in different members whose
single-column entity keys share a name (an SCD2 table's natural key counts;
`id` and `event_id` do not, every fact has one) and whose key values are
contained one in the other. It writes:

```yaml
    - name: finance_dim_customer_same_entity_as_platform_dim_customer
      from: {table: finance/dim_customer,  columns: [customer_id]}
      to:   {table: platform/dim_customer, columns: [customer_id]}
      cardinality: one_to_one
      same_entity: true
      canonical: platform/dim_customer
      shared_columns: [customer_id, customer_name, segment]
      extends_with: [credit_limit, owner_team]
      key_overlap: {from_in_to: 1.0, to_in_from: 0.667}
      confidence: 0.85
```

`canonical` is the table the organisation stands behind: a `certified` copy
first, else the one whose keys contain the other's. When neither decides
(the same keys, neither certified) the edge is written at 0.5 without
`canonical` and `semlayer review acme.yaml` shows it as a `same_entity`
item for the analyst to settle. `key_overlap` is measured both ways, so a
subset reads as it is: finance holds two thirds of platform's customers.
`extends_with` is what finance added. Among several tables in one member
that hold the same keys (`dim_ticket` and the `int__ticket` it is built
from), the one in the same naming layer as the copy wins. Without a
connection the pair is proposed from names and distinct counts at 0.3 with
no `canonical`.

On the 5,000-table multi-schema estate (41 members, 6 shadow dimensions)
stitch found all 6 with no false pair and named the canonical table on each;
the seam itself is unchanged at 1,019 of 1,019.
