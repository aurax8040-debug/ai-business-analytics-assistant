"""SFA 销售拜访分析 —— 查询层（阶段一）。

业务口径（本模块是唯一实现处，页面不重算）
----------------------------------------
- **有效拜访**：``visit_status = '已拜访'`` 且 ``store_id`` 非空且 ``visit_date`` 非空，
  且战区在五大战区白名单内。缺门店或缺失日期的记录不计入完成量。
- **实际完成门店数**：按「客户经理 × 自然月」对 ``store_id`` 去重后求和。
  该口径用于目标完成率，避免同一人一个月内多次拜访同一家店把完成量刷高。
- **覆盖门店数**：全公司口径对 ``store_id`` 去重（跨客户经理去重），单独展示，
  与完成量分列，避免「同一门店被两人拜访」被算两次。
- **重复拜访次数**：``Σ(拜访记录数 − 该客户经理该月去重门店数)``，
  即同客户经理、同门店、同自然月内第 2 次及以后。**不进入完成量**。
- **完成率** = 实际完成门店数 / 月度目标门店数；目标未配置或为 0 → ``None``（不参与计算）。
- 目标来自 ``data/app_config.db``，未配置的客户经理页面上显示「未配置」，
  既不按 0 计、也不按平均补，**本模块不生成任何默认目标**。
- **示例目标（DEMO）**：库里没有真实目标时，可由 ``/api/sfa/targets/seed-demo`` 生成一组
  显式标记为 ``DEMO`` 的目标用于演示。这类目标在接口里带 ``target_source='DEMO'``、
  ``kpi.target_is_demo=True``、``meta.target_status.demo``，页面必须标注，不得静默混入。
  手工录入 / CSV 导入的目标来源为 ``MANUAL`` / ``IMPORT``，优先级高于示例目标。

数据来源：``fact_sfa_visit`` / ``dim_employee`` / ``dim_store``（ETL V1，只读）
+ ``targets``（独立可写配置库）。全部 SQL 只查单表，不跨事实表 JOIN。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from . import board_filters, config, targets

BOARD = "sfa_visit"
TOP_EMPLOYEE = 15          # 客户经理排名展示条数
TOP_STORE = 10             # 下钻门店 / 拜访记录展示条数

NOTES = (
    "有效拜访 = 已拜访状态 + 门店编码齐全 + 日期齐全 + 五大战区内；缺门店或缺失日期的记录不计入完成量。",
    "实际完成门店数按「客户经理 × 自然月」去重；覆盖门店数按全公司去重，两者分列，不混用。",
    "重复拜访 = 同客户经理、同门店、同自然月内第 2 次及以后，单独统计，不增加完成量。",
    "月度目标来自独立配置库，未配置的客户经理显示「未配置」，不参与完成率分子分母。",
    "标记「示例」的目标为 DEMO 演示数据（按实际完成数×0.80–1.30 系数生成），非真实业务目标，可一键清除。",
    "SFA 底表仅覆盖 2026-09-28 ~ 2026-10-03 六个拜访日；「月度」为自然月归集，不代表整月完整执行。",
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


def _envelope(f: board_filters.BoardFilters, data, extra: dict | None = None,
              source_views: list[str] | None = None) -> dict:
    meta = {
        "board": BOARD,
        "filters": f.as_dict(),
        "region_label": f.region_label,
        "period": f"{config.FIXED_YEAR} · {f.month_label}",
        "year": config.FIXED_YEAR,
        "year_int": config.FIXED_YEAR_INT,
        "scope_note": config.SCOPE_NOTE,
        "source_views": source_views or ["fact_sfa_visit", "dim_employee", "dim_store"],
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "notes": list(NOTES),
        "target_store": targets.target_meta(),
    }
    if extra:
        meta.update(extra)
    return {"ok": True, "data": data, "meta": meta}


# ------------------------------------------------------------------ 花名册
def _roster(conn: sqlite3.Connection, f: board_filters.BoardFilters) -> list[dict]:
    """当前筛选下的客户经理花名册（来自 dim_employee，不来自拜访表）。

    用花名册而不是拜访记录做人员全集，才能把「一次都没拜访」的人也列出来。
    """
    clauses = ["employee_key > 0",
               "REPLACE(war_zone, '战区', '') IN (" + ",".join("?" * len(f.regions)) + ")"]
    params: list = list(f.regions)
    if f.market_department:
        clauses.append("market_department = ?")
        params.append(f.market_department)
    if f.employee:
        clauses.append("manager_virtual_id = ?")
        params.append(f.employee)
    sql = ("SELECT employee_key, manager_virtual_id AS employee_id, "
           "store_manager_name AS employee_name, war_zone, market_department, "
           "managed_store_count, REPLACE(war_zone, '战区', '') AS region_name "
           "FROM dim_employee WHERE " + " AND ".join(clauses) +
           " ORDER BY manager_virtual_id")
    return _rows(conn, sql, params)


def _visit_agg(conn: sqlite3.Connection, f: board_filters.BoardFilters) -> list[dict]:
    """按「客户经理 × 自然月」聚合有效拜访：记录数 + 去重门店数。"""
    clause, params = board_filters.where_sfa_visit(f)
    sql = (
        "SELECT v.manager_virtual_id AS employee_id, "
        "CAST(substr(v.visit_date, 6, 2) AS INTEGER) AS month, "
        "COUNT(*) AS visit_count, COUNT(DISTINCT v.store_id) AS store_count "
        "FROM fact_sfa_visit v WHERE " + clause + " GROUP BY 1, 2"
    )
    return _rows(conn, sql, params)


def _store_universe(conn: sqlite3.Connection, f: board_filters.BoardFilters) -> dict:
    """应拜访门店基数（来自 dim_store，按当前战区 / 市场部 / 客户经理 / KA 筛选）。"""
    clauses = ["employee_key > 0",
               "region_name_std IN (" + ",".join("?" * len(f.regions)) + ")"]
    params: list = list(f.regions)
    if f.market_department:
        clauses.append("market_department = ?")
        params.append(f.market_department)
    if f.employee:
        clauses.append("manager_virtual_id = ?")
        params.append(f.employee)
    if f.ka:
        clauses.append("ka_system = ?")
        params.append(f.ka)
    sql = "SELECT COUNT(*) AS stores FROM dim_store WHERE " + " AND ".join(clauses)
    return _one(conn, sql, params) or {"stores": 0}


def _coverage_actual(conn: sqlite3.Connection, f: board_filters.BoardFilters) -> dict:
    """全公司口径去重门店数（跨客户经理去重），与完成量分开统计。"""
    clause, params = board_filters.where_sfa_visit(f)
    sql = ("SELECT COUNT(DISTINCT v.store_id) AS covered_stores, "
           "COUNT(*) AS visits, COUNT(DISTINCT v.manager_virtual_id) AS active_employees "
           "FROM fact_sfa_visit v WHERE " + clause)
    return _one(conn, sql, params) or {}


def _own_portfolio(conn: sqlite3.Connection, f: board_filters.BoardFilters) -> dict[str, int]:
    """每位客户经理实际覆盖到的「自己名下门店」数量。

    走访别人名下的门店也会计入完成量（确实到店了），但不能算作自己对口门店的覆盖，
    否则会出现「覆盖率 119%」这种不可能的数字。事实表只 JOIN 维度表，不与其他事实表 JOIN。
    """
    clause, params = board_filters.where_sfa_visit(f)
    sql = ("SELECT v.manager_virtual_id AS employee_id, COUNT(DISTINCT v.store_id) AS own_covered "
           "FROM fact_sfa_visit v "
           "JOIN dim_store ds ON ds.store_id = v.store_id "
           "AND ds.manager_virtual_id = v.manager_virtual_id "
           "WHERE " + clause + " GROUP BY 1")
    return {r["employee_id"]: r["own_covered"] for r in _rows(conn, sql, params)}


# ------------------------------------------------------------------ 目标拼装
def _month_target_maps(f: board_filters.BoardFilters, months: list[int]) -> dict:
    """一次批量取「月份 → {客户经理 → 目标/来源}」，供月度汇总与趋势共用。"""
    return targets.targets_map_for_months(f.year, months)


def _targets_by_employee(f: board_filters.BoardFilters) -> tuple[dict, list[int], dict]:
    """取所选月份的目标：返回 ({employee_id: {target, months, source}}, months, 月度映射)。

    ``source`` 用于页面标注「示例目标（DEMO）」：多月混合时记为 ``MIXED``，
    只要含示例数据就一定会被标注出来，不会伪装成真实目标。
    """
    months = list(f.months) or list(range(config.COMPARABLE_FIRST_MONTH,
                                          config.COMPARABLE_LAST_MONTH + 1))
    month_maps = _month_target_maps(f, months)
    agg: dict[str, dict] = {}
    for month in months:
        for employee_id, info in (month_maps.get(month) or {}).items():
            slot = agg.setdefault(employee_id, {"target": 0, "months": [], "sources": set()})
            slot["target"] += int(info["target"])
            slot["months"].append(month)
            slot["sources"].add(info["source"])
    # 目标为 0 视同未配置（业务规则：目标为 0 不计算完成率）
    for employee_id in [k for k, v in agg.items() if v["target"] <= 0]:
        agg.pop(employee_id)
    for slot in agg.values():
        sources = slot.pop("sources")
        slot["source"] = ("DEMO" if sources == {"DEMO"}
                          else "MIXED" if "DEMO" in sources else "MANUAL")
    return agg, months, month_maps


# ------------------------------------------------------------------ 主查询
def analysis(conn: sqlite3.Connection, f: board_filters.BoardFilters) -> dict:
    roster = _roster(conn, f)
    visit_rows = _visit_agg(conn, f)
    universe = _store_universe(conn, f).get("stores") or 0
    cov = _coverage_actual(conn, f)
    own = _own_portfolio(conn, f)
    target_map, months, month_targets = _targets_by_employee(f)

    by_employee: dict[str, dict] = {}
    for row in roster:
        eid = row["employee_id"]
        by_employee[eid] = {
            "employee_id": eid,
            "employee_name": row["employee_name"],
            "region_name": row["region_name"],
            "war_zone": row["war_zone"],
            "market_department": row["market_department"],
            "managed_store_count": row["managed_store_count"] or 0,
            "visit_count": 0,
            "visited_store_count": 0,
            "covered_stores_company": 0,
            "months_active": 0,
        }

    # 只有出现在花名册里的人才进入排名；拜访表里出现但不在花名册的（理论上不存在）单独计数
    orphan_visits = 0
    monthly: dict[int, dict] = {}
    for row in visit_rows:
        eid, month = row["employee_id"], int(row["month"])
        slot = by_employee.get(eid)
        if slot is None:
            orphan_visits += row["visit_count"]
            continue
        slot["visit_count"] += row["visit_count"]
        slot["visited_store_count"] += row["store_count"]
        slot["months_active"] += 1
        bucket = monthly.setdefault(month, {"month": month, "visit_count": 0,
                                            "visited_store_count": 0})
        bucket["visit_count"] += row["visit_count"]
        bucket["visited_store_count"] += row["store_count"]

    # 目标 + 完成率 + 重复拜访
    for eid, slot in by_employee.items():
        tinfo = target_map.get(eid)
        slot["target_store_count"] = tinfo["target"] if tinfo else None
        slot["target_months"] = tinfo["months"] if tinfo else []
        slot["target_source"] = tinfo["source"] if tinfo else None
        slot["configured"] = tinfo is not None
        slot["completion_rate"] = _ratio(slot["visited_store_count"], slot["target_store_count"])
        slot["gap"] = (slot["target_store_count"] - slot["visited_store_count"]) \
            if tinfo else None
        slot["repeat_visit_count"] = slot["visit_count"] - slot["visited_store_count"]
        slot["own_covered"] = own.get(eid, 0)
        # 覆盖率只用「自己名下门店」，避免走访他人门店把覆盖率刷到 100% 以上
        slot["coverage_rate"] = _ratio(slot["own_covered"], slot["managed_store_count"])
        slot["status"] = (
            "未配置" if not tinfo else
            ("已达标" if slot["completion_rate"] is not None and slot["completion_rate"] >= 100
             else "未达标"))

    employees = sorted(by_employee.values(),
                       key=lambda x: (x["completion_rate"] is None, -(x["completion_rate"] or 0),
                                      -x["visited_store_count"]))

    configured = [e for e in employees if e["configured"]]
    demo_configured = [e for e in configured if e["target_source"] in ("DEMO", "MIXED")]
    target_total = sum(e["target_store_count"] for e in configured)
    actual_configured = sum(e["visited_store_count"] for e in configured)
    actual_total = sum(e["visited_store_count"] for e in employees)
    visits_total = sum(e["visit_count"] for e in employees)
    repeat_total = sum(e["repeat_visit_count"] for e in employees)

    covered = cov.get("covered_stores") or 0
    kpi = {
        "employee_total": len(employees),
        "employee_configured": len(configured),
        "employee_unconfigured": len(employees) - len(configured),
        "employee_demo_target": len(demo_configured),
        "target_is_demo": len(demo_configured) > 0,
        "employee_active": cov.get("active_employees") or 0,
        "target_store_total": target_total,
        "actual_store_total": actual_total,
        "actual_store_configured": actual_configured,
        "completion_rate": _ratio(actual_configured, target_total),
        "employee_reached": len([e for e in configured
                                 if (e["completion_rate"] or 0) >= 100]),
        "employee_missed": len([e for e in configured
                                if (e["completion_rate"] or 0) < 100]),
        "visit_count": visits_total,
        "repeat_visit_count": repeat_total,
        "covered_stores": covered,
        "universe_stores": universe,
        "unvisited_stores": max(0, universe - covered),
        "coverage_rate": _ratio(covered, universe),
        "months_active": len([m for m in monthly.values() if m["visit_count"]]),
        "orphan_visits": orphan_visits,
        "target_total_note": (
            ("当前目标含示例数据（DEMO）：完成率仅用于演示展示逻辑，不代表真实目标达成情况"
             if demo_configured else
             "已完成目标配置；完成率 = 已配置客户经理的实际完成合计 / 目标合计")
            if configured else
            "当前筛选下全部客户经理均未配置月度目标，完成率不可计算（不按 0 计）"),
    }

    # ---- 战区汇总（沿用五大战区固定骨架，无记录的战区也出现，值为 None）
    region_roll: dict[str, dict] = {}
    for e in employees:
        slot = region_roll.setdefault(e["region_name"], {
            "region_name": e["region_name"], "employee_total": 0, "employee_configured": 0,
            "target_store_count": 0, "actual_store_count": 0, "visit_count": 0,
            "repeat_visit_count": 0, "managed_store_count": 0,
        })
        slot["employee_total"] += 1
        slot["visit_count"] += e["visit_count"]
        slot["repeat_visit_count"] += e["repeat_visit_count"]
        slot["actual_store_count"] += e["visited_store_count"]
        slot["managed_store_count"] += e["managed_store_count"]
        slot["own_covered"] = slot.get("own_covered", 0) + e["own_covered"]
        if e["configured"]:
            slot["employee_configured"] += 1
            slot["target_store_count"] += e["target_store_count"]
            # 完成率的分子只取「已配置目标」的人，否则会拿全员实际去除少数人的目标
            slot["actual_configured"] = slot.get("actual_configured", 0) \
                + e["visited_store_count"]
    regions = []
    for name in f.regions:
        slot = region_roll.get(name, {
            "region_name": name, "employee_total": 0, "employee_configured": 0,
            "target_store_count": 0, "actual_store_count": 0, "visit_count": 0,
            "repeat_visit_count": 0, "managed_store_count": 0, "own_covered": 0,
            "actual_configured": 0,
        })
        slot.setdefault("actual_configured", 0)
        slot["completion_rate"] = _ratio(slot["actual_configured"], slot["target_store_count"])
        slot["coverage_rate"] = _ratio(slot["own_covered"], slot["managed_store_count"])
        regions.append(slot)

    # ---- 月度趋势
    # 只输出「有拜访或有目标」的月份：既没有拜访也没有目标的月份直接不出现，
    # 不用 0 占位（0 会被误读成「该月拜访了 0 家」，而事实是根本没有数据窗口）。
    trend = []
    target_months = {m for info in target_map.values() for m in info["months"]}
    for month in sorted(set(monthly) | target_months):
        bucket = monthly.get(month, {"visit_count": 0, "visited_store_count": 0})
        # 逐月目标必须按单月重新取，不能把多月合计摊到每个月；
        # 月度映射已在上面一次性取好，这里只做查表，不再按月开库。
        month_map = month_targets.get(month) or {}
        month_target = sum(month_map.get(e["employee_id"], {}).get("target", 0)
                           for e in employees if e["employee_id"] in target_map)
        trend.append({
            "month": month,
            "label": f"{month} 月",
            "target_store_count": month_target or None,
            "visited_store_count": bucket["visited_store_count"] or None,
            "visit_count": bucket["visit_count"],
            "has_target": bool(month_target),
            "has_visit": bool(bucket["visit_count"]),
        })

    rank_items = [{
        "employee_id": e["employee_id"], "employee_name": e["employee_name"],
        "region_name": e["region_name"], "market_department": e["market_department"],
        "target_store_count": e["target_store_count"],
        "target_source": e["target_source"],
        "visited_store_count": e["visited_store_count"],
        "visit_count": e["visit_count"],
        "completion_rate": e["completion_rate"],
        "repeat_visit_count": e["repeat_visit_count"],
        "status": e["status"],
    } for e in employees[:TOP_EMPLOYEE]]

    data = {
        "kpi": kpi,
        "regions": regions,
        "employees": employees,
        "employee_rank": rank_items,
        "trend": trend,
        "coverage": {
            "universe_stores": universe,
            "covered_stores": covered,
            "unvisited_stores": max(0, universe - covered),
            "coverage_rate": _ratio(covered, universe),
            "repeat_visit_count": repeat_total,
            "visit_count": visits_total,
            "dedup_visit_count": visits_total - repeat_total,
        },
    }
    extra = {
        "target_status": {
            "configured": len(configured),
            "unconfigured": len(employees) - len(configured),
            "demo": len(demo_configured),
            "db": targets.target_meta(),
        },
        "coverage_window": _coverage_window(conn, f),
    }
    return _envelope(f, data, extra)


def _coverage_window(conn: sqlite3.Connection, f: board_filters.BoardFilters) -> dict:
    """SFA 底表实际覆盖的日期窗口 —— 月度趋势只有两个月时，页面必须说明原因。"""
    clause, params = board_filters.where_sfa_visit(f)
    row = _one(conn, "SELECT MIN(v.visit_date) AS first_date, MAX(v.visit_date) AS last_date, "
                     "COUNT(DISTINCT v.visit_date) AS days FROM fact_sfa_visit v WHERE " + clause,
               params)
    return {
        "first_date": row.get("first_date"), "last_date": row.get("last_date"),
        "days": row.get("days") or 0,
        "note": "SFA 底表为真实拜访流水，仅覆盖上述窗口；不足整月时趋势图按月归集，不外推、不补零。",
    }


# ------------------------------------------------------------------ 明细
DETAIL_COLUMNS = ("employee_id", "employee_name", "region_name", "market_department",
                  "target_store_count", "target_source", "visited_store_count",
                  "completion_rate", "gap", "repeat_visit_count", "coverage_rate",
                  "visit_count", "managed_store_count", "status")

_SEARCH_FIELDS = ("employee_id", "employee_name", "region_name", "market_department")


def detail(conn: sqlite3.Connection, f: board_filters.BoardFilters) -> dict:
    """明细表：服务端排序 / 搜索 / 分页，数据与图表同源（同一套聚合结果）。"""
    payload = analysis(conn, f)
    rows = payload["data"]["employees"]

    if f.q:
        needle = f.q.lower()
        rows = [r for r in rows
                if any(needle in str(r.get(k) or "").lower() for k in _SEARCH_FIELDS)]

    if f.sort:
        def key(row):
            value = row.get(f.sort)
            if isinstance(value, str):
                return (value is None, value)
            return (value is None, value if value is not None else 0)
        rows = sorted(rows, key=key, reverse=(f.order == "desc"))
    else:
        rows = sorted(rows, key=lambda r: (r["completion_rate"] is None,
                                           -(r["completion_rate"] or 0),
                                           -r["visited_store_count"]))

    total = len(rows)
    start = (f.page - 1) * f.limit
    page_rows = rows[start:start + f.limit]

    out = _envelope(f, {
        "rows": page_rows,
        "columns": list(DETAIL_COLUMNS),
        "total": total, "page": f.page, "limit": f.limit,
        "pages": max(1, (total + f.limit - 1) // f.limit),
    })
    out["meta"]["sort"] = {"column": f.sort, "order": f.order, "q": f.q}
    return out


def drill(conn: sqlite3.Connection, f: board_filters.BoardFilters, employee_id: str) -> dict:
    """客户经理下钻：负责门店 + 拜访记录。

    门店来自 dim_store（该经理名下），拜访记录来自 fact_sfa_visit（有效拜访口径）。
    两者分别查各自的表，不在 SQL 里 JOIN 事实表。
    """
    employee_id = (employee_id or "").strip()
    if not employee_id:
        raise board_filters.BoardFilterError("employee 不能为空")

    stores = _rows(conn,
        "SELECT store_id, store_name, ka_system, store_level, store_type, province, city, "
        "market_department, region_name_std, pos_link_status FROM dim_store "
        "WHERE manager_virtual_id = ? AND region_name_std IN ("
        + ",".join("?" * len(f.regions)) + ") ORDER BY store_id LIMIT ?",
        [employee_id, *f.regions, TOP_STORE * 5])

    clause, params = board_filters.where_sfa_visit(f)
    clause += " AND v.manager_virtual_id = ?"
    params.append(employee_id)
    visits = _rows(conn,
        "SELECT v.visit_id, v.visit_date, v.store_id, v.store_name, v.ka_system, "
        "v.store_level, v.actual_arrival_time, v.visit_status, v.visit_complete, "
        "v.focus_product_name, v.focus_retail_price, v.focus_sku_code, "
        "v.display_location, v.posm_display, v.quality_flag "
        "FROM fact_sfa_visit v WHERE " + clause +
        " ORDER BY v.visit_date DESC, v.visit_id LIMIT ?",
        [*params, TOP_STORE * 20])

    summary = _one(conn,
        "SELECT COUNT(*) AS visit_count, COUNT(DISTINCT v.store_id) AS store_count, "
        "MIN(v.visit_date) AS first_date, MAX(v.visit_date) AS last_date "
        "FROM fact_sfa_visit v WHERE " + clause, params)

    tinfo = {}
    demo_months = set()
    month_list = [int(m) for m in (f.months or range(config.COMPARABLE_FIRST_MONTH,
                                                     config.COMPARABLE_LAST_MONTH + 1))]
    month_targets = targets.targets_map_for_months(f.year, month_list)
    for month in month_list:
        info = (month_targets.get(month) or {}).get(employee_id)
        if info and info["target"]:
            tinfo[month] = info["target"]
            if info["source"] == "DEMO":
                demo_months.add(month)
    target_sum = sum(tinfo.values()) if tinfo else None

    out = _envelope(f, {
        "employee_id": employee_id,
        "stores": stores,
        "visits": visits,
        "summary": {
            **summary,
            "managed_store_count": len(stores),
            "target_store_count": target_sum,
            "target_by_month": tinfo,
            "target_source": ("DEMO" if tinfo and len(demo_months) == len(tinfo)
                              else "MIXED" if demo_months else ("MANUAL" if tinfo else None)),
            "completion_rate": _ratio(summary.get("store_count"), target_sum),
        },
    })
    out["meta"]["drill"] = employee_id
    return out


def meta_options(conn: sqlite3.Connection, f: board_filters.BoardFilters) -> dict:
    """前端筛选器选项：只回该看板用得到的下拉项（战区 / 市场部 / 客户经理 / 月份）。"""
    md_rows = _rows(conn,
        "SELECT DISTINCT market_department FROM dim_employee WHERE employee_key>0 "
        "AND market_department IS NOT NULL AND market_department <> 'UNKNOWN' "
        "AND REPLACE(war_zone,'战区','') IN (" + ",".join("?" * len(f.regions)) + ") "
        "ORDER BY market_department", list(f.regions))
    emp_rows = _rows(conn,
        "SELECT manager_virtual_id AS employee_id, store_manager_name AS employee_name, "
        "REPLACE(war_zone,'战区','') AS region_name, market_department, managed_store_count "
        "FROM dim_employee WHERE employee_key>0 AND REPLACE(war_zone,'战区','') IN ("
        + ",".join("?" * len(f.regions)) + ") ORDER BY manager_virtual_id", list(f.regions))
    month_rows = _rows(conn,
        "SELECT DISTINCT CAST(substr(v.visit_date,6,2) AS INTEGER) AS month "
        "FROM fact_sfa_visit v WHERE v.visit_date IS NOT NULL "
        "AND v.region_name_std IN (" + ",".join("?" * len(f.regions)) + ") ORDER BY month",
        list(f.regions))
    periods = targets.list_targets(conn)
    return {
        "ok": True,
        "data": {
            "regions": list(f.regions),
            "market_departments": [r["market_department"] for r in md_rows],
            "employees": emp_rows,
            "months": [r["month"] for r in month_rows if r["month"]],
            "target_summary": periods["summary"],
            "target_registry": periods["registry"],
            "target_rows": periods["rows"],
        },
        "meta": {"board": BOARD, "generated_at":
                 datetime.now().astimezone().isoformat(timespec="seconds")},
    }
