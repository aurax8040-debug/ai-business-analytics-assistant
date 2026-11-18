"""战区销售分析 V1 — 查询层。

严格复用全国经营驾驶舱 V1.1 的只读连接（``db.connect`` / ``mode=ro``）、
全局筛选器（``filters.sql`` 参数化 WHERE）与已验收视图
``v_dash_erp`` / ``v_dash_pos``；不新增业务指标、不修改 ETL、不读取源 CSV。

设计边界：
- 每条 SQL 只引用**一张**事实投影视图；ERP / POS 各自独立聚合后，
  在 Python 层按「五大战区」维度骨架拼接，绝不在明细层 JOIN 两张事实表。
- 金额、盒数永不相加：ERP Sell-in 与 POS Sell-out 分列，各自计算系统内占比。
- 不计算任何跨系统比率（Sell-through / 进销差 / 消化率），原因见 MISSING_CAPABILITIES。
- 无记录的战区 / 月份 / 维度保持 ``None``，前端显示「无数据」，不按 0 填充。
- 全局固定 Y26（2026 自然年 1–10 月）；``months`` 为可选白名单切片。
"""
from __future__ import annotations

import sqlite3
from dataclasses import replace
from typing import Any

from . import config, filters
from .queries import ERP_VIEW, POS_VIEW, _envelope, _one, _ratio, _rows

MONTHS = tuple(range(config.COMPARABLE_FIRST_MONTH, config.COMPARABLE_LAST_MONTH + 1))
CATEGORY_ORDER = ("品类A", "品类B", "品类C")
TOP_N = 8
TOP_MEMBERS = 5

# ---------------------------------------------------------------------------
# 现有接口 / ETL 无法支持、且本阶段明确「不自行编造」的分析项。
# 同时返回给前端展示，并写入验收报告。
# ---------------------------------------------------------------------------
MISSING_CAPABILITIES = (
    {
        "key": "ka_to_erp_mapping",
        "title": "同一 KA 的 Sell-in / Sell-out 对照",
        "reason": "dim_customer.ka_mapping_status 全为 PENDING、dim_ka.erp_customer_id 为空；"
                  "v_dash_erp 无 ka_system 字段，无业务确认映射。",
        "handling": "ERP 侧只按 customer_id、POS 侧只按 ka_system 分列统计，不猜测映射。",
    },
    {
        "key": "cross_system_ratio",
        "title": "Sell-through 率 / 进销差 / 渠道消化率",
        "reason": "POS 仅覆盖 27 家 POS_ALIGNED 门店（84.63% 行 / 80.93% 金额），"
                  "ERP 为全渠道出货；两者口径不可比，ETL V1 也未冻结此类跨系统指标。",
        "handling": "仅并列展示两套金额、盒数与各自系统内占比；不相加、不相除、不计算差额。",
    },
    {
        "key": "pos_margin",
        "title": "POS 毛利率 / 毛利额",
        "reason": "POS 事实表未物化成本与毛利字段。",
        "handling": "毛利率仅在 ERP 侧按 gross_profit ÷ sales_amount 汇总后重算。",
    },
    {
        "key": "store_level",
        "title": "门店级堆叠与门店下钻",
        "reason": "本阶段按要求不逐家门店堆叠；且 SFA×POS 仅 27 家 POS_ALIGNED 门店，"
                  "SFA_ONLY 门店不按 0 销售计。",
        "handling": "页面最高下钻到 SKU / KA / 客户，明细按单据行展示，不做门店排名。",
    },
    {
        "key": "future_months",
        "title": "2026 年 11–12 月 / 跨年对比",
        "reason": "数据截止 2026-10-31；本页固定 Y26 单一年份口径。",
        "handling": "月份白名单 1–10 月，越界参数返回 400；缺口不补零、不预测。",
    },
)

NOTES = (
    "五大战区（战区A / 战区B / 战区C / 战区D / 战区E）；战区F、战区G为 OUT_OF_SCOPE，不展示。",
    "固定 Y26 · 2026-01-01 ~ 2026-10-31 自然月；单年口径，不提供跨年切换。",
    "ERP Sell-in 与 POS Sell-out 独立核算，金额 / 盒数永不相加；批发进销存不纳入本页。",
    "占比在「所选战区 + 所选月份 + 同一系统」的分子分母内重算，分母为空返回 None。",
)


def _view(side: str) -> str:
    return ERP_VIEW if side == "erp" else POS_VIEW


