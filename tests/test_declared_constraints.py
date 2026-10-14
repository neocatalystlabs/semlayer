"""Rung 1: the catalog's own PK/FK declarations, read as priors.

Free -- one catalog query, no table touched -- and it removes work, because a
declared primary key makes composite-key probing return early.

Everything here uses DuckDB, which actually enforces constraints. The
PostgreSQL side is covered against a live server in test_postgres.py.
"""

from __future__ import annotations

import duckdb
import pytest

from semlayer.link import link_source
from semlayer.profile.run import profile_with_stats
from semlayer.source import DeclaredConstraints, DuckDBSource

DDL = """
CREATE SCHEMA mart;
CREATE TABLE mart.cust (cust_id INTEGER PRIMARY KEY, cust_nm TEXT, cntry_cd TEXT);
CREATE TABLE mart.ord (ord_id INTEGER PRIMARY KEY,
                       cust_id INTEGER REFERENCES mart.cust(cust_id),
                       amt DECIMAL(10,2));
CREATE TABLE mart.ord_ln (ord_id INTEGER REFERENCES mart.ord(ord_id),
                          ln_num INTEGER, qty INTEGER,
                          PRIMARY KEY (ord_id, ln_num));
"""


@pytest.fixture
def con():
    c = duckdb.connect()
    c.execute(DDL)
    for i in range(1, 41):
        c.execute("INSERT INTO mart.cust VALUES (?, ?, ?)", [i, f"c{i}", "US" if i % 3 else "CA"])
    for i in range(1, 121):
        c.execute("INSERT INTO mart.ord VALUES (?, ?, ?)", [i, (i % 40) + 1, 10.0 + i])
        for ln in range(1, (i % 3) + 2):
            c.execute("INSERT INTO mart.ord_ln VALUES (?, ?, ?)", [i, ln, ln * 2])
    yield c
    c.close()


def test_reads_single_and_composite_primary_keys(con):
    d = DuckDBSource(con).list_constraints()
    assert d.primary_keys[("mart", "cust")] == ("cust_id",)
    assert d.primary_keys[("mart", "ord_ln")] == ("ord_id", "ln_num"), \
        "composite keys must keep declaration order, not be alphabetised"


def test_reads_foreign_keys_including_the_target(con):
    """DuckDB exposes the child columns structurally but the TARGET only inside
    constraint_text, so the parse is the part that can break."""
    d = DuckDBSource(con).list_constraints()
    edges = {(f.child_table, f.child_columns, f.parent_table, f.parent_columns)
             for f in d.foreign_keys}
    assert edges == {
        ("ord", ("cust_id",), "cust", ("cust_id",)),
        ("ord_ln", ("ord_id",), "ord", ("ord_id",)),
    }
    assert all(f.enforced for f in d.foreign_keys), "DuckDB enforces"


def test_unparseable_constraint_text_is_skipped_not_guessed(con):
    src = DuckDBSource(con)
    assert src._FK_RE.search("FOREIGN KEY (a) REFERENCES s.t(b)")
    assert src._FK_RE.search("something we have never seen") is None


def test_empty_constraints_are_falsey_so_callers_can_skip():
    assert not DeclaredConstraints()
    assert DeclaredConstraints(primary_keys={("s", "t"): ("c",)})


def _link(con, use_declared):
    src = DuckDBSource(con)
    doc, stats = profile_with_stats(src, llm=None)
    link_source(src, doc, stats, llm=None, use_declared=use_declared)
    return doc["semantic_layer"]


def test_declared_composite_pk_is_applied_with_catalog_provenance(con):
    sl = _link(con, True)
    t = next(x for x in sl["tables"] if x["name"] == "ord_ln")
    assert t["primary_key"] == ["ord_id", "ln_num"]
    assert any(p["signal"] == "catalog_prior" and "PRIMARY KEY" in p["detail"]
               for p in t["provenance"])


def test_agreeing_declaration_is_recorded_as_corroboration(con):
    """Profile already picks a single-column key from uniqueness.

    Without this, a declaration that agrees would vanish -- and two independent
    signals agreeing is exactly what we claim to record.
    """
    sl = _link(con, True)
    t = next(x for x in sl["tables"] if x["name"] == "cust")
    assert t["primary_key"] == ["cust_id"]
    assert any(p["signal"] == "catalog_prior" and "agrees" in p["detail"]
               for p in t["provenance"])


