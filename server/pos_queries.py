"""POS 门店动销 —— 查询层（阶段五）。

本看板只回答一个问题：**这 27 家对齐门店，卖得怎么样、谁在涨、谁在跌。**
它不重复「综合经营分析」的 ERP/POS 双口径对比，也不重复「SFA 门店画像」的
SFA×POS 联合分析——那两块已有独立页面，这里不复制指标。

四条硬边界（来自已冻结的 P01 / P05 / P12 / P13）
------------------------------------------------
1. **只有对齐版 POS**：``pos_link_status = 'POS_ALIGNED'``（33 家 ``SFA-ST-xxxxx``）；
   原 POS V3 的 ``ST-00001~33`` 与本版门店交集为 0，绝不混算（P12）。
2. **只有五大战区**：``region_scope_status = 'IN_SCOPE'``，战区F/战区G整段排除（P01）。
3. **只有 Y26**：2026-01-01 ~ 2026-10-31，Y25 的 45,000 行不进本期、不做同比（P05）。
4. **435 家 SFA_ONLY 门店不进任何 POS 分母**，也**不按零销售计**（P13）。
   因此「门店数」永远指「有 POS 销售记录的门店数」，不是「应卖门店数」。

两个必须写在页面上的事实
------------------------
- **POS 无记录 ≠ 零销售**：没有记录只代表这家店没有 POS 数据源，不是卖不动。
- **价格是 SKU 级固定标价**：本数据集同一 SKU 在 27 家门店、304 天里单价完全一致
  （0 个店×SKU 组合出现多价），也没有促销 / 满减 / 券字段。
  所以这里只做「单盒均价」这种**结构指标**（＝金额÷盒数，反映卖的是什么规格），
  **不做价格执行、折扣率、跨店价差分析**——没有差异化的数据画出来只会误导。

指标定义
--------
- 终端销售额 = ``SUM(sales_amount)``；终端销量 = ``SUM(sales_boxes)``（盒）；
- 单店月均 = 金额 ÷ (门店数 × 有记录的门店×月 组合数)，分母不用「月份数 × 27」硬凑；
- 单店日均销量 = 盒数 ÷ (门店数 × 有销售记录的日期数)；
- 单盒均价 = 金额 ÷ 盒数（加权，不是各 SKU 简单平均）；
- **动能** = 所选窗口对半分，后半段月均 vs 前半段月均的变化率；
  窗口只有 1 个月时不可比，返回 ``None``，不拿同比或上期硬凑；
- 集中度 = 门店金额占比的 CR3 / CR5 / CR10，用于说明「大盘被几家店撑着」。
"""
from __future__ import annotations

import dataclasses
import sqlite3
from datetime import datetime

from . import board_filters, config

BOARD = "pos"
TOP_STORE = 12
TOP_SKU = 10

# 品类固定三色，与「综合经营分析」「批发」保持一致，不在页面另起配色
CATEGORY_COLORS = {"品类A": "#e8a33d", "品类B": "#0e9f8a", "品类C": "#df4640"}

NOTES = (
    "POS 终端销售 = 门店收银端成交（Sell-out），与 ERP 出货（Sell-in）不是同一口径，金额永不相加。",
    "只使用对齐版 POS（POS_ALIGNED 33 家）；原 POS V3 门店与本版交集为 0，不混算。",
    "P01 五大战区口径：战区F/战区G门店整段排除，合计口径为「五大战区合计（不含战区F/战区G）」。",
    "POS 无销售记录 ≠ 零销售：435 家 SFA_ONLY 门店没有 POS 数据源，不进任何 POS 分母。",
    "本数据集单价为 SKU 级固定标价（同店同 SKU 304 天无变化），故只做结构均价，不做价格执行分析。",
    "动能 = 窗口前后半段月均对比，不是同比；窗口仅 1 个月时不可比，返回空值。",
)


# ------------------------------------------------------------------ 基础工具
def _rows(conn: sqlite3.Connection, sql: str, params) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _one(conn: sqlite3.Connection, sql: str, params) -> dict:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else {}


def _ratio(num, den):
    if den in (None, 0) or num is None:
        return None
    return 100.0 * num / den


