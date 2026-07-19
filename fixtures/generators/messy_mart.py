"""messy_mart fixture: the primary evaluation warehouse.

~35 tables mimicking real enterprise cruft: cryptic names (ord_hdr, cust_mstr,
sts_cd, tot_amt), NO declared PK/FK constraints in DDL, an SCD2 customer
dimension keyed by surrogate + natural key, a product hierarchy
(department -> category -> product), a geography hierarchy on stores
(country -> state -> city), two lineage-backed aggregate tables that
reconcile exactly with their base facts, two deprecated legacy tables,
enum columns some with decode dictionaries and some without, and a handful
of operational/staging tables.

Even though no DDL constraints are declared, the DATA satisfies real
inclusion dependencies (every FK-shaped column's values are a subset of the
referenced natural/surrogate key's values) and the customer SCD2 validity
windows never overlap for a given natural key. That's the point: the gold
semantic layer (fixtures/golds/messy_mart.yaml) has to recover these
relationships without help from the catalog.

Deterministic: seeded RNG (random.Random(SEED)), no wall-clock dependence.
Row counts scale with env var MESSY_SCALE (default 1.0); at MESSY_SCALE=100
combined fact rows reach the 1-10M range.
"""

from __future__ import annotations

import os
import random
from datetime import date, datetime, timedelta

try:
    from generators._bulk import bulk_insert
except ImportError:
    from _bulk import bulk_insert

SEED = 8675309


def _scale() -> float:
    try:
        return float(os.environ.get("MESSY_SCALE", "1.0"))
    except ValueError:
        return 1.0


def _n(base: int, scale: float, floor: int = 10) -> int:
    return max(floor, int(round(base * scale)))


# ---------------------------------------------------------------------------
# Static reference vocabularies (kept small & fixed -> deterministic)
# ---------------------------------------------------------------------------

CITIES = [
    ("New York", "NY", "US"), ("Los Angeles", "CA", "US"), ("Chicago", "IL", "US"),
    ("Houston", "TX", "US"), ("Phoenix", "AZ", "US"), ("Toronto", "ON", "CA"),
    ("Vancouver", "BC", "CA"), ("London", "LDN", "GB"), ("Manchester", "MAN", "GB"),
    ("Berlin", "BER", "DE"), ("Munich", "BAV", "DE"), ("Paris", "IDF", "FR"),
]

DEPTS = [("ELEC", "Electronics"), ("APPRL", "Apparel"), ("HOME", "Home & Garden"),
          ("GROC", "Grocery"), ("TOYS", "Toys")]

CATEGORIES_BY_DEPT = {
    "ELEC": [("ELEC-PHN", "Phones"), ("ELEC-TV", "Televisions"), ("ELEC-AUD", "Audio")],
    "APPRL": [("APR-MEN", "Men's Apparel"), ("APR-WMN", "Women's Apparel")],
    "HOME": [("HOM-FURN", "Furniture"), ("HOM-KIT", "Kitchen"), ("HOM-GDN", "Garden")],
    "GROC": [("GRC-BEV", "Beverages"), ("GRC-SNK", "Snacks")],
    "TOYS": [("TOY-EDU", "Educational Toys"), ("TOY-OUT", "Outdoor Toys")],
}

CHNL = [("WEB", "Web Storefront"), ("STORE", "Physical Store"), ("MOBILE", "Mobile App"), ("CALL", "Call Center")]
PMT_MTHD = [("CC", "Credit Card"), ("ACH", "ACH Bank Transfer"), ("GC", "Gift Card"), ("COD", "Cash on Delivery")]
STS_CD = [("P", "Pending"), ("C", "Complete"), ("X", "Cancelled")]
EVT_TYP = [("START", "Subscription started"), ("RENEW", "Subscription renewed"),
           ("UPGRADE", "Plan upgraded"), ("DOWNGRADE", "Plan downgraded"), ("CANCEL", "Subscription cancelled")]
RTN_RSN = [("DEFECT", "Product defective"), ("WRONG_ITEM", "Wrong item shipped"),
           ("NLN", "No longer needed"), ("SIZE", "Size/fit issue")]
