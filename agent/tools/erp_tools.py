"""工具：query_erp_actual —— 查询 ERP 实际出货（Sell-in）TE、金额、毛利。

数据源：``fact_erp_shipment`` + 演示补充库的 ``fact_shipment_ext``（经 ``a_erp`` 视图，
与 BI 页面同一条口径）。

口径边界（面试必答题）：
- ERP 出货 = 星禾发给客户（Sell-in），**不是**终端零售；
- 金额永不与 POS 相加；
- 同比必须「同期」：本期与实际都取同一批月份，不允许拿 2025 全年比 2026 年 1–10 月。
"""
from __future__ import annotations

import sqlite3

from . import registry
from ._common import (SEGMENT_SQL, envelope, month_marks, pct, r2, resolve_period,
                      scope_clause, yoy)

NAME = "query_erp_actual"
SOURCE = "fact_erp_shipment + demo_ext.fact_shipment_ext（shipment_te / sales_amount / cost_amount / gross_profit）"
CALIBER = ("实际 TE = Σ shipment_te（ERP 出货行，五大战区内 IN_SCOPE）；"
           "毛利率 = Σ毛利 ÷ Σ出货金额；同比 = (本期 ÷ 去年同期 − 1) × 100%。")


def _sum(conn, clause: str, params: list) -> dict:
    row = conn.execute(
        "SELECT COALESCE(SUM(shipment_te),0), COALESCE(SUM(sales_boxes),0), "
        "COALESCE(SUM(sales_amount),0), COALESCE(SUM(cost_amount),0), "
        "COALESCE(SUM(gross_profit),0), COUNT(DISTINCT shipment_no) "
        f"FROM a_erp WHERE {clause}", params).fetchone()
    te, boxes, amount, cost, gp, orders = row
    return {"te": r2(te), "boxes": r2(boxes), "amount": r2(amount), "cost": r2(cost),
            "gross_profit": r2(gp), "gross_margin_pct": pct(gp, amount),
            "shipments": int(orders or 0)}


def _dim_conds(channel: str, category: str, sku: str) -> tuple[list[str], list]:
    """P8/P9 新增维度条件：渠道 / 品类 / SKU。

    单独抽出来，是为了让「本期」与「去年同期」两条 SQL 用**完全相同**的筛选，
    否则同比会变成「今年KA客户A vs 去年全部」——这种错最难在报告里看出来。
    """
    conds: list[str] = []
    params: list = []
    if channel:
        conds.append("channel_type = ?")
        params.append(channel)
    if category:
        conds.append("category = ?")
        params.append(category)
    if sku:
        conds.append("(sku_code = ? OR sku_name = ?)")
        params += [sku, sku]
    return conds, params


