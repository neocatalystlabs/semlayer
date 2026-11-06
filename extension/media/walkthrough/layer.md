## One YAML file

`semlayer infer` reads a warehouse it can already reach — profiling every
column, discovering keys and join paths, matching aggregate tables back to
their facts — and writes one portable document.

```
semlayer infer snowflake:analytics_prod
semlayer infer duckdb:warehouse.duckdb
semlayer infer postgresql://localhost/shop
```

It only ever reads. Cost is shown before anything runs, and a run resumes from
cache if you interrupt it.

**Already have one?** A `*.yaml` layer anywhere in the workspace is picked up
automatically. The status bar names the one being used.
