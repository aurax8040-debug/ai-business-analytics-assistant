"""SFA 门店画像分析 —— 查询层（阶段二）。

业务边界（写死在本模块，页面不推断）
------------------------------------
- **门店口径**：以「当前筛选下的有效拜访实际到过的门店」为画像对象（去重 ``store_id``），
  门店属性统一从 ``dim_store`` 取（维度表），不从拜访行反推店级属性。
- **热门 SKU**：SFA 只采集「门店重点分销产品」**一组**现场反馈（底表列 25），
  不代表门店全部在售 SKU。仅当名称能精确映射到 72 SKU 主数据（``MATCHED``）才计入排名；
  ``PENDING`` / ``NOT_COLLECTED`` 一律单列，**不猜测、不当成销量**。
- **零售价**：底表列 26「门店前端标价（元/盒）」，不含满减 / 券 / 补贴 / 线上 APP 价。
  线上与线下价格不混合计算；异常价（低于同 SKU 25 分位的一半）单独计数，标记待确认，
  **不删除、不改写**。
- **陈列 / 执行**：字段为空一律视为「未采集」，与「未执行」严格区分；
  同时展示采集覆盖率，避免把未采集误读为未执行。
- **SFA × POS**：只在 33 家 ``POS_ALIGNED`` 门店（27 家 join eligible）内做联合分析；
  未匹配门店保留 ``PENDING``，不强行关联；POS 无该 SKU 记录**不等于零销售**。

只读 ETL V1；任何 SQL 只引用单张事实表（必要时 JOIN 维度表）。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from . import board_filters, config

BOARD = "sfa_profile"
TOP_SKU = 10
TOP_MD = 10
TOP_KA = 8

NOTES = (
    "门店口径 = 当前筛选下有效拜访实际到过的门店（去重）；门店属性取自 dim_store。",
    "SFA 只采集一组「门店重点分销产品」，不代表门店全部在售 SKU；未匹配名称标 PENDING，不当作销量。",
    "零售价为门店前端标价（元/盒），不含满减 / 券 / 线上优惠价；线上与线下不混合。",
    "字段为空 = 未采集，不等于未执行；页面并列展示采集覆盖率。",
    "SFA×POS 仅在 27 家可关联门店内进行；未匹配保留 PENDING，POS 无记录不等于零销售。",
)


# ------------------------------------------------------------------ 工具
def _rows(conn, sql, params) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _one(conn, sql, params) -> dict:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else {}


def _ratio(num, den):
    if den in (None, 0) or num is None:
        return None
    return 100.0 * num / den


def _percentile(values: list[float], q: float):
    """线性插值分位数（与 Excel PERCENTILE.INC 同法），用于价格离散度。"""
    if not values:
        return None
    data = sorted(values)
    if len(data) == 1:
        return float(data[0])
    pos = (len(data) - 1) * q
    low = int(pos)
    high = min(low + 1, len(data) - 1)
    frac = pos - low
    return float(data[low] + (data[high] - data[low]) * frac)


def _envelope(f: board_filters.BoardFilters, data, extra: dict | None = None) -> dict:
    meta = {
        "board": BOARD,
        "filters": f.as_dict(),
        "region_label": f.region_label,
        "period": f"{config.FIXED_YEAR} · {f.month_label}",
        "year": config.FIXED_YEAR,
        "year_int": config.FIXED_YEAR_INT,
        "scope_note": config.SCOPE_NOTE,
        "source_views": ["fact_sfa_visit", "dim_store", "bridge_sfa_pos_store",
                         "bridge_sfa_sku", "dim_sku", "fact_pos_sales"],
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "notes": list(NOTES),
    }
    if extra:
        meta.update(extra)
    return {"ok": True, "data": data, "meta": meta}


# ------------------------------------------------------------------ 门店集合
def _store_set_sql(f: board_filters.BoardFilters) -> tuple[str, list]:
    """当前筛选下实际到过的门店集合（子查询，单表，不跨事实表 JOIN）。"""
    clause, params = board_filters.where_sfa_visit(f)
    return ("SELECT DISTINCT v.store_id FROM fact_sfa_visit v WHERE " + clause), params


def _stores(conn, f) -> list[dict]:
    sub, params = _store_set_sql(f)
    sql = ("SELECT ds.store_id, ds.store_name, ds.ka_system, ds.store_level, ds.store_type, "
           "ds.customer_type, ds.region_name_std, ds.market_department, ds.city, "
           "ds.manager_virtual_id, ds.store_manager_name, ds.pos_link_status "
           "FROM dim_store ds WHERE ds.store_id IN (" + sub + ") ORDER BY ds.store_id")
    return _rows(conn, sql, params)


def _distribution(rows: list[dict], key: str, order: list[str] | None = None) -> list[dict]:
    counter: dict[str, int] = {}
    for row in rows:
        raw = row.get(key)
        label = (str(raw).strip() if raw not in (None, "") else "未采集")
        counter[label] = counter.get(label, 0) + 1
    keys = list(counter)
    if order:
        keys = [k for k in order if k in counter] + \
               sorted([k for k in keys if k not in order], key=lambda k: -counter[k])
    else:
        keys.sort(key=lambda k: -counter[k])
    return [{"label": k, "value": counter[k]} for k in keys]


# ------------------------------------------------------------------ 热门 SKU
def _hot_sku(conn, f) -> dict:
    clause, params = board_filters.where_sfa_visit(f)
    # 空字符串等于「没采到」，必须排除，否则会多出一个名为空的「热门 SKU」
    valid_sku = " AND NULLIF(TRIM(v.focus_sku_code), '') IS NOT NULL "
    rows = _rows(conn,
        "SELECT v.focus_sku_code AS sku_code, MAX(v.focus_product_name) AS raw_name, "
        "MAX(s.sku_name) AS sku_name, MAX(s.category) AS category, "
        "MAX(s.pieces_per_box) AS pieces, MAX(s.specification) AS specification, "
        "COUNT(*) AS feedback_count, COUNT(DISTINCT v.store_id) AS store_count, "
        "COUNT(DISTINCT v.manager_virtual_id) AS employee_count, "
        "ROUND(AVG(v.focus_retail_price), 2) AS avg_price "
        "FROM fact_sfa_visit v LEFT JOIN dim_sku s ON s.sku_code = v.focus_sku_code "
        "WHERE " + clause + valid_sku +
        "GROUP BY v.focus_sku_code ORDER BY feedback_count DESC, store_count DESC", params)

    total_visits = _one(conn,
        "SELECT COUNT(*) AS visits, "
        "SUM(CASE WHEN v.focus_sku_code IS NOT NULL "
        "AND TRIM(v.focus_sku_code) <> '' THEN 1 ELSE 0 END) AS with_sku, "
        "SUM(CASE WHEN v.focus_sku_match_status='PENDING' THEN 1 ELSE 0 END) AS pending, "
        "SUM(CASE WHEN v.focus_sku_match_status='NOT_COLLECTED' THEN 1 ELSE 0 END) AS not_collected "
        "FROM fact_sfa_visit v WHERE " + clause, params) or {}

    # 不同门店等级的 SKU 结构差异：等级 × TOP SKU 的反馈次数
    level_rows = _rows(conn,
        "SELECT COALESCE(NULLIF(TRIM(v.store_level),''),'未采集') AS level, "
        "v.focus_sku_code AS sku_code, COUNT(*) AS n FROM fact_sfa_visit v "
        "WHERE " + clause + valid_sku + "GROUP BY 1, 2 ORDER BY n DESC", params)

    top_codes = [r["sku_code"] for r in rows[:6]]
    by_level: dict[str, dict] = {}
    for row in level_rows:
        slot = by_level.setdefault(row["level"], {"level": row["level"], "total": 0,
                                                 "items": {}})
        slot["total"] += row["n"]
        if row["sku_code"] in top_codes:
            slot["items"][row["sku_code"]] = row["n"]
    level_matrix = []
    for level in ("A", "B", "C", "D", "E", "未采集"):
        slot = by_level.get(level)
        if not slot:
            continue
        cells = [{"sku_code": code, "n": slot["items"].get(code, 0)} for code in top_codes]
        # total 必须是「本行各列之和」，否则表格看起来加了不对；
        # 该等级的真实反馈总数另存 level_total，差额即 TOP6 之外的 SKU 数（other）。
        row_sum = sum(c["n"] for c in cells)
        level_matrix.append({
            "level": level, "total": row_sum, "values": cells,
            "level_total": slot["total"], "other": max(0, slot["total"] - row_sum),
        })

    return {
        "rows": rows,
        "top": rows[:TOP_SKU],
        "coverage": {
            "visits": total_visits.get("visits") or 0,
            "with_sku": total_visits.get("with_sku") or 0,
            "pending": total_visits.get("pending") or 0,
            "not_collected": total_visits.get("not_collected") or 0,
            "coverage_rate": _ratio(total_visits.get("with_sku"), total_visits.get("visits")),
        },
        "level_matrix": level_matrix,
        "level_codes": top_codes,
    }


# ------------------------------------------------------------------ 价格
def _price(conn, f) -> dict:
    clause, params = board_filters.where_sfa_visit(f)
    rows = _rows(conn,
        "SELECT v.focus_sku_code AS sku_code, v.focus_product_name AS raw_name, "
        "v.focus_retail_price AS price, "
        "MAX(v.focus_sku_match_status) AS match_status, MAX(v.ka_system) AS ka_system, "
        "MAX(s.sku_name) AS sku_name, MAX(s.category) AS category "
        "FROM fact_sfa_visit v LEFT JOIN dim_sku s ON s.sku_code = v.focus_sku_code "
        "WHERE " + clause + " AND v.focus_retail_price IS NOT NULL "
        "GROUP BY v.visit_id", params)

    # 已匹配主数据的按 sku_code 归组；未匹配的按「原始商品名」归组并单独标注，
    # 不把不同商品混成一个「名称未匹配」大桶，也不反过来猜测它对应哪个 SKU。
    buckets: dict[str, dict] = {}
    for row in rows:
        code = (row["sku_code"] or "").strip()
        raw = (row["raw_name"] or "").strip()
        if code:
            key, name = code, (row["sku_name"] or raw or code)
            matched = True
        else:
            key = f"RAW::{raw or '名称缺失'}"
            name = f"{raw or '名称缺失'}（未匹配主数据）"
            matched = False
        slot = buckets.setdefault(key, {
            "key": key, "name": name, "category": row["category"], "prices": [],
            "ka": {}, "matched": matched, "raw_name": raw,
        })
        slot["prices"].append(float(row["price"]))
        ka = row["ka_system"] or "未采集"
        slot["ka"].setdefault(ka, []).append(float(row["price"]))

    items = []
    for slot in buckets.values():
        prices = slot["prices"]
        if not prices:
            continue
        p25 = _percentile(prices, 0.25)
        median = _percentile(prices, 0.5)
        p75 = _percentile(prices, 0.75)
        low = [p for p in prices if p25 is not None and p < p25 * 0.5]
        items.append({
            "key": slot["key"], "name": slot["name"], "category": slot["category"],
            "matched": slot["matched"], "raw_name": slot["raw_name"],
            "n": len(prices), "min": min(prices), "max": max(prices),
            "median": median, "p25": p25, "p75": p75,
            "mean": sum(prices) / len(prices),
            "spread": (max(prices) - min(prices)),
            "spread_rate": _ratio(max(prices) - min(prices), median),
            "abnormal": len(low), "abnormal_values": sorted(set(low))[:5],
            "ka": [{"ka": ka, "n": len(v), "median": _percentile(v, 0.5),
                    "min": min(v), "max": max(v)}
                   for ka, v in sorted(slot["ka"].items(), key=lambda kv: -len(kv[1]))[:5]],
        })
    items.sort(key=lambda x: -x["n"])

    total = len(rows)
    with_price = sum(1 for r in rows if r["price"] is not None)
    base_visits = _one(conn, "SELECT COUNT(*) AS visits FROM fact_sfa_visit v WHERE " + clause,
                       params) or {}
    return {
        "items": items,
        "top": items[:TOP_SKU],
        "coverage": {
            "rows": total, "with_price": with_price,
            # 采集率的分母是「有效拜访」，不是「有价格的行」，否则永远是 100%
            "visits": base_visits.get("visits") or 0,
            "coverage_rate": _ratio(with_price, base_visits.get("visits")),
            "sku_groups": len(items),
            "abnormal_total": sum(i["abnormal"] for i in items),
        },
    }


# ------------------------------------------------------------------ 陈列与执行
DISPLAY_FIELDS = (
    ("display_location", "陈列位置"),
    ("display_form", "陈列形式"),
    ("display_area", "陈列面积"),
    ("posm_display", "POSM 物料"),
    ("packaging_type", "包装陈列"),
    ("oos_offline_detail", "线下缺货反馈"),
    ("oos_online_complete_detail", "线上在售齐全"),
)


def _display(conn, f) -> dict:
    clause, params = board_filters.where_sfa_visit(f)
    total_row = _one(conn, "SELECT COUNT(*) AS visits FROM fact_sfa_visit v WHERE " + clause,
                     params) or {}
    total = total_row.get("visits") or 0

    fields = []
    for col, label in DISPLAY_FIELDS:
        row = _one(conn,
            f"SELECT SUM(CASE WHEN v.{col} IS NOT NULL AND TRIM(v.{col}) <> '' "
            f"THEN 1 ELSE 0 END) AS filled FROM fact_sfa_visit v WHERE " + clause, params) or {}
        filled = row.get("filled") or 0
        fields.append({"field": col, "label": label, "filled": filled,
                       "total": total, "rate": _ratio(filled, total)})

    def top_values(col: str, limit: int = 6) -> list[dict]:
        rows = _rows(conn,
            f"SELECT v.{col} AS label, COUNT(*) AS value FROM fact_sfa_visit v "
            f"WHERE {clause} AND v.{col} IS NOT NULL AND TRIM(v.{col}) <> '' "
            f"GROUP BY v.{col} ORDER BY value DESC LIMIT ?", [*params, limit])
        return rows

    status = _one(conn,
        "SELECT SUM(CASE WHEN v.visit_complete = '已完成' THEN 1 ELSE 0 END) AS completed, "
        "SUM(CASE WHEN v.visit_complete = '未完成' THEN 1 ELSE 0 END) AS incomplete, "
        "SUM(CASE WHEN v.visit_complete IS NULL OR TRIM(v.visit_complete) = '' "
        "THEN 1 ELSE 0 END) AS unknown "
        "FROM fact_sfa_visit v WHERE " + clause, params) or {}

    arrival = _one(conn,
        "SELECT SUM(CASE WHEN v.departure_time_status = 'PENDING' THEN 1 ELSE 0 END) AS bad_departure, "
        "COUNT(*) AS visits FROM fact_sfa_visit v WHERE " + clause, params) or {}

    return {
        "fields": fields,
        "location": top_values("display_location"),
        "form": top_values("display_form"),
        "posm": top_values("posm_display"),
        "status": status,
        "arrival": arrival,
        "photo_note": "底表无执行照片字段（图片列未在宽表中落地），故本页不展示照片，改为展示采集覆盖率。",
    }


# ------------------------------------------------------------------ SFA × POS
def _joint(conn, f) -> dict:
    """仅 27 家可关联门店；未匹配保留 PENDING；POS 无记录不等于零销售。"""
    # 门店映射（单表）
    mapping = _rows(conn,
        "SELECT sfa_store_id, pos_store_id, pos_link_status, is_v1_join_eligible "
        "FROM bridge_sfa_pos_store WHERE pos_link_status = 'POS_ALIGNED'", [])
    eligible = {m["sfa_store_id"] for m in mapping if m["is_v1_join_eligible"]}
    aligned = {m["sfa_store_id"] for m in mapping}

    clause, params = board_filters.where_sfa_visit(f)
    sfa_rows = _rows(conn,
        "SELECT v.store_id, v.focus_sku_code AS sku_code, v.focus_product_name AS raw_name, "
        "v.focus_retail_price AS price, v.visit_date, v.ka_system, v.store_level "
        "FROM fact_sfa_visit v WHERE " + clause +
        " AND NULLIF(TRIM(v.focus_sku_code), '') IS NOT NULL "
        "ORDER BY v.store_id, v.visit_date DESC", params)

    latest: dict[str, dict] = {}
    for row in sfa_rows:
        if row["store_id"] not in latest:
            latest[row["store_id"]] = row

    store_ids = sorted(latest)
    if not store_ids:
        return {"summary": {"stores": 0}, "rows": [], "pending": [],
                "pos_top": [], "note": "当前筛选下没有可关联门店的有效反馈"}

    placeholders = ",".join("?" * len(store_ids))
    month_clause = ("AND p.month IN (" + ",".join("?" * len(f.months)) + ")"
                    if f.months else
                    "AND p.month BETWEEN ? AND ?")
    pos_params = [config.FIXED_YEAR_INT, *store_ids]
    pos_params.extend(f.months or [config.COMPARABLE_FIRST_MONTH, config.COMPARABLE_LAST_MONTH])
    pos_rows = _rows(conn,
        "SELECT p.store_id, p.sku_code, MAX(p.sku_name) AS sku_name, "
        "SUM(p.sales_amount) AS amount, SUM(p.sales_boxes) AS boxes "
        "FROM fact_pos_sales p WHERE p.year = ? AND p.store_id IN (" + placeholders + ") "
        "AND p.region_scope_status = 'IN_SCOPE' " + month_clause +
        " GROUP BY p.store_id, p.sku_code", pos_params)

    pos_by_store: dict[str, list[dict]] = {}
    for row in pos_rows:
        pos_by_store.setdefault(row["store_id"], []).append(row)
    for rows in pos_by_store.values():
        rows.sort(key=lambda r: -(r["amount"] or 0))

    out_rows, pending = [], []
    for store_id in store_ids:
        sfa = latest[store_id]
        if store_id not in aligned:
            pending.append({"store_id": store_id, "reason": "SFA_ONLY：无 POS 对齐门店，保留 PENDING"})
            continue
        if store_id not in eligible:
            pending.append({"store_id": store_id,
                            "reason": "已对齐但不满足 V1 join 条件（门店口径未确认），不强行关联"})
            continue
        plist = pos_by_store.get(store_id, [])
        if not plist:
            out_rows.append({
                "store_id": store_id, "ka_system": sfa["ka_system"],
                "sfa_sku": sfa["sku_code"], "sfa_name": sfa["raw_name"],
                "sfa_price": sfa["price"], "visit_date": sfa["visit_date"],
                "pos_top_sku": None, "pos_top_name": None, "pos_top_amount": None,
                "sfa_sku_amount": None, "sfa_sku_rank": None,
                "pos_sku_count": 0, "consistent": None,
                "status": "POS_NO_RECORD",
                "note": "POS 在该期间无记录，不等于零销售，标记待核实",
            })
            continue
        total_rows = len(plist)
        top = plist[0]
        rank = None
        amount = None
        for idx, row in enumerate(plist, 1):
            if row["sku_code"] == sfa["sku_code"]:
                rank, amount = idx, row["amount"]
                break
        out_rows.append({
            "store_id": store_id, "ka_system": sfa["ka_system"],
            "sfa_sku": sfa["sku_code"], "sfa_name": sfa["raw_name"],
            "sfa_price": sfa["price"], "visit_date": sfa["visit_date"],
            "pos_top_sku": top["sku_code"], "pos_top_name": top["sku_name"],
            "pos_top_amount": top["amount"],
            "sfa_sku_amount": amount, "sfa_sku_rank": rank,
            "pos_sku_count": total_rows,
            "consistent": (rank == 1),
            "status": "SAME_TOP" if rank == 1 else ("SOLD_NOT_TOP" if rank else "NOT_SOLD"),
            "note": ("现场反馈 SKU 即为 POS 销量第一" if rank == 1 else
                     (f"现场反馈 SKU 在 POS 排第 {rank}/{total_rows}" if rank else
                      "POS 无该 SKU 记录，待核实（不等于未销售）")),
        })

    def count(status):
        return len([r for r in out_rows if r["status"] == status])

    # 汇总：SFA 热点 SKU 与 POS 销量 TOP 的对照（按门店数）
    pos_top_counter: dict[str, dict] = {}
    for rows in pos_by_store.values():
        top = rows[0]
        slot = pos_top_counter.setdefault(top["sku_code"], {
            "sku_code": top["sku_code"], "sku_name": top["sku_name"],
            "stores": 0, "amount": 0})
        slot["stores"] += 1
        slot["amount"] += top["amount"] or 0
    pos_top = sorted(pos_top_counter.values(), key=lambda x: -x["stores"])[:8]

    return {
        "summary": {
            "sfa_feedback_stores": len(store_ids),
            "aligned_stores": len([s for s in store_ids if s in aligned]),
            "join_eligible_stores": len([s for s in store_ids if s in eligible]),
            "evaluated_stores": len(out_rows),
            "same_top": count("SAME_TOP"),
            "sold_not_top": count("SOLD_NOT_TOP"),
            "not_sold": count("NOT_SOLD"),
            "pos_no_record": count("POS_NO_RECORD"),
            "pending_stores": len(pending),
            "consistent_rate": _ratio(count("SAME_TOP"), len(out_rows)),
        },
        "rows": out_rows,
        "pending": pending,
        "pos_top": pos_top,
        "note": ("联合分析只在 27 家可关联门店内进行；未匹配门店保留 PENDING，"
                 "POS 无该 SKU 记录标记待核实，不解释为零销售。"),
    }


# ------------------------------------------------------------------ 主查询
def analysis(conn: sqlite3.Connection, f: board_filters.BoardFilters) -> dict:
    stores = _stores(conn, f)
    roster = _one(conn,
        "SELECT COUNT(*) AS n FROM dim_store WHERE employee_key > 0 AND region_name_std IN ("
        + ",".join("?" * len(f.regions)) + ")", list(f.regions)) or {}

    ka_dist = _distribution(stores, "ka_system")
    level_dist = _distribution(stores, "store_level",
                               order=["A", "B", "C", "D", "E", "未采集"])
    channel_dist = _distribution(stores, "store_type")
    region_dist = _distribution(stores, "region_name_std", order=list(f.regions))
    md_dist = _distribution(stores, "market_department")

    hot = _hot_sku(conn, f)
    price = _price(conn, f)
    display = _display(conn, f)
    joint = _joint(conn, f)

    level_known = len([s for s in stores if s["store_level"] not in (None, "", "未采集")])
    hot_clause, hot_params = board_filters.where_sfa_visit(f)
    hot_sku_stores = _one(conn,
        "SELECT COUNT(DISTINCT v.store_id) AS n FROM fact_sfa_visit v WHERE "
        + hot_clause + " AND NULLIF(TRIM(v.focus_sku_code), '') IS NOT NULL",
        hot_params) or {}

    kpi = {
        "store_total": len(stores),
        "store_roster": roster.get("n") or 0,
        "ka_count": len([k for k in ka_dist if k["label"] not in ("未采集",)]),
        "level_known": level_known,
        "level_rate": _ratio(level_known, len(stores)),
        "hot_sku_stores": hot_sku_stores.get("n") or 0,
        "price_records": price["coverage"]["with_price"],
        "price_coverage": price["coverage"]["coverage_rate"],
        "display_filled": display["fields"][0]["filled"],
        "display_rate": display["fields"][0]["rate"],
        "joint_evaluated": joint["summary"].get("evaluated_stores", 0),
        "joint_consistent": joint["summary"].get("same_top", 0),
        "visit_total": hot["coverage"]["visits"],
    }

    data = {
        "kpi": kpi,
        "stores": stores,
        "ka_dist": ka_dist[:TOP_KA],
        "ka_other": max(0, len(ka_dist) - TOP_KA),
        "level_dist": level_dist,
        "channel_dist": channel_dist,
        "region_dist": region_dist,
        "md_dist": md_dist[:TOP_MD],
        "hot": hot,
        "price": price,
        "display": display,
        "joint": joint,
    }
    window = _one(conn,
        "SELECT MIN(v.visit_date) AS first_date, MAX(v.visit_date) AS last_date, "
        "COUNT(DISTINCT v.visit_date) AS days FROM fact_sfa_visit v WHERE " + hot_clause,
        hot_params)
    return _envelope(f, data, extra={"coverage_window": {
        "first_date": window.get("first_date"), "last_date": window.get("last_date"),
        "days": window.get("days") or 0,
    }})


# ------------------------------------------------------------------ 明细
DETAIL_COLUMNS = ("store_id", "store_name", "ka_system", "region_name_std",
                  "market_department", "store_level", "store_type", "visit_count",
                  "last_visit_date", "hot_sku_name", "hot_price", "display_location",
                  "pos_link_status")


def detail(conn: sqlite3.Connection, f: board_filters.BoardFilters) -> dict:
    stores = _stores(conn, f)
    ids = [s["store_id"] for s in stores]
    if not ids:
        return _envelope(f, {"rows": [], "columns": list(DETAIL_COLUMNS), "total": 0,
                             "page": 1, "limit": f.limit, "pages": 1})

    clause, params = board_filters.where_sfa_visit(f)
    placeholders = ",".join("?" * len(ids))
    agg = _rows(conn,
        "SELECT v.store_id, COUNT(*) AS visit_count, MAX(v.visit_date) AS last_visit_date, "
        "MAX(v.focus_product_name) AS hot_sku_name, MAX(v.focus_retail_price) AS hot_price, "
        "MAX(v.display_location) AS display_location "
        "FROM fact_sfa_visit v WHERE " + clause + " AND v.store_id IN (" + placeholders + ") "
        "GROUP BY v.store_id", [*params, *ids])
    by_store = {r["store_id"]: r for r in agg}

    rows = []
    for store in stores:
        extra = by_store.get(store["store_id"], {})
        rows.append({**store, **extra})

    if f.q:
        needle = f.q.lower()
        fields = ("store_id", "store_name", "ka_system", "market_department",
                  "store_level", "hot_sku_name")
        rows = [r for r in rows
                if any(needle in str(r.get(k) or "").lower() for k in fields)]
    if f.sort:
        def key(row):
            value = row.get(f.sort)
            if isinstance(value, str):
                return (value is None, value)
            return (value is None, value if value is not None else 0)
        rows = sorted(rows, key=key, reverse=(f.order == "desc"))
    else:
        rows = sorted(rows, key=lambda r: (-(r.get("visit_count") or 0), r["store_id"]))

    total = len(rows)
    start = (f.page - 1) * f.limit
    page_rows = rows[start:start + f.limit]
    out = _envelope(f, {
        "rows": page_rows, "columns": list(DETAIL_COLUMNS),
        "total": total, "page": f.page, "limit": f.limit,
        "pages": max(1, (total + f.limit - 1) // f.limit),
    })
    out["meta"]["sort"] = {"column": f.sort, "order": f.order, "q": f.q}
    return out


def meta_options(conn: sqlite3.Connection, f: board_filters.BoardFilters) -> dict:
    ka_rows = _rows(conn,
        "SELECT DISTINCT ka_system FROM dim_store WHERE employee_key>0 "
        "AND region_name_std IN (" + ",".join("?" * len(f.regions)) + ") "
        "AND ka_system IS NOT NULL AND TRIM(ka_system) <> '' ORDER BY ka_system",
        list(f.regions))
    md_rows = _rows(conn,
        "SELECT DISTINCT market_department FROM dim_store WHERE employee_key>0 "
        "AND region_name_std IN (" + ",".join("?" * len(f.regions)) + ") "
        "AND market_department IS NOT NULL AND TRIM(market_department) <> '' "
        "ORDER BY market_department", list(f.regions))
    emp_rows = _rows(conn,
        "SELECT manager_virtual_id AS employee_id, store_manager_name AS employee_name, "
        "REPLACE(war_zone,'战区','') AS region_name, COUNT(*) AS managed_store_count "
        "FROM dim_store WHERE employee_key>0 AND region_name_std IN ("
        + ",".join("?" * len(f.regions)) + ") GROUP BY manager_virtual_id "
        "ORDER BY manager_virtual_id", list(f.regions))
    sku_rows = _rows(conn,
        "SELECT sku_code, sku_name, category FROM dim_sku "
        "WHERE sku_code IN (SELECT DISTINCT focus_sku_code FROM fact_sfa_visit "
        "WHERE focus_sku_code IS NOT NULL) ORDER BY sku_code", [])
    month_rows = _rows(conn,
        "SELECT DISTINCT CAST(substr(v.visit_date,6,2) AS INTEGER) AS month "
        "FROM fact_sfa_visit v WHERE v.visit_date IS NOT NULL "
        "AND v.region_name_std IN (" + ",".join("?" * len(f.regions)) + ") ORDER BY month",
        list(f.regions))
    return {
        "ok": True,
        "data": {
            "regions": list(f.regions),
            "ka_systems": [r["ka_system"] for r in ka_rows],
            "market_departments": [r["market_department"] for r in md_rows],
            "employees": emp_rows,
            "skus": sku_rows,
            "months": [r["month"] for r in month_rows if r["month"]],
        },
        "meta": {"board": BOARD, "generated_at":
                 datetime.now().astimezone().isoformat(timespec="seconds")},
    }
