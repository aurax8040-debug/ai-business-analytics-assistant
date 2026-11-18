"""批发经营分析 —— 查询层（阶段三）。

严格执行已冻结的 P07 库存口径
----------------------------
1. ``fact_wholesale_inventory_daily``（源 ``wholesale_inventory_daily_v2.csv``）是唯一库存趋势数据源；
2. 有效组合 2,946 个；1,014 个不适用组合在 ETL 层已排除，**不填零**；
3. ``NOT_YET_ACTIVE``（启用前零库存）与 ``REAL_ZERO``（经营后真实归零）分列统计；
4. ``ending_qty`` 是**时点**指标，跨日期一律不 SUM：日趋势按「同一天跨组合求和」，
   月末库存取「该月最后一个自然日的真实记录」；
5. 不补零、不前向填充、不使用 CARRIED_FORWARD（ETL 已处理，本层不重建）。

金额边界
--------
- 批发采购金额 = 星禾 ERP Sell-in 给批发客户的出货（单据号与 ERP 同源），
  与「全国经营驾驶舱」的 ERP 全渠道出货不是同一口径，**不相加**；
- 批发出库金额 = 批发客户卖给自己下游客户（独立于 POS），**不与 POS 金额相加**；
- 期末库存金额 = 结存数量 × 单位成本价（已逐行核对与 ``inventory_amount`` 完全一致）。

单据口径：单据数一律 ``COUNT(DISTINCT 单据号)``，不把 SKU 行数当单据数。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from . import board_filters, config

BOARD = "wholesale"
TOP_CUSTOMER = 6
TOP_STALL = 12
TOP_SKU = 10

NOTES = (
    "批发采购 = 星禾 ERP 向批发客户出货（单据同源），与全渠道 ERP 出货不是同一口径，不相加。",
    "批发出库 = 批发客户对下游客户的销售，独立于 POS；三类金额永不相加。",
    "库存唯一数据源为日库存 V2；ending_qty 为时点指标，跨日期不求和。",
    "月末库存取该月最后一个自然日的真实记录，不补零、不前向填充。",
    "NOT_YET_ACTIVE（启用前零库存）与 REAL_ZERO（经营后真实归零）分开统计，不混为一类。",
    "单据数按 COUNT(DISTINCT 单据号) 统计，不把 SKU 行数当单据数。",
)


def _rows(conn, sql, params) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _one(conn, sql, params) -> dict:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else {}


def _ratio(num, den):
    if den in (None, 0) or num is None:
        return None
    return 100.0 * num / den


def _envelope(f: board_filters.BoardFilters, data, extra: dict | None = None) -> dict:
    meta = {
        "board": BOARD,
        "filters": f.as_dict(),
        "region_label": f.region_label,
        "period": f"{config.FIXED_YEAR} · {f.month_label}",
        "year": config.FIXED_YEAR,
        "year_int": config.FIXED_YEAR_INT,
        "source_views": ["fact_wholesale_purchase", "fact_wholesale_sales",
                         "fact_wholesale_inventory_daily", "dim_wholesale_stall",
                         "dim_customer"],
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "notes": list(NOTES),
    }
    if extra:
        meta.update(extra)
    return {"ok": True, "data": data, "meta": meta}


# ------------------------------------------------------------------ 期间与库存时点
def _period(conn, f) -> dict:
    """当前筛选下有业务记录的月份区间；库存时点取区间最后一天。"""
    clause, params = board_filters.where_wholesale(f, "t", "t.month")
    row = _one(conn,
        "SELECT MIN(t.month) AS m0, MAX(t.month) AS m1 FROM fact_wholesale_sales t "
        "WHERE " + clause, params) or {}
    months = list(f.months) or list(range(config.COMPARABLE_FIRST_MONTH,
                                          config.COMPARABLE_LAST_MONTH + 1))
    return {"months": months, "first_month": row.get("m0"), "last_month": row.get("m1")}


def _date_key(date_str: str) -> int:
    """snapshot_date → date_key（与 ETL 主键同值），用于单日快照的点查。"""
    return int(date_str.replace("-", ""))


def _inventory_clause(f: board_filters.BoardFilters,
                      point_date: str | None = None) -> tuple[str, list]:
    """库存事实没有 month/year 列，年份与月份一律由 snapshot_date 派生。

    P05 冻结口径 = 自然年 Y26，所以这里硬性限定 snapshot_date 前 4 位为 2026，
    否则会混入 2025 年的 669 天快照（库存表是跨年滚动的）。

    point_date 非空时改用 date_key 等值条件命中主键索引（约 0.01s），
    而不是对 89 万行再做一次全表扫描——口径完全一致，只是取数路径不同。
    """
    clauses = ["i.region_scope_status = 'IN_SCOPE'",
               "i.region_name_std IN (" + ",".join("?" * len(f.regions)) + ")"]
    params: list = [*f.regions]
    if point_date:
        clauses.insert(0, "i.date_key = ?")
        params.insert(0, _date_key(point_date))
    else:
        clauses.insert(0, "substr(i.snapshot_date, 1, 4) = ?")
        params.insert(0, str(config.FIXED_YEAR_INT))
    if f.months:
        clauses.append("CAST(substr(i.snapshot_date, 6, 2) AS INTEGER) IN ("
                       + ",".join("?" * len(f.months)) + ")")
        params.extend(f.months)
    if f.customer:
        clauses.append("i.customer_key = (SELECT customer_key FROM dim_customer "
                       "WHERE customer_id = ?)")
        params.append(f.customer)
    if f.stall:
        clauses.append("i.stall_key = (SELECT stall_key FROM dim_wholesale_stall "
                       "WHERE stall_id = ?)")
        params.append(f.stall)
    if f.sku:
        clauses.append("i.sku_key = (SELECT sku_key FROM dim_sku WHERE sku_code = ?)")
        params.append(f.sku)
    if f.category:
        clauses.append("i.sku_key IN (SELECT sku_key FROM dim_sku WHERE category = ?)")
        params.append(f.category)
    return " AND ".join(clauses), params


def _inventory_series(conn, f) -> list[dict]:
    """日库存序列：同一天跨组合求和（时点），绝不跨日期累加。

    一次扫描同时产出 KPI 锚定日、月末库存与趋势所需的全部字段，
    避免对 89 万行库存明细重复全表扫描（原实现 10 次扫描约 13s）。
    """
    clause, params = _inventory_clause(f)
    return _rows(conn,
        "SELECT i.snapshot_date AS date, SUM(i.ending_qty) AS ending_qty, "
        "SUM(i.inventory_amount) AS inventory_amount, SUM(i.inventory_te) AS inventory_te, "
        "SUM(CASE WHEN i.ending_qty > 0 THEN 1 ELSE 0 END) AS positive_combos, "
        "SUM(CASE WHEN i.inventory_zero_type = 'REAL_ZERO' THEN 1 ELSE 0 END) AS real_zero, "
        "SUM(CASE WHEN i.inventory_zero_type = 'NOT_YET_ACTIVE' THEN 1 ELSE 0 END) AS not_active, "
        "COUNT(*) AS combinations "
        "FROM fact_wholesale_inventory_daily i WHERE " + clause +
        " GROUP BY i.snapshot_date ORDER BY i.snapshot_date", params)


def _anchor_from_series(series: list[dict]) -> dict:
    """观察时点 = 期间最后一个有真实记录的自然日（不插值、不前向填充）。"""
    if not series:
        return {"anchor_date": None, "first_date": None, "summary": {}}
    last = series[-1]
    summary = {
        "combinations": last["combinations"],
        "ending_qty": last["ending_qty"],
        "inventory_amount": last["inventory_amount"],
        "inventory_te": last["inventory_te"],
        "positive": last["positive_combos"],
        "real_zero": last["real_zero"],
        "not_active": last["not_active"],
    }
    return {"anchor_date": last["date"], "first_date": series[0]["date"],
            "summary": summary}


def _month_end_from_series(series: list[dict]) -> list[dict]:
    """月末库存：每月取该月最后一个自然日的真实快照，不插值、不前向填充。"""
    last: dict[str, dict] = {}
    for row in series:
        label = row["date"][:7]
        if label not in last or row["date"] > last[label]["date"]:
            last[label] = row
    out = []
    for label in sorted(last):
        row = last[label]
        out.append({
            "month_label": label,
            "anchor_date": row["date"],
            "ending_qty": row["ending_qty"],
            "inventory_amount": row["inventory_amount"],
            "real_zero": row["real_zero"] or 0,
            "not_active": row["not_active"] or 0,
        })
    return out


def _stock_by(conn, f, dim: str, anchor: str | None) -> list[dict]:
    """库存分布：按档口 / SKU（时点，锚定最后一天）。"""
    if not anchor:
        return []
    clause, params = _inventory_clause(f, point_date=anchor)
    col = "i.stall_key" if dim == "stall" else "i.sku_key"
    name = "stall_id" if dim == "stall" else "sku_code"
    table = "dim_wholesale_stall" if dim == "stall" else "dim_sku"
    key = "stall_key" if dim == "stall" else "sku_key"
    label = "stall_name" if dim == "stall" else "sku_name"
    return _rows(conn,
        f"SELECT d.{name} AS name, d.{label} AS label, "
        "SUM(i.ending_qty) AS ending_qty, SUM(i.inventory_amount) AS inventory_amount "
        f"FROM fact_wholesale_inventory_daily i JOIN {table} d ON d.{key} = {col} "
        "WHERE " + clause +
        f" GROUP BY d.{name}, d.{label} ORDER BY inventory_amount DESC", params)


def _zero_structure(conn, f) -> dict:
    """归零结构：期间内「曾出现」的两类零库存组合，与锚定日仍有库存的组合。

    P07 要求 NOT_YET_ACTIVE（启用前）与 REAL_ZERO（经营后真实归零）严格区分，
    因此这里按组合去重分别计数，不做任何合并。

    写法上先按（档口×SKU）分组求每组的类型标记，再在外层计数：
    与 COUNT(DISTINCT 拼接键) 结果完全一致，但对 89 万行少一次去重运算。
    """
    clause, params = _inventory_clause(f)
    row = _one(conn,
        "SELECT COUNT(*) AS combinations, "
        "SUM(rz) AS real_zero_combos, SUM(na) AS not_active_combos "
        "FROM (SELECT i.stall_key, i.sku_key, "
        "MAX(CASE WHEN i.inventory_zero_type = 'REAL_ZERO' THEN 1 ELSE 0 END) AS rz, "
        "MAX(CASE WHEN i.inventory_zero_type = 'NOT_YET_ACTIVE' THEN 1 ELSE 0 END) AS na "
        "FROM fact_wholesale_inventory_daily i WHERE " + clause +
        " GROUP BY i.stall_key, i.sku_key)", params) or {}
    return {k: int(v or 0) for k, v in row.items()}


def _combination_universe(conn, f) -> int:
    """理论组合数 = 在范围内档口数 × 在范围内 SKU 数。

    库存表只落地「有业务发生」的组合，其余为 N/A；页面上要说明「排除了多少」，
    必须来自同一口径的实时计算，而不是写死一个常量。
    """
    row = _one(conn,
        "SELECT (SELECT COUNT(*) FROM dim_wholesale_stall WHERE region_name_std IN ("
        + ",".join("?" * len(f.regions)) + ")) * "
        "(SELECT COUNT(*) FROM dim_sku WHERE sku_key IN ("
        "SELECT sku_key FROM fact_wholesale_sales WHERE year = ?)) AS theoretical",
        [*f.regions, config.FIXED_YEAR_INT]) or {}
    return int(row.get("theoretical") or 0)


# ------------------------------------------------------------------ 明细聚合
def _flow(conn, f, kind: str, group: str) -> list[dict]:
    """采购 / 出库的统一聚合。kind = purchase | sales。"""
    if kind == "purchase":
        table, amount, qty, te, doc, gp = (
            "fact_wholesale_purchase", "purchase_amount", "purchase_qty",
            "purchase_te", "purchase_id", None)
    else:
        table, amount, qty, te, doc, gp = (
            "fact_wholesale_sales", "sales_amount", "sales_qty",
            "sales_te", "outbound_id", "gross_profit")
    clause, params = board_filters.where_wholesale(f, "t", "t.month")

    group_sql = {
        "month": "t.month",
        "customer": "t.customer_id",
        "stall": "t.stall_id",
        "sku": "t.sku_code",
        "category": "t.category",
    }.get(group, "t.month")
    label_sql = {
        "customer": "MAX(t.customer_id)",
        "stall": "MAX(t.stall_id)",
        "sku": "MAX(t.sku_name)",
    }.get(group, group_sql)

    gp_sql = f"SUM(t.{gp})" if gp else "NULL"
    sql = (f"SELECT {group_sql} AS name, {label_sql} AS label, "
           f"SUM(t.{amount}) AS amount, SUM(t.{qty}) AS qty, SUM(t.{te}) AS te_qty, "
           f"COUNT(DISTINCT t.{doc}) AS doc_count, COUNT(*) AS row_count, "
           f"{gp_sql} AS gross_profit "
           f"FROM {table} t WHERE " + clause + f" GROUP BY {group_sql}")
    return _rows(conn, sql, params)


def _customer_names(conn) -> dict:
    return {r["customer_id"]: r["customer_name"]
            for r in _rows(conn, "SELECT customer_id, customer_name FROM dim_customer", [])}


def _stall_names(conn) -> dict:
    return {r["stall_id"]: r["stall_name"]
            for r in _rows(conn, "SELECT stall_id, stall_name FROM dim_wholesale_stall", [])}


# ------------------------------------------------------------------ 主查询
def analysis(conn: sqlite3.Connection, f: board_filters.BoardFilters) -> dict:
    purchase = _flow(conn, f, "purchase", "month")
    sales = _flow(conn, f, "sales", "month")
    by_customer_p = _flow(conn, f, "purchase", "customer")
    by_customer_s = _flow(conn, f, "sales", "customer")
    by_stall_p = _flow(conn, f, "purchase", "stall")
    by_stall_s = _flow(conn, f, "sales", "stall")
    by_sku_p = _flow(conn, f, "purchase", "sku")
    by_sku_s = _flow(conn, f, "sales", "sku")
    series = _inventory_series(conn, f)
    inv = _anchor_from_series(series)
    month_end = _month_end_from_series(series)
    stock_stall = _stock_by(conn, f, "stall", inv["anchor_date"])
    stock_sku = _stock_by(conn, f, "sku", inv["anchor_date"])

    cust_names = _customer_names(conn)
    stall_names = _stall_names(conn)

    def total(rows, key):
        return sum((r.get(key) or 0) for r in rows)

    purchase_total = {
        "amount": total(purchase, "amount"), "qty": total(purchase, "qty"),
        "te": total(purchase, "te_qty"), "docs": total(purchase, "doc_count"),
        "rows": total(purchase, "row_count"),
    }
    sales_total = {
        "amount": total(sales, "amount"), "qty": total(sales, "qty"),
        "te": total(sales, "te_qty"), "docs": total(sales, "doc_count"),
        "rows": total(sales, "row_count"), "gross_profit": total(sales, "gross_profit"),
    }
    sales_total["gross_margin"] = _ratio(sales_total["gross_profit"], sales_total["amount"])

    inv_sum = inv["summary"] or {}
    kpi = {
        "purchase_amount": purchase_total["amount"],
        "purchase_qty": purchase_total["qty"],
        "purchase_docs": purchase_total["docs"],
        "sales_amount": sales_total["amount"],
        "sales_qty": sales_total["qty"],
        "sales_docs": sales_total["docs"],
        "gross_profit": sales_total["gross_profit"],
        "gross_margin": sales_total["gross_margin"],
        "ending_qty": inv_sum.get("ending_qty"),
        "inventory_amount": inv_sum.get("inventory_amount"),
        "inventory_te": inv_sum.get("inventory_te"),
        "combinations": inv_sum.get("combinations"),
        "real_zero": inv_sum.get("real_zero"),
        "not_yet_active": inv_sum.get("not_active"),
        "positive_combos": inv_sum.get("positive"),
        "anchor_date": inv["anchor_date"],
        "first_inventory_date": inv["first_date"],
        "purchase_minus_sales_qty": (purchase_total["qty"] or 0) - (sales_total["qty"] or 0),
    }

    # 客户：采购 + 出库合并成一张表（按客户维度），贡献度按各自总额
    cust = {}
    for row in by_customer_p:
        slot = cust.setdefault(row["name"], {"customer_id": row["name"],
                                             "customer_name": cust_names.get(row["name"],
                                                                             row["name"]),
                                             "purchase_amount": 0, "purchase_qty": 0,
                                             "purchase_docs": 0,
                                             "sales_amount": 0, "sales_qty": 0,
                                             "sales_docs": 0, "gross_profit": 0})
        slot["purchase_amount"] += row["amount"] or 0
        slot["purchase_qty"] += row["qty"] or 0
        slot["purchase_docs"] += row["doc_count"] or 0
    for row in by_customer_s:
        slot = cust.setdefault(row["name"], {"customer_id": row["name"],
                                             "customer_name": cust_names.get(row["name"],
                                                                             row["name"]),
                                             "purchase_amount": 0, "purchase_qty": 0,
                                             "purchase_docs": 0,
                                             "sales_amount": 0, "sales_qty": 0,
                                             "sales_docs": 0, "gross_profit": 0})
        slot["sales_amount"] += row["amount"] or 0
        slot["sales_qty"] += row["qty"] or 0
        slot["sales_docs"] += row["doc_count"] or 0
        slot["gross_profit"] += row["gross_profit"] or 0
    customers = sorted(cust.values(), key=lambda x: -(x["purchase_amount"] or 0))
    for row in customers:
        row["purchase_share"] = _ratio(row["purchase_amount"], purchase_total["amount"])
        row["sales_share"] = _ratio(row["sales_amount"], sales_total["amount"])
        row["gross_margin"] = _ratio(row["gross_profit"], row["sales_amount"])
        row["gap_amount"] = (row["purchase_amount"] or 0) - (row["sales_amount"] or 0)

    # 档口
    stall = {}
    for row in by_stall_p:
        slot = stall.setdefault(row["name"], {"stall_id": row["name"],
                                              "stall_name": stall_names.get(row["name"],
                                                                            row["name"]),
                                              "purchase_amount": 0, "purchase_qty": 0,
                                              "sales_amount": 0, "sales_qty": 0,
                                              "gross_profit": 0})
        slot["purchase_amount"] += row["amount"] or 0
        slot["purchase_qty"] += row["qty"] or 0
    for row in by_stall_s:
        slot = stall.setdefault(row["name"], {"stall_id": row["name"],
                                              "stall_name": stall_names.get(row["name"],
                                                                            row["name"]),
                                              "purchase_amount": 0, "purchase_qty": 0,
                                              "sales_amount": 0, "sales_qty": 0,
                                              "gross_profit": 0})
        slot["sales_amount"] += row["amount"] or 0
        slot["sales_qty"] += row["qty"] or 0
        slot["gross_profit"] += row["gross_profit"] or 0
    stock_by_stall = {r["name"]: r for r in stock_stall}
    for row in stall.values():
        stock = stock_by_stall.get(row["stall_id"], {})
        row["ending_qty"] = stock.get("ending_qty")
        row["inventory_amount"] = stock.get("inventory_amount")
    stalls = sorted(stall.values(), key=lambda x: -(x["purchase_amount"] or 0))

    # SKU
    sku = {}
    for row in by_sku_p:
        slot = sku.setdefault(row["name"], {"sku_code": row["name"],
                                            "sku_name": row["label"] or row["name"],
                                            "purchase_amount": 0, "purchase_qty": 0,
                                            "sales_amount": 0, "sales_qty": 0,
                                            "gross_profit": 0})
        slot["purchase_amount"] += row["amount"] or 0
        slot["purchase_qty"] += row["qty"] or 0
    for row in by_sku_s:
        slot = sku.setdefault(row["name"], {"sku_code": row["name"],
                                            "sku_name": row["label"] or row["name"],
                                            "purchase_amount": 0, "purchase_qty": 0,
                                            "sales_amount": 0, "sales_qty": 0,
                                            "gross_profit": 0})
        slot["sales_amount"] += row["amount"] or 0
        slot["sales_qty"] += row["qty"] or 0
        slot["gross_profit"] += row["gross_profit"] or 0
    stock_by_sku = {r["name"]: r for r in stock_sku}
    for row in sku.values():
        stock = stock_by_sku.get(row["sku_code"], {})
        row["ending_qty"] = stock.get("ending_qty")
        row["inventory_amount"] = stock.get("inventory_amount")
    skus = sorted(sku.values(), key=lambda x: -(x["sales_amount"] or 0))
    for row in skus:
        row["gap_qty"] = (row["purchase_qty"] or 0) - (row["sales_qty"] or 0)
        row["gross_margin"] = _ratio(row["gross_profit"], row["sales_amount"])

    # 趋势：采购 / 出库按月并列（金额与数量两套）
    trend_map: dict[int, dict] = {}
    for row in purchase:
        slot = trend_map.setdefault(int(row["name"]), {"month": int(row["name"])})
        slot["purchase_amount"] = row["amount"]
        slot["purchase_qty"] = row["qty"]
        slot["purchase_docs"] = row["doc_count"]
    for row in sales:
        slot = trend_map.setdefault(int(row["name"]), {"month": int(row["name"])})
        slot["sales_amount"] = row["amount"]
        slot["sales_qty"] = row["qty"]
        slot["sales_docs"] = row["doc_count"]
    trend = []
    for month in sorted(trend_map):
        slot = trend_map[month]
        trend.append({
            "month": month, "label": f"{month} 月",
            "purchase_amount": slot.get("purchase_amount"),
            "purchase_qty": slot.get("purchase_qty"),
            "sales_amount": slot.get("sales_amount"),
            "sales_qty": slot.get("sales_qty"),
            "purchase_docs": slot.get("purchase_docs"),
            "sales_docs": slot.get("sales_docs"),
        })

    data = {
        "kpi": kpi,
        "trend": trend,
        "customers": customers,
        "stalls": stalls,
        "skus": skus,
        "inventory": {
            "anchor_date": inv["anchor_date"],
            "first_date": inv["first_date"],
            "months": [{"label": r["month_label"], "date": r["anchor_date"],
                        "ending_qty": r["ending_qty"],
                        "inventory_amount": r["inventory_amount"],
                        "real_zero": r["real_zero"] or 0,
                        "not_yet_active": r["not_active"] or 0}
                       for r in month_end],
            "series": series,
            "zero_breakdown": {
                **_zero_structure(conn, f),
                "anchor_positive": kpi["positive_combos"] or 0,
                "anchor_combinations": kpi["combinations"] or 0,
                # 理论组合 - 有效组合 = 标记 N/A 的组合，由同比径实时算出，不写死
                "theoretical_combinations": _combination_universe(conn, f),
                "excluded_combinations": max(
                    0, _combination_universe(conn, f) - (kpi["combinations"] or 0)),
            },
            "by_stall": stock_stall[:TOP_STALL],
            "by_sku": stock_sku[:TOP_SKU],
        },
    }
    return _envelope(f, data, extra={
        "period": f"{config.FIXED_YEAR} · {f.month_label}",
        "inventory_anchor": inv["anchor_date"],
    })


# ------------------------------------------------------------------ 明细
DETAIL_COLUMNS = {
    "purchase": ("full_date", "purchase_id", "customer_id", "stall_id", "sku_code",
                 "sku_name", "category", "purchase_qty", "purchase_unit_price",
                 "purchase_amount", "purchase_te", "region_name_std"),
    "sales": ("full_date", "outbound_id", "customer_id", "stall_id",
              "downstream_customer_name", "sku_code", "sku_name", "sales_qty",
              "sales_unit_price", "sales_amount", "cost_amount", "gross_profit",
              "region_name_std"),
    "inventory": ("snapshot_date", "stall_key", "sku_key", "opening_qty", "inbound_qty",
                  "outbound_qty", "adjustment_qty", "ending_qty", "inventory_amount",
                  "inventory_zero_type"),
}

_FLOW_COLUMNS = {
    "purchase": {
        "full_date": "t.full_date", "purchase_id": "t.purchase_id",
        "customer_id": "t.customer_id", "stall_id": "t.stall_id",
        "sku_code": "t.sku_code", "sku_name": "t.sku_name", "category": "t.category",
        "purchase_qty": "t.purchase_qty", "purchase_unit_price": "t.purchase_unit_price",
        "purchase_amount": "t.purchase_amount", "purchase_te": "t.purchase_te",
        "region_name_std": "t.region_name_std",
    },
    "sales": {
        "full_date": "t.full_date", "outbound_id": "t.outbound_id",
        "customer_id": "t.customer_id", "stall_id": "t.stall_id",
        "downstream_customer_name": "t.downstream_customer_name",
        "sku_code": "t.sku_code", "sku_name": "t.sku_name",
        "sales_qty": "t.sales_qty", "sales_unit_price": "t.sales_unit_price",
        "sales_amount": "t.sales_amount", "cost_amount": "t.cost_amount",
        "gross_profit": "t.gross_profit", "region_name_std": "t.region_name_std",
    },
    "inventory": {
        "snapshot_date": "i.snapshot_date", "stall_key": "d1.stall_name",
        "sku_key": "d2.sku_name", "opening_qty": "i.opening_qty",
        "inbound_qty": "i.inbound_qty", "outbound_qty": "i.outbound_qty",
        "adjustment_qty": "i.adjustment_qty", "ending_qty": "i.ending_qty",
        "inventory_amount": "i.inventory_amount",
        "inventory_zero_type": "i.inventory_zero_type",
    },
}

_SEARCH = {
    "purchase": ("t.purchase_id", "t.customer_id", "t.stall_id", "t.sku_name",
                 "t.sku_code"),
    "sales": ("t.outbound_id", "t.customer_id", "t.stall_id", "t.sku_name",
              "t.sku_code", "t.downstream_customer_name"),
    "inventory": ("d1.stall_name", "d2.sku_name", "i.inventory_zero_type"),
}


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def detail(conn: sqlite3.Connection, f: board_filters.BoardFilters) -> dict:
    kind = f.kind
    columns = DETAIL_COLUMNS[kind]
    sort_col = _FLOW_COLUMNS[kind].get(f.sort) if f.sort else None
    if f.sort and not sort_col:
        raise board_filters.BoardFilterError(f"{kind} 明细不支持按 {f.sort!r} 排序")

    if kind == "inventory":
        clause, params = _inventory_clause(f)
        base = ("FROM fact_wholesale_inventory_daily i "
                "JOIN dim_wholesale_stall d1 ON d1.stall_key = i.stall_key "
                "JOIN dim_sku d2 ON d2.sku_key = i.sku_key WHERE " + clause)
        search_fields = [x for x in _SEARCH[kind]]
    else:
        table = "fact_wholesale_purchase" if kind == "purchase" else "fact_wholesale_sales"
        clause, params = board_filters.where_wholesale(f, "t", "t.month")
        base = f"FROM {table} t WHERE " + clause
        search_fields = [x for x in _SEARCH[kind]]

    extra_where, extra_params = "", []
    if f.q:
        needle = f"%{_like_escape(f.q)}%"
        clause_like = " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in search_fields)
        extra_where = " AND (" + clause_like + ")"
        extra_params = [needle] * len(search_fields)

    total_row = _one(conn, "SELECT COUNT(*) AS n " + base + extra_where,
                     [*params, *extra_params]) or {}
    total = total_row.get("n") or 0

    order = ""
    if sort_col:
        order = f" ORDER BY {sort_col} {f.order.upper()}"
    else:
        order = " ORDER BY 1 DESC, 2" if kind != "inventory" else " ORDER BY 1 DESC, 2, 3"

    select = ", ".join(f"{_FLOW_COLUMNS[kind][c]} AS {c}" for c in columns)
    rows = _rows(conn, f"SELECT {select} " + base + extra_where + order +
                 " LIMIT ? OFFSET ?",
                 [*params, *extra_params, f.limit, (f.page - 1) * f.limit])

    # 单据数：同一筛选下的去重单据号，不用行数冒充
    if kind == "purchase":
        doc = _one(conn, "SELECT COUNT(DISTINCT t.purchase_id) AS n " + base + extra_where,
                   [*params, *extra_params]) or {}
    elif kind == "sales":
        doc = _one(conn, "SELECT COUNT(DISTINCT t.outbound_id) AS n " + base + extra_where,
                   [*params, *extra_params]) or {}
    else:
        doc = {"n": None}

    out = _envelope(f, {
        "rows": rows, "columns": list(columns), "kind": kind,
        "total": total, "page": f.page, "limit": f.limit,
        "pages": max(1, (total + f.limit - 1) // f.limit),
        "doc_count": doc.get("n"),
        "row_count": total,
    })
    out["meta"]["sort"] = {"column": f.sort, "order": f.order, "q": f.q}
    return out


def meta_options(conn: sqlite3.Connection, f: board_filters.BoardFilters) -> dict:
    customers = _rows(conn,
        "SELECT customer_id, customer_name FROM dim_customer WHERE is_wholesale = 'Y' "
        "ORDER BY customer_id", [])
    stalls = _rows(conn,
        "SELECT stall_id, stall_name, customer_id FROM dim_wholesale_stall "
        "WHERE region_name_std IN (" + ",".join("?" * len(f.regions)) + ") "
        "ORDER BY stall_id", list(f.regions))
    skus = _rows(conn,
        "SELECT DISTINCT d.sku_code, d.sku_name, d.category FROM dim_sku d "
        "JOIN fact_wholesale_sales s ON s.sku_key = d.sku_key "
        "WHERE s.year = ? ORDER BY d.sku_code", [config.FIXED_YEAR_INT])
    months = _rows(conn,
        "SELECT DISTINCT month FROM fact_wholesale_sales WHERE year = ? ORDER BY month",
        [config.FIXED_YEAR_INT])
    return {
        "ok": True,
        "data": {
            "regions": list(f.regions),
            "customers": customers,
            "stalls": stalls,
            "skus": skus,
            "months": [r["month"] for r in months],
        },
        "meta": {"board": BOARD, "generated_at":
                 datetime.now().astimezone().isoformat(timespec="seconds")},
    }