def test_declared_fks_are_emitted_at_higher_confidence_than_discovery(con):
    sl = _link(con, True)
    by = {(r["from"]["table"], r["to"]["table"]): r for r in sl["relationships"]}
    r = by[("ord", "cust")]
    assert r["from"]["columns"] == ["cust_id"] and r["to"]["columns"] == ["cust_id"]
    assert r["confidence"] == 0.95, "enforced declaration"
    assert [p["signal"] for p in r["provenance"]] == ["catalog_prior"]
    col = next(c for t in sl["tables"] if t["name"] == "ord"
               for c in t["columns"] if c["name"] == "cust_id")
    assert col["entity_role"] == "foreign_key"
    assert col["foreign_key"]["references"] == "cust.cust_id"


def test_unenforced_declarations_land_lower_and_say_so():
    """Snowflake and BigQuery never enforce; Postgres allows NOT VALID."""
    from semlayer.link.run import _emit_relationships
    doc = {"semantic_layer": {"tables": [
        {"name": "a", "columns": [{"name": "k"}]}, {"name": "b", "columns": [{"name": "k"}]}]}}
    cols = {("a", "k"): doc["semantic_layer"]["tables"][0]["columns"][0]}
    _emit_relationships([], cols, doc, [("a", ["k"], "b", ["k"], False)])
    r = doc["semantic_layer"]["relationships"][0]
    assert r["confidence"] == 0.85
    assert "NOT enforced" in r["provenance"][0]["detail"]


def test_use_declared_false_changes_nothing_at_all(con):
    """The eval measures discovery, so this path must be inert.

    Our generators declare keys in 7 of 9 fixtures; reading them while scoring
    against gold would credit a key we never inferred, and it also changes the
    Describe prompt hash and invalidates the cassettes.
    """
    sl = _link(con, False)
    for t in sl["tables"]:
        assert not any("declared in the warehouse catalog" in p.get("detail", "")
                       for p in t.get("provenance", []))
    for r in sl.get("relationships", []):
        assert all(p["signal"] != "catalog_prior" for p in r.get("provenance", []))


def test_a_source_without_list_constraints_still_links(con):
    """Optional capability: no method, no change, no crash."""
    class Bare:
        def __init__(self, c): self._c = c
        def list_tables(self): return DuckDBSource(self._c).list_tables()
        def query(self, sql): return self._c.execute(sql).fetchall()
    src = Bare(con)
    assert not hasattr(src, "list_constraints")
    doc, stats = profile_with_stats(src, llm=None)
    link_source(src, doc, stats, llm=None, use_declared=True)
    assert doc["semantic_layer"]["tables"]


def test_a_failing_catalog_does_not_stop_inference(con):
    """A catalog we cannot read must degrade, not abort."""
    class Angry(DuckDBSource):
        def list_constraints(self):
            raise RuntimeError("permission denied on pg_constraint")
    src = Angry(con)
    doc, stats = profile_with_stats(src, llm=None)
    link_source(src, doc, stats, llm=None, use_declared=True)
    assert doc["semantic_layer"]["tables"], "inference must survive"


def test_declared_fk_suppresses_rediscovery_of_the_same_column(con):
    sl = _link(con, True)
    edges = [(r["from"]["table"], tuple(r["from"]["columns"])) for r in sl["relationships"]]
    assert len(edges) == len(set(edges)), "one parent per child column, not two"


# --- positional key suffixes ----------------------------------------------
#
# OMOP's concept_relationship has concept_id_1 and concept_id_2, both
# referencing concept.concept_id. One naming blind spot cost us BOTH the typing
# and the link: _ID_NAME_RE anchors on $, so the trailing ordinal hid them and
# they typed as quantity/measure -- which excluded them from being a foreign-key
# child at all, before naming similarity ever got a say.

def test_ordinal_suffix_folds_only_when_the_stem_is_key_shaped():
    from semlayer.profile.typing_rules import strip_ordinal

    assert strip_ordinal("concept_id_1") == "concept_id"
    assert strip_ordinal("concept_id_2") == "concept_id"
    assert strip_ordinal("cust_key_3") == "cust_key"
    assert strip_ordinal("order_sk_10") == "order_sk"
    # and NOT otherwise: folding these would invent a reference to a `sales`
    # or `addr_line` table
    assert strip_ordinal("sales_2024") == "sales_2024"
    assert strip_ordinal("q1_revenue") == "q1_revenue"
    assert strip_ordinal("addr_line_1") == "addr_line_1"
    assert strip_ordinal("x_9999") == "x_9999"
    assert strip_ordinal("plain") == "plain"


