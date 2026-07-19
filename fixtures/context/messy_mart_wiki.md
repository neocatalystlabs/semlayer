# Retail mart — analytics wiki export

Maintained by the data team. Some pages may be out of date.

## Monthly customer aggregate

`mth_cust_agg` is refreshed on the 2nd of each month by the `mth_cust_rollup`
job. `yr_mth` is the year-month period key in `YYYYMM` format (for example
`202405` is May 2024) — treat it as a date period, never as a quantity.

## Order status codes

`ord_hdr.sts_cd` values: P = Pending, C = Completed, X = Refunded.
Finance reporting excludes X orders from revenue. The decode dimension
`sts_cd_dim` carries `sts_desc`, the human-readable status name for each
code — a label, not a code itself.

## Daily sales aggregate

`dly_sls_agg` is refreshed nightly after the warehouse load completes.
`store_id` is the store identifier, joining to `store_dim` — it is a key,
not a categorical code.

## SKU cross-reference

`sku_xref.ext_sku` is the external partner SKU code used by the EDI feed;
it is an opaque code, not a measure.