def _div(num, den):
    if den in (None, 0) or num is None:
        return None
    return num / den


def _round(value, digits: int = 2):
    return None if value is None else round(value, digits)


def _envelope(f: board_filters.BoardFilters, data, extra: dict | None = None) -> dict:
    meta = {
        "board": BOARD,
        "filters": f.as_dict(),
        "region_label": f.region_label,
        "period": f"{config.FIXED_YEAR} · {f.month_label}",
        "year": config.FIXED_YEAR,
        "year_int": config.FIXED_YEAR_INT,
        "source_views": ["fact_pos_sales", "dim_store", "dim_sku", "dim_ka"],
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "notes": list(NOTES),
        "etl": _etl_stamp(),
    }
    if extra:
        meta.update(extra)
    return {"ok": True, "data": data, "meta": meta}


_ETL_CACHE: dict | None = None


def _etl_stamp() -> dict:
    """数据刷新时间：取 ETL 运行记录，页面页脚展示，不写死日期。"""
    global _ETL_CACHE
    if _ETL_CACHE is not None:
        return _ETL_CACHE
    try:
        from .db import connect
        conn = connect()
        try:
            row = _one(conn, "SELECT run_id, finished_at, status, quality_passed, "
                             "quality_total FROM etl_run ORDER BY finished_at DESC LIMIT 1", [])
        finally:
            conn.close()
        _ETL_CACHE = row or {}
    except Exception:                                    # pragma: no cover - 容错
        _ETL_CACHE = {}
    return _ETL_CACHE


# ------------------------------------------------------------------ 窗口与半段切分
def _months(f: board_filters.BoardFilters) -> list[int]:
    return list(f.months) or list(range(config.COMPARABLE_FIRST_MONTH,
                                       config.COMPARABLE_LAST_MONTH + 1))


def _split(months: list[int]) -> dict:
    """把窗口对半切：10 个月 → 1–5 月 vs 6–10 月。

    ``mode`` 决定页面文案：``even`` 可直接对比，``uneven`` 用月均对比（月份数不等），
    ``single`` 表示窗口只有 1 个月、动能不可比。
    """
    n = len(months)
    if n < 2:
        return {"first": [], "second": [], "mode": "single", "note": "窗口仅 1 个月，动能不可比"}
    half = n // 2
    first, second = months[:n - half], months[n - half:]

    def span(seq: list[int]) -> str:
        return f"{seq[0]} 月" if seq[0] == seq[-1] else f"{seq[0]}–{seq[-1]} 月"

    return {
        "first": first, "second": second,
        "mode": "even" if len(first) == len(second) else "uneven",
        "note": (f"前半 {span(first)} vs 后半 {span(second)}"
                 + ("（月份数不等，用月均对比）" if len(first) != len(second) else "")),
    }


# ------------------------------------------------------------------ 取数
def _store_month(conn, f) -> list[dict]:
    """门店 × 月的动销网格：本看板几乎所有区块都由这一张网格派生。"""
    clause, params = board_filters.where_pos(f, "p")
    return _rows(conn, f"""
        SELECT p.store_id, p.month,
               SUM(p.sales_amount) AS amount,
               SUM(p.sales_boxes)  AS boxes,
               SUM(p.sales_te)     AS te,
               COUNT(DISTINCT p.sku_code) AS sku_count
        FROM fact_pos_sales p
        WHERE {clause}
        GROUP BY 1, 2
        ORDER BY 1, 2""", params)


def _store_width(conn, f) -> dict[str, int]:
    """门店在整个窗口内的 SKU 动销宽度（跨月去重，不能由月度宽度相加）。"""
    clause, params = board_filters.where_pos(f, "p")
    rows = _rows(conn, f"""
        SELECT p.store_id, COUNT(DISTINCT p.sku_code) AS sku_count
        FROM fact_pos_sales p WHERE {clause} GROUP BY 1""", params)
    return {r["store_id"]: r["sku_count"] for r in rows}


