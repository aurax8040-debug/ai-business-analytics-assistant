"""工具：query_wholesale_performance —— 批发客户 / 档口经营表现。

数据源：``fact_wholesale_sales``（出库）、``fact_wholesale_purchase``（采购）、
``v_inventory_month_end``（全国月末库存）。

口径边界：
- 批发出库是「批发客户 → 下游档口/二批」的流转，跟 ERP 出货给批发客户是**两段**，
  不能把批发出库 TE 与 ERP 批发客户 TE 相加；
- 库存视图只有全国口径，没有战区维度，所以不按战区拆库存（拆不出来就不拆）。
"""
from __future__ import annotations

import sqlite3

from . import registry
from ._common import envelope, month_marks, pct, r2, resolve_period

NAME = "query_wholesale_performance"
SOURCE = "fact_wholesale_sales / fact_wholesale_purchase / v_inventory_month_end"
CALIBER = ("批发出库 TE = Σ sales_te（批发客户→下游）；毛利率 = Σ毛利 ÷ Σ出库金额；"
           "批发出库与 ERP 出货给批发客户是两段业务，不相加。")


def run(conn: sqlite3.Connection, region: str = "", year=None, months=None,
        customer: str = "", top_n: int = 10,
        stall: str = "", category: str = "", sku: str = "") -> dict:
    period = resolve_period(year, months)
    top_n = int(top_n or 10)

    mwhere, margs = month_marks(period["months"])
    conds = ["year = ?", "region_scope_status = 'IN_SCOPE'", mwhere]
    params: list = [period["year"], *margs]
    if region:
        conds.append("region_name_std = ?")
        params.append(region)
    if customer:
        conds.append("customer_id = ?")
        params.append(customer)
    if stall:
        conds.append("stall_id = ?")
        params.append(stall)
    if category:
        conds.append("category = ?")
        params.append(category)
    if sku:
        conds.append("(sku_code = ? OR sku_name = ?)")
        params += [sku, sku]
    clause = " AND ".join(c for c in conds if c)

    row = conn.execute(
        "SELECT COALESCE(SUM(sales_te),0), COALESCE(SUM(sales_amount),0), "
        "COALESCE(SUM(cost_amount),0), COALESCE(SUM(gross_profit),0), "
        "COUNT(DISTINCT customer_id), COUNT(DISTINCT stall_id), "
        "COUNT(DISTINCT downstream_customer_name) "
        f"FROM fact_wholesale_sales WHERE {clause}", params).fetchone()
    te, amount, cost, gp, customers, stalls, downstream = row

    by_customer = [{"customer_id": cid, "sales_te": r2(t), "sales_amount": r2(a),
                    "gross_profit": r2(g), "gross_margin_pct": pct(g, a)}
                   for cid, t, a, g in conn.execute(
                       "SELECT customer_id, SUM(sales_te), SUM(sales_amount), "
                       f"SUM(gross_profit) FROM fact_wholesale_sales WHERE {clause} "
                       f"GROUP BY 1 ORDER BY 2 DESC LIMIT ?", [*params, top_n])]

    by_stall = [{"stall_id": sid, "sales_te": r2(t), "sales_amount": r2(a)}
                for sid, t, a in conn.execute(
                    "SELECT stall_id, SUM(sales_te), SUM(sales_amount) "
                    f"FROM fact_wholesale_sales WHERE {clause} "
                    f"GROUP BY 1 ORDER BY 2 DESC LIMIT ?", [*params, top_n])]

    by_month = [{"month": m, "sales_te": r2(t), "sales_amount": r2(a)}
                for m, t, a in conn.execute(
                    "SELECT month, SUM(sales_te), SUM(sales_amount) "
                    f"FROM fact_wholesale_sales WHERE {clause} "
                    f"GROUP BY month ORDER BY month", params)]

    # 采购：同一期间（区域条件同样适用）
    pconds = ["year = ?", "region_scope_status = 'IN_SCOPE'", mwhere]
    pparams: list = [period["year"], *margs]
    if region:
        pconds.append("region_name_std = ?")
        pparams.append(region)
    purchase_te = conn.execute(
        "SELECT COALESCE(SUM(purchase_te),0) FROM fact_wholesale_purchase WHERE "
        f"{' AND '.join(c for c in pconds if c)}", pparams).fetchone()[0]

    inventory = []
    try:
        inventory = [{"month": m, "ending_qty": r2(q), "inventory_amount": r2(a)}
                     for m, q, a in conn.execute(
                         "SELECT month, ending_qty, inventory_amount "
                         "FROM v_inventory_month_end WHERE year = ? AND month IN "
                         f"({','.join('?' for _ in period['months'])}) ORDER BY month",
                         [period["year"], *period["months"]])]
    except sqlite3.Error:
        inventory = []

    notes = ["批发出库与 ERP 出货给批发客户是两段业务，本工具结果不与 ERP 相加。"]
    if inventory:
        notes.append("库存为全国口径（月末最后一日），没有战区维度，未按战区拆分。")

    return envelope(
        NAME,
        args={"region": region, "customer": customer, "stall": stall,
              "category": category, "sku": sku,
              "year": period["year"], "months": period["months"], "top_n": top_n},
        period=period,
        scope={"region": region or "全国（五大战区）", "customer": customer or "全部批发客户",
               "stall": stall or "全部档口", "category": category or "全部品类",
               "sku": sku or "全部 SKU"},
        data_source=SOURCE, caliber=CALIBER,
        metrics={"sales_te": r2(te), "sales_amount": r2(amount), "cost": r2(cost),
                 "gross_profit": r2(gp), "gross_margin_pct": pct(gp, amount),
                 "purchase_te": r2(purchase_te),
                 "customers": int(customers or 0), "stalls": int(stalls or 0),
                 "downstream_customers": int(downstream or 0)},
        rows={"by_customer": by_customer, "by_stall": by_stall,
              "by_month": by_month, "inventory_month_end": inventory},
        notes=notes,
        tables=["fact_wholesale_sales", "fact_wholesale_purchase", "v_inventory_month_end"],
    )


registry.register(
    NAME,
    "查询批发客户与档口的经营表现：出库 TE、金额、毛利、采购量、月末库存，"
    "按客户与档口拆分，可按档口/品类/SKU 筛选。批发客户 ID 形如 WS-N20x。",
    {"region": {"type": "string", "description": "战区：战区A/战区B/战区C/战区D/战区E"},
     "customer": {"type": "string", "description": "批发客户 ID，如 WS-N201"},
     "stall": {"type": "string", "description": "档口 ID"},
     "category": {"type": "string", "description": "品类：品类A/品类B/品类C"},
     "sku": {"type": "string", "description": "SKU 编码或名称"},
     "year": {"type": "integer", "description": "年份，当前只支持 2026"},
     "months": {"type": "array", "items": {"type": "integer"},
                "description": "月份列表，如 [10]"}},
    required=["year"],          # 期间口径必须显式给出，不猜年份
    fn=run,
    source=SOURCE,
    returns="metrics.sales_te/amount/gross_profit/purchase_te + 按客户/档口/月度拆分 + 月末库存",
)
