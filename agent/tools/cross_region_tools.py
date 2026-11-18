"""跨战区汇总指标与脱敏最佳实践工具。

只返回战区/市场部/SKU 汇总，不返回客户、门店、订单或人员标识。
"""
from __future__ import annotations

import sqlite3

from . import registry
from ._common import SEGMENT_SQL, envelope, month_marks, pct, r2, resolve_period

NAME = "query_cross_region_practices"
SOURCE = ("v_budget（MOCK_BUDGET_TE）+ a_erp（ERP Sell-in）+ "
          "fact_pos_sales（POS Sell-out，T+1）")
CALIBER = (
    "预算与 ERP 实际按相同战区/市场部/月份汇总，达成率=实际TE÷预算TE；"
    "SKU增长按POS本期与上年同期盒数比较；价格为同SKU跨战区元/盒汇总；"
    "所有结果仅到战区、市场部或SKU汇总粒度，不含客户和门店敏感明细。")
SCENARIOS = {"DIAGNOSIS", "OPPORTUNITY", "PRICE", "SUPPLY_CHAIN",
             "PROCUREMENT", "SIMULATION"}


def _region_benchmarks(conn: sqlite3.Connection, year: int, months: list[int]) -> list[dict]:
    marks, month_args = month_marks(months)
    actual = {region: (float(te or 0), float(amount or 0))
              for region, te, amount in conn.execute(
                  "SELECT region_name_std,SUM(shipment_te),SUM(sales_amount) FROM a_erp "
                  f"WHERE region_scope_status='IN_SCOPE' AND year=? AND {marks} "
                  "GROUP BY region_name_std", [year, *month_args])}
    budget = {region: float(value or 0) / 1000.0
              for region, value in conn.execute(
                  "SELECT region_name,SUM(budget_te) FROM v_budget "
                  f"WHERE year=? AND {marks} GROUP BY region_name", [year, *month_args])}
    rows = []
    for region in sorted(set(actual) | set(budget)):
        te, amount = actual.get(region, (0.0, 0.0))
        target = budget.get(region, 0.0)
        rows.append({"region": region, "actual_te": r2(te), "budget_te": r2(target),
                     "attainment_pct": pct(te, target), "gap_te": r2(te - target),
                     "sales_amount": r2(amount)})
    return sorted(rows, key=lambda row: (-(row.get("attainment_pct") or 0), row["region"]))


def _department_candidates(conn: sqlite3.Connection, year: int, months: list[int],
                           compare_region: str) -> list[dict]:
    marks, month_args = month_marks(months)
    region_filter = " AND region_name_std=?" if compare_region else ""
    params = [year, *month_args] + ([compare_region] if compare_region else [])
    actual = {(region, dept): {"actual_te": float(te or 0), "sales_amount": float(amount or 0)}
              for region, dept, te, amount in conn.execute(
                  "SELECT region_name_std,dept_short,SUM(shipment_te),SUM(sales_amount) "
                  "FROM a_erp WHERE region_scope_status='IN_SCOPE' AND year=? AND "
                  f"{marks}{region_filter} GROUP BY region_name_std,dept_short", params)}
    budget_filter = " AND region_name=?" if compare_region else ""
    budget = {(region, dept): float(value or 0) / 1000.0
              for region, dept, value in conn.execute(
                  "SELECT region_name,dept_short,SUM(budget_te) FROM v_budget "
                  f"WHERE year=? AND {marks}{budget_filter} GROUP BY region_name,dept_short",
                  params)}
    out = []
    for key in sorted(set(actual) | set(budget)):
        item = actual.get(key, {"actual_te": 0.0, "sales_amount": 0.0})
        target = budget.get(key, 0.0)
        out.append({"source_region": key[0], "source_department": key[1],
                    "actual_te": r2(item["actual_te"]), "budget_te": r2(target),
                    "attainment_pct": pct(item["actual_te"], target),
                    "gap_te": r2(item["actual_te"] - target),
                    "sales_amount": r2(item["sales_amount"])})
    return out