WEB_EVT_TYP = ["VIEW", "CLICK", "ADD_CART", "PURCHASE"]  # deliberately undecoded
SEG_CD = ["CON", "SMB", "ENT"]  # deliberately undecoded
SRC_SYS = ["CRM1", "CRM2", "LEGACY"]  # deliberately undecoded
ADDR_TYP = ["BILL", "SHIP"]  # deliberately undecoded
PROMO_TYP = ["PCT_OFF", "FLAT_OFF", "BOGO"]  # deliberately undecoded
CURR = [("USD", "US Dollar", 1.0), ("EUR", "Euro", 1.08), ("GBP", "British Pound", 1.27), ("CAD", "Canadian Dollar", 0.74)]

DATE_START = date(2023, 1, 1)
DATE_END = date(2025, 12, 31)


def _daterange(start: date, end: date):
    d = start
    out = []
    while d <= end:
        out.append(d)
        d += timedelta(days=1)
    return out


def _ymd(d: date) -> int:
    return d.year * 10000 + d.month * 100 + d.day


def build(con) -> None:
    con.execute("BEGIN TRANSACTION")
    try:
        _build(con)
    except BaseException:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


def _build(con) -> None:
    scale = _scale()
    rng = random.Random(SEED)

    N_CUST = _n(1500, scale)
    N_PROD = _n(400, scale, floor=20)
    N_STORE = _n(40, scale, floor=5)
    N_WHS = _n(8, scale, floor=2)
    N_REP = _n(80, scale, floor=5)
    N_PROMO = _n(60, scale, floor=5)
    N_CMPGN = _n(20, scale, floor=3)
    N_ORD_HDR = _n(8000, scale, floor=200)
    N_SUB_EVT = _n(4000, scale, floor=100)
    N_WEB_EVT = _n(10000, scale, floor=200)
    N_INV_SNPSHT = _n(2500, scale, floor=100)
    N_ORD_HDR_LEGACY = _n(1500, scale, floor=100)
    N_CUST_ADDR_PER = 2  # billing + shipping, most customers
    N_STG_ORDERS = _n(500, scale, floor=50)
    N_STG_CUST = _n(300, scale, floor=50)

    all_dates = _daterange(DATE_START, DATE_END)

    # ---------------------------------------------------------------
    # DDL — no PK/FK constraints anywhere, on purpose.
    # ---------------------------------------------------------------
    con.execute("CREATE TABLE date_dim (date_key INTEGER, cal_dt DATE, day_of_wk INTEGER, "
                "day_nm VARCHAR, wk_of_yr INTEGER, mth_nbr INTEGER, mth_nm VARCHAR, "
                "qtr_nbr INTEGER, yr_nbr INTEGER, is_wknd_flg INTEGER)")

    con.execute("CREATE TABLE dept_dim (dept_cd VARCHAR, dept_nm VARCHAR)")
    con.execute("CREATE TABLE category_dim (category_cd VARCHAR, category_nm VARCHAR, dept_cd VARCHAR)")
    con.execute("CREATE TABLE prod_ref (prod_sk INTEGER, prod_id VARCHAR, prod_nm VARCHAR, "
                "category_cd VARCHAR, dept_cd VARCHAR, unit_cost DECIMAL(10,2), "
                "list_price DECIMAL(10,2), actv_flg INTEGER, crt_dt DATE)")
    con.execute("CREATE TABLE prod_ref_legacy (prod_id VARCHAR, prod_desc VARCHAR, "
                "dept_cd VARCHAR, load_dt DATE)")
    con.execute("CREATE TABLE prod_attr_ext (prod_id VARCHAR, attr_nm VARCHAR, attr_val VARCHAR)")
    con.execute("CREATE TABLE sku_xref (prod_id VARCHAR, ext_sys_id VARCHAR, ext_sku VARCHAR, sync_dt DATE)")

    con.execute("CREATE TABLE whs_dim (whs_id VARCHAR, whs_nm VARCHAR, city_nm VARCHAR, state_cd VARCHAR)")
    con.execute("CREATE TABLE store_dim (store_sk INTEGER, store_id VARCHAR, store_nm VARCHAR, "
                "city_nm VARCHAR, state_cd VARCHAR, cntry_cd VARCHAR, whs_id VARCHAR, open_dt DATE)")
    con.execute("CREATE TABLE sls_rep_dim (rep_id VARCHAR, rep_nm VARCHAR, store_id VARCHAR, hire_dt DATE)")

    con.execute("CREATE TABLE chnl_dim (chnl_cd VARCHAR, chnl_nm VARCHAR)")
    con.execute("CREATE TABLE pmt_mthd_dim (pmt_mthd_cd VARCHAR, pmt_mthd_desc VARCHAR)")
    con.execute("CREATE TABLE sts_cd_dim (sts_cd VARCHAR, sts_desc VARCHAR)")
    con.execute("CREATE TABLE evt_typ_dim (evt_typ_cd VARCHAR, evt_typ_desc VARCHAR)")
    con.execute("CREATE TABLE rtn_rsn_dim (rtn_rsn_cd VARCHAR, rtn_rsn_desc VARCHAR)")
    con.execute("CREATE TABLE curr_dim (curr_cd VARCHAR, curr_nm VARCHAR, to_usd_rate DECIMAL(10,4))")
    con.execute("CREATE TABLE promo_dim (promo_id VARCHAR, promo_nm VARCHAR, promo_typ_cd VARCHAR, "
                "disc_pct DECIMAL(5,2), start_dt DATE, end_dt DATE)")
    con.execute("CREATE TABLE mktg_cmpgn_dim (cmpgn_id VARCHAR, cmpgn_nm VARCHAR, chnl_cd VARCHAR, "
                "start_dt DATE, end_dt DATE, budget_amt DECIMAL(12,2))")

    con.execute("CREATE TABLE cust_mstr (cust_sk INTEGER, cust_id VARCHAR, cust_nm VARCHAR, "
                "email VARCHAR, city_nm VARCHAR, state_cd VARCHAR, cntry_cd VARCHAR, "
                "seg_cd VARCHAR, src_sys_id VARCHAR, eff_start_dt DATE, eff_end_dt DATE, "
                "is_curr_flg INTEGER, crt_dt DATE)")
    con.execute("CREATE TABLE cust_mstr_legacy (cust_id VARCHAR, cust_nm VARCHAR, email VARCHAR, "
                "city_nm VARCHAR, state_cd VARCHAR, load_dt DATE)")
    con.execute("CREATE TABLE cust_addr (addr_id INTEGER, cust_id VARCHAR, addr_typ_cd VARCHAR, "
                "line1 VARCHAR, city_nm VARCHAR, state_cd VARCHAR, postal_cd VARCHAR, cntry_cd VARCHAR)")

    con.execute("CREATE TABLE ord_hdr (ord_id INTEGER, cust_id VARCHAR, store_id VARCHAR, "
                "date_key INTEGER, ord_dt DATE, sts_cd VARCHAR, chnl_cd VARCHAR, "
                "pmt_mthd_cd VARCHAR, promo_id VARCHAR, rep_id VARCHAR, curr_cd VARCHAR, "
                "tot_amt DECIMAL(12,2), crt_dt DATE, src_sys_id VARCHAR)")
    con.execute("CREATE TABLE ord_ln (ord_ln_id INTEGER, ord_id INTEGER, ln_num INTEGER, "
                "prod_id VARCHAR, qty INTEGER, unit_price DECIMAL(10,2), "
                "line_amt DECIMAL(12,2), line_sts_cd VARCHAR)")
    con.execute("CREATE TABLE ord_rtn (rtn_id INTEGER, ord_ln_id INTEGER, rtn_dt DATE, "
                "rtn_rsn_cd VARCHAR, rtn_qty INTEGER, rtn_amt DECIMAL(12,2))")
    con.execute("CREATE TABLE ord_hdr_legacy (order_number INTEGER, customer_number VARCHAR, "
                "order_dt DATE, status VARCHAR, amount DECIMAL(12,2))")

    con.execute("CREATE TABLE sub_evt (evt_id INTEGER, cust_id VARCHAR, sub_id VARCHAR, "
                "evt_typ_cd VARCHAR, evt_dt DATE, mrr_amt DECIMAL(10,2), crt_dt DATE)")
    con.execute("CREATE TABLE web_evt (evt_id BIGINT, cust_id VARCHAR, sess_id VARCHAR, "
                "evt_typ_cd VARCHAR, pg_url VARCHAR, evt_ts TIMESTAMP, ord_id INTEGER)")
    con.execute("CREATE TABLE inv_snpsht (snpsht_id INTEGER, date_key INTEGER, store_id VARCHAR, "
                "prod_id VARCHAR, on_hand_qty INTEGER, on_order_qty INTEGER)")

    con.execute("CREATE TABLE dly_sls_agg (agg_dt_key INTEGER, store_id VARCHAR, "
                "tot_sls_amt DECIMAL(14,2), ord_cnt INTEGER)")
    con.execute("CREATE TABLE mth_cust_agg (yr_mth VARCHAR, cust_id VARCHAR, "
                "tot_spend_amt DECIMAL(14,2), ord_cnt INTEGER)")

    con.execute("CREATE TABLE etl_log (run_id INTEGER, job_nm VARCHAR, start_ts TIMESTAMP, "
                "end_ts TIMESTAMP, sts_cd VARCHAR, rows_in INTEGER, rows_out INTEGER)")
    con.execute("CREATE TABLE stg_orders_raw (raw_id INTEGER, raw_ord_id VARCHAR, "
                "raw_payload VARCHAR, load_ts TIMESTAMP, file_nm VARCHAR, processed_flg INTEGER)")
    con.execute("CREATE TABLE stg_customers_raw (raw_id INTEGER, raw_cust_id VARCHAR, "
                "raw_payload VARCHAR, load_ts TIMESTAMP, file_nm VARCHAR)")
    con.execute("CREATE TABLE job_ctrl (job_nm VARCHAR, last_run_dt DATE, next_run_dt DATE, actv_flg INTEGER)")
    con.execute("CREATE TABLE dq_chk_log (chk_id INTEGER, tbl_nm VARCHAR, chk_nm VARCHAR, "
                "chk_dt DATE, pass_flg INTEGER, fail_cnt INTEGER)")
    con.execute("CREATE TABLE file_load_hist (load_id INTEGER, file_nm VARCHAR, src_sys_id VARCHAR, "
                "load_dt DATE, row_cnt INTEGER, sts_cd VARCHAR)")

    # ---------------------------------------------------------------
    # Reference / dimension data
    # ---------------------------------------------------------------
    date_rows = []
    for d in all_dates:
        date_rows.append((_ymd(d), d, d.isoweekday(), d.strftime("%A"), d.isocalendar()[1],
                           d.month, d.strftime("%B"), (d.month - 1) // 3 + 1, d.year,
                           1 if d.isoweekday() >= 6 else 0))
    bulk_insert(con, "date_dim", date_rows)

    bulk_insert(con, "dept_dim", DEPTS)
    cat_rows = [(c, n, dept) for dept, cats in CATEGORIES_BY_DEPT.items() for c, n in cats]
    bulk_insert(con, "category_dim", cat_rows)
    bulk_insert(con, "chnl_dim", CHNL)
    bulk_insert(con, "pmt_mthd_dim", PMT_MTHD)
    bulk_insert(con, "sts_cd_dim", STS_CD)
    bulk_insert(con, "evt_typ_dim", EVT_TYP)
    bulk_insert(con, "rtn_rsn_dim", RTN_RSN)
    bulk_insert(con, "curr_dim", CURR)

    all_cats = list(cat_rows)  # (category_cd, category_nm, dept_cd)

    products = []
    prod_ids = []
    for i in range(1, N_PROD + 1):
        pid = f"P{i:06d}"
        prod_ids.append(pid)
        cat = rng.choice(all_cats)
        cost = round(rng.uniform(3, 250), 2)
        price = round(cost * rng.uniform(1.3, 2.5), 2)
        crt = rng.choice(all_dates[:365])
        products.append((i, pid, f"product {pid}", cat[0], cat[2], cost, price,
                          1 if rng.random() > 0.05 else 0, crt))
    bulk_insert(con, "prod_ref", products)

    prod_legacy_ids = rng.sample(prod_ids, max(1, N_PROD // 4))
    prod_legacy_rows = [(pid, f"legacy desc {pid}",
                          next(p[4] for p in products if p[1] == pid),
                          rng.choice(all_dates[:180]))
                         for pid in prod_legacy_ids]
    bulk_insert(con, "prod_ref_legacy", prod_legacy_rows)

    attr_names = ["color", "weight_kg", "warranty_months"]
    prod_attr_rows = []
    for pid in rng.sample(prod_ids, max(1, N_PROD // 2)):
        for attr in rng.sample(attr_names, rng.randint(1, len(attr_names))):
            prod_attr_rows.append((pid, attr, str(rng.choice(["red", "blue", "1.2", "2.5", "12", "24"]))))
    bulk_insert(con, "prod_attr_ext", prod_attr_rows)

    sku_xref_rows = [(pid, rng.choice(["SAP", "NETSUITE", "SHOPIFY"]), f"SKU-{rng.randint(100000, 999999)}",
                       rng.choice(all_dates[-90:])) for pid in prod_ids]
    bulk_insert(con, "sku_xref", sku_xref_rows)

    whs_rows = []
    whs_ids = []
    for i in range(1, N_WHS + 1):
        wid = f"WH{i:03d}"
        whs_ids.append(wid)
        city, state, _cc = rng.choice(CITIES)
        whs_rows.append((wid, f"warehouse {wid}", city, state))
    bulk_insert(con, "whs_dim", whs_rows)

    store_rows = []
    store_ids = []
    for i in range(1, N_STORE + 1):
        sid = f"ST{i:04d}"
        store_ids.append(sid)
        city, state, cc = rng.choice(CITIES)
        store_rows.append((i, sid, f"store {sid}", city, state, cc, rng.choice(whs_ids),
                            rng.choice(all_dates[:400])))
    bulk_insert(con, "store_dim", store_rows)

    rep_rows = []
    rep_ids = []
    for i in range(1, N_REP + 1):
        rid = f"REP{i:05d}"
        rep_ids.append(rid)
        rep_rows.append((rid, f"rep {rid}", rng.choice(store_ids), rng.choice(all_dates[:500])))
    bulk_insert(con, "sls_rep_dim", rep_rows)

    promo_rows = []
    promo_ids = []
    for i in range(1, N_PROMO + 1):
        pid = f"PROMO{i:04d}"
        promo_ids.append(pid)
        s = rng.choice(all_dates[:700])
        e = s + timedelta(days=rng.randint(7, 60))
        promo_rows.append((pid, f"promo {pid}", rng.choice(PROMO_TYP), round(rng.uniform(5, 40), 2), s, e))
    bulk_insert(con, "promo_dim", promo_rows)

    cmpgn_rows = []
    for i in range(1, N_CMPGN + 1):
        cid = f"CMPGN{i:04d}"
        s = rng.choice(all_dates[:700])
        e = s + timedelta(days=rng.randint(14, 90))
        cmpgn_rows.append((cid, f"campaign {cid}", rng.choice(CHNL)[0], s, e, round(rng.uniform(5000, 100000), 2)))
    bulk_insert(con, "mktg_cmpgn_dim", cmpgn_rows)

    # ---------------------------------------------------------------
    # cust_mstr: SCD2 — 1-3 non-overlapping versions per natural key
    # ---------------------------------------------------------------
    cust_rows = []
    cust_ids = []
    cust_sk = 1
    for i in range(1, N_CUST + 1):
        cid = f"C{i:06d}"
        cust_ids.append(cid)
        n_versions = rng.choices([1, 2, 3], weights=[70, 20, 10])[0]
        base_crt = rng.choice(all_dates[:600])
        city, state, cc = rng.choice(CITIES)
        seg = rng.choice(SEG_CD)
        src = rng.choice(SRC_SYS)
        start = base_crt
        for v in range(n_versions):
            is_last = v == n_versions - 1
            if is_last:
                end = None
            else:
                span = rng.randint(30, 300)
                end = start + timedelta(days=span)
            if not is_last:
                city, state, cc = rng.choice(CITIES)
                seg = rng.choice(SEG_CD)
            cust_rows.append((cust_sk, cid, f"customer {cid}", f"{cid.lower()}@example.com",
                               city, state, cc, seg, src, start, end,
                               1 if is_last else 0, base_crt))
            cust_sk += 1
            if not is_last:
                start = end + timedelta(days=1)
    bulk_insert(con, "cust_mstr", cust_rows)

    legacy_cust_ids = rng.sample(cust_ids, max(1, N_CUST // 3))
    cust_legacy_rows = [(cid, f"legacy customer {cid}", f"{cid.lower()}@old-example.com",
                          *rng.choice(CITIES)[:2], rng.choice(all_dates[:200])) for cid in legacy_cust_ids]
    bulk_insert(con, "cust_mstr_legacy", cust_legacy_rows)

    addr_rows = []
    addr_id = 1
    for cid in cust_ids:
        for typ in rng.sample(ADDR_TYP, rng.randint(1, 2)):
            city, state, cc = rng.choice(CITIES)
            addr_rows.append((addr_id, cid, typ, f"{rng.randint(1, 9999)} Main St", city, state,
                               f"{rng.randint(10000, 99999)}", cc))
            addr_id += 1
    bulk_insert(con, "cust_addr", addr_rows)

    # ---------------------------------------------------------------
    # ord_hdr / ord_ln — header total is derived from its lines (reconciles)
    # ---------------------------------------------------------------
    prod_price = {p[1]: float(p[6]) for p in products}
    ord_hdr_rows = []
    ord_ln_rows = []
    ord_ids = []
    ord_cust_of = {}
    ord_ln_id = 1
    for oid in range(1, N_ORD_HDR + 1):
        cid = rng.choice(cust_ids)
        ord_dt = rng.choice(all_dates)
        sts = rng.choices(["P", "C", "X"], weights=[10, 80, 10])[0]
        n_lines = rng.randint(1, 5)
        tot = 0.0
        for ln in range(1, n_lines + 1):
            pid = rng.choice(prod_ids)
            qty = rng.randint(1, 5)
            price = prod_price[pid]
            line_amt = round(qty * price, 2)
            tot = round(tot + line_amt, 2)
            ord_ln_rows.append((ord_ln_id, oid, ln, pid, qty, price, line_amt, sts))
            ord_ln_id += 1
        crt = ord_dt + timedelta(days=rng.randint(0, 2))
        ord_hdr_rows.append((oid, cid, rng.choice(store_ids), _ymd(ord_dt), ord_dt, sts,
                              rng.choice(CHNL)[0], rng.choice(PMT_MTHD)[0],
                              rng.choice(promo_ids) if rng.random() < 0.3 else None,
                              rng.choice(rep_ids) if rng.random() < 0.8 else None,
                              rng.choices([c[0] for c in CURR], weights=[70, 12, 10, 8])[0],
                              tot, crt, rng.choice(SRC_SYS)))
        ord_ids.append(oid)
        ord_cust_of[oid] = cid
    bulk_insert(con, "ord_hdr", ord_hdr_rows)
    bulk_insert(con, "ord_ln", ord_ln_rows)

    rtn_rows = []
    rtn_id = 1
    sample_lines = rng.sample(ord_ln_rows, max(1, len(ord_ln_rows) // 20))
    for ln in sample_lines:
        ord_ln_id_, _oid, _lnnum, _pid, qty, price, _amt, _sts = ln
        rtn_qty = rng.randint(1, qty)
        rtn_rows.append((rtn_id, ord_ln_id_, rng.choice(all_dates[-180:]), rng.choice(RTN_RSN)[0],
                          rtn_qty, round(rtn_qty * float(price), 2)))
        rtn_id += 1
    bulk_insert(con, "ord_rtn", rtn_rows)

    # legacy order headers: earlier period, different vocabulary
    legacy_dates = _daterange(date(2020, 1, 1), date(2022, 12, 31))
    legacy_rows = []
    for i in range(1, N_ORD_HDR_LEGACY + 1):
        legacy_rows.append((i, rng.choice(cust_ids), rng.choice(legacy_dates),
                             rng.choices(["OPEN", "CLOSED", "CANCELLED"], weights=[5, 85, 10])[0],
                             round(rng.uniform(10, 800), 2)))
    bulk_insert(con, "ord_hdr_legacy", legacy_rows)

    # ---------------------------------------------------------------
    # sub_evt / web_evt
    # ---------------------------------------------------------------
    sub_rows = []
    n_subs = max(1, N_SUB_EVT // 3)
    for s in range(1, n_subs + 1):
        sub_id = f"SUB{s:06d}"
        cid = rng.choice(cust_ids)
        n_events = rng.randint(1, 4)
        base_dt = rng.choice(all_dates[:600])
        mrr = round(rng.uniform(9, 199), 2)
        seq = ["START"] + rng.choices(["RENEW", "UPGRADE", "DOWNGRADE"], k=max(0, n_events - 2))
        if rng.random() < 0.3:
            seq.append("CANCEL")
        evt_dt = base_dt
        for typ in seq:
            if typ == "UPGRADE":
                mrr = round(mrr * 1.2, 2)
            elif typ == "DOWNGRADE":
                mrr = round(mrr * 0.8, 2)
            elif typ == "CANCEL":
                mrr = 0.0
            sub_rows.append((len(sub_rows) + 1, cid, sub_id, typ, evt_dt, mrr,
                              evt_dt + timedelta(days=rng.randint(0, 1))))
            evt_dt = evt_dt + timedelta(days=rng.randint(20, 90))
            if evt_dt > DATE_END:
                break
    bulk_insert(con, "sub_evt", sub_rows)

    web_rows = []
    for i in range(1, N_WEB_EVT + 1):
        anon = rng.random() < 0.3
        cid = None if anon else rng.choice(cust_ids)
        typ = rng.choices(WEB_EVT_TYP, weights=[55, 25, 12, 8])[0]
        ord_ref = None
        if typ == "PURCHASE" and not anon and ord_ids:
            ord_ref = rng.choice(ord_ids)
            cid = ord_cust_of[ord_ref]
        ts = datetime.combine(rng.choice(all_dates), datetime.min.time()) + timedelta(
            seconds=rng.randint(0, 86399))
        web_rows.append((i, cid, f"SESS{rng.randint(1, N_WEB_EVT * 2):08d}", typ,
                          f"/p/{rng.randint(1, 999)}", ts, ord_ref))
    bulk_insert(con, "web_evt", web_rows)

    # ---------------------------------------------------------------
    # inv_snpsht (dedupe on natural composite key)
    # ---------------------------------------------------------------
    seen = set()
    inv_rows = []
    snpsht_id = 1
    attempts = 0
    while len(inv_rows) < N_INV_SNPSHT and attempts < N_INV_SNPSHT * 5:
        attempts += 1
        d = rng.choice(all_dates)
        sid = rng.choice(store_ids)
        pid = rng.choice(prod_ids)
        key = (d, sid, pid)
        if key in seen:
            continue
        seen.add(key)
        inv_rows.append((snpsht_id, _ymd(d), sid, pid, rng.randint(0, 500), rng.randint(0, 200)))
        snpsht_id += 1
    bulk_insert(con, "inv_snpsht", inv_rows)

    # ---------------------------------------------------------------
    # Aggregate tables — lineage-derived, must reconcile exactly with facts.
    # Business rule (documented in the gold): excludes cancelled ('X') orders.
    # ---------------------------------------------------------------
    dly = {}
    mth = {}
    for row in ord_hdr_rows:
        oid, cid, sid, date_key, ord_dt, sts, *_rest, tot, crt, src = row
        if sts == "X":
            continue
        dkey = (date_key, sid)
        d = dly.setdefault(dkey, [0.0, 0])
        d[0] = round(d[0] + float(tot), 2)
        d[1] += 1
        yr_mth = f"{ord_dt.year:04d}-{ord_dt.month:02d}"
        mkey = (yr_mth, cid)
        m = mth.setdefault(mkey, [0.0, 0])
        m[0] = round(m[0] + float(tot), 2)
        m[1] += 1

    dly_rows = [(dkey[0], dkey[1], v[0], v[1]) for dkey, v in sorted(dly.items())]
    mth_rows = [(mkey[0], mkey[1], v[0], v[1]) for mkey, v in sorted(mth.items())]
    bulk_insert(con, "dly_sls_agg", dly_rows)
    bulk_insert(con, "mth_cust_agg", mth_rows)

    # ---------------------------------------------------------------
    # Operational / staging tables
    # ---------------------------------------------------------------
    etl_rows = []
    for i in range(1, 200 + 1):
        start = datetime.combine(rng.choice(all_dates), datetime.min.time()) + timedelta(
            hours=rng.randint(0, 23))
        end = start + timedelta(minutes=rng.randint(1, 90))
        etl_rows.append((i, rng.choice(["load_ord_hdr", "load_cust_mstr", "load_web_evt", "agg_dly_sls"]),
                          start, end, rng.choices(["SUCCESS", "FAILED", "RUNNING"], weights=[90, 8, 2])[0],
                          rng.randint(100, 50000), rng.randint(90, 50000)))
    bulk_insert(con, "etl_log", etl_rows)

    stg_ord_rows = []
    for i in range(1, N_STG_ORDERS + 1):
        raw_oid = rng.choice(ord_ids) if ord_ids else i
        stg_ord_rows.append((i, str(raw_oid), '{"order_id": %d, "raw": true}' % raw_oid,
                              datetime.combine(rng.choice(all_dates[-30:]), datetime.min.time()),
                              f"orders_{rng.randint(1, 30):03d}.json", rng.choice([0, 1])))
    bulk_insert(con, "stg_orders_raw", stg_ord_rows)

    stg_cust_rows = []
    for i in range(1, N_STG_CUST + 1):
        raw_cid = rng.choice(cust_ids)
        stg_cust_rows.append((i, raw_cid, '{"cust_id": "%s", "raw": true}' % raw_cid,
                               datetime.combine(rng.choice(all_dates[-30:]), datetime.min.time()),
                               f"customers_{rng.randint(1, 30):03d}.json"))
    bulk_insert(con, "stg_customers_raw", stg_cust_rows)

    job_rows = [
        ("load_ord_hdr", all_dates[-1], all_dates[-1] + timedelta(days=1), 1),
        ("load_cust_mstr", all_dates[-1], all_dates[-1] + timedelta(days=1), 1),
        ("agg_dly_sls", all_dates[-1], all_dates[-1] + timedelta(days=1), 1),
        ("agg_mth_cust", all_dates[-1], all_dates[-1] + timedelta(days=1), 1),
        ("load_web_evt", all_dates[-1], all_dates[-1] + timedelta(days=1), 1),
    ]
    bulk_insert(con, "job_ctrl", job_rows)

    dq_rows = []
    for i in range(1, 150 + 1):
        dq_rows.append((i, rng.choice(["ord_hdr", "cust_mstr", "ord_ln", "web_evt"]),
                         rng.choice(["not_null_check", "row_count_check", "range_check"]),
                         rng.choice(all_dates[-60:]), rng.choices([1, 0], weights=[95, 5])[0],
                         rng.randint(0, 20)))
    bulk_insert(con, "dq_chk_log", dq_rows)

    file_rows = []
    for i in range(1, 120 + 1):
        file_rows.append((i, f"file_{i:04d}.csv", rng.choice(SRC_SYS), rng.choice(all_dates[-90:]),
                           rng.randint(10, 100000), rng.choices(["SUCCESS", "FAILED"], weights=[95, 5])[0]))
    bulk_insert(con, "file_load_hist", file_rows)


NAME = "messy_mart"