def _days(conn, f) -> int:
    clause, params = board_filters.where_pos(f, "p")
    return _one(conn, f"SELECT COUNT(DISTINCT p.full_date) AS d "
                      f"FROM fact_pos_sales p WHERE {clause}", params).get("d") or 0


def _dim_stores(conn, store_ids: list[str]) -> dict[str, dict]:
    if not store_ids:
        return {}
    marks = ",".join("?" * len(store_ids))
    rows = _rows(conn, f"""
        SELECT store_id, store_name, ka_system, region_name_std AS war_zone,
               market_department, store_level, store_type
        FROM dim_store WHERE store_id IN ({marks})""", store_ids)
    return {r["store_id"]: r for r in rows}


# ------------------------------------------------------------------ 主查询
def analysis(conn: sqlite3.Connection, f: board_filters.BoardFilters) -> dict:
    grid = _store_month(conn, f)
    width = _store_width(conn, f)
    days = _days(conn, f)
    split = _split(_months(f))

    total_amount = sum(r["amount"] or 0 for r in grid)
    total_boxes = sum(r["boxes"] or 0 for r in grid)
    total_te = sum(r["te"] or 0 for r in grid)
    store_ids = sorted({r["store_id"] for r in grid})
    meta_stores = _dim_stores(conn, store_ids)

    # ---------------- 趋势（月度）：环比只在相邻自然月之间算
    by_month: dict[int, dict] = {}
    for row in grid:
        slot = by_month.setdefault(int(row["month"]), {
            "month": int(row["month"]), "amount": 0.0, "boxes": 0.0, "te": 0.0,
            "stores": set(), "skus": 0,
        })
        slot["amount"] += row["amount"] or 0
        slot["boxes"] += row["boxes"] or 0
        slot["te"] += row["te"] or 0
        slot["stores"].add(row["store_id"])
    trend = []
    prev = None
    for month in sorted(by_month):
        slot = by_month[month]
        trend.append({
            "month": month, "label": f"{month} 月",
            "amount": round(slot["amount"], 2),
            "boxes": slot["boxes"], "te": round(slot["te"], 2),
            "store_count": len(slot["stores"]),
            "mom_amount": _ratio(slot["amount"] - prev["amount"], prev["amount"]) if prev else None,
            "mom_boxes": _ratio(slot["boxes"] - prev["boxes"], prev["boxes"]) if prev else None,
        })
        prev = slot

    # ---------------- 门店层（规模 + 动能 + 集中度）
    buckets: dict[str, dict] = {}
    for row in grid:
        slot = buckets.setdefault(row["store_id"], {
            "store_id": row["store_id"], "amount": 0.0, "boxes": 0.0, "te": 0.0,
            "store_months": 0, "monthly": {},
            "amount_first": 0.0, "amount_second": 0.0,
        })
        slot["amount"] += row["amount"] or 0
        slot["boxes"] += row["boxes"] or 0
        slot["te"] += row["te"] or 0
        slot["store_months"] += 1
        slot["monthly"][int(row["month"])] = round(row["amount"] or 0, 2)
        if int(row["month"]) in split["second"]:
            slot["amount_second"] += row["amount"] or 0
        elif int(row["month"]) in split["first"]:
            slot["amount_first"] += row["amount"] or 0

    stores = []
    for store_id, slot in buckets.items():
        info = meta_stores.get(store_id, {})
        first_avg = _div(slot["amount_first"], len(split["first"]))
        second_avg = _div(slot["amount_second"], len(split["second"]))
        stores.append({
            "store_id": store_id,
            "store_name": info.get("store_name") or store_id,
            "ka_system": info.get("ka_system"),
            "war_zone": info.get("war_zone"),
            "market_department": info.get("market_department"),
            "store_level": info.get("store_level"),
            "store_type": info.get("store_type"),
            "amount": round(slot["amount"], 2),
            "boxes": slot["boxes"],
            "te": round(slot["te"], 2),
            "store_months": slot["store_months"],
            "sku_count": width.get(store_id),
            "unit_price": _round(_div(slot["amount"], slot["boxes"]), 2),
            "share": _round(_ratio(slot["amount"], total_amount), 2),
            "amount_per_month": _round(_div(slot["amount"], slot["store_months"]), 2),
            "amount_first": round(slot["amount_first"], 2),
            "amount_second": round(slot["amount_second"], 2),
            "monthly": slot["monthly"],
            "momentum": _round(_ratio(second_avg - first_avg, first_avg)
                               if split["mode"] != "single" else None, 2),
        })
    stores.sort(key=lambda x: x["amount"], reverse=True)
    for i, s in enumerate(stores, 1):
        s["rank"] = i

    ranked = [s["share"] or 0 for s in stores]
    concentration = {
        "cr3": round(sum(ranked[:3]), 2),
        "cr5": round(sum(ranked[:5]), 2),
        "cr10": round(sum(ranked[:10]), 2),
        "top_store": stores[0]["store_name"] if stores else None,
        "top_share": stores[0]["share"] if stores else None,
        "store_count": len(stores),
        # 供环形图直接用：Top3 金额与其余门店金额（后端算，页面不再求和）
        "top3_amount": round(sum(s["amount"] for s in stores[:3]), 2),
        "others_amount": round(sum(s["amount"] for s in stores[3:]), 2),
        "top5_amount": round(sum(s["amount"] for s in stores[:5]), 2),
    }

    # ---------------- 动能排名（只看可比门店）
    comparable = [s for s in stores if s["momentum"] is not None]
    comparable.sort(key=lambda x: x["momentum"], reverse=True)
    momentum = {
        "split": split,
        "up": len([s for s in comparable if s["momentum"] > 0]),
        "down": len([s for s in comparable if s["momentum"] < 0]),
        "flat": len([s for s in comparable if s["momentum"] == 0]),
        "comparable": len(comparable),
        "rows": [{
            "store_id": s["store_id"], "store_name": s["store_name"],
            "ka_system": s["ka_system"], "war_zone": s["war_zone"],
            "amount_first": s["amount_first"], "amount_second": s["amount_second"],
            "momentum": s["momentum"], "amount": s["amount"], "share": s["share"],
        } for s in comparable],
    }

    # ---------------- KA / 品类 / SKU
    clause, params = board_filters.where_pos(f, "p")
    ka = _rows(conn, f"""
        SELECT p.ka_system, COUNT(DISTINCT p.store_id) AS store_count,
               SUM(p.sales_amount) AS amount, SUM(p.sales_boxes) AS boxes
        FROM fact_pos_sales p WHERE {clause}
        GROUP BY 1 ORDER BY amount DESC""", params)
    for row in ka:
        row["share"] = _ratio(row["amount"], total_amount)
        row["amount_per_store"] = _div(row["amount"], row["store_count"])

    categories = _rows(conn, f"""
        SELECT p.category, SUM(p.sales_amount) AS amount,
               SUM(p.sales_boxes) AS boxes, COUNT(DISTINCT p.store_id) AS store_count
        FROM fact_pos_sales p WHERE {clause}
        GROUP BY 1 ORDER BY amount DESC""", params)
    for row in categories:
        row["share"] = _ratio(row["amount"], total_amount)
        row["unit_price"] = _div(row["amount"], row["boxes"])
        row["color"] = CATEGORY_COLORS.get(row["category"])

    skus = _rows(conn, f"""
        SELECT p.sku_code, p.sku_name, p.category,
               SUM(p.sales_amount) AS amount, SUM(p.sales_boxes) AS boxes,
               COUNT(DISTINCT p.store_id) AS store_count,
               COUNT(DISTINCT p.month) AS month_count
        FROM fact_pos_sales p WHERE {clause}
        GROUP BY 1, 2, 3 ORDER BY amount DESC""", params)
    for row in skus:
        row["share"] = _ratio(row["amount"], total_amount)
        row["unit_price"] = _div(row["amount"], row["boxes"])

    kpi = {
        "amount": round(total_amount, 2),
        "boxes": total_boxes,
        "te": round(total_te, 2),
        "store_count": len(store_ids),
        "store_month_count": len(grid),
        "month_count": len(by_month),
        "day_count": days,
        "ka_count": len(ka),
        "sku_count": len({r["sku_code"] for r in skus}),
        "unit_price": _round(_div(total_amount, total_boxes), 2),
        "amount_per_store_month": _round(_div(total_amount, len(grid)), 2),
        "amount_per_store": _round(_div(total_amount, len(store_ids)), 2),
        "boxes_per_store_day": _round(_div(total_boxes, (len(store_ids) * days) if days else 0), 1),
        "top_share": _round(concentration["top_share"], 2),
        "last_month_mom": _round(trend[-1]["mom_amount"] if trend else None, 2),
    }

    data = {
        "kpi": kpi,
        "trend": trend,
        "stores": stores,
        "ka": ka,
        "categories": categories,
        "skus": skus[:TOP_SKU],
        "sku_count": len(skus),
        "momentum": momentum,
        "concentration": concentration,
        "coverage": _coverage(conn, f),
    }
    return _envelope(f, data, extra={"momentum_split": split.get("note")})