def _side_agg(conn: sqlite3.Connection, f: filters.Filters, side: str,
              group: str, extra_cols: str = "") -> tuple[list[dict], dict]:
    """单视图 GROUP BY；返回 (rows, applicability)。不改变任何筛选语义。"""
    sql, params, app = filters.sql(
        f"SELECT {group}, SUM(sales_amount) AS amount, SUM(sales_boxes) AS boxes, "
        f"COUNT(*) AS rows{extra_cols} FROM {_view(side)} "
        f"WHERE {{where}} GROUP BY {group.split(' AS ')[0]}",
        f, side)
    return _rows(conn, sql, params), app


def _rank(items: list[dict], key: str, out_key: str) -> None:
    order = sorted(items, key=lambda x: (x.get(key) is None, -(x.get(key) or 0)))
    for i, it in enumerate(order, 1):
        it[out_key] = i


# ---------------------------------------------------------------- 1 总览
def _overview(conn: sqlite3.Connection, f: filters.Filters) -> tuple[dict, dict]:
    app: dict = {}
    agg: dict = {}
    for side in ("erp", "pos"):
        extra = (", SUM(gross_profit) AS gross_profit, SUM(cost_amount) AS cost_amount"
                 if side == "erp" else "")
        rows, app[side] = _side_agg(conn, f, side, "region_name_std AS name", extra)
        agg[side] = {r["name"]: r for r in rows}

    erp_total = sum((r["amount"] or 0) for r in agg["erp"].values())
    pos_total = sum((r["amount"] or 0) for r in agg["pos"].values())
    items: list[dict] = []
    for name in config.IN_SCOPE_REGIONS:
        if name not in f.regions:
            continue
        e, p = agg["erp"].get(name, {}), agg["pos"].get(name, {})
        items.append({
            "name": name,
            "erp_amount": e.get("amount"), "erp_boxes": e.get("boxes"),
            "erp_gross_profit": e.get("gross_profit"),
            "erp_margin_pct": _ratio(e.get("gross_profit"), e.get("amount")),
            "erp_share_pct": _ratio(e.get("amount"), erp_total or None),
            "erp_rows": e.get("rows", 0),
            "pos_amount": p.get("amount"), "pos_boxes": p.get("boxes"),
            "pos_share_pct": _ratio(p.get("amount"), pos_total or None),
            "pos_rows": p.get("rows", 0),
        })
    for side in ("erp", "pos"):
        for metric in ("amount", "boxes"):
            _rank(items, f"{side}_{metric}", f"{side}_{metric}_rank")

    data = {
        "items": items,
        "erp_total_amount": erp_total, "pos_total_amount": pos_total,
        "erp_total_boxes": sum((r["boxes"] or 0) for r in agg["erp"].values()),
        "pos_total_boxes": sum((r["boxes"] or 0) for r in agg["pos"].values()),
        "rank_note": "金额 / 数量排名在 ERP 与 POS 两套系统内各自独立排序",
        "note": "两套系统独立聚合后按战区骨架拼接；无记录战区保持 None，不按 0 计",
    }
    return data, app


# ---------------------------------------------------------------- 2 月度趋势
def _monthly(conn: sqlite3.Connection, f: filters.Filters) -> dict:
    """战区 × 自然月矩阵。

    趋势图保留完整 Y26 1–10 月（只受战区 / 渠道 / KA / 客户 / 品类 / SKU 影响，
    不接受 months 切片），便于定位趋势；所选月份由前端置空未选点做视觉高亮，
    汇总类区块与明细则严格按 months 在 SQL 层过滤。
    """
    f_all = replace(f, months=())
    out: dict[str, Any] = {"months": list(MONTHS), "erp": {}, "pos": {}}
    for side in ("erp", "pos"):
        sql, params, _ = filters.sql(
            f"SELECT region_name_std AS name, month, SUM(sales_amount) AS amount, "
            f"SUM(sales_boxes) AS boxes, COUNT(*) AS rows FROM {_view(side)} "
            f"WHERE {{where}} GROUP BY region_name_std, month", f_all, side)
        rows = _rows(conn, sql, params)
        matrix: dict[str, dict] = {}
        for name in config.IN_SCOPE_REGIONS:
            if name not in f.regions:
                continue
            matrix[name] = {"amount": [None] * len(MONTHS),
                            "boxes": [None] * len(MONTHS),
                            "rows": [0] * len(MONTHS)}
        for r in rows:
            if r["name"] not in matrix or r["month"] not in MONTHS:
                continue
            i = MONTHS.index(r["month"])
            matrix[r["name"]]["amount"][i] = r["amount"]
            matrix[r["name"]]["boxes"][i] = r["boxes"]
            matrix[r["name"]]["rows"][i] = r["rows"]
        out[side] = matrix
    out["note"] = ("趋势保留完整 Y26 1–10 月真实序列；月度筛选只作用于总览、排名、结构、"
                   "KA、对照与明细，未选月份在图上断开、不补零")
    return out


