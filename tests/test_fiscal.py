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
    cal_rows = CON.execute(cal["sql"]).fetchall()
    assert got != {(r[0].year, (r[0].month - 1) // 3 + 1): r[1] for r in cal_rows}


def test_calendar_choice_uses_date_trunc():
    m = _amt_metric()
    out = compile_metric(DOC, m["name"], time_grain="quarter", calendar="calendar")
    assert "sql" in out and "date_trunc('quarter'" in out["sql"]
    assert CON.execute(out["sql"]).fetchall()


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
