"""工具：query_pos_performance —— POS 终端销售（Sell-out）表现。

数据源：``fact_pos_sales``（27 家与 SFA 对齐的门店，覆盖五大战区）。

**口径红线**：POS 是终端零售，ERP 是出货给客户。两者环节不同、覆盖不同
（POS 只有 27 家对齐门店），本工具**绝不与 ERP 相加、不相除**。
调用本工具的目的只有一个：给「为什么没达成」补一层终端侧的可验证证据。
"""
from __future__ import annotations

import sqlite3

from . import registry
from ._common import ToolError, envelope, month_marks, pct, r2, resolve_period

NAME = "query_pos_performance"
SOURCE = "fact_pos_sales（sales_te / sales_amount / sales_boxes，27 家对齐门店）"
CALIBER = ("POS 终端销售 TE = Σ sales_te；仅覆盖 27 家与 SFA 对齐的门店，"
           "不是全国零售；与 ERP 出货不相加、不互相校验。")

# POS 的 KA 系统取值（库里实际存在，不含推测）
KA_SYSTEMS = tuple(
    f"KA系统{i:03d}" for i in (26, 40, 46, 47, 69, 71, 74, 75, 78, 83, 87, 97, 98, 103)
)


def run(conn: sqlite3.Connection, region: str = "", department: str = "",
        ka_system: str = "", year=None, months=None, top_n: int = 10,
        channel: str = "", category: str = "", sku: str = "", store: str = "",
        full_store_ranking: bool = False) -> dict:
    """POS 终端销售。

    ``full_store_ranking``（P9 新增）：为 True 时 ``by_store`` 返回全部门店而不是 Top N。
    只影响返回行数，不改变任何指标 —— 用途是让「SFA 拜访 × POS 动销」能做门店交集，
    交集必须基于完整门店集合，拿两个 Top10 求交集会得出「几乎没有重叠」的错误结论。
    """
    period = resolve_period(year, months)
    top_n = int(top_n or 10)

    conds = ["year = ?", "region_scope_status = 'IN_SCOPE'"]
    params: list = [period["year"]]
    mwhere, margs = month_marks(period["months"])
    conds.append(mwhere)
    params += margs
    if region:
        conds.append("region_name_std = ?")
        params.append(region)
    if department:
        conds.append("market_department = ?")
        params.append(department)
    if ka_system:
        if ka_system not in KA_SYSTEMS:
            raise ToolError("InvalidArgument", f"POS 没有 KA 系统「{ka_system}」",
                            f"可用：{'、'.join(KA_SYSTEMS)}")
        conds.append("ka_system = ?")
        params.append(ka_system)
    if channel:
        conds.append("channel_type = ?")
        params.append(channel)
    if category:
        conds.append("category = ?")
        params.append(category)
    if sku:
        conds.append("(sku_code = ? OR sku_name = ?)")
        params += [sku, sku]
    if store:
        conds.append("(store_name = ? OR store_id = ? OR standard_store_name = ?)")
        params += [store, store, store]
    clause = " AND ".join(conds)

    row = conn.execute(
        "SELECT COALESCE(SUM(sales_te),0), COALESCE(SUM(sales_amount),0), "
        "COALESCE(SUM(sales_boxes),0), COUNT(DISTINCT store_id), COUNT(*) "
        f"FROM fact_pos_sales WHERE {clause}", params).fetchone()
    te, amount, boxes, stores, records = row

    scope = {"region": region or "全国", "department": department or "全部",
             "ka_system": ka_system or "全部", "channel": channel or "全部渠道",
             "category": category or "全部品类", "sku": sku or "全部 SKU",
             "store": store or "全部门店"}
    args = {"region": region, "department": department, "ka_system": ka_system,
            "channel": channel, "category": category, "sku": sku, "store": store,
            "year": period["year"], "months": period["months"], "top_n": top_n}

    if not records:
        return envelope(
            NAME, args=args, period=period, scope=scope,
            data_source=SOURCE, caliber=CALIBER,
            metrics={"sales_te": 0.0, "stores": 0, "records": 0},
            notes=["该范围内 POS 无记录：POS 只覆盖 27 家对齐门店，"
                   "没有记录不等于终端销售为零。"]
                  + ([f"门店「{store}」不在 POS 对齐门店内，无法核对终端动销。"] if store else []),
            tables=["fact_pos_sales"])

    by_ka = [{"ka_system": k, "sales_te": r2(t), "sales_amount": r2(a), "stores": int(s)}
             for k, t, a, s in conn.execute(
                 "SELECT ka_system, SUM(sales_te), SUM(sales_amount), "
                 f"COUNT(DISTINCT store_id) FROM fact_pos_sales WHERE {clause} "
                 f"GROUP BY 1 ORDER BY 2 DESC LIMIT ?", [*params, top_n])]

    store_limit = "" if full_store_ranking else "LIMIT ?"
    store_params = [*params] if full_store_ranking else [*params, max(top_n, 20)]
    by_store = [{"store_id": sid, "store": s, "ka_system": k, "city": c,
                 "sales_te": r2(t), "sales_amount": r2(a)}
                for sid, s, k, c, t, a in conn.execute(
                    "SELECT store_id, store_name, ka_system, province_city, "
                    f"SUM(sales_te), SUM(sales_amount) FROM fact_pos_sales WHERE {clause} "
                    f"GROUP BY 1,2,3,4 ORDER BY 5 DESC {store_limit}", store_params)]

    # 单月问「动销下降」时，需要上一个自然月的同店对比；累计口径不做环比（没有可比基期）
    mom_month = None
    extra_metrics: dict = {}
    if len(period["months"]) == 1 and period["months"][0] > 1:
        mom_month = period["months"][0] - 1
        # conds 里月份标记的位置不固定（在 year / IN_SCOPE 之后），按值定位，
        # 只把月份换成基期，其余筛选原样复用，避免「本月KA客户A vs 上月全部门店」这种口径漂移。
        prev_conds = list(conds)
        prev_conds[prev_conds.index(mwhere)] = "month = ?"
        prev_params = [params[0], mom_month, *params[1 + len(margs):]]
        prev = {sid: float(t or 0) for sid, t in conn.execute(
            "SELECT store_id, SUM(sales_te) FROM fact_pos_sales WHERE "
            + " AND ".join(prev_conds) + " GROUP BY 1", prev_params)}
        for r in by_store:
            base = prev.get(r["store_id"])
            r["prev_sales_te"] = r2(base) if base is not None else None
            r["mom_pct"] = (round((r["sales_te"] / base - 1) * 100, 2)
                            if base else None)
        # 整体环比：给「出货 vs 终端方向是否一致」用，避免为了一个百分比再跑一次工具
        prev_total = sum(prev.values()) if prev else 0.0
        if prev_total:
            te = float(te or 0)
            extra_metrics = {"prev_sales_te": r2(prev_total),
                             "mom_pct": round((te / prev_total - 1) * 100, 2)}
        else:
            extra_metrics = {"prev_sales_te": None, "mom_pct": None}

    by_month = [{"month": m, "sales_te": r2(t)} for m, t in conn.execute(
        "SELECT month, SUM(sales_te) FROM fact_pos_sales WHERE "
        f"{clause} GROUP BY month ORDER BY month", params)]

    notes = ["POS 只覆盖 27 家与 SFA 对齐的门店，样本不代表全国终端。",
             "POS 与 ERP 是不同环节，本工具结果不与出货数据相加或相除。"]
    if mom_month:
        notes.append(f"门店环比基期为 {mom_month} 月；基期无记录的门店 mom_pct 返回 null，"
                     "不按 0 计算（新店/断货都会造成假下滑）。")

    return envelope(
        NAME,
        args={**args, "full_store_ranking": bool(full_store_ranking)},
        period=period,
        scope=scope,
        data_source=SOURCE, caliber=CALIBER,
        metrics={"sales_te": r2(te), "sales_amount": r2(amount),
                 "sales_boxes": r2(boxes), "stores": int(stores),
                 "records": int(records), "mom_base_month": mom_month,
                 **extra_metrics},
        rows={"by_ka_system": by_ka, "by_store": by_store, "by_month": by_month},
        notes=notes,
        tables=["fact_pos_sales"],
    )


