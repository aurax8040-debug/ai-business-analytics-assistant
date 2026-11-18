"""综合经营分析 V1 —— 六维度统一查询层。

产品架构修正（本模块的核心变化）：
    战区销售分析不再是独立一级看板，而是「综合经营分析」的**一个分析维度**。
    六个维度共用同一套筛选器、KPI、趋势、排名、结构与明细查询：

        region 战区 / channel 渠道 / ka KA 系统 / customer 客户 / category 品类 / sku SKU

维度只决定 ``GROUP BY`` 的拆解轴，**不改变任何指标口径**：
- 仍然只读 ETL V1 的 ``v_dash_erp`` / ``v_dash_pos``，不修改 ETL、不读源 CSV；
- 每条 SQL 只引用一张事实投影视图，绝不跨事实表 JOIN；
- ERP Sell-in 与 POS Sell-out 独立聚合、并列展示，金额 / 盒数永不相加；
- 维度可用性遵循已冻结口径：KA 系统仅 POS、客户仅 ERP，不猜测映射；
- 无记录成员返回 ``None``（前端「无数据」），不补零、不预测。

复用：``region_queries`` 的战区骨架与能力边界声明、``queries`` 的 KPI 与视图常量。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import config, filters
from .queries import ERP_VIEW, POS_VIEW, _envelope, _one, _ratio, _rows, kpi
from .region_queries import MISSING_CAPABILITIES

MONTHS = tuple(range(config.COMPARABLE_FIRST_MONTH, config.COMPARABLE_LAST_MONTH + 1))
CATEGORY_ORDER = ("品类A", "品类B", "品类C")
TOP_RANK = 10          # 排名图展示条数
TOP_CARD = 6           # 成员总览卡 / 结构图成员数
TOP_SERIES = 5         # 趋势图成员序列数
TOP_SKU = 8            # SKU TOP 条数
TOP_SECONDARY = 8      # KA / 客户表现条数（与 SKU TOP 对齐，避免单卡被撑高）

# ---------------------------------------------------------------------------
# 维度 → 事实视图列。label_expr 允许聚合函数（如 MAX(customer_name)）。
# ---------------------------------------------------------------------------
DIM_SPEC: dict[str, dict] = {
    "region":   {"col": "region_name_std", "label_expr": "region_name_std"},
    "channel":  {"col": "channel_type",    "label_expr": "channel_type"},
    "ka":       {"col": "ka_system",       "label_expr": "ka_system"},
    "customer": {"col": "customer_id",     "label_expr": "MAX(customer_name)"},
    "category": {"col": "category",        "label_expr": "category"},
    "sku":      {"col": "sku_code",        "label_expr": "MAX(sku_name)"},
}

# 明细表字段顺序（全部来自 /api/detail 已返回的列，不新增查询）
DETAIL_COLUMNS = {
    "region":   ["region_name_std", "month", "category", "sku_name", "channel_type"],
    "channel":  ["channel_type", "month", "region_name_std", "category", "sku_name"],
    "ka":       ["ka_system", "store_name", "month", "category", "sku_name"],
    "customer": ["customer_name", "customer_id", "month", "category", "sku_name"],
    "category": ["category", "month", "sku_name", "region_name_std", "channel_type"],
    "sku":      ["sku_name", "sku_code", "category", "month", "region_name_std"],
}

NOTES = (
    "六个维度共用同一套筛选器、KPI、趋势、排名与明细；切换维度只改变拆解轴，不改变指标口径。",
    "五大战区（战区A / 战区B / 战区C / 战区D / 战区E）；战区F、战区G为 OUT_OF_SCOPE，不展示。",
    "固定 Y26 · 2026-01-01 ~ 2026-10-31 自然月；单年口径，不提供跨年切换。",
    "ERP Sell-in 与 POS Sell-out 独立核算，金额 / 盒数永不相加；批发进销存不纳入本页。",
    "占比在「所选筛选 + 所选月份 + 同一系统」的分子分母内重算，分母为空返回 None。",
)


def _view(side: str) -> str:
    return ERP_VIEW if side == "erp" else POS_VIEW


def _dim_meta(key: str) -> dict:
    base = next(d for d in config.DIMENSIONS if d["key"] == key)
    has_erp = "erp" in base["sides"]
    has_pos = "pos" in base["sides"]
    return {
        "key": key,
        "label": base["label"],
        "member_label": base["member_label"],
        "sides": list(base["sides"]),
        "has_erp": has_erp,
        "has_pos": has_pos,
        "note": base["note"],
        "erp_note": None if has_erp else "ERP 侧无该维度字段，不展示、不猜测映射",
        "pos_note": None if has_pos else "POS 侧无该维度字段，不跨系统推断",
    }


def _group_agg(conn: sqlite3.Connection, f: filters.Filters, side: str,
               spec: dict, extra: str = "") -> list[dict]:
    """单视图 GROUP BY 当前维度列；不改变任何筛选语义。"""
    col, label_expr = spec["col"], spec["label_expr"]
    sql, params, _ = filters.sql(
        f"SELECT {col} AS name, {label_expr} AS label, "
        f"SUM(sales_amount) AS amount, SUM(sales_boxes) AS boxes, "
        f"COUNT(*) AS rows{extra} FROM {_view(side)} "
        f"WHERE {{where}} GROUP BY {col}", f, side)
    return _rows(conn, sql, params)


def _sort_key(primary: str):
    """按单一系统的金额降序排；该侧无记录的成员排最后。

    不允许「ERP 金额」与「POS 金额」混排比较——两套口径不可比，
    因此只选一个主系统：有 POS 用 POS（终端口径），只有 ERP 的维度用 ERP。
    """
    def key(x: dict):
        v = x.get(f"{primary}_amount")
        return (v is None, -(v or 0))
    return key


def _rank(items: list[dict], key: str, out_key: str) -> None:
    order = sorted(items, key=lambda x: (x.get(key) is None, -(x.get(key) or 0)))
    for i, it in enumerate(order, 1):
        it[out_key] = i


# ---------------------------------------------------------------- 1 成员总览
def _overview(conn: sqlite3.Connection, f: filters.Filters, spec: dict) -> dict:
    agg: dict[str, dict] = {}
    for side in spec["sides"]:
        extra = ", SUM(gross_profit) AS gross_profit" if side == "erp" else ""
        rows = _group_agg(conn, f, side, spec, extra)
        agg[side] = {r["name"]: r for r in rows}

    totals = {
        side: {
            "amount": sum((r["amount"] or 0) for r in agg[side].values()),
            "boxes": sum((r["boxes"] or 0) for r in agg[side].values()),
        }
        for side in agg
    }

    if spec["col"] == "region_name_std":
        # 战区维度沿用 P01 固定骨架：即使某战区无记录也保留位置（值为 None）
        names = [n for n in config.IN_SCOPE_REGIONS if n in f.regions]
    else:
        merged = list(dict.fromkeys([*agg.get("pos", {}).keys(), *agg.get("erp", {}).keys()]))
        names = [n for n in merged if n not in (None, "")]

    items: list[dict] = []
    for name in names:
        e = agg.get("erp", {}).get(name, {})
        p = agg.get("pos", {}).get(name, {})
        items.append({
            "name": name,
            "label": (p.get("label") or e.get("label") or name),
            "erp_amount": e.get("amount"),
            "erp_boxes": e.get("boxes"),
            "erp_gross_profit": e.get("gross_profit"),
            "erp_margin_pct": _ratio(e.get("gross_profit"), e.get("amount")),
            "erp_share_pct": _ratio(e.get("amount"), totals["erp"]["amount"] if "erp" in totals else None),
            "erp_rows": e.get("rows", 0),
            "pos_amount": p.get("amount"),
            "pos_boxes": p.get("boxes"),
            "pos_share_pct": _ratio(p.get("amount"), totals["pos"]["amount"] if "pos" in totals else None),
            "pos_rows": p.get("rows", 0),
        })

    items.sort(key=_sort_key("pos" if "pos" in spec["sides"] else "erp"))
    for side in spec["sides"]:
        for metric in ("amount", "boxes"):
            _rank(items, f"{side}_{metric}", f"{side}_{metric}_rank")

    return {
        "items": items,
        "member_count": len(items),
        "erp_total_amount": totals.get("erp", {}).get("amount"),
        "erp_total_boxes": totals.get("erp", {}).get("boxes"),
        "pos_total_amount": totals.get("pos", {}).get("amount"),
        "pos_total_boxes": totals.get("pos", {}).get("boxes"),
        "rank_note": "金额 / 数量排名在 ERP 与 POS 两套系统内各自独立排序",
        "note": "两套系统独立聚合后按当前维度骨架拼接；无记录成员保持 None，不按 0 计",
    }


# ---------------------------------------------------------------- 2 排名
def _ranking(overview: dict, spec: dict) -> dict:
    items = overview["items"]
    ranks: dict[str, list[dict]] = {}
    for side in spec["sides"]:
        for metric in ("amount", "boxes"):
            key = f"{side}_{metric}"
            ordered = sorted(items, key=lambda x: (x[key] is None, -(x[key] or 0)))[:TOP_RANK]
            total = sum((x[key] or 0) for x in items)
            ranks[key] = [{"name": x["name"], "label": x["label"], "value": x[key],
                           "rank": i, "share_pct": _ratio(x[key], total or None)}
                          for i, x in enumerate(ordered, 1)]
    return {"ranks": ranks, "limit": TOP_RANK,
            "note": "ERP / POS 分别按金额与盒数排名，两套结果不做合并或比较"}


# ---------------------------------------------------------------- 3 月度趋势
def _monthly(conn: sqlite3.Connection, f: filters.Filters, spec: dict,
             overview: dict) -> dict:
    """成员 × 自然月矩阵；只画当前维度金额 Top N 成员，其余成员不外推、不合并。"""
    members = [x["name"] for x in overview["items"][:TOP_SERIES]]
    out: dict[str, Any] = {"months": list(MONTHS), "members": members,
                           "labels": [next((x["label"] for x in overview["items"]
                                            if x["name"] == m), m) for m in members],
                           "erp": {}, "pos": {}}
    col = spec["col"]
    for side in spec["sides"]:
        sql, params, _ = filters.sql(
            f"SELECT {col} AS name, month, SUM(sales_amount) AS amount, "
            f"SUM(sales_boxes) AS boxes FROM {_view(side)} "
            f"WHERE {{where}} GROUP BY {col}, month", f, side)
        rows = _rows(conn, sql, params)
        matrix: dict[str, dict] = {m: {"amount": [None] * len(MONTHS),
                                       "boxes": [None] * len(MONTHS)} for m in members}
        for r in rows:
            if r["name"] not in matrix or r["month"] not in MONTHS:
                continue
            i = MONTHS.index(r["month"])
            matrix[r["name"]]["amount"][i] = r["amount"]
            matrix[r["name"]]["boxes"][i] = r["boxes"]
        out[side] = matrix
    out["note"] = (f"趋势只画当前维度金额 TOP {TOP_SERIES} 成员，完整 Y26 1–10 月真实序列；"
                   "所选月份之外在图上断开、不补零；其余成员可点排名图下钻查看")
    return out


# ---------------------------------------------------------------- 4 维度 × 品类
def _structure(conn: sqlite3.Connection, f: filters.Filters, spec: dict,
               overview: dict) -> dict:
    members = [x["name"] for x in overview["items"][:TOP_CARD]]
    labels = [next((x["label"] for x in overview["items"] if x["name"] == m), m)
              for m in members]
    out: dict[str, Any] = {"members": members, "labels": labels,
                           "categories": list(CATEGORY_ORDER),
                           "erp": {}, "pos": {}, "shares": {"erp": {}, "pos": {}}}
    col = spec["col"]
    for side in spec["sides"]:
        sql, params, _ = filters.sql(
            f"SELECT {col} AS name, category, SUM(sales_amount) AS amount "
            f"FROM {_view(side)} WHERE {{where}} GROUP BY {col}, category", f, side)
        rows = _rows(conn, sql, params)
        lookup = {(r["name"], r["category"]): r["amount"] for r in rows}
        for cat in CATEGORY_ORDER:
            out[side][cat] = [lookup.get((m, cat)) for m in members]
        for m in members:
            total = sum((lookup.get((m, c)) or 0) for c in CATEGORY_ORDER)
            out["shares"][side][m] = {
                c: _ratio(lookup.get((m, c)), total or None) for c in CATEGORY_ORDER}
    out["note"] = (f"只展示当前维度金额 TOP {TOP_CARD} 成员的品类结构；"
                   "占比分母为「该成员 · 该系统 · 所选月份」合计")
    # 品类维度下这张图就是「品类 × 品类」的自身矩阵，属于重复视角，前端不出图
    out["redundant_with"] = "category" if spec["col"] == "category" else None
    return out


# ---------------------------------------------------------------- 5 SKU TOP
def _sku(conn: sqlite3.Connection, f: filters.Filters, spec: dict) -> dict:
    out: dict[str, Any] = {"erp": [], "pos": [], "total": {}, "limit": TOP_SKU,
                           "redundant_with": "sku" if spec["col"] == "sku_code" else None,
                           "pieces": {"erp": [], "pos": []},
                           "spec": {"erp": [], "pos": []}}
    for side in spec["sides"]:
        sql, params, _ = filters.sql(
            f"SELECT sku_code AS name, MAX(sku_name) AS label, MAX(category) AS category, "
            f"SUM(sales_amount) AS amount, SUM(sales_boxes) AS boxes "
            f"FROM {_view(side)} WHERE {{where}} GROUP BY sku_code "
            f"ORDER BY amount DESC LIMIT ?", f, side)
        params.append(TOP_SKU)
        rows = _rows(conn, sql, params)
        for i, r in enumerate(rows, 1):
            r["rank"] = i
        out[side] = rows
        c_sql, c_params, _ = filters.sql(
            f"SELECT COUNT(*) AS n FROM (SELECT sku_code FROM {_view(side)} "
            f"WHERE {{where}} GROUP BY sku_code)", f, side)
        out["total"][side] = _one(conn, c_sql, c_params).get("n", 0)
        # 粒数 / 规格：同样只查这一侧的投影视图，按已物化的 SKU 属性分组
        out["pieces"][side] = _sku_attr(conn, f, side, "pieces_per_box", "粒装")
        out["spec"][side] = _sku_attr(conn, f, side, "specification", "#")
    out["note"] = ("SKU TOP 分别来自 v_dash_erp / v_dash_pos 的单表聚合；"
                   "点击条形联动全局 SKU 筛选，不跨系统合并 SKU 金额")
    out["attr_note"] = ("粒数（pieces_per_box）与规格（specification）来自已物化的 SKU 属性列，"
                        "分子分母同为该侧合计；不推断缺失组合")
    return out


def _sku_attr(conn: sqlite3.Connection, f: filters.Filters, side: str,
              col: str, suffix: str) -> list[dict]:
    """按粒数或规格聚合当前筛选内的销售结构；空值不参与分组。"""
    sql, params, _ = filters.sql(
        f"SELECT {col} AS name, SUM(sales_amount) AS amount, SUM(sales_boxes) AS boxes, "
        f"COUNT(DISTINCT sku_code) AS skus FROM {_view(side)} "
        f"WHERE {{where}} AND {col} IS NOT NULL GROUP BY {col} "
        f"ORDER BY amount DESC", f, side)
    rows = _rows(conn, sql, params)
    total = sum((r["amount"] or 0) for r in rows)
    for r in rows:
        r["label"] = f"{r['name']}#" if suffix == "#" else f"{r['name']} {suffix}"
        r["share_pct"] = _ratio(r["amount"], total or None)
    return rows


# ---------------------------------------------------------------- 6 该维度下的 KA / 客户
def _secondary(conn: sqlite3.Connection, f: filters.Filters, spec: dict) -> dict:
    """固定按 POS ka_system / ERP customer_id 分组，观察当前维度内部的下钻对象。"""
    out: dict[str, Any] = {
        "pos": {"redundant": spec["col"] == "ka_system", "top": [], "total_members": 0},
        "erp": {"redundant": spec["col"] == "customer_id", "top": [], "total_members": 0},
    }
    specs = (
        ("pos", "ka_system", "ka_system"),
        ("erp", "customer_id", "MAX(customer_name)"),
    )
    for side, member_col, label_expr in specs:
        sql, params, _ = filters.sql(
            f"SELECT {member_col} AS name, {label_expr} AS label, "
            f"SUM(sales_amount) AS amount, SUM(sales_boxes) AS boxes, COUNT(*) AS rows "
            f"FROM {_view(side)} WHERE {{where}} GROUP BY {member_col}", f, side)
        rows = _rows(conn, sql, params)
        rows.sort(key=lambda x: -(x["amount"] or 0))
        total = sum((r["amount"] or 0) for r in rows)
        for i, r in enumerate(rows, 1):
            r["rank"] = i
            r["share_pct"] = _ratio(r["amount"], total or None)
        out[side]["total_members"] = len(rows)
        out[side]["top"] = rows[:TOP_SECONDARY]
    out["note"] = ("POS 按 ka_system、ERP 按 customer_id 分组；KA↔ERP 客户映射 PENDING，"
                   "两套名单不合并、不对照，点击条形联动对应全局筛选")
    return out


# ---------------------------------------------------------------- 7 经营明细
# 关键词只在这几列上做 LIKE（参数绑定）；列名来自本文件常量，不接受前端传入列名。
DETAIL_TEXT_COLUMNS = {
    "erp": ("region_name_std", "sku_name", "sku_code", "category", "channel_type",
            "customer_name", "customer_id"),
    "pos": ("region_name_std", "sku_name", "sku_code", "category", "channel_type",
            "ka_system", "store_name"),
}


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def detail(conn: sqlite3.Connection, f: filters.Filters) -> dict:
    """经营明细：支持服务端排序与关键词搜索；与全局筛选同一套 WHERE。

    与 ``/api/detail`` 的区别只有两点：可排序、可搜索。列集合、筛选语义、
    分页上限完全一致，避免两个明细接口出现口径分叉。
    """
    side = f.source
    view = _view(side)
    cols = ("year, month, full_date, region_name_std, sku_code, sku_name, category, "
            "channel_type, sales_boxes, sales_amount")
    cols += (", customer_id, customer_name, shipment_te, cost_amount, gross_profit"
             if side == "erp" else ", ka_system, store_name, sales_te")
    available = [c.strip() for c in cols.split(",")]

    extra_where, extra_params = "", []
    if f.q:
        targets = DETAIL_TEXT_COLUMNS[side]
        clause = " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in targets)
        extra_where = f" AND ({clause})"
        extra_params = [f"%{_like_escape(f.q)}%"] * len(targets)

    order_sql = "ORDER BY full_date, sku_code"
    if f.sort and f.sort in available:
        # 排序方向已由 filters.parse 限定为 asc/desc；列名已命中白名单与本侧列集合
        order_sql = f"ORDER BY {f.sort} {f.order.upper()}, sku_code"

    sql, params, app = filters.sql(
        f"SELECT {cols} FROM {view} WHERE {{where}}{extra_where} "
        f"{order_sql} LIMIT ? OFFSET ?", f, side)
    params = [*params, *extra_params, f.limit, (f.page - 1) * f.limit]
    rows = _rows(conn, sql, params)

    c_sql, c_params, _ = filters.sql(
        f"SELECT COUNT(*) AS n FROM {view} WHERE {{where}}{extra_where}", f, side)
    total = _one(conn, c_sql, [*c_params, *extra_params]).get("n", 0)

    data = {
        "source": side, "rows": rows, "total": total,
        "page": f.page, "page_size": f.limit,
        "columns": available,
        "sort": f.sort if f.sort in available else None,
        "order": f.order, "q": f.q,
        "sortable": [c for c in filters.SORTABLE_COLUMNS if c in available],
        "note": "排序与搜索在服务端完成；关键词只匹配文本列，数值列不参与模糊匹配",
    }
    return _envelope(data, f, [view], {side: app})


# ---------------------------------------------------------------- 对外接口
def analysis(conn: sqlite3.Connection, f: filters.Filters) -> dict:
    """一次连接返回综合经营分析页的全部区块；维度仅决定拆解轴。"""
    dim_meta = _dim_meta(f.dimension)
    # 列映射来自 DIM_SPEC，可用系统来自 config.DIMENSIONS，二者合并后传给各区块
    spec = {**DIM_SPEC[f.dimension], "sides": tuple(dim_meta["sides"])}

    overview = _overview(conn, f, spec)
    kpi_data = kpi(conn, f)["data"]

    data = {
        "dimension": dim_meta,
        "dimensions": [_dim_meta(d["key"]) for d in config.DIMENSIONS],
        "kpi": kpi_data,
        "overview": overview,
        "ranking": _ranking(overview, spec),
        "monthly": _monthly(conn, f, spec, overview),
        "structure": _structure(conn, f, spec, overview),
        "sku": _sku(conn, f, spec),
        "secondary": _secondary(conn, f, spec),
        "compare": {
            "rows": overview["items"],
            "erp_total_amount": overview["erp_total_amount"],
            "pos_total_amount": overview["pos_total_amount"],
            "note": "并列展示两套独立核算结果；不计算差额、比率或 Sell-through，金额永不相加",
            "not_supported": [m["title"] for m in MISSING_CAPABILITIES
                              if m["key"] in ("cross_system_ratio", "ka_to_erp_mapping",
                                              "pos_margin")],
        },
        "detail_columns": {
            "order": DETAIL_COLUMNS[f.dimension],
            # 高亮列 = 当前维度的可读列（明细首列），而不是编码列：
            # 客户维度高亮「客户名称」、SKU 维度高亮「SKU 名称」，便于肉眼核对。
            "highlight": DETAIL_COLUMNS[f.dimension][0],
        },
        "capabilities": {
            "missing": list(MISSING_CAPABILITIES),
            "notes": list(NOTES),
            "months_all": list(MONTHS),
            "months_selected": list(f.months),
            "month_scope": "全部 1–10 月" if not f.months
                           else "、".join(f"{m} 月" for m in f.months),
            "dimension_matrix": [
                {"key": d["key"], "label": d["label"],
                 "erp": "erp" in d["sides"], "pos": "pos" in d["sides"]}
                for d in config.DIMENSIONS],
            "dimension": f.dimension,
        },
    }

    views = [ERP_VIEW, POS_VIEW] if spec["sides"] == ("erp", "pos") \
        else [_view(spec["sides"][0])]
    applicabilities = {side: filters.where(f, side)[2] for side in spec["sides"]}
    return _envelope(
        data, f, views, applicabilities,
        {"page": "business-analysis-v1",
         "title": "综合经营分析 V1 · 六维度统一分析",
         "years_exposed": ["Y26"],
         "dimension": f.dimension,
         "dimension_label": dim_meta["label"],
         "systems_are_independent": True,
         "amount_sum_forbidden": True,
         "missing_capabilities": [m["key"] for m in MISSING_CAPABILITIES]},
    )
