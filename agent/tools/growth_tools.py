"""高增长 SKU 铺货机会只读工具，复用 sku_gap_v1 确定性引擎。"""
from __future__ import annotations

import sqlite3

from server import config, sku_gap_queries

from . import registry
from ._common import envelope

NAME = "query_sku_distribution_opportunities"
SOURCE = "AI_ANALYSIS_LAYER_V1（metric_pos_yoy + fact_pos_sales + fact_sfa_visit）"
CALIBER = "；".join(sku_gap_queries.CALIBER)


def run(conn: sqlite3.Connection, region: str = "", top_n: int = 10) -> dict:
    result = sku_gap_queries.opportunities(conn, region=region, top_n=top_n)
    items = result["items"]
    return envelope(
        NAME,
        args={"region": region, "top_n": int(top_n)},
        period={"year": config.GAP_WINDOW[0],
                "months": list(config.GAP_WINDOW[1]),
                "label": "2026 年 9–10 月连续窗口", "notes": []},
        scope={"region": region or "全国 POS 对齐门店",
               "responsible_stores": result["responsible_stores"]},
        data_source="AI_ANALYSIS_LAYER_V1",
        caliber=CALIBER,
        metrics={"high_growth_skus": result["total_high_growth"],
                 "opportunity_skus": len(items),
                 "low_velocity_store_hits": sum(
                     row.get("low_velocity_stores", 0) for row in items),
                 "responsible_stores": result["responsible_stores"],
                 "yoy_min_pct": config.GAP_YOY_MIN_PCT,
                 "y26_amount_min_yuan": config.GAP_Y26_AMOUNT_MIN},
        rows={"opportunities": items},
        notes=list(sku_gap_queries.DATA_FACTS) + [
            "无 POS 记录 ≠ 未上架；结果仅分为疑似缺货与待核实，不输出确认未铺货。",
            "已有 POS 销售的门店不重复推荐铺货；低于责任范围同 SKU P25 时列为已售低动销。",
            "门店城市与等级未采集，基准降级为当前责任范围内在售门店分位数。",
        ],
        tables=["metric_pos_yoy", "fact_pos_sales", "fact_sfa_visit", "dim_sku"],
    )


registry.register(
    NAME,
    "识别达到规模门槛的高增长 SKU，并在指定战区的 POS 对齐门店中查找连续两月无销售记录的机会；"
    "联合 SFA 标记疑似缺货/待核实；已铺货门店低于 P25 时转为低动销分析，并用 P50 测算缺口增量。",
    {"region": {"type": "string", "description": "当前负责战区"},
     "top_n": {"type": "integer", "description": "返回机会 SKU 数，默认 10"}},
    required=["region"], fn=run, source=SOURCE,
    returns="metrics + rows.opportunities（SKU、门店三态、P50 增量与证据）",
)