# ---------------------------------------------------------------- 3 金额 / 数量排名
def _ranking(overview: dict) -> dict:
    items = overview["items"]
    ranks: dict[str, list[dict]] = {}
    for side in ("erp", "pos"):
        for metric in ("amount", "boxes"):
            key = f"{side}_{metric}"
            ordered = sorted(items, key=lambda x: (x[key] is None, -(x[key] or 0)))
            total = sum((x[key] or 0) for x in ordered)
            ranks[key] = [{"name": x["name"], "value": x[key], "rank": i,
                           "share_pct": _ratio(x[key], total or None)}
                          for i, x in enumerate(ordered, 1)]
    return {"ranks": ranks, "note": "ERP / POS 分别按金额与盒数排名，两套结果不做合并或比较"}


# ---------------------------------------------------------------- 4 品类结构
def _category(conn: sqlite3.Connection, f: filters.Filters) -> dict:
    raw: dict = {}
    for side in ("erp", "pos"):
        sql, params, _ = filters.sql(
            f"SELECT region_name_std AS name, category, SUM(sales_amount) AS amount, "
            f"SUM(sales_boxes) AS boxes, COUNT(*) AS rows FROM {_view(side)} "
            f"WHERE {{where}} GROUP BY region_name_std, category", f, side)
        raw[side] = _rows(conn, sql, params)

    regions = [r for r in config.IN_SCOPE_REGIONS if r in f.regions]
    cats = [c for c in CATEGORY_ORDER]
    out: dict[str, Any] = {"regions": regions, "categories": cats,
                           "erp": {}, "pos": {}, "shares": {"erp": {}, "pos": {}}}
    for side in ("erp", "pos"):
        lookup = {(r["name"], r["category"]): r for r in raw[side]}
        for cat in cats:
            out[side][cat] = [lookup.get((rg, cat), {}).get("amount") for rg in regions]
        for rg in regions:
            total = sum((lookup.get((rg, c), {}).get("amount") or 0) for c in cats)
            out["shares"][side][rg] = {
                c: _ratio(lookup.get((rg, c), {}).get("amount"), total or None) for c in cats}
    out["note"] = "占比分母为「该战区 · 该系统 · 所选月份」的合计，分子分母同限定后重算"
    return out


# ---------------------------------------------------------------- 5 SKU TOP
def _sku(conn: sqlite3.Connection, f: filters.Filters) -> dict:
    out: dict[str, Any] = {"erp": [], "pos": [], "total": {}, "limit": TOP_N}
    for side in ("erp", "pos"):
        sql, params, _ = filters.sql(
            f"SELECT sku_code AS name, MAX(sku_name) AS sku_name, MAX(category) AS category, "
            f"SUM(sales_amount) AS amount, SUM(sales_boxes) AS boxes, COUNT(*) AS rows "
            f"FROM {_view(side)} WHERE {{where}} GROUP BY sku_code "
            f"ORDER BY amount DESC LIMIT ?", f, side)
        params.append(TOP_N)
        rows = _rows(conn, sql, params)
        for i, r in enumerate(rows, 1):
            r["rank"] = i
        out[side] = rows
        c_sql, c_params, _ = filters.sql(
            f"SELECT COUNT(*) AS n FROM (SELECT sku_code FROM {_view(side)} "
            f"WHERE {{where}} GROUP BY sku_code)", f, side)
        out["total"][side] = _one(conn, c_sql, c_params).get("n", 0)
    out["note"] = ("SKU TOP 分别来自 v_dash_erp / v_dash_pos 的单表聚合；"
                   "点击条形联动全局 SKU 筛选，不跨系统合并 SKU 金额")
    return out


