"""工具：query_customer_performance —— 客户维度的预算 vs 实际（含缺口排序）。

这是第一个演示场景的核心工具：先算清「谁没达成、差多少、占整体缺口多少」，
再交给 LLM 写原因——原因可以推测，缺口不能推测。

实现要点：
- 实际与预算**分别聚合后在 Python 里按 customer_id 对齐**，不写跨库 JOIN；
  预算库和 ERP 库是物理分离的两个文件，硬 JOIN 只会把「口径」问题变成「SQL」问题。
- 缺口 = 实际 − 预算（负数 = 未达成）；贡献 = 该客户缺口 ÷ 范围内缺口合计。
- 缺失一侧时（有实际无预算 / 有预算无实际）如实标 ``budget_missing`` / ``actual_missing``，
  达成率返回 None，不补 0。
"""
from __future__ import annotations

import sqlite3

from . import registry
from ._common import (SEGMENT_LABEL, SEGMENT_SQL, ToolError, envelope, month_marks, pct,
                      r2, resolve_period, scope_clause, yoy)

NAME = "query_customer_performance"
SOURCE = ("fact_erp_shipment（实际）+ budget_te_v1.fact_te_budget_monthly（预算），"
          "按 customer_id 在应用层对齐")
CALIBER = ("客户达成率 = 该客户实际 TE ÷ 该客户同期预算 TE；"
           "缺口 = 实际 − 预算（负数为未达成）；"
           "缺口贡献 = 该客户缺口 ÷ 当前范围内全部客户缺口合计。")