def run(conn: sqlite3.Connection, region: str = "", department: str = "",
        customer: str = "", year=None, months=None, segment: str = "",
        channel: str = "", category: str = "", sku: str = "") -> dict:
    period = resolve_period(year, months)
    where, args = scope_clause(region, department, "erp")
    mwhere, margs = month_marks(period["months"])
    extra, extra_args = _dim_conds(channel, category, sku)

    conds = ["year = ?", mwhere, where, *extra]
    params: list = [period["year"], *margs, *args, *extra_args]
    if customer:
        conds.append("(customer_id = ? OR customer_short = ?)")
        params += [customer, customer]
    if segment:
        conds.append(f"({SEGMENT_SQL}) = ?")
        params.append(segment)
    clause = " AND ".join(c for c in conds if c)

    metrics = _sum(conn, clause, params)

    by_month = []
    for m, te, amt, gp in conn.execute(
            f"SELECT month, SUM(shipment_te), SUM(sales_amount), SUM(gross_profit) "
            f"FROM a_erp WHERE {clause} GROUP BY month ORDER BY month", params):
        by_month.append({"month": m, "te": r2(te), "amount": r2(amt),
                         "gross_profit": r2(gp)})

    by_dept = [{"department": d, "te": r2(te), "amount": r2(a), "gross_profit": r2(g),
                "gross_margin_pct": pct(g, a)}
               for d, te, a, g in conn.execute(
                   f"SELECT dept_short, SUM(shipment_te), SUM(sales_amount), "
                   f"SUM(gross_profit) FROM a_erp WHERE {clause} "
                   f"GROUP BY dept_short ORDER BY 2 DESC", params)]

    by_segment = [{"segment": s, "te": r2(te), "amount": r2(a), "gross_profit": r2(g),
                   "gross_margin_pct": pct(g, a)}
                  for s, te, a, g in conn.execute(
                      f"SELECT {SEGMENT_SQL} AS seg, SUM(shipment_te), SUM(sales_amount), "
                      f"SUM(gross_profit) FROM a_erp WHERE {clause} "
                      f"GROUP BY seg ORDER BY 2 DESC", params)]

    # 客户级利润拆分：支撑「哪个客户量大但毛利率低」
    by_customer = [{"customer": c, "te": r2(te), "amount": r2(a), "gross_profit": r2(g),
                    "gross_margin_pct": pct(g, a)}
                   for c, te, a, g in conn.execute(
                       f"SELECT customer_short, SUM(shipment_te), SUM(sales_amount), "
                       f"SUM(gross_profit) FROM a_erp WHERE {clause} "
                       f"GROUP BY customer_short ORDER BY 2 DESC LIMIT 20", params)]

    # 品类级利润拆分（P9）：「哪个品类最赚钱」必须按品类答，
    # 不能拿客户维度顶上——那是在回答另一个问题。
    by_category = [{"category": c, "te": r2(te), "amount": r2(a), "gross_profit": r2(g),
                    "gross_margin_pct": pct(g, a)}
                   for c, te, a, g in conn.execute(
                       f"SELECT category, SUM(shipment_te), SUM(sales_amount), "
                       f"SUM(gross_profit) FROM a_erp WHERE {clause} "
                       f"GROUP BY category ORDER BY 2 DESC", params)]

    # 同期同比：同月份、上一年，条件除年份外保持一致
    ly_params = [period["year"] - 1, *margs, *args, *extra_args]
    ly_conds = ["year = ?", mwhere, where, *extra]
    if customer:
        ly_conds.append("(customer_id = ? OR customer_short = ?)")
        ly_params += [customer, customer]
    if segment:
        ly_conds.append(f"({SEGMENT_SQL}) = ?")
        ly_params.append(segment)
    last_te = conn.execute(
        f"SELECT COALESCE(SUM(shipment_te),0) FROM a_erp WHERE "
        f"{' AND '.join(c for c in ly_conds if c)}", ly_params).fetchone()[0]

    metrics["last_year_te"] = r2(last_te)
    metrics["yoy_pct"] = yoy(metrics["te"], last_te)

    scope = {"region": region or "全国（五大战区）", "department": department or "全部",
             "customer": customer or "全部客户", "segment": segment or "全部类型",
             "channel": channel or "全部渠道", "category": category or "全部品类",
             "sku": sku or "全部 SKU"}
    return envelope(
        NAME,
        args={"region": region, "department": department, "customer": customer,
              "channel": channel, "category": category, "sku": sku,
              "year": period["year"], "months": period["months"], "segment": segment},
        period=period,
        scope=scope,
        data_source=SOURCE, caliber=CALIBER, metrics=metrics,
        rows={"by_month": by_month, "by_department": by_dept,
              "by_segment": by_segment, "by_customer": by_customer,
              "by_category": by_category},
        notes=["ERP 出货是 Sell-in 口径，与 POS 终端销售属于不同业务环节，金额不相加。"]
              + ([f"已按 SKU「{sku}」筛选；预算无 SKU 维度，达成率不可用该条件。"] if sku else []),
        tables=["a_erp", "fact_erp_shipment", "fact_shipment_ext"],
    )


registry.register(
    NAME,
    "查询 ERP 实际出货（Sell-in）：TE、盒数、出货金额、成本、毛利额、毛利率，"
    "以及同期同比。可选按战区/市场部/客户/客户类型/渠道/品类/SKU 筛选。",
    {"region": {"type": "string", "description": "战区：战区A/战区B/战区C/战区D/战区E"},
     "department": {"type": "string", "description": "市场部，如 北京、河北"},
     "customer": {"type": "string", "description": "客户名称或客户 ID"},
     "year": {"type": "integer", "description": "年份，当前只支持 2026"},
     "months": {"type": "array", "items": {"type": "integer"},
                "description": "月份列表，如 [10]；不传表示累计 1–10 月"},
     "segment": {"type": "string", "description": "客户类型 clubz/nka/lka/wholesale"},
     "channel": {"type": "string", "description": "渠道：商超/终端、电商、批发"},
     "category": {"type": "string", "description": "品类：品类A/品类B/品类C"},
     "sku": {"type": "string", "description": "SKU 编码或名称，如 果萃品类A 22 粒"}},
    required=["year"],          # 期间口径必须显式给出，不猜年份
    fn=run,
    source=SOURCE,
    returns="metrics: te/amount/cost/gross_profit/gross_margin_pct/yoy_pct + 按月/市场部/类型/客户/品类拆分",
)
