"""工具：query_budget —— 查询指定期间的销售预算 TE。

数据源：``budget_te_v1.db`` 的 ``fact_te_budget_monthly``（经 ``v_budget`` 视图，
与 BI 页面同源）。预算单位是**整数毫米 TE**，读出后 ÷1000 转 TE。

重要口径：
- 预算只下达到「客户 × 市场部 × 月」，**没有 SKU / 产地维度**，也没有金额；
- 因此本工具不接受品类/产地参数，不猜、不按比例倒推（分摊逻辑只属于页面口径）。
"""
from __future__ import annotations

import sqlite3

from . import registry
from ._common import (SEGMENT_SQL, ToolError, envelope, month_marks, r2,
                      resolve_period, scope_clause)

NAME = "query_budget"
SOURCE = "budget_te_v1.fact_te_budget_monthly.budget_te ÷ 1000（MOCK_BUDGET_TE，单位 TE）"
CALIBER = ("同期预算 TE = 该范围内「客户 × 市场部 × 月」预算之和；"
           "预算库只到月度粒度，无 SKU / 产地维度，也无法换算成金额。")


def run(conn: sqlite3.Connection, region: str = "", department: str = "",
        customer: str = "", year=None, months=None, segment: str = "") -> dict:
    period = resolve_period(year, months)
    try:
        conn.execute("SELECT 1 FROM v_budget LIMIT 1")
    except sqlite3.Error as exc:
        raise ToolError("BudgetUnavailable",
                        f"预算库未就绪：{exc}",
                        "确认 data/budget_te_v1.db 存在（这是独立只读库，缺失不影响 ERP 实际数）")

    # v_budget 没有 region_scope_status 列（预算已按五大战区下达），
    # 也不能套用事实表的过滤器，这里单独拼条件。
    where, args = ["1=1"], []
    if region:
        where.append("region_name = ?")
        args.append(region)
    if department:
        where.append("dept_short = ?")
        args.append(department)
    mwhere, margs = month_marks(period["months"])
    conds = ["year = ?", mwhere, *where]
    params: list = [period["year"], *margs, *args]
    if customer:
        conds.append("(customer_name = ? OR customer_id = ?)")
        params += [customer, customer]
    if segment:
        conds.append(f"({SEGMENT_SQL}) = ?")
        params.append(segment)
    clause = " AND ".join(c for c in conds if c)

    total = conn.execute(
        f"SELECT COALESCE(SUM(budget_te), 0) / 1000.0 FROM v_budget WHERE {clause}",
        params).fetchone()[0]

    by_month = [{"month": m, "budget_te": r2(v)} for m, v in conn.execute(
        f"SELECT month, SUM(budget_te) / 1000.0 FROM v_budget WHERE {clause} "
        f"GROUP BY month ORDER BY month", params)]

    by_dept = [{"department": d, "budget_te": r2(v)} for d, v in conn.execute(
        f"SELECT dept_short, SUM(budget_te) / 1000.0 FROM v_budget WHERE {clause} "
        f"GROUP BY dept_short ORDER BY 2 DESC", params)]

    by_segment = [{"segment": s, "label": _label(s), "budget_te": r2(v)}
                  for s, v in conn.execute(
                      f"SELECT {SEGMENT_SQL} AS seg, SUM(budget_te) / 1000.0 "
                      f"FROM v_budget WHERE {clause} GROUP BY seg ORDER BY 2 DESC", params)]

    by_customer = [{"customer_id": cid, "customer": cname, "budget_te": r2(v)}
                   for cid, cname, v in conn.execute(
                       f"SELECT customer_id, customer_name, SUM(budget_te) / 1000.0 "
                       f"FROM v_budget WHERE {clause} GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 30",
                       params)]

    return envelope(
        NAME,
        args={"region": region, "department": department, "customer": customer,
              "year": period["year"], "months": period["months"], "segment": segment},
        period=period,
        scope={"region": region or "全国（五大战区）", "department": department or "全部",
               "customer": customer or "全部客户", "segment": segment or "全部类型"},
        data_source=SOURCE, caliber=CALIBER,
        metrics={"budget_te": r2(total), "customers": len(by_customer),
                 "months_covered": len(by_month)},
        rows={"by_month": by_month, "by_department": by_dept,
              "by_segment": by_segment, "by_customer": by_customer},
        notes=["预算库为演示用模拟预算（MOCK_BUDGET_TE），不是真实业务预算。"],
        tables=["v_budget"],
    )


def _label(seg: str) -> str:
    from ._common import SEGMENT_LABEL
    return SEGMENT_LABEL.get(seg, seg)


registry.register(
    NAME,
    "查询指定期间（战区/市场部/客户/月份）的销售预算 TE。预算只有月度粒度，"
    "没有 SKU、产地和金额维度。",
    {"region": {"type": "string", "description": "战区：战区A/战区B/战区C/战区D/战区E"},
     "department": {"type": "string", "description": "市场部，如 北京、河北"},
     "customer": {"type": "string", "description": "客户名称或客户 ID"},
     "year": {"type": "integer", "description": "年份，当前只支持 2026"},
     "months": {"type": "array", "items": {"type": "integer"},
                "description": "月份列表，如 [10]；不传表示累计 1–10 月"},
     "segment": {"type": "string", "description": "客户类型 clubz/nka/lka/wholesale"}},
    required=["year"],          # 期间口径必须显式给出，不猜年份
    fn=run,
    source=SOURCE,
    returns="metrics.budget_te + 按月/按市场部/按客户类型/按客户拆分",
)