def _coverage(conn, f) -> dict:
    """覆盖边界：全部用实时值算，不在页面写死 84.63% 这类审计数字。

    注意分母差异（写进口径文档）：
    - P01 冻结文档的 84.63% 行 / 80.93% 金额，分母是**两个自然年的全量 85,000 行**；
    - 本页只做 Y26，分母是 **Y26 的 40,000 行**，所以会是 84.76% / 81.33%。
    两者都对，差别只在分母，页面以后端实时值为准。
    """
    row = _one(conn, """
        SELECT COUNT(*) AS rows_all,
               COUNT(DISTINCT store_id) AS stores_all,
               SUM(sales_amount) AS amount_all
        FROM fact_pos_sales
        WHERE year = ? AND pos_link_status = 'POS_ALIGNED'""",
        [config.FIXED_YEAR_INT]) or {}
    scope = _one(conn, """
        SELECT COUNT(*) AS rows_scope,
               COUNT(DISTINCT store_id) AS stores_scope,
               SUM(sales_amount) AS amount_scope
        FROM fact_pos_sales
        WHERE year = ? AND pos_link_status = 'POS_ALIGNED'
          AND region_scope_status = 'IN_SCOPE'""",
        [config.FIXED_YEAR_INT]) or {}
    sfa_only = _one(conn, """
        SELECT COUNT(*) AS n FROM dim_store
        WHERE pos_link_status = 'SFA_ONLY' AND region_scope_status = 'IN_SCOPE'""", {}) or {}

    rows_all = row.get("rows_all") or 0
    stores_scope = scope.get("stores_scope") or 0
    sfa_n = sfa_only.get("n") or 0
    return {
        "rows_all": rows_all,
        "rows_scope": scope.get("rows_scope") or 0,
        "row_share": _ratio(scope.get("rows_scope") or 0, rows_all),
        "stores_all": row.get("stores_all") or 0,
        "stores_scope": stores_scope,
        "amount_all": round(row.get("amount_all") or 0, 2),
        "amount_scope": round(scope.get("amount_scope") or 0, 2),
        "amount_share": _ratio(scope.get("amount_scope") or 0, row.get("amount_all") or 0),
        "sfa_only_stores": sfa_n,
        "sfa_total_stores": stores_scope + sfa_n,
        "excluded_regions": "战区F、战区G（P01 不纳入）",
    }


