"""BI 查询层：所有接口只查询 ETL V1 的 13 个交付视图 + 2 个筛选投影视图。

约束：
- 每个 SQL 只引用一张事实（或其视图），ERP / POS / 批发 / SFA 独立聚合后在 Python 层按维度骨架拼接。
- 绝不在明细层 JOIN 不同事实表。
- 不重新加载、不重新生成任何数据；仅对已物化的行级度量做 SUM。
- 汇总类比率（毛利率 / 占比 / 环比）按冻结指标字典在「汇总后重算」，分母为空返回 None。

V1.1 展示口径：
- 全局固定 Y26（2026 自然年 1–10 月），WHERE 恒定带 ``year = 2026``。
- 不读取其他年份、不计算跨年增长率；仅允许 Y26 相邻自然月环比（MoM）。
- 所有数值来自真实数据库；无数据的月份 / 维度保持 None，前端显示「无数据」，不按 0 填充。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from . import config, filters
from .db import health as db_health

ERP_VIEW = "v_dash_erp"
POS_VIEW = "v_dash_pos"
LATEST_MONTH = config.COMPARABLE_LAST_MONTH          # 10 月
PREV_MONTH = LATEST_MONTH - 1                        # 9 月


def _one(conn: sqlite3.Connection, sql: str, params: list[Any]) -> dict:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else {}


def _rows(conn: sqlite3.Connection, sql: str, params: list[Any]) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _pct(now: float | None, base: float | None) -> float | None:
    """Y26 相邻自然月环比：100*(本月-上月)/上月；分母 0/空 → None（不可比）。"""
    if base in (None, 0) or now is None:
        return None
    return 100.0 * (now - base) / base


def _ratio(num: float | None, den: float | None) -> float | None:
    if den in (None, 0) or num is None:
        return None
    return 100.0 * num / den


def _is_baseline(f: filters.Filters) -> bool:
    return not any((f.channel, f.ka, f.customer, f.category, f.sku, f.pieces)) \
        and len(f.regions) == len(config.IN_SCOPE_REGIONS)


def _envelope(data: Any, f: filters.Filters, views: list[str],
              applicability: dict | None = None, extra: dict | None = None) -> dict:
    out: dict[str, Any] = {
        "ok": True,
        "data": data,
        "meta": {
            "filters": f.as_dict(),
            "region_label": f.region_label,
            "year": config.FIXED_YEAR,
            "year_int": config.FIXED_YEAR_INT,
            "period": config.PERIOD_LABEL,
            "scope_note": config.SCOPE_NOTE,
            "source_views": views,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        },
    }
    if applicability:
        out["meta"]["filter_applicability"] = applicability
    if extra:
        out["meta"].update(extra)
    return out


# ---------------------------------------------------------------- 元数据 / 健康
def meta(conn: sqlite3.Connection, f: filters.Filters) -> dict:
    regions = [r[0] for r in conn.execute(
        "SELECT region_name FROM dim_region WHERE is_business_member=1 ORDER BY region_key")]
    channels = [r[0] for r in conn.execute(
        "SELECT channel_type FROM dim_channel WHERE channel_key>0 ORDER BY channel_key")]
    kas = [r[0] for r in conn.execute(
        "SELECT DISTINCT ka_system FROM v_dash_pos WHERE ka_system IS NOT NULL "
        "AND TRIM(ka_system)<>'' ORDER BY ka_system")]
    customers = _rows(conn,
        "SELECT customer_id, customer_name, channel_type FROM dim_customer "
        "WHERE customer_key>0 ORDER BY customer_key", [])
    categories = [r[0] for r in conn.execute(
        "SELECT DISTINCT category FROM dim_sku WHERE sku_key>0 AND category IS NOT NULL "
        "ORDER BY category")]
    skus = _rows(conn,
        "SELECT sku_code, sku_name, category, pieces_per_box, specification "
        "FROM dim_sku WHERE sku_key>0 ORDER BY sku_code", [])
    pieces = [r[0] for r in conn.execute(
        "SELECT DISTINCT pieces_per_box FROM dim_sku WHERE sku_key>0 "
        "AND pieces_per_box IS NOT NULL ORDER BY pieces_per_box")]
    pending_ka = conn.execute(
        "SELECT COUNT(*) FROM dim_customer WHERE ka_mapping_status<>'MATCHED'").fetchone()[0]
    online_status = [r[0] for r in conn.execute(
        "SELECT DISTINCT online_offline FROM dim_channel")]
    pos_channel_status = [r[0] for r in conn.execute(
        "SELECT DISTINCT channel_status FROM v_dash_pos")]

    data = {
        "title": "Marketing Intelligence Platform · 全国经营驾驶舱",
        "year": {"key": "Y26", "label": "Y26 · 2026", "range": config.CALENDAR_Y26},
        "period": config.PERIOD_LABEL,
        "regions": regions,
        "channels": channels,
        "kas": kas,
        "customers": customers,
        "categories": categories,
        "skus": skus,
        "pieces": pieces,
        "metrics": [{"key": k, "label": v["label"]} for k, v in config.METRICS.items()],
        "scope_label": config.EXCLUDED_REGION_NOTE,
        "scope_note": config.SCOPE_NOTE,
        "excluded_regions": ["战区F", "战区G"],
        "pending": {
            "erp_ka_mapping": f"ka_mapping_status 非 MATCHED 的客户 {pending_ka} 个；"
                              "dim_ka.erp_customer_id 为空，ERP 侧不提供 KA 映射",
            "online_offline": f"dim_channel.online_offline 取值 {online_status}；"
                              f"POS channel_status 取值 {pos_channel_status}；未提供线上/线下筛选，不推断",
        },
        "filter_applicability_notes": list(config.FILTER_APPLICABILITY_NOTES),
        "delivered_views": list(config.DELIVERED_VIEWS),
        "health": db_health(conn),
    }
    return _envelope(data, f, ["dim_region", "dim_channel", "dim_customer", "dim_sku",
                               "v_dash_pos", "v_scope_coverage"])


def health(conn: sqlite3.Connection, f: filters.Filters) -> dict:
    return _envelope(db_health(conn), f, list(config.DELIVERED_VIEWS))


def scope(conn: sqlite3.Connection, f: filters.Filters) -> dict:
    rows = _rows(conn, "SELECT * FROM v_scope_coverage", [])
    regions = _rows(conn,
        "SELECT region_key, region_name, region_scope_status, is_business_member, "
        "exists_in_erp, exists_in_pos, exists_in_sfa, exists_in_wholesale "
        "FROM dim_region ORDER BY region_key", [])
    return _envelope({"coverage": rows, "regions": regions,
                      "label": config.EXCLUDED_REGION_NOTE,
                      "note": config.SCOPE_NOTE}, f,
                     ["v_scope_coverage", "dim_region"])


# ---------------------------------------------------------------- KPI
ERP_MOM_COLS = (("amount", "sales_amount"), ("boxes", "sales_boxes"),
                ("te", "shipment_te"), ("gp", "gross_profit"))
POS_MOM_COLS = (("amount", "sales_amount"), ("boxes", "sales_boxes"),
                ("te", "sales_te"))


def _month_pair(conn: sqlite3.Connection, f: filters.Filters, side: str,
                cols: tuple[tuple[str, str], ...]) -> dict:
    """取 Y26 最新月与上一月的同口径汇总，用于相邻自然月环比（不使用跨年数据）。"""
    view = ERP_VIEW if side == "erp" else POS_VIEW
    sel = ", ".join(
        f"SUM(CASE WHEN month={LATEST_MONTH} THEN {col} END) AS {alias}_now, "
        f"SUM(CASE WHEN month={PREV_MONTH} THEN {col} END) AS {alias}_prev"
        for alias, col in cols)
    sql, params, _ = filters.sql(f"SELECT {sel} FROM {view} WHERE {{where}}", f, side)
    return _one(conn, sql, params)


def _mom_of(pair: dict, cols: tuple[tuple[str, str], ...]) -> dict:
    return {alias: _pct(pair.get(f"{alias}_now"), pair.get(f"{alias}_prev"))
            for alias, _ in cols}


def kpi(conn: sqlite3.Connection, f: filters.Filters) -> dict:
    e_sql, e_p, e_app = filters.sql(
        "SELECT COALESCE(SUM(sales_boxes),0) AS sales_boxes, "
        "COALESCE(SUM(sales_amount),0) AS sales_amount, "
        "COALESCE(SUM(shipment_te),0) AS shipment_te, "
        "COALESCE(SUM(cost_amount),0) AS cost_amount, "
        "COALESCE(SUM(gross_profit),0) AS gross_profit, "
        "COUNT(*) AS rows_count FROM " + ERP_VIEW + " WHERE {where}", f, "erp")
    erp = _one(conn, e_sql, e_p)
    erp["gross_margin_pct"] = _ratio(erp.get("gross_profit"), erp.get("sales_amount"))

    p_sql, p_p, p_app = filters.sql(
        "SELECT COALESCE(SUM(sales_boxes),0) AS sales_boxes, "
        "COALESCE(SUM(sales_amount),0) AS sales_amount, "
        "COALESCE(SUM(sales_te),0) AS sales_te, "
        "COUNT(*) AS rows_count FROM " + POS_VIEW + " WHERE {where}", f, "pos")
    pos = _one(conn, p_sql, p_p)

    ep = _month_pair(conn, f, "erp", ERP_MOM_COLS)
    pp = _month_pair(conn, f, "pos", POS_MOM_COLS)
    erp["mom"] = _mom_of(ep, ERP_MOM_COLS)
    pos["mom"] = _mom_of(pp, POS_MOM_COLS)
    # 毛利率环比用「百分点差」，不把比率再做一次百分比变化
    m_now = _ratio(ep.get("gp_now"), ep.get("amount_now"))
    m_prev = _ratio(ep.get("gp_prev"), ep.get("amount_prev"))
    erp["mom"]["margin_pp"] = None if m_now is None or m_prev is None else m_now - m_prev
    erp["latest_margin_pct"] = m_now
    pos["latest_amount"] = pp.get("amount_now")

    # 无筛选时用已验收的交付视图反查，证明 KPI 直接来自真实数据库
    baseline: dict[str, Any] = {"checked": _is_baseline(f)}
    if _is_baseline(f):
        ev = _one(conn, "SELECT SUM(sales_amount) AS amt FROM v_erp_monthly "
                        "WHERE year=2026 AND month<=10", [])
        pv = _one(conn, "SELECT SUM(sales_amount) AS amt FROM v_pos_monthly "
                        "WHERE year=2026 AND month<=10", [])
        baseline["erp_view_amount"] = ev.get("amt")
        baseline["pos_view_amount"] = pv.get("amt")
        baseline["erp_match"] = abs((erp.get("sales_amount") or 0) - (ev.get("amt") or 0)) < 0.01
        baseline["pos_match"] = abs((pos.get("sales_amount") or 0) - (pv.get("amt") or 0)) < 0.01
        baseline["note"] = "无筛选时用 v_erp_monthly / v_pos_monthly 的 Y26 1–10 月汇总反查"

    return _envelope(
        {"erp": erp, "pos": pos, "year": config.FIXED_YEAR, "year_int": config.FIXED_YEAR_INT,
         "period": config.PERIOD_LABEL, "latest_month": LATEST_MONTH, "prev_month": PREV_MONTH,
         "region_label": f.region_label},
        f, [ERP_VIEW, POS_VIEW, "v_erp_monthly", "v_pos_monthly"],
        {"erp": e_app, "pos": p_app}, {"view_baseline": baseline})


# ---------------------------------------------------------------- 图表 1 月度趋势（Y26 单序列 + 环比）
def trend(conn: sqlite3.Connection, f: filters.Filters) -> dict:
    mc = config.METRICS[f.metric]
    out: dict[str, Any] = {"metric": f.metric, "metric_label": mc["label"],
                           "months": list(range(1, 11)), "erp": [], "pos": [],
                           "latest_month": LATEST_MONTH, "prev_month": PREV_MONTH,
                           "applicability": {}}
    for side, key, col in (("erp", "erp", mc["erp"]), ("pos", "pos", mc["pos"])):
        view = ERP_VIEW if side == "erp" else POS_VIEW
        sql, params, app = filters.sql(
            f"SELECT month, SUM({col}) AS value, COUNT(*) AS n "
            f"FROM {view} WHERE {{where}} GROUP BY month ORDER BY month", f, side)
        rows = _rows(conn, sql, params)
        by_month = {r["month"]: r for r in rows}
        series: list[dict] = []
        prev = None
        for m in range(1, 11):
            hit = by_month.get(m)
            val = hit["value"] if hit else None
            series.append({"month": m, "value": val,
                           "mom_pct": _pct(val, prev) if val is not None and prev is not None else None,
                           "rows": hit["n"] if hit else 0})
            prev = val
        out[key] = series
        out["applicability"][key] = app
    out["latest_mom"] = {
        "erp": out["erp"][-1]["mom_pct"], "pos": out["pos"][-1]["mom_pct"],
    }
    return _envelope(out, f, [ERP_VIEW, POS_VIEW, "v_erp_monthly", "v_pos_monthly"],
                     out["applicability"])


# ---------------------------------------------------------------- 图表 2 五大战区
def region(conn: sqlite3.Connection, f: filters.Filters) -> dict:
    rows_pos = _by_dim(conn, f, "pos")
    rows_erp = _by_dim(conn, f, "erp")
    erp_map = {r["name"]: r for r in rows_erp}
    pos_total = sum((r["amount"] or 0) for r in rows_pos)
    erp_total = sum((r["amount"] or 0) for r in rows_erp)
    items = []
    for name in config.IN_SCOPE_REGIONS:
        if name not in f.regions:
            continue
        p = next((r for r in rows_pos if r["name"] == name), None)
        e = erp_map.get(name)
        items.append({
            "name": name,
            "pos_amount": (p or {}).get("amount"),
            "pos_boxes": (p or {}).get("boxes"),
            "pos_share_pct": _ratio((p or {}).get("amount"), pos_total or None),
            "erp_amount": (e or {}).get("amount"),
            "erp_boxes": (e or {}).get("boxes"),
            "erp_share_pct": _ratio((e or {}).get("amount"), erp_total or None),
            "rows": (p or {}).get("rows", 0) + (e or {}).get("rows", 0),
        })
    items.sort(key=lambda x: (x["pos_amount"] is None, -(x["pos_amount"] or 0)))
    for i, it in enumerate(items, 1):
        it["rank"] = i
    baseline = None
    if _is_baseline(f):
        baseline = _rows(conn,
            f"SELECT region_name_std AS name, SUM(sales_amount) AS amount "
            f"FROM v_pos_monthly WHERE year=2026 AND month<=10 "
            f"AND region_name_std IN ({','.join('?' * len(config.IN_SCOPE_REGIONS))}) "
            f"GROUP BY region_name_std ORDER BY amount DESC",
            list(config.IN_SCOPE_REGIONS))
    return _envelope({"items": items, "rank_by": "POS 终端销售额（Y26 1–10 月）",
                      "view_baseline": baseline},
                     f, [POS_VIEW, ERP_VIEW, "v_pos_monthly", "dim_region"])


def _by_dim(conn: sqlite3.Connection, f: filters.Filters, side: str) -> list[dict]:
    view = ERP_VIEW if side == "erp" else POS_VIEW
    sql, params, _ = filters.sql(
        "SELECT region_name_std AS name, SUM(sales_amount) AS amount, "
        "SUM(sales_boxes) AS boxes, COUNT(*) AS rows "
        "FROM " + view + " WHERE {where} GROUP BY region_name_std", f, side)
    return _rows(conn, sql, params)


# ---------------------------------------------------------------- 图表 3 渠道（保留接口，Y26 单年）
def channel(conn: sqlite3.Connection, f: filters.Filters) -> dict:
    e_sql, e_p, _ = filters.sql(
        "SELECT channel_type AS name, SUM(sales_amount) AS amount, "
        "SUM(sales_boxes) AS boxes, COUNT(*) AS rows "
        "FROM " + ERP_VIEW + " WHERE {where} GROUP BY channel_type", f, "erp")
    erp_rows = _rows(conn, e_sql, e_p)
    total = sum((r["amount"] or 0) for r in erp_rows)
    for r in erp_rows:
        r["share_pct"] = _ratio(r["amount"], total or None)
    erp_rows.sort(key=lambda x: -(x["amount"] or 0))

    p_sql, p_p, p_app = filters.sql(
        "SELECT channel_type AS name, SUM(sales_amount) AS amount, "
        "COUNT(*) AS rows FROM " + POS_VIEW + " WHERE {where} GROUP BY channel_type", f, "pos")
    pos_rows = _rows(conn, p_sql, p_p)
    online = [r[0] for r in conn.execute("SELECT DISTINCT online_offline FROM dim_channel")]
    return _envelope({
        "erp": erp_rows, "pos": pos_rows, "months": list(range(1, 11)),
        "online_offline": online,
        "online_offline_note": f"dim_channel.online_offline = {online}（未采集），不提供线上/线下筛选，不推断",
        "pos_coverage_note": "POS 仅覆盖「商超/终端」；选电商/批发时 POS 侧显示无数据，不按 0 计",
    }, f, [ERP_VIEW, POS_VIEW, "dim_channel", "v_pos_category_share"], {"pos": p_app})


# ---------------------------------------------------------------- 图表 4 品类（品类A / 品类B / 品类C）
def category(conn: sqlite3.Connection, f: filters.Filters) -> dict:
    pos_rows = _category_rows(conn, f, "pos")
    erp_rows = _category_rows(conn, f, "erp")
    emap = {r["name"]: r for r in erp_rows}
    total = sum((r["amount"] or 0) for r in pos_rows)
    for r in pos_rows:
        r["share_pct"] = _ratio(r["amount"], total or None)
        e = emap.get(r["name"], {})
        r["erp_amount"] = e.get("amount")
    pos_rows.sort(key=lambda x: (x["amount"] is None, -(x["amount"] or 0)))
    baseline = _rows(conn, "SELECT year, category, share_pct FROM v_pos_category_share "
                           "WHERE year=2026", []) if _is_baseline(f) else None
    return _envelope({"items": pos_rows, "erp": erp_rows,
                      "view_baseline_share": baseline,
                      "share_note": "占比按五大战区 + Y26 1–10 月的分子分母同时限定后重算（G5）"},
                     f, [POS_VIEW, ERP_VIEW, "v_pos_category_share", "dim_sku"])


def _category_rows(conn, f, side):
    view = ERP_VIEW if side == "erp" else POS_VIEW
    sql, p, _ = filters.sql(
        "SELECT category AS name, SUM(sales_amount) AS amount, "
        "SUM(sales_boxes) AS boxes, COUNT(*) AS rows "
        "FROM " + view + " WHERE {where} GROUP BY category", f, side)
    return _rows(conn, sql, p)


# ---------------------------------------------------------------- 图表 5 SKU 排名 + 规格粒数结构
def sku(conn: sqlite3.Connection, f: filters.Filters) -> dict:
    sql, p, app = filters.sql(
        "SELECT sku_code AS name, MAX(sku_name) AS sku_name, "
        "MAX(pieces_per_box) AS pieces_per_box, MAX(specification) AS specification, "
        "MAX(category) AS category, "
        "SUM(sales_amount) AS amount, SUM(sales_boxes) AS boxes, "
        "SUM(sales_te) AS te, COUNT(*) AS rows "
        "FROM " + POS_VIEW + " WHERE {where} GROUP BY sku_code", f, "pos")
    rows = _rows(conn, sql, p)
    rows.sort(key=lambda x: (x["amount"] is None, -(x["amount"] or 0)))
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    # 规格 / 粒数结构：仍只查 v_dash_pos 一张事实，按已物化的 sku 属性分组
    by_pieces = _structure(conn, f, "pieces_per_box")
    by_spec = _structure(conn, f, "specification")
    combos = _rows(conn, *filters.sql(
        "SELECT COUNT(*) AS n FROM (SELECT pieces_per_box, specification FROM " + POS_VIEW +
        " WHERE {where} GROUP BY pieces_per_box, specification)", f, "pos")[0:2])
    _decorate_share(by_pieces)
    _decorate_share(by_spec)

    drilldown = None
    if f.sku:
        d_sql, d_p, _ = filters.sql(
            "SELECT month, SUM(sales_amount) AS value FROM " + POS_VIEW +
            " WHERE {where} GROUP BY month ORDER BY month", f, "pos")
        dd = _rows(conn, d_sql, d_p)
        by_month = {r["month"]: r for r in dd}
        monthly, prev = [], None
        for m in range(1, 11):
            v = (by_month.get(m) or {}).get("value")
            monthly.append({"month": m, "value": v,
                            "mom_pct": _pct(v, prev) if v is not None and prev is not None else None})
            prev = v
        drilldown = {"sku_code": f.sku, "detail": rows[0] if rows else None, "monthly": monthly}
    baseline = None
    if _is_baseline(f):
        baseline = _rows(conn, "SELECT year, sku_code, rank_by_amount FROM v_pos_sku_rank "
                               "WHERE rank_by_amount<=10 AND year=2026", [])
    return _envelope({
        "items": rows[:f.limit], "total_skus": len(rows), "rank_by": "POS 终端销售额（Y26 1–10 月）",
        "drilldown": drilldown, "view_baseline_rank": baseline,
        "by_pieces": by_pieces, "by_spec": by_spec,
        "combos": combos[0]["n"] if combos else 0,
        "pieces_options": [r[0] for r in conn.execute(
            "SELECT DISTINCT pieces_per_box FROM dim_sku WHERE sku_key>0 "
            "ORDER BY pieces_per_box")],
        "note": "粒数 / 规格来自 dim_sku；未动销 SKU 不参与排名，不补零、不推断缺失组合",
    }, f, [POS_VIEW, "v_pos_sku_rank", "dim_sku"], {"pos": app})


def _structure(conn, f, dim):
    sql, p, _ = filters.sql(
        f"SELECT {dim} AS name, SUM(sales_amount) AS amount, SUM(sales_boxes) AS boxes, "
        f"COUNT(DISTINCT sku_code) AS skus, COUNT(*) AS rows "
        f"FROM {POS_VIEW} WHERE {{where}} AND {dim} IS NOT NULL GROUP BY {dim} "
        f"ORDER BY amount DESC", f, "pos")
    return _rows(conn, sql, p)


def _decorate_share(rows: list[dict]) -> None:
    total = sum((r["amount"] or 0) for r in rows)
    for r in rows:
        r["share_pct"] = _ratio(r["amount"], total or None)
        r["label"] = str(r["name"])


# ---------------------------------------------------------------- 图表 6 KA / 客户表现
def customer(conn: sqlite3.Connection, f: filters.Filters) -> dict:
    e_sql, e_p, e_app = filters.sql(
        "SELECT customer_id AS name, MAX(customer_name) AS label, "
        "SUM(sales_amount) AS amount, SUM(sales_boxes) AS boxes, "
        "SUM(gross_profit) AS gross_profit, COUNT(*) AS rows "
        "FROM " + ERP_VIEW + " WHERE {where} GROUP BY customer_id", f, "erp")
    erp = _rows(conn, e_sql, e_p)
    _decorate(erp)

    p_sql, p_p, p_app = filters.sql(
        "SELECT ka_system AS name, SUM(sales_amount) AS amount, "
        "SUM(sales_boxes) AS boxes, COUNT(*) AS rows "
        "FROM " + POS_VIEW + " WHERE {where} GROUP BY ka_system", f, "pos")
    pos = _rows(conn, p_sql, p_p)
    _decorate(pos)

    return _envelope({
        "erp_customers": erp[:f.limit], "erp_total": len(erp),
        "pos_ka": pos[:f.limit], "pos_total": len(pos),
        "rank_by": "Y26 1–10 月销售额",
        "note": "ERP 客户出货（Sell-in）与 POS KA 终端销售（Sell-out）分列，金额不相加；"
                "KA↔ERP 客户映射 PENDING，不猜测",
    }, f, [ERP_VIEW, POS_VIEW, "dim_customer", "dim_ka"],
        {"erp": e_app, "pos": p_app})


def _decorate(rows: list[dict]) -> None:
    rows.sort(key=lambda x: (x["amount"] is None, -(x["amount"] or 0)))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    total = sum((r["amount"] or 0) for r in rows)
    for r in rows:
        r["share_pct"] = _ratio(r["amount"], total or None)


# ---------------------------------------------------------------- 数据明细
def detail(conn: sqlite3.Connection, f: filters.Filters) -> dict:
    side = f.source
    view = ERP_VIEW if side == "erp" else POS_VIEW
    cols = ("year, month, full_date, region_name_std, sku_code, sku_name, category, "
            "channel_type, sales_boxes, sales_amount")
    if side == "erp":
        cols += ", customer_id, customer_name, shipment_te, cost_amount, gross_profit"
    else:
        cols += ", ka_system, store_name, sales_te"
    sql, params, app = filters.sql(
        f"SELECT {cols} FROM {view} WHERE {{where}} "
        f"ORDER BY full_date, sku_code LIMIT ? OFFSET ?", f, side)
    params.extend([f.limit, (f.page - 1) * f.limit])
    rows = _rows(conn, sql, params)

    c_sql, c_params, _ = filters.sql(
        f"SELECT COUNT(*) AS n FROM {view} WHERE {{where}}", f, side)
    total = _one(conn, c_sql, c_params).get("n", 0)
    return _envelope({"source": side, "rows": rows, "total": total,
                      "page": f.page, "page_size": f.limit,
                      "columns": [c.strip() for c in cols.split(",")]},
                     f, [view], {side: app})