def _top_mix(conn: sqlite3.Connection, year: int, months: list[int],
             region: str, department: str) -> tuple[dict, dict]:
    marks, month_args = month_marks(months)
    params = [year, *month_args, region, department]
    total = float(conn.execute(
        "SELECT COALESCE(SUM(shipment_te),0) FROM a_erp WHERE year=? AND "
        f"{marks} AND region_name_std=? AND dept_short=? AND region_scope_status='IN_SCOPE'",
        params).fetchone()[0] or 0)
    channel = conn.execute(
        f"SELECT channel_type,SUM(shipment_te) FROM a_erp WHERE year=? AND {marks} "
        "AND region_name_std=? AND dept_short=? AND region_scope_status='IN_SCOPE' "
        "GROUP BY channel_type ORDER BY 2 DESC LIMIT 1", params).fetchone()
    category = conn.execute(
        f"SELECT category,SUM(shipment_te) FROM a_erp WHERE year=? AND {marks} "
        "AND region_name_std=? AND dept_short=? AND region_scope_status='IN_SCOPE' "
        "GROUP BY category ORDER BY 2 DESC LIMIT 1", params).fetchone()
    channel_out = {"name": channel[0] if channel else "待确认",
                   "share_pct": pct(float(channel[1] or 0), total) if channel else None}
    category_out = {"name": category[0] if category else "待确认",
                    "share_pct": pct(float(category[1] or 0), total) if category else None}
    return channel_out, category_out


def _sku_growth(conn: sqlite3.Connection, year: int, months: list[int],
                compare_region: str, top_n: int) -> list[dict]:
    marks, month_args = month_marks(months)
    region_filter = " AND region_name_std=?" if compare_region else ""
    def values(target_year: int) -> dict:
        params = [target_year, *month_args] + ([compare_region] if compare_region else [])
        return {(region, code): {"sku_name": name, "category": category,
                                 "boxes": float(boxes or 0), "amount": float(amount or 0),
                                 "active_store_count": int(active or 0)}
                for region, code, name, category, boxes, amount, active in conn.execute(
                    "SELECT region_name_std,sku_code,MAX(sku_name),MAX(category),"
                    "SUM(sales_boxes),SUM(sales_amount),COUNT(DISTINCT store_id) "
                    "FROM fact_pos_sales WHERE year=? AND "
                    f"{marks} AND region_scope_status='IN_SCOPE'{region_filter} "
                    "GROUP BY region_name_std,sku_code", params)}
    current, previous = values(year), values(year - 1)
    rows = []
    for key, item in current.items():
        prior = previous.get(key, {}).get("boxes", 0.0)
        growth = None if not prior else round((item["boxes"] / prior - 1) * 100, 2)
        rows.append({"region": key[0], "sku_code": key[1],
                     "sku_name": item["sku_name"], "category": item["category"],
                     "current_boxes": r2(item["boxes"]), "previous_boxes": r2(prior),
                     "yoy_pct": growth, "sales_amount": r2(item["amount"]),
                     "active_store_count": item["active_store_count"]})
    rows.sort(key=lambda row: (-(row.get("yoy_pct") or -10**9),
                               -(row.get("current_boxes") or 0), row["sku_code"]))
    return [row for row in rows if row["yoy_pct"] is not None][:top_n]


def _price_benchmarks(conn: sqlite3.Connection, year: int, months: list[int],
                      compare_region: str, top_n: int) -> list[dict]:
    marks, month_args = month_marks(months)
    region_filter = " AND region_name_std=?" if compare_region else ""
    params = [year, *month_args] + ([compare_region] if compare_region else [])
    return [{"region": region, "sku_code": code, "sku_name": name,
             "average_price": r2(avg_price), "min_price": r2(min_price),
             "max_price": r2(max_price), "observation_count": int(count or 0)}
            for region, code, name, avg_price, min_price, max_price, count in conn.execute(
                "SELECT region_name_std,sku_code,MAX(sku_name),AVG(unit_price),"
                "MIN(unit_price),MAX(unit_price),COUNT(*) FROM fact_pos_sales "
                f"WHERE year=? AND {marks} AND region_scope_status='IN_SCOPE'"
                f"{region_filter} GROUP BY region_name_std,sku_code "
                "ORDER BY COUNT(*) DESC LIMIT ?", [*params, top_n])]