# ------------------------------------------------------------------ 明细
_STORE_COLUMNS = ("store_id", "store_name", "ka_system", "war_zone", "month",
                  "sales_amount", "sales_boxes", "sku_count", "unit_price",
                  "share", "mom")
_SKU_COLUMNS = ("store_id", "store_name", "sku_code", "sku_name", "category",
                "sales_amount", "sales_boxes", "unit_price", "share")

_SORT_MAP = {
    "store": {
        "store_id": "store_id", "store_name": "store_name", "ka_system": "ka_system",
        "war_zone": "war_zone", "month": "month", "sales_amount": "sales_amount",
        "sales_boxes": "sales_boxes", "sku_count": "sku_count",
        "unit_price": "unit_price", "share": "share", "mom": "mom",
    },
    "sku": {
        "store_id": "store_id", "store_name": "store_name", "sku_code": "sku_code",
        "sku_name": "sku_name", "category": "category", "sales_amount": "sales_amount",
        "sales_boxes": "sales_boxes", "unit_price": "unit_price", "share": "share",
    },
}

_SEARCH_FIELDS = {
    "store": ("COALESCE(d.store_name, p.store_name)", "p.store_id", "d.ka_system"),
    "sku": ("COALESCE(d.store_name, p.store_name)", "p.store_id",
            "p.sku_name", "p.sku_code"),
}


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def detail(conn: sqlite3.Connection, f: board_filters.BoardFilters) -> dict:
    """明细两种粒度：门店×月（动销节奏）与门店×SKU（动销结构）。

    「门店×月」这一档 270 行、可以看月度节奏；「门店×SKU」这一档 1,908 行，
    用来看每家店卖的是宽还是窄。两者都从同一套 WHERE 出发，口径不会漂移。
    """
    kind = f.kind if f.kind in ("store", "sku") else "store"
    columns = _STORE_COLUMNS if kind == "store" else _SKU_COLUMNS
    sort_col = _SORT_MAP[kind].get(f.sort) if f.sort else None
    if f.sort and not sort_col:
        raise board_filters.BoardFilterError(f"{kind} 明细不支持按 {f.sort!r} 排序")

    clause, params = board_filters.where_pos(f, "p")
    search_where, search_params = "", []
    if f.q:
        needle = f"%{_like_escape(f.q)}%"
        search_where = " AND (" + " OR ".join(
            f"{c} LIKE ? ESCAPE '\\'" for c in _SEARCH_FIELDS[kind]) + ")"
        search_params = [needle] * len(_SEARCH_FIELDS[kind])

    if kind == "store":
        cte = f"""
        WITH base AS (
          SELECT p.store_id,
                 COALESCE(d.store_name, p.store_name) AS store_name,
                 d.ka_system, p.region_name_std AS war_zone, p.month,
                 SUM(p.sales_amount) AS sales_amount,
                 SUM(p.sales_boxes)  AS sales_boxes,
                 COUNT(DISTINCT p.sku_code) AS sku_count
          FROM fact_pos_sales p
          LEFT JOIN dim_store d ON d.store_id = p.store_id
          WHERE {clause}{search_where}
          GROUP BY 1, 2, 3, 4, 5
        ), win AS (
          SELECT base.*,
                 LAG(sales_amount) OVER (PARTITION BY store_id ORDER BY month) AS prev_amount,
                 SUM(sales_amount) OVER () AS total_amount
          FROM base
        )"""
        select = """
          store_id, store_name, ka_system, war_zone, month,
          ROUND(sales_amount, 2) AS sales_amount, sales_boxes, sku_count,
          ROUND(sales_amount * 1.0 / NULLIF(sales_boxes, 0), 2) AS unit_price,
          ROUND(100.0 * sales_amount / NULLIF(total_amount, 0), 2) AS share,
          CASE WHEN prev_amount > 0
               THEN ROUND(100.0 * (sales_amount - prev_amount) / prev_amount, 2)
               ELSE NULL END AS mom"""
        count_sql = "WITH b AS (SELECT 1 FROM fact_pos_sales p " \
                    "LEFT JOIN dim_store d ON d.store_id = p.store_id " \
                    f"WHERE {clause}{search_where} GROUP BY p.store_id, p.month) " \
                    "SELECT COUNT(*) AS n FROM b"
        default_order = " ORDER BY sales_amount DESC, store_id, month"
    else:
        cte = f"""
        WITH base AS (
          SELECT p.store_id,
                 COALESCE(d.store_name, p.store_name) AS store_name,
                 p.sku_code, p.sku_name, p.category,
                 SUM(p.sales_amount) AS sales_amount,
                 SUM(p.sales_boxes)  AS sales_boxes
          FROM fact_pos_sales p
          LEFT JOIN dim_store d ON d.store_id = p.store_id
          WHERE {clause}{search_where}
          GROUP BY 1, 2, 3, 4, 5
        ), win AS (
          SELECT base.*, SUM(sales_amount) OVER () AS total_amount FROM base
        )"""
        select = """
          store_id, store_name, sku_code, sku_name, category,
          ROUND(sales_amount, 2) AS sales_amount, sales_boxes,
          ROUND(sales_amount * 1.0 / NULLIF(sales_boxes, 0), 2) AS unit_price,
          ROUND(100.0 * sales_amount / NULLIF(total_amount, 0), 2) AS share"""
        count_sql = "WITH b AS (SELECT 1 FROM fact_pos_sales p " \
                    "LEFT JOIN dim_store d ON d.store_id = p.store_id " \
                    f"WHERE {clause}{search_where} GROUP BY p.store_id, p.sku_code) " \
                    "SELECT COUNT(*) AS n FROM b"
        default_order = " ORDER BY sales_amount DESC, store_id, sku_code"

    order = f" ORDER BY {sort_col} {f.order.upper()}" if sort_col else default_order
    if sort_col:
        order += ", store_id, month" if kind == "store" else ", store_id, sku_code"

    total = _one(conn, count_sql, [*params, *search_params]).get("n") or 0
    rows = _rows(conn, cte + " SELECT " + select + " FROM win" + order +
                 " LIMIT ? OFFSET ?",
                 [*params, *search_params, f.limit, (f.page - 1) * f.limit])

    out = _envelope(f, {
        "rows": rows, "columns": list(columns), "kind": kind,
        "total": total, "page": f.page, "limit": f.limit,
        "pages": max(1, (total + f.limit - 1) // f.limit),
        "row_count": total,
        "grain": "门店 × 月" if kind == "store" else "门店 × SKU",
    })
    out["meta"]["sort"] = {"column": f.sort, "order": f.order, "q": f.q}
    return out


# ------------------------------------------------------------------ 门店下钻
def drill(conn: sqlite3.Connection, f: board_filters.BoardFilters,
          store_id: str) -> dict:
    """单店下钻：月度节奏 + 品类结构 + Top SKU + 该店在大盘里的位置。"""
    store_id = (store_id or "").strip()
    if not store_id:
        raise board_filters.BoardFilterError("缺少 store 参数（门店编码）")

    # 下钻时门店条件单独加，不放进公共 WHERE——否则「该店占大盘多少」会算成 100%。
    base_f = dataclasses.replace(f, store=None)
    clause, params = board_filters.where_pos(base_f, "p")
    info = _one(conn, """
        SELECT store_id, store_name, ka_system, region_name_std AS war_zone,
               market_department, store_level, store_type, city, province
        FROM dim_store WHERE store_id = ?""", [store_id])
    if not info:
        raise board_filters.BoardFilterError(f"门店 {store_id} 不在 dim_store 中")

    one_clause = clause + " AND p.store_id = ?"
    one_params = [*params, store_id]

    monthly = _rows(conn, f"""
        SELECT p.month, SUM(p.sales_amount) AS amount, SUM(p.sales_boxes) AS boxes,
               COUNT(DISTINCT p.sku_code) AS sku_count
        FROM fact_pos_sales p WHERE {one_clause}
        GROUP BY 1 ORDER BY 1""", one_params)
    prev = None
    for row in monthly:
        row["amount"] = round(row["amount"], 2)
        row["unit_price"] = _round(_div(row["amount"], row["boxes"]), 2)
        row["mom"] = _round(_ratio(row["amount"] - prev["amount"], prev["amount"])
                            if prev else None, 2)
        prev = row

    cats = _rows(conn, f"""
        SELECT p.category, SUM(p.sales_amount) AS amount, SUM(p.sales_boxes) AS boxes
        FROM fact_pos_sales p WHERE {one_clause}
        GROUP BY 1 ORDER BY amount DESC""", one_params)
    total_amount = sum(r["amount"] or 0 for r in monthly)
    for row in cats:
        row["amount"] = round(row["amount"], 2)
        row["share"] = _round(_ratio(row["amount"], total_amount), 2)
        row["color"] = CATEGORY_COLORS.get(row["category"])

    skus = _rows(conn, f"""
        SELECT p.sku_code, p.sku_name, p.category,
               SUM(p.sales_amount) AS amount, SUM(p.sales_boxes) AS boxes
        FROM fact_pos_sales p WHERE {one_clause}
        GROUP BY 1, 2, 3 ORDER BY amount DESC LIMIT 8""", one_params)
    for row in skus:
        row["amount"] = round(row["amount"], 2)
        row["share"] = _round(_ratio(row["amount"], total_amount), 2)
        row["unit_price"] = _round(_div(row["amount"], row["boxes"]), 2)

    totals = _one(conn, f"""
        SELECT SUM(p.sales_amount) AS amount, SUM(p.sales_boxes) AS boxes,
               COUNT(DISTINCT p.month) AS month_count,
               COUNT(DISTINCT p.sku_code) AS sku_count,
               COUNT(DISTINCT p.full_date) AS day_count
        FROM fact_pos_sales p WHERE {one_clause}""", one_params) or {}
    grand = _one(conn, f"SELECT SUM(p.sales_amount) AS amount, "
                       f"COUNT(DISTINCT p.store_id) AS stores "
                       f"FROM fact_pos_sales p WHERE {clause}", params) or {}

    amount = totals.get("amount") or 0
    summary = {
        "amount": round(amount, 2),
        "boxes": totals.get("boxes") or 0,
        "month_count": totals.get("month_count") or 0,
        "sku_count": totals.get("sku_count") or 0,
        "day_count": totals.get("day_count") or 0,
        "unit_price": _round(_div(amount, totals.get("boxes")), 2),
        "share": _round(_ratio(amount, grand.get("amount")), 2),
        "amount_per_month": _round(_div(amount, totals.get("month_count")), 2),
        "store_count_in_scope": grand.get("stores") or 0,
    }
    return _envelope(f, {
        "store": info, "summary": summary,
        "monthly": monthly, "categories": cats, "skus": skus,
    }, extra={"drill_store": store_id})


# ------------------------------------------------------------------ 筛选选项
def meta_options(conn: sqlite3.Connection, f: board_filters.BoardFilters) -> dict:
    base = ("FROM fact_pos_sales p WHERE p.year = ? "
            "AND p.pos_link_status = 'POS_ALIGNED' AND p.region_scope_status = 'IN_SCOPE'")
    months = _rows(conn, f"SELECT DISTINCT p.month {base} ORDER BY p.month",
                   [config.FIXED_YEAR_INT])
    ka = _rows(conn, f"SELECT p.ka_system, COUNT(DISTINCT p.store_id) AS store_count "
                     f"{base} GROUP BY 1 ORDER BY store_count DESC",
               [config.FIXED_YEAR_INT])
    skus = _rows(conn, "SELECT DISTINCT d.sku_code, d.sku_name, d.category "
                       "FROM dim_sku d JOIN fact_pos_sales p ON p.sku_key = d.sku_key "
                       "WHERE p.year = ? AND p.pos_link_status = 'POS_ALIGNED' "
                       "AND p.region_scope_status = 'IN_SCOPE' ORDER BY d.sku_code",
                 [config.FIXED_YEAR_INT])
    stores = _rows(conn, f"""
        SELECT p.store_id, COALESCE(d.store_name, p.store_name) AS store_name,
               d.ka_system, p.region_name_std AS war_zone
        {base.replace('FROM fact_pos_sales p', 'FROM fact_pos_sales p LEFT JOIN dim_store d ON d.store_id = p.store_id')}
        GROUP BY p.store_id ORDER BY store_name""", [config.FIXED_YEAR_INT])
    categories = _rows(conn, f"SELECT p.category, SUM(p.sales_amount) AS amount {base} "
                             f"GROUP BY 1 ORDER BY amount DESC", [config.FIXED_YEAR_INT])
    return {
        "ok": True,
        "data": {
            "regions": list(f.regions),
            "months": [r["month"] for r in months],
            "ka": ka,
            "skus": skus,
            "stores": stores,
            "categories": [r["category"] for r in categories],
        },
        "meta": {"board": BOARD, "etl": _etl_stamp(),
                 "generated_at": datetime.now().astimezone().isoformat(timespec="seconds")},
    }