def test_an_ordinal_key_is_typed_as_an_identifier_not_a_quantity():
    """The first half of the bug: typing excluded it before linking could try."""
    from semlayer.profile.typing_rules import _ID_NAME_RE, strip_ordinal

    assert not _ID_NAME_RE.search("concept_id_1"), "the raw name still does not match"
    assert _ID_NAME_RE.search(strip_ordinal("concept_id_1")), "the folded one does"


def test_ordinal_columns_score_as_an_exact_name_match():
    """The second half: once typed, naming must reach the parent."""
    from semlayer.link.candidates import name_similarity
    from semlayer.link.run import AUTO_NAME_THRESHOLD

    for col in ("concept_id_1", "concept_id_2"):
        s = name_similarity(col, "concept_relationship", "concept_id", "concept")
        assert s >= AUTO_NAME_THRESHOLD, f"{col} scored {s}"
    # a non-key ordinal must NOT be dragged onto a same-named table
    assert name_similarity("sales_2024", "t", "sales_id", "sales") < AUTO_NAME_THRESHOLD


def test_both_positional_columns_survive_as_role_playing_references(con):
    """They reference the SAME parent column, which is what role-playing is."""
    con.execute("CREATE TABLE mart.rel (concept_id_1 INTEGER, concept_id_2 INTEGER, kind TEXT)")
    con.execute("CREATE TABLE mart.concept (concept_id INTEGER PRIMARY KEY, nm TEXT)")
    for i in range(1, 51):
        con.execute("INSERT INTO mart.concept VALUES (?, ?)", [i, f"c{i}"])
    for i in range(1, 121):
        con.execute("INSERT INTO mart.rel VALUES (?, ?, ?)",
                    [(i % 50) + 1, ((i * 7) % 50) + 1, "maps_to"])
    sl = _link(con, False)
    edges = {(r["from"]["table"], r["from"]["columns"][0], r["to"]["table"])
             for r in sl.get("relationships", [])}
    assert ("rel", "concept_id_1", "concept") in edges
    assert ("rel", "concept_id_2", "concept") in edges


# --- dbt documentation as a catalog prior ---------------------------------

def test_dbt_descriptions_fill_gaps_but_never_overwrite_a_human(con):
    """Same precedence rule as a native COMMENT: fill gaps, flag conflicts.

    Two humans documenting the same column differently is worth a review, not a
    coin toss, so a disagreement is recorded rather than silently resolved.
    """
    from semlayer.lineage import LineageBundle
    from semlayer.profile.run import profile_with_stats
    from semlayer.source import DuckDBSource

    con.execute("COMMENT ON COLUMN mart.cust.cust_nm IS 'from the warehouse'")
    lin = LineageBundle(
        parents={"cust": set()},
        table_descriptions={"cust": "customers, per dbt"},
        column_descriptions={("cust", "cust_nm"): "from dbt",
                             ("cust", "cntry_cd"): "ISO country, per dbt"},
    )
    src = DuckDBSource(con)
    doc, _ = profile_with_stats(src, llm=None, lineage=lin)
    t = next(x for x in doc["semantic_layer"]["tables"] if x["name"] == "cust")
    cols = {c["name"]: c for c in t["columns"]}

    # the warehouse comment stands; dbt's is NOT written over it
    assert cols["cust_nm"]["description"] == "from the warehouse"
    # the gap is filled
    assert cols["cntry_cd"]["description"] == "ISO country, per dbt"
    # both are recorded as catalog priors
    assert any(p["signal"] == "catalog_prior" for p in cols["cust_nm"]["provenance"])
    assert t["description"] == "customers, per dbt"


def test_a_contradiction_between_comment_and_dbt_is_flagged(con):
    from semlayer.lineage import LineageBundle
    from semlayer.profile.run import profile_with_stats
    from semlayer.source import DuckDBSource

    con.execute("COMMENT ON TABLE mart.cust IS 'one row per customer'")
    lin = LineageBundle(parents={"cust": set()},
                        table_descriptions={"cust": "one row per ACCOUNT"})
    doc, _ = profile_with_stats(DuckDBSource(con), llm=None, lineage=lin)
    t = next(x for x in doc["semantic_layer"]["tables"] if x["name"] == "cust")
    assert t["description"] == "one row per customer", "the warehouse wins"
    assert any("differ" in c["detail"] for c in t.get("conflicts", [])), \
        "the disagreement must be surfaced, not resolved by coin toss"


def test_no_lineage_means_no_change(con):
    from semlayer.profile.run import profile_with_stats
    from semlayer.source import DuckDBSource

    doc, _ = profile_with_stats(DuckDBSource(con), llm=None, lineage=None)
    assert doc["semantic_layer"]["tables"], "must not crash without a manifest"
