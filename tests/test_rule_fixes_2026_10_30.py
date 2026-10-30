"""Rule fixes the 2,000-table review queue pointed at (docs/calibration.md, 2026-10-30).

Seven of the nine questions that estate asked were the engine's, not the
user's. Each test here pins one of the fixes; the typing scores that moved
are recorded in the calibration report.
"""

from semlayer.link.table_type import classify_table
from semlayer.profile.stats import ColumnStats
from semlayer.profile.typing_rules import classify, is_keyish


def _col(name, sql_type="VARCHAR", n_distinct=85, unique=False, rows=1000):
    return ColumnStats(name=name, sql_type=sql_type, row_count=rows, n_null=0,
                       n_distinct=n_distinct, is_unique=unique)


def test_etl_bookkeeping_columns_are_metadata_not_untyped_dimensions():
    """src_system / record_hash were 1,198 of 1,708 review items: unknown at 0.05."""
    for name in ("src_system", "record_hash", "src_sys", "dw_inserted_at", "_fivetran_synced"):
        t = classify(_col(name))
        assert t.entity_role == "metadata", (name, t)
        assert t.confidence >= 0.7, (name, t.confidence)
    # a real batch key on a fact table is not claimed by the ETL list
    assert classify(_col("batch_id", "INTEGER", n_distinct=400)).entity_role == "foreign_key"


def test_a_raw_payload_column_is_a_json_blob_for_bookkeeping():
    t = classify(_col("payload"))
    assert (t.semantic_type, t.entity_role) == ("json_object", "metadata")


def test_sk_prefix_is_a_surrogate_key_like_sk_suffix():
    """30 `sk_*` columns on the estate were typed summed measures by the integer fallback."""
    t = classify(_col("sk_customer", "INTEGER", n_distinct=155, unique=True, rows=155))
    assert (t.semantic_type, t.entity_role) == ("identifier", "primary_key")
    t = classify(_col("sk_customer", "INTEGER", n_distinct=155, unique=False, rows=9000))
    assert (t.semantic_type, t.entity_role) == ("identifier", "foreign_key")
    assert is_keyish("sk_customer") and is_keyish("fk_order") and not is_keyish("skew")


def test_a_coded_dimensions_display_name_is_a_name_not_a_code():
    """`channel_name`, `category_nm`: gold types every one of these `name`."""
    for name in ("channel_name", "category_nm", "segment_name", "priority_name"):
        t = classify(_col(name, n_distinct=54))
        assert t.semantic_type == "name", (name, t)
    # the specific vocabularies above the name rule still win
    assert classify(_col("cust_name")).semantic_type == "pii_name"
    assert classify(_col("city_nm")).semantic_type == "geo_city"
    # and the name rule's confidence is unchanged: it must keep escalating
    assert classify(_col("channel_name")).confidence == 0.65


def test_an_isolated_int_table_follows_its_staging_tier_not_operational():
    # the shape the 2,000-table estate had: key, name, payload and four ETL columns
    t = {"name": "int__hr_region", "tier": {"value": "staging", "confidence": 0.9},
         "columns": [{"name": "hr_region_id", "entity_role": "primary_key"},
                     {"name": "hr_region_name", "entity_role": "dimension"},
                     {"name": "payload", "entity_role": "metadata"},
                     {"name": "etl_batch_id", "entity_role": "metadata"},
                     {"name": "dw_inserted_at", "entity_role": "metadata"},
                     {"name": "src_system", "entity_role": "metadata"},
                     {"name": "record_hash", "entity_role": "metadata"}]}
    ttype, conf, detail = classify_table(t, fk_out=0, fk_in=0)
    assert (ttype, conf) == ("staging", 0.85) and "staging tier" in detail
    # the same shape with no tier signal is still the honest 0.1 operational
    t.pop("tier")
    assert classify_table(t, fk_out=0, fk_in=0)[0] == "operational"
    # a table the graph can type keeps the graph's answer, whatever the tier
    t["tier"] = {"value": "staging", "confidence": 0.9}
    t["columns"].append({"name": "amount", "entity_role": "measure"})
    assert classify_table(t, fk_out=2, fk_in=0)[0] == "fact"


def test_metric_names_do_not_say_total_twice_and_reconciled_rollups_are_not_asked():
    from semlayer import review
    from semlayer.enrich.run import _metric_candidates
    agg_cols = [{"name": "total_tax", "sql_type": "DECIMAL", "semantic_type": "monetary_value",
                 "entity_role": "measure", "aggregations": {"allowed": ["sum"], "default": "sum"}}]
    sl = {"tables": [
        {"name": "fct_sales", "table_type": "fact", "primary_key": ["sale_id"],
         "columns": [{"name": "sale_id", "sql_type": "INTEGER", "semantic_type": "identifier",
                      "entity_role": "primary_key"},
                     {"name": "tax", "sql_type": "DECIMAL", "semantic_type": "monetary_value",
                      "entity_role": "measure", "aggregations": {"allowed": ["sum"], "default": "sum"}},
                     {"name": "tot_amt", "sql_type": "DECIMAL", "semantic_type": "monetary_value",
                      "entity_role": "measure", "aggregations": {"allowed": ["sum"], "default": "sum"}}]},
        {"name": "agg_sales_monthly", "table_type": "aggregate", "columns": agg_cols},
        {"name": "agg_sales_weekly", "table_type": "aggregate", "columns": list(agg_cols)},
    ]}
    metrics = _metric_candidates(sl)
    names = {m["name"] for m in metrics}
    assert "total_tax" in names and "total_total_tax" not in names
    # only the full word is folded: `tot_amt` stays `total_tot_amt` (messy_mart's gold)
    assert "total_tot_amt" in names
    # both rollups keep their metrics (a consumer may compile against the rollup directly);
    # only the one whose sums reconciled to the fact is spared the review question
    sl["metrics"] = metrics
    sl["aggregate_tables"] = [{"table": "agg_sales_monthly", "aggregates": "fct_sales",
                               "lifecycle": "inferred"}]
    asked = {i.table for i in review.collect({"semantic_layer": sl})
             if i.kind == "metric_plausibility"}
    assert asked == {"agg_sales_weekly"}