def run(conn: sqlite3.Connection, home_region: str, year=None, months=None,
        scenario: str = "DIAGNOSIS", compare_region: str = "", top_n: int = 10) -> dict:
    period = resolve_period(year, months)
    scenario = str(scenario or "DIAGNOSIS").upper()
    if scenario not in SCENARIOS:
        from ._common import ToolError
        raise ToolError("InvalidArgument", f"不支持的跨战区场景 {scenario}")
    regions = _region_benchmarks(conn, period["year"], period["months"])
    departments = _department_candidates(
        conn, period["year"], period["months"], compare_region)
    sku_growth = _sku_growth(
        conn, period["year"], period["months"], compare_region, int(top_n))
    prices = _price_benchmarks(
        conn, period["year"], period["months"], compare_region, int(top_n))
    cases = []
    for row in sorted(departments, key=lambda x: (-(x.get("attainment_pct") or 0),
                                                   x["source_department"])):
        if row["source_region"] == home_region or (row.get("attainment_pct") or 0) < 100:
            continue
        channel, category = _top_mix(conn, period["year"], period["months"],
                                     row["source_region"], row["source_department"])
        matching_sku = next((x for x in sku_growth
                             if x["region"] == row["source_region"]), None)
        factors = [{"factor": f"{channel['name']}渠道TE占比 {channel['share_pct']}%",
                    "evidence_type": "ASSOCIATION",
                    "statement": "结构相关发现，不代表该渠道导致预算达成"},
                   {"factor": f"{category['name']}为该市场部TE最高品类",
                    "evidence_type": "ASSOCIATION",
                    "statement": "品类结构相关发现，不代表因果"},
                   {"factor": "促销、陈列与价格策略可能影响表现",
                    "evidence_type": "HYPOTHESIS",
                    "statement": "缺少完整活动实验数据，待人工核验"}]
        if matching_sku:
            factors.insert(2, {"factor": (f"{matching_sku['sku_name']} POS同比 "
                                             f"{matching_sku['yoy_pct']}%"),
                               "evidence_type": "ASSOCIATION",
                               "statement": "同期增长相关发现，不代表单一经营动作导致增长"})
        cases.append({
            "source_region": row["source_region"],
            "source_department": row["source_department"],
            "applicable_channel": channel["name"],
            "sku_or_category": (matching_sku or {}).get("sku_name") or category["name"],
            "success_performance": {"actual_te": row["actual_te"],
                                    "budget_te": row["budget_te"],
                                    "attainment_pct": row["attainment_pct"]},
            "related_factors": factors,
            "data_evidence": ["ERP Sell-in与同期预算同口径对比",
                              "POS仅作为终端同期增长辅助证据"],
            "applicable_conditions": ["当前区域具有相近渠道与品类结构",
                                      "目标SKU可供货且库存能够支持",
                                      "当地价格带、门店覆盖与活动条件可比"],
            "replicability": "MEDIUM" if channel["name"] != "待确认" else "LOW",
            "human_validation": ["待人工确认来源区域实际促销与陈列动作",
                                 "待人工核验当地竞争、库存与渠道条件"],
        })
        if len(cases) >= int(top_n):
            break
    return envelope(
        NAME,
        args={"home_region": home_region, "compare_region": compare_region,
              "year": period["year"], "months": period["months"],
              "scenario": scenario, "top_n": int(top_n)},
        period=period,
        scope={"home_region": home_region,
               "compare_region": compare_region or "其他四大战区",
               "aggregation_level": "REGION_OR_DEPARTMENT",
               "sensitive_detail": False},
        data_source=SOURCE, caliber=CALIBER,
        metrics={"regions_compared": len(regions), "success_cases": len(cases),
                 "sku_rank_rows": len(sku_growth), "price_benchmark_rows": len(prices)},
        rows={"region_benchmarks": regions,
              "department_success_summary": departments[:int(top_n)],
              "sku_growth_ranking": sku_growth,
              "price_benchmarks": prices,
              "success_cases": cases},
        notes=[
            "跨战区数据只到战区、市场部或SKU汇总，不返回客户、门店、订单和人员敏感明细。",
            "经营因素仅为数据关联发现，不代表因果；缺少促销/陈列实验数据时标记为分析假设。",
            "供应链场景仅参考跨区域需求汇总；采购建议仍由供应链V2确定性公式计算。",
            "所有数据均为固定种子MOCK演示数据；成功经验必须经当地业务人员验证。",
        ],
        tables=["v_budget", "a_erp", "fact_pos_sales"],
    )


registry.register(
    NAME,
    "查询跨战区汇总指标、全国SKU增长排名和脱敏最佳实践；不返回其他区域客户或门店明细。",
    {"home_region": {"type": "string", "description": "当前用户所属/主分析战区"},
     "compare_region": {"type": "string", "description": "可选对标战区"},
     "year": {"type": "integer", "description": "年份"},
     "months": {"type": "array", "items": {"type": "integer"}, "description": "月份"},
     "scenario": {"type": "string", "description": "六类场景编码"},
     "top_n": {"type": "integer", "description": "汇总返回上限"}},
    required=["home_region", "year", "scenario"], fn=run,
    source=SOURCE,
    returns="战区/市场部汇总、SKU增长排名、价格带和脱敏成功经验",
    access_class="CROSS_REGION_AGGREGATE",
)
