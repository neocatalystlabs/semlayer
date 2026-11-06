## The mistakes that return a number

Open any `.sql` file. Checking happens as you type — no command, no button.

The findings worth having are not unknown tables. They are the two that
**execute cleanly and return the wrong answer**:

```
fanout_aggregate         SUM(ord_hdr.tot_amt) in a block that joins ord_ln
                         (many rows per ord_hdr) multiplies ord_hdr's values

missing_required_filter  ord_hdr requires `sts_cd <> 'X'` for amount
                         aggregations
```

On one real warehouse those two are the difference between **$1,108,382.99**
and **$4,924,778.63**.

No warehouse connection is involved: it is the YAML plus a parse, so any
dialect works — including Redshift, Databricks, T-SQL and Trino, which have no
semlayer connector at all. Set **`semlayer.dialect`**.
