"""Fiscal calendars: detection from the customer's date dim + compile routing.

The customer's date dimension materializes their fiscal calendar as derived
columns (Gregorian date grain, fiscal_yr/fiscal_qtr attributes). We verify
each attribute against Gregorian extraction — divergent columns are the
fiscal calendar — and compile_metric must DISAMBIGUATE quarter/year grains
rather than silently impose our calendar on theirs.

Synthetic Feb-start (NRF-style) warehouse: fiscal Q1 = Feb-Apr, so fiscal
answers genuinely differ from Gregorian ones and a wrong implementation
fails these tests.
"""

import datetime as dt

import duckdb

from semlayer.compile import compile_metric
from semlayer.pipeline import infer
from semlayer.source import DuckDBSource


def _fiscal_parts(d: dt.date) -> tuple[int, int]:
    """Feb-start fiscal calendar: FY labeled by start year; Q1 = Feb-Apr."""
    shifted = d.month - 2 if d.month >= 2 else d.month + 10  # months since Feb
    fy = d.year if d.month >= 2 else d.year - 1
    return fy, shifted // 3 + 1


def _build():
    con = duckdb.connect(":memory:")
    con.execute("""CREATE TABLE dt_dim (
        date_key INTEGER, cal_dt DATE, yr_nbr INTEGER, qtr_nbr INTEGER,
        fis_yr INTEGER, fis_qtr INTEGER)""")
    con.execute("CREATE TABLE sls_fct (sls_id INTEGER, date_key INTEGER, amt DOUBLE)")
    day = dt.date(2023, 1, 1)
    rows, facts, i = [], [], 0
    while day < dt.date(2025, 1, 1):
        key = int(day.strftime("%Y%m%d"))
        fy, fq = _fiscal_parts(day)
        rows.append((key, day, day.year, (day.month - 1) // 3 + 1, fy, fq))
        for _ in range(2):
            i += 1
            facts.append((i, key, float(100 + (i % 7))))
        day += dt.timedelta(days=1)
    con.executemany("INSERT INTO dt_dim VALUES (?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO sls_fct VALUES (?,?,?)", facts)
    return con


CON = _build()
DOC = infer(DuckDBSource(CON))


def _cols():
    t = next(t for t in DOC["semantic_layer"]["tables"] if t["name"] == "dt_dim")
    return {c["name"]: c for c in t["columns"]}


def test_detection_classifies_calendar_vs_fiscal():
    cols = _cols()
    assert cols["qtr_nbr"].get("time_attribute") == "calendar_quarter"
    assert cols["yr_nbr"].get("time_attribute") == "calendar_year"
    assert cols["fis_qtr"].get("time_attribute") == "fiscal_quarter"
    assert cols["fis_yr"].get("time_attribute") == "fiscal_year"
    assert any("verified against cal_dt" in p["detail"]
               for p in cols["fis_qtr"]["provenance"])


def _amt_metric():
    return next(m for m in DOC["semantic_layer"]["metrics"]
                if m["measure"] == "sls_fct.amt" and not m.get("filter"))


def test_quarter_without_calendar_choice_refuses():
    m = _amt_metric()
    out = compile_metric(DOC, m["name"], time_grain="quarter")
    assert out.get("refused") and "fiscal" in out["reason"]
    assert "calendar='fiscal'" in out["reason"]


def test_fiscal_quarter_parity():
    m = _amt_metric()
    out = compile_metric(DOC, m["name"], time_grain="quarter", calendar="fiscal")
    assert "sql" in out, out
    got = {(r[0], r[1]): r[2] for r in CON.execute(out["sql"]).fetchall()}
    want = {(r[0], r[1]): r[2] for r in CON.execute(
        "SELECT d.fis_yr, d.fis_qtr, SUM(f.amt) FROM sls_fct f "
        "LEFT JOIN dt_dim d ON f.date_key = d.date_key "
        "GROUP BY 1, 2").fetchall()}
    assert got == want
    # fiscal answers genuinely differ from Gregorian: Jan-2023 belongs to
    # FY2022 Q4 fiscally but 2023 Q1 on the calendar
    cal = compile_metric(DOC, m["name"], time_grain="quarter", calendar="calendar")
    cal_rows = {(r[0], r[1]): r[2] for r in CON.execute(cal["sql"]).fetchall()}
    assert got != cal_rows


def test_calendar_choice_uses_the_declared_levels():
    """SPEC 2.5: bucket through the declared hierarchy, not ad hoc date math.

    The dim already carries verified yr_nbr/qtr_nbr; using them must give the
    same answer as re-deriving it with date_trunc, or the levels were wrong.
    """
    m = _amt_metric()
    out = compile_metric(DOC, m["name"], time_grain="quarter", calendar="calendar")
    assert "sql" in out, out
    assert "yr_nbr" in out["sql"] and "qtr_nbr" in out["sql"]
    assert "date_trunc" not in out["sql"]
    got = {(r[0], r[1]): r[2] for r in CON.execute(out["sql"]).fetchall()}
    want = {(r[0].year, (r[0].month - 1) // 3 + 1): r[1] for r in CON.execute(
        "SELECT date_trunc('quarter', d.cal_dt), SUM(f.amt) FROM sls_fct f "
        "LEFT JOIN dt_dim d ON f.date_key = d.date_key GROUP BY 1").fetchall()}
    assert got == want


def test_fiscal_year_grain():
    m = _amt_metric()
    out = compile_metric(DOC, m["name"], time_grain="year", calendar="fiscal")
    assert "sql" in out and "fis_yr" in out["sql"] and "fis_qtr" not in out["sql"]
    got = dict(CON.execute(out["sql"]).fetchall())
    want = dict(CON.execute(
        "SELECT d.fis_yr, SUM(f.amt) FROM sls_fct f "
        "LEFT JOIN dt_dim d ON f.date_key = d.date_key GROUP BY 1").fetchall())
    assert got == want


def test_month_grain_needs_no_choice():
    m = _amt_metric()
    out = compile_metric(DOC, m["name"], time_grain="month")
    assert "sql" in out  # fiscal periods not modeled here; month is unambiguous


def test_no_fiscal_warehouse_needs_no_choice():
    # messy_mart-style: when no fiscal columns verify, quarter compiles plain
    import importlib
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fixtures"))
    mod = importlib.import_module("generators.messy_mart")
    con = duckdb.connect(":memory:")
    mod.build(con)
    doc = infer(DuckDBSource(con))
    out = compile_metric(doc, "total_tot_amt", time_grain="quarter")
    assert "sql" in out and "date_trunc" in out["sql"]
    con.close()


def test_a_semester_is_not_a_fiscal_quarter():
    """A two-valued date-dimension column must not be tagged a fiscal quarter.

    `quarter_shaped` was `2 <= n_distinct <= 4`, so a semester's values (1, 2)
    passed the shape test, failed the exact EXTRACT(QUARTER) match, and fell
    through to `fiscal_quarter`. AdventureWorksDW carries both CalendarSemester
    and FiscalSemester and BOTH came out `fiscal_quarter` -- the first of them
    not even fiscal.

    The spec's `time_attribute` enum has no semester, so the correct output is
    NO tag rather than a wrong one. Adding `calendar_semester`/
    `fiscal_semester` is a MINOR spec bump and needs a decision. Every real
    quarter column in the corpus has exactly 4 distinct values, so requiring 3
    costs nothing.
    """
    from semlayer.enrich.run import _time_attribute_tag

    class _C:
        def __init__(self, n):
            self.n_distinct = n

    class _TS:
        def __init__(self, n):
            self.columns = {"c": _C(n)}

    class _Src:
        """Values 1 and 2 against a date column: no row matches EXTRACT(QUARTER)."""

        def __init__(self, q_match):
            self.q_match = q_match

        def query(self, _sql):
            # (q_match, m_match, y_match, y_dev, lo, hi)
            return [(self.q_match, 0, 0, 2000, 1, 2)]

    col = {"name": "c", "sql_type": "INTEGER"}
    # a semester: 2 distinct values, no quarter agreement -> no tag at all
    assert _time_attribute_tag(_Src(0), "t", "d", col, _TS(2), 100) is None
    # a genuine fiscal quarter keeps its tag
    assert _time_attribute_tag(_Src(0), "t", "d", col, _TS(4), 100) == "fiscal_quarter"