def run(conn: sqlite3.Connection, region: str = "", department: str = "",
        customer: str = "", year=None, months=None, segment: str = "",
        top_n: int = 10) -> dict:
    period = resolve_period(year, months)
    top_n = int(top_n or 10)

    where, args = scope_clause(region, department, "erp")
    mwhere, margs = month_marks(period["months"])

    # ---- 实际（含去年同期）
    act_conds = ["year = ?", mwhere, where]
    act_params: list = [period["year"], *margs, *args]
    if customer:
        act_conds.append("(customer_id = ? OR customer_short = ?)")
        act_params += [customer, customer]
    if segment:
        act_conds.append(f"({SEGMENT_SQL}) = ?")
        act_params.append(segment)
    act_clause = " AND ".join(c for c in act_conds if c)

    actual: dict[str, dict] = {}
    for cid, name, seg, te, amt, gp in conn.execute(
            f"SELECT customer_id, MAX(customer_short), {SEGMENT_SQL} AS seg, "
            f"SUM(shipment_te), SUM(sales_amount), SUM(gross_profit) "
            f"FROM a_erp WHERE {act_clause} GROUP BY customer_id, seg", act_params):
        actual[cid] = {"customer_id": cid, "customer": name or cid, "segment": seg,
                       "te": r2(te), "amount": r2(amt), "gross_profit": r2(gp)}

    # 去年同期：条件与本年完全相同，只换年份（同期口径）
    last_conds = list(act_conds)
    last_params = [period["year"] - 1, *act_params[1:]]
    last: dict[str, float] = {}
    for cid, te in conn.execute(
            f"SELECT customer_id, SUM(shipment_te) FROM a_erp WHERE "
            f"{' AND '.join(c for c in last_conds if c)} GROUP BY 1", last_params):
        last[cid] = float(te or 0)

    # ---- 预算（v_budget 无 region_scope_status，单独拼条件）
    bud_conds = ["year = ?", mwhere, "1=1"]
    bud_params: list = [period["year"], *margs]
    if region:
        bud_conds.append("region_name = ?")
        bud_params.append(region)
    if department:
        bud_conds.append("dept_short = ?")
        bud_params.append(department)
    if customer:
        bud_conds.append("(customer_name = ? OR customer_id = ?)")
        bud_params += [customer, customer]
    if segment:
        bud_conds.append(f"({SEGMENT_SQL}) = ?")
        bud_params.append(segment)
    budget: dict[str, dict] = {}
    for cid, name, seg, te in conn.execute(
            f"SELECT customer_id, MAX(customer_name), {SEGMENT_SQL} AS seg, "
            f"SUM(budget_te) / 1000.0 FROM v_budget WHERE "
            f"{' AND '.join(bud_conds)} GROUP BY customer_id, seg", bud_params):
        budget[cid] = {"customer": name or cid, "segment": seg, "budget_te": r2(te)}

    # ---- 合并
    rows: list[dict] = []
    for cid in sorted(set(actual) | set(budget)):
        a, b = actual.get(cid), budget.get(cid)
        te = a["te"] if a else None
        bud = b["budget_te"] if b else None
        gap = None if (te is None or bud is None) else round(te - bud, 2)
        rows.append({
            "customer_id": cid,
            "customer": (b or a)["customer"],
            "segment": (a or b)["segment"],
            "segment_label": SEGMENT_LABEL.get((a or b)["segment"], ""),
            "te": te,
            "budget_te": bud,
            "attainment_pct": pct(te, bud),
            "gap_te": gap,
            "last_te": r2(last.get(cid)) if cid in last else None,
            "yoy_pct": yoy(te, last.get(cid)) if cid in last else None,
            "amount": a["amount"] if a else None,
            "gross_profit": a["gross_profit"] if a else None,
            "budget_missing": bud is None,
            "actual_missing": te is None,
        })

    # 合计回归「单条 SQL 直接汇总」，不用客户行相加：行值做过 2 位小数四舍五入，
    # 相加会产生 0.01 级别误差，而对外报的全国/战区合计必须与 BI 页面逐分一致。
    independent = conn.execute(
        f"SELECT COALESCE(SUM(shipment_te),0) FROM a_erp WHERE {act_clause}",
        act_params).fetchone()[0]
    budget_total = conn.execute(
        f"SELECT COALESCE(SUM(budget_te),0) / 1000.0 FROM v_budget WHERE "
        f"{' AND '.join(bud_conds)}", bud_params).fetchone()[0]

    total_te = r2(independent)
    total_bud = r2(budget_total)
    total_gap = None if (total_te is None or total_bud is None) else round(total_te - total_bud, 2)
    unmet = [r for r in rows if r["gap_te"] is not None and r["gap_te"] < 0
             and r["budget_te"]]
    unmet.sort(key=lambda r: r["gap_te"])
    met = [r for r in rows if r["gap_te"] is not None and r["gap_te"] > 0]
    met.sort(key=lambda r: -r["gap_te"])

    # 缺口贡献的分母用「未达成客户的缺口绝对值之和」，不用全体客户的绝对值和：
    # 区域整体超额时，正缺口会把分母撑大，让真正的缺口客户看起来只有 4%（假象）。
    gap_pool = round(sum(abs(r["gap_te"]) for r in unmet), 2)
    for r in rows:
        r["gap_share_pct"] = (round(abs(r["gap_te"]) / gap_pool * 100, 4)
                              if r["gap_te"] and r["gap_te"] < 0 and gap_pool else None)
        # 缺口 ÷ 预算：回答「这个缺口相当于多少预算」，与整体是否超额无关，可跨期比较
        r["gap_vs_budget_pct"] = (round(r["gap_te"] / r["budget_te"] * 100, 4)
                                  if r["gap_te"] and r["budget_te"] else None)

    # 必须能对上：客户合计 = 范围内实际合计（否则说明漏了客户或重复计数）
    reconciliation = {
        "customer_sum_te": round(sum(r["te"] or 0 for r in rows), 2),
        "direct_sum_te": total_te,
        "diff": round(sum(r["te"] or 0 for r in rows) - (total_te or 0), 2),
        "ok": abs(sum(r["te"] or 0 for r in rows) - (total_te or 0)) < 0.05,
        "customers": len(rows),
    }

    return envelope(
        NAME,
        args={"region": region, "department": department, "customer": customer,
              "year": period["year"], "months": period["months"],
              "segment": segment, "top_n": top_n},
        period=period,
        scope={"region": region or "全国（五大战区）", "department": department or "全部",
               "customer": customer or "全部客户", "segment": segment or "全部类型"},
        data_source=SOURCE, caliber=CALIBER,
        metrics={"te": total_te, "budget_te": total_bud,
                 "attainment_pct": pct(total_te, total_bud),
                 "gap_te": total_gap,
                 "unmet_customers": len(unmet), "met_customers": len(met),
                 "budget_missing_customers": sum(1 for r in rows if r["budget_missing"]),
                 "actual_missing_customers": sum(1 for r in rows if r["actual_missing"])},
        rows={"by_customer": sorted(rows, key=lambda r: (r["gap_te"] is None,
                                                         r["gap_te"] or 0))[:50],
              "top_gap": unmet[:top_n],
              "top_over": met[:top_n],
              "reconciliation": reconciliation},
        notes=["预算只下达到客户×市场部×月，客户层缺口可加总回战区/全国，不做二次去重。"],
        tables=["a_erp", "v_budget"],
    )


registry.register(
    NAME,
    "按客户查询预算达成：每个客户的 TE、预算、达成率、缺口，并按缺口从大到小排序，"
    "同时给出整体缺口、未达成客户数与累计贡献。用于回答「谁没达成、差多少」。",
    {"region": {"type": "string", "description": "战区：战区A/战区B/战区C/战区D/战区E"},
     "department": {"type": "string", "description": "市场部，如 北京、河北"},
     "customer": {"type": "string", "description": "只看某个客户（名称或 ID）"},
     "year": {"type": "integer", "description": "年份，当前只支持 2026"},
     "months": {"type": "array", "items": {"type": "integer"},
                "description": "月份列表，如 [10]；不传表示累计 1–10 月"},
     "segment": {"type": "string", "description": "客户类型 clubz/nka/lka/wholesale"},
     "top_n": {"type": "integer", "description": "缺口 Top N，默认 10，范围 1–50"}},
    required=["year"],          # 期间口径必须显式给出，不猜年份
    fn=run,
    source=SOURCE,
    returns="metrics(合计) + rows.by_customer / top_gap / top_over / reconciliation",
)
