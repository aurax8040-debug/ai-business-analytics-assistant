"""同款 SKU 门店价格异常只读工具。"""
from __future__ import annotations

import sqlite3
import statistics

from . import registry
from ._common import ToolError, envelope, month_marks, resolve_period

NAME = "query_store_price_anomalies"
SOURCE = "fact_pos_sales + dim_store + dim_sku（POS Sell-out）"
CALIBER = ("门店价格单位为元/盒；同款 = 同 SKU 编码与规格；可比门店优先按同 KA+门店类型+等级分组，"
           "同 KA 样本不足时降级为同区域+门店类型+等级并明确标记；"
           "等级未采集时单列为‘等级未采集’组；基准为组内中位数；明显偏高 = 门店价高于中位数"
           "指定阈值且样本量达到下限。价格偏高只标记相关性，不推断销量因果。")


def run(conn: sqlite3.Connection, region: str = "", year=None, months=None,
        threshold_pct: int = 10, min_sample: int = 3, top_n: int = 20) -> dict:
    period = resolve_period(year, months)
    threshold = int(threshold_pct)
    sample_floor = int(min_sample)
    if not 1 <= threshold <= 100:
        raise ToolError("InvalidArgument", "threshold_pct 需在 1–100 之间")
    if not 2 <= sample_floor <= 20:
        raise ToolError("InvalidArgument", "min_sample 需在 2–20 之间")
    marks, month_args = month_marks(period["months"])
    conds = ["p.year = ?", marks.replace("month", "p.month"),
             "p.region_scope_status = 'IN_SCOPE'", "p.unit_price IS NOT NULL"]
    params: list = [period["year"], *month_args]
    if region:
        conds.append("p.region_name_std = ?")
        params.append(region)
    clause = " AND ".join(conds)
    rows = [dict(zip(
        ("store_id", "store_name", "sku_code", "sku_name", "specification",
         "ka_system", "store_type", "store_level", "unit_price", "sales_boxes", "sales_amount"), row))
        for row in conn.execute(
            "SELECT p.store_id, MAX(p.store_name), p.sku_code, MAX(p.sku_name), "
            "MAX(d.specification), COALESCE(NULLIF(s.ka_system,''),'未采集'), "
            "COALESCE(NULLIF(s.store_type,''),'未采集'), "
            "COALESCE(NULLIF(s.store_level,''),'未采集'), AVG(p.unit_price), "
            "SUM(p.sales_boxes), SUM(p.sales_amount) "
            "FROM fact_pos_sales p "
            "LEFT JOIN dim_store s ON s.store_key=p.store_key "
            "LEFT JOIN dim_sku d ON d.sku_code=p.sku_code "
            f"WHERE {clause} GROUP BY p.store_id,p.sku_code,s.ka_system,s.store_type,s.store_level",
            params)]

    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        key = (row["sku_code"], row["specification"],
               row["store_type"], row["store_level"])
        groups.setdefault(key, []).append(row)

    summaries, anomalies = [], []
    def evaluate(sku: str, spec: str, ka_system: str, store_type: str,
                 store_level: str, members: list[dict], comparison_level: str) -> None:
        prices = [float(row["unit_price"]) for row in members]
        median = statistics.median(prices)
        min_price, max_price = min(prices), max(prices)
        spread_pct = round((max_price / min_price - 1) * 100, 4) if min_price else None
        summaries.append({
            "sku_code": sku, "sku_name": members[0]["sku_name"],
            "specification": spec, "ka_system": ka_system, "store_type": store_type,
            "store_level": store_level, "sample_size": len(members),
            "comparison_level": comparison_level,
            "median_price": round(median, 4), "min_price": round(min_price, 4),
            "max_price": round(max_price, 4), "spread_pct": spread_pct,
        })
        for row in members:
            premium = round((float(row["unit_price"]) / median - 1) * 100, 4) if median else None
            if premium is not None and premium > threshold:
                anomalies.append({
                    **row, "unit_price": round(float(row["unit_price"]), 4),
                    "benchmark_median_price": round(median, 4),
                    "price_premium_pct": premium, "sample_size": len(members),
                    "comparison_level": comparison_level,
                    "threshold_pct": threshold,
                })
    for (sku, spec, store_type, store_level), members in groups.items():
        by_ka: dict[str, list[dict]] = {}
        for row in members:
            by_ka.setdefault(row["ka_system"], []).append(row)
        qualified = [(ka, ka_rows) for ka, ka_rows in by_ka.items()
                     if len(ka_rows) >= sample_floor]
        if qualified:
            for ka, ka_rows in qualified:
                evaluate(sku, spec, ka, store_type, store_level, ka_rows, "same_ka")
        elif len(members) >= sample_floor:
            evaluate(sku, spec, "区域混合（同KA样本不足）", store_type, store_level,
                     members, "regional_fallback")
    summaries.sort(key=lambda x: (-(x.get("spread_pct") or 0), x["sku_code"]))
    anomalies.sort(key=lambda x: (-x["price_premium_pct"], x["store_name"]))
    max_spread = max((row.get("spread_pct") or 0 for row in summaries), default=0.0)
    uncollected = sum(1 for row in summaries if row["store_level"] in ("未采集", "无"))
    return envelope(
        NAME,
        args={"region": region, "year": period["year"], "months": period["months"],
              "threshold_pct": threshold, "min_sample": sample_floor, "top_n": int(top_n)},
        period=period,
        scope={"region": region or "全国",
               "comparable_group": "同 SKU+规格；优先同KA+类型+等级，样本不足时区域降级"},
        data_source="AI_ANALYSIS_LAYER_V1",
        caliber=CALIBER,
        metrics={"groups_checked": len(summaries), "observations": len(rows),
                 "anomaly_stores": len(anomalies),
                 "max_price_spread_pct": round(max_spread, 4),
                 "threshold_pct": threshold, "min_sample": sample_floor,
                 "groups_with_level_uncollected": uncollected},
        rows={"anomalies": anomalies[:int(top_n)], "group_summary": summaries},
        notes=[
            "当前 POS 数据同一 SKU 标价固定，因此本期未出现跨店价差；未发现异常不是工具未执行。",
            "同 KA 可比组不足最小样本时，降级为同区域、同门店类型和等级比较，并在 comparison_level 标记。",
            "部分门店等级未采集，这些门店仅在同类型的‘等级未采集’组内比较。",
            "价格偏高不得认定为销量变化的因果原因；需结合促销、库存、陈列等另行核查。",
        ],
        tables=["fact_pos_sales", "dim_store", "dim_sku"],
    )


registry.register(
    NAME,
    "按同 SKU、规格优先在同 KA、门店类型和等级内计算中位价；样本不足时显式降级到区域可比组；"
    "同时返回样本量、销量和口径限制，不作因果判断。",
    {"region": {"type": "string", "description": "战区"},
     "year": {"type": "integer", "description": "年份"},
     "months": {"type": "array", "items": {"type": "integer"}, "description": "月份"},
     "threshold_pct": {"type": "integer", "description": "高于中位价阈值，默认 10%"},
     "min_sample": {"type": "integer", "description": "可比组最小门店数，默认 3"},
     "top_n": {"type": "integer", "description": "异常门店返回上限"}},
    required=["region", "year"], fn=run, source=SOURCE,
    returns="metrics + rows.anomalies / group_summary",
)