# ---------------------------------------------------------------- 6 各战区 KA / 客户
def _ka(conn: sqlite3.Connection, f: filters.Filters) -> dict:
    regions = [r for r in config.IN_SCOPE_REGIONS if r in f.regions]
    specs = (
        ("pos", "ka_system", "MAX(ka_system)"),
        ("erp", "customer_id", "MAX(customer_name)"),
    )
    out: dict[str, Any] = {"regions": regions, "pos": {}, "erp": {},
                           "series": {"pos": [], "erp": []}, "top_members": {"pos": [], "erp": []}}
    for side, member_col, label_expr in specs:
        sql, params, _ = filters.sql(
            f"SELECT region_name_std AS name, {member_col} AS member, {label_expr} AS label, "
            f"SUM(sales_amount) AS amount, SUM(sales_boxes) AS boxes, COUNT(*) AS rows "
            f"FROM {_view(side)} WHERE {{where}} GROUP BY region_name_std, {member_col}",
            f, side)
        rows = _rows(conn, sql, params)
        by_region: dict[str, list[dict]] = {rg: [] for rg in regions}
        for r in rows:
            if r["name"] in by_region:
                by_region[r["name"]].append(r)
        for rg in regions:
            by_region[rg].sort(key=lambda x: (x["amount"] is None, -(x["amount"] or 0)))
            for i, r in enumerate(by_region[rg], 1):
                r["rank"] = i
        out[side]["by_region"] = {rg: by_region[rg][:TOP_N] for rg in regions}

        # 当前切片（全部所选战区）TOP N，供可点击下钻的排名图使用
        merged: dict[str, dict] = {}
        for r in rows:
            m = r["member"]
            if m not in merged:
                merged[m] = {"name": m, "label": r.get("label") or m,
                             "amount": 0.0, "boxes": 0.0, "rows": 0}
            merged[m]["amount"] += r["amount"] or 0
            merged[m]["boxes"] += r["boxes"] or 0
            merged[m]["rows"] += r["rows"] or 0
        ranked = sorted(merged.values(), key=lambda x: -x["amount"])
        for i, r in enumerate(ranked, 1):
            r["rank"] = i
        out[side]["top"] = ranked[:TOP_N]
        out[side]["total_members"] = len(ranked)
        total_all = sum(x["amount"] for x in ranked)
        for r in out[side]["top"]:
            r["share_pct"] = _ratio(r["amount"], total_all or None)

        # 五战区 × TOP5 成员矩阵（供分组柱状图）
        top_names = [x["name"] for x in ranked[:TOP_MEMBERS]]
        out["top_members"][side] = top_names
        lookup = {(r["name"], r["member"]): r["amount"] for r in rows}
        out["series"][side] = [
            {"name": nm, "values": [lookup.get((rg, nm)) for rg in regions]}
            for nm in top_names]

    out["note"] = ("POS 按 ka_system、ERP 按 customer_id 分组；KA↔ERP 客户映射 PENDING，"
                   "两套名单不合并、不对照，点击排名条形联动对应全局筛选")
    return out


# ---------------------------------------------------------------- 7 ERP / POS 对照
def _compare(overview: dict) -> dict:
    return {
        "rows": overview["items"],
        "erp_total_amount": overview["erp_total_amount"],
        "pos_total_amount": overview["pos_total_amount"],
        "note": "并列展示两套独立核算结果；不计算差额、比率或 Sell-through，金额永不相加",
        "not_supported": [m["title"] for m in MISSING_CAPABILITIES
                          if m["key"] in ("cross_system_ratio", "ka_to_erp_mapping", "pos_margin")],
    }


# ---------------------------------------------------------------- 对外接口
def analysis(conn: sqlite3.Connection, f: filters.Filters) -> dict:
    """一次连接返回战区销售分析页的全部区块；所有数值来自 ETL V1 已验收视图。"""
    overview, app = _overview(conn, f)
    data = {
        "overview": overview,
        "monthly": _monthly(conn, f),
        "ranking": _ranking(overview),
        "category": _category(conn, f),
        "sku": _sku(conn, f),
        "ka": _ka(conn, f),
        "compare": _compare(overview),
        "capabilities": {
            "missing": list(MISSING_CAPABILITIES),
            "notes": list(NOTES),
            "months_all": list(MONTHS),
            "months_selected": list(f.months),
            "month_scope": "全部 1–10 月" if not f.months else "、".join(f"{m} 月" for m in f.months),
        },
    }
    return _envelope(
        data, f,
        [ERP_VIEW, POS_VIEW],
        {"erp": app["erp"], "pos": app["pos"]},
        {"page": "region-analysis-v1",
         "title": "战区销售分析 V1 · Y26 五大战区",
         "years_exposed": ["Y26"],
         "systems_are_independent": True,
         "amount_sum_forbidden": True,
         "missing_capabilities": [m["key"] for m in MISSING_CAPABILITIES]},
    )