registry.register(
    NAME,
    "查询 POS 终端销售（Sell-out）表现：TE、金额、覆盖门店数，按 KA 系统与门店拆分；"
    "可选按渠道/品类/SKU/门店筛选，单月时可给出门店环比。"
    "只能作为辅助证据，不能与 ERP 出货相加。",
    {"region": {"type": "string", "description": "战区：战区A/战区B/战区C/战区D/战区E"},
     "department": {"type": "string", "description": "市场部，如 北京、河北"},
     "ka_system": {"type": "string",
                   "description": "KA 系统，如 KA客户A/KA客户B/KA客户C/沃尔玛/胖东来"},
     "channel": {"type": "string", "description": "渠道：商超/终端"},
     "category": {"type": "string", "description": "品类：品类A/品类B/品类C"},
     "sku": {"type": "string", "description": "SKU 编码或名称"},
     "store": {"type": "string", "description": "门店名称或门店 ID"},
     "year": {"type": "integer", "description": "年份，当前只支持 2026"},
     "months": {"type": "array", "items": {"type": "integer"},
                "description": "月份列表，如 [10]；不传表示累计 1–10 月"},
     "top_n": {"type": "integer", "description": "KA/门店 Top N，默认 10"},
     "full_store_ranking": {"type": "boolean",
                            "description": "true 时返回全部门店（用于门店交集），默认 false"}},
    required=["year"],          # 期间口径必须显式给出，不猜年份
    fn=run,
    source=SOURCE,
    returns="metrics.sales_te/stores + rows.by_ka_system / by_store(含环比) / by_month",
)
