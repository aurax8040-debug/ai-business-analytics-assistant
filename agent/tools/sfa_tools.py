"""工具：query_sfa_visit —— 销售拜访执行与门店陈列证据。

数据源：``fact_sfa_visit``。

**口径红线**：SFA 是销售人员在某个拜访日填写的现场记录（陈列、库存、执行），
它既不是当日销售数据，也不是 ERP 出货；只能用来解释「终端执行有没有跟上」。
而且原始数据只覆盖 2026-09-28 ~ 2026-10-03 六个拜访日 —— 工具会把实际覆盖
的日期范围算出来放进 ``metrics.date_range``，避免把「三天执行情况」说成「整月」。
"""
from __future__ import annotations

import sqlite3

from . import registry
from ._common import envelope, pct, r2, resolve_period

NAME = "query_sfa_visit"
SOURCE = "fact_sfa_visit（visit_status / visit_complete / display_* / oos_*）"
CALIBER = ("拜访完成率 = 已完成拜访次数 ÷ 拜访次数（含空门店名的记录）；"
           "陈列执行率 = 有陈列位置记录的拜访 ÷ 拜访次数；"
           "SFA 是人员现场反馈，不是销售数据，不能换算成销量。")


def run(conn: sqlite3.Connection, region: str = "", department: str = "",
        year=None, months=None, ka_system: str = "", store: str = "",
        full_store_ranking: bool = False) -> dict:
    """SFA 拜访执行。

    ``full_store_ranking``（P9）：返回全部有拜访记录的门店，用于与 POS 做门店交集。
    默认仍只返回 Top 15，保持既有返回体积。
    """
    period = resolve_period(year, months)

    conds = ["region_scope_status = 'IN_SCOPE'"]
    params: list = []
    marks = ",".join("?" for _ in period["months"])
    conds.append(f"date_key / 100 IN ({marks})")     # YYYYMMDD / 100 = YYYYMM
    params += [period["year"] * 100 + m for m in period["months"]]
    if region:
        conds.append("region_name_std = ?")
        params.append(region)
    if department:
        conds.append("market_department = ?")
        params.append(department)
    if ka_system:
        conds.append("ka_system = ?")
        params.append(ka_system)
    if store:
        conds.append("(store_name = ? OR store_id = ?)")
        params += [store, store]
    clause = " AND ".join(conds)

    row = conn.execute(
        "SELECT COUNT(*), "
        "SUM(CASE WHEN visit_complete = '已完成' THEN 1 ELSE 0 END), "
        "COUNT(DISTINCT store_id), COUNT(DISTINCT employee_key), "
        "SUM(CASE WHEN IFNULL(display_location,'') <> '' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN IFNULL(online_sku_oos,'') <> '' AND online_sku_oos <> '0' "
        "         THEN 1 ELSE 0 END), "
        "MIN(visit_date), MAX(visit_date) "
        f"FROM fact_sfa_visit WHERE {clause}", params).fetchone()
    visits, done, stores, employees, display, oos, dmin, dmax = row

    if not visits:
        return envelope(
            NAME, args={"region": region, "department": department,
                        "year": period["year"], "months": period["months"]},
            period=period,
            scope={"region": region or "全国", "department": department or "全部"},
            data_source=SOURCE, caliber=CALIBER,
            metrics={"visits": 0, "stores": 0},
            notes=["该范围内没有 SFA 拜访记录。SFA 只覆盖 2026-09-28 ~ 2026-10-03，"
                   "其它月份本来就没有数据，不能据此判断执行好坏。"],
            tables=["fact_sfa_visit"])

    by_dept = [{"department": d, "visits": int(n), "stores": int(s),
                "completed_rate_pct": pct(c, n)}
               for d, n, s, c in conn.execute(
                   "SELECT market_department, COUNT(*), COUNT(DISTINCT store_id), "
                   "SUM(CASE WHEN visit_complete = '已完成' THEN 1 ELSE 0 END) "
                   f"FROM fact_sfa_visit WHERE {clause} GROUP BY 1 ORDER BY 2 DESC", params)]

    store_limit = "" if full_store_ranking else "LIMIT 15"
    by_store = [{"store_id": sid, "store": s, "department": d, "visits": int(n),
                 "last_visit": lv, "completed": int(c), "completed_rate_pct": pct(c, n)}
                for sid, s, d, n, lv, c in conn.execute(
                    "SELECT store_id, store_name, market_department, COUNT(*), "
                    "MAX(visit_date), "
                    "SUM(CASE WHEN visit_complete = '已完成' THEN 1 ELSE 0 END) "
                    f"FROM fact_sfa_visit WHERE {clause} "
                    f"GROUP BY 1,2,3 ORDER BY 4 DESC {store_limit}", params)]

    by_display = [{"display_location": loc, "visits": int(n)} for loc, n in conn.execute(
        "SELECT IFNULL(NULLIF(display_location,''),'未记录'), COUNT(*) "
        f"FROM fact_sfa_visit WHERE {clause} GROUP BY 1 ORDER BY 2 DESC", params)]

    by_form = [{"display_form": f, "visits": int(n)} for f, n in conn.execute(
        "SELECT IFNULL(NULLIF(display_form,''),'未记录'), COUNT(*) "
        f"FROM fact_sfa_visit WHERE {clause} GROUP BY 1 ORDER BY 2 DESC LIMIT 10", params)]

    cover_note = (f"SFA 实际覆盖 {dmin} ~ {dmax}，"
                  f"与所查期间（{period['label']}）不完全一致，执行率只代表这几天。")

    return envelope(
        NAME,
        args={"region": region, "department": department, "ka_system": ka_system,
              "store": store, "year": period["year"], "months": period["months"],
              "full_store_ranking": bool(full_store_ranking)},
        period=period,
        scope={"region": region or "全国", "department": department or "全部",
               "ka_system": ka_system or "全部", "store": store or "全部门店"},
        data_source=SOURCE, caliber=CALIBER,
        metrics={"visits": int(visits), "completed": int(done or 0),
                 "completed_rate_pct": pct(done, visits),
                 "stores": int(stores), "employees": int(employees or 0),
                 "display_recorded_rate_pct": pct(display, visits),
                 "oos_records": int(oos or 0),
                 "date_range": {"from": dmin, "to": dmax}},
        rows={"by_department": by_dept, "by_store": by_store,
              "by_display_location": by_display, "by_display_form": by_form},
        notes=[cover_note, "SFA 拜访记录不代表当日销量，也不等于 ERP 出货。"]
              + ([f"门店「{store}」无拜访记录，不代表未拜访——可能不在 SFA 覆盖范围内。"]
                 if store and not visits else []),
        tables=["fact_sfa_visit"],
    )


registry.register(
    NAME,
    "查询 SFA 销售拜访记录：拜访次数、完成率、覆盖门店与业代、陈列位置与形式分布。"
    "可选按 KA 系统/门店筛选；用于补充「终端执行」证据；它不是销售数据。",
    {"region": {"type": "string", "description": "战区：战区A/战区B/战区C/战区D/战区E"},
     "department": {"type": "string", "description": "市场部，如 北京、河北"},
     "ka_system": {"type": "string", "description": "KA 系统，如 KA客户A/KA客户C/KA客户B"},
     "store": {"type": "string", "description": "门店名称或门店 ID"},
     "year": {"type": "integer", "description": "年份，当前只支持 2026"},
     "months": {"type": "array", "items": {"type": "integer"},
                "description": "月份列表，如 [10]"},
     "full_store_ranking": {"type": "boolean",
                            "description": "true 时返回全部门店（用于门店交集），默认 false"}},
    required=["year"],          # 期间口径必须显式给出，不猜年份
    fn=run,
    source=SOURCE,
    returns="metrics.visits/completed_rate_pct/stores/date_range + 按市场部/门店(含store_id)/陈列分布",
)
