"""简单单指标查询的确定性分析配置。"""
from __future__ import annotations


METRICS = {
    "query_budget": ("同期预算", "budget_te", "TE"),
    "query_erp_actual": ("ERP 实际出货", "te", "TE"),
    "query_customer_performance": ("预算达成率", "attainment_pct", "%"),
    "query_pos_performance": ("POS 终端销售", "sales_te", "TE"),
    "query_sfa_visit": ("SFA 拜访次数", "visits", "次"),
    "query_wholesale_performance": ("批发出库", "sales_te", "TE"),
}


def analyze(tool: str, result: dict) -> dict:
    """从单个标准工具返回中提取主指标；不进行跨系统推断。"""
    label, field, unit = METRICS[tool]
    value = (result.get("metrics") or {}).get(field)
    gaps = [] if result.get("ok") and value is not None else [f"{label}未返回可用值"]
    return {
        "scenario": "direct_query", "tool": tool, "verified": bool(result.get("ok")),
        "metric_label": label, "metric_field": field, "metric_value": value,
        "metric_unit": unit, "metrics": result.get("metrics") or {},
        "period": result.get("period") or {}, "scope": result.get("scope") or {},
        "data_source": result.get("data_source") or "",
        "caliber": result.get("caliber") or "", "tables": result.get("tables") or [],
        "notes": result.get("notes") or [], "data_gaps": gaps,
    }
