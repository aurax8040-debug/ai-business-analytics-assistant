"""从 Checker 已验证的工具结果生成可追溯图表规格。"""
from __future__ import annotations

import math
from typing import Any

DIMENSIONS = (
    ("month", "月份"), ("date", "日期"), ("region", "战区"),
    ("department", "市场部"), ("customer", "客户"), ("ka_system", "KA 系统"),
    ("store", "门店"), ("sku", "SKU"), ("category", "品类"),
    ("segment_label", "客户类型"), ("segment", "客户类型"),
    ("display_location", "陈列位置"), ("display_form", "陈列形式"),
)
METRICS = (
    ("attainment_pct", "达成率", "%"), ("gap_te", "缺口", "TE"),
    ("sales_te", "POS 终端销售", "TE"), ("te", "ERP 出货", "TE"),
    ("budget_te", "预算", "TE"), ("gross_margin_pct", "毛利率", "%"),
    ("completed_rate_pct", "拜访完成率", "%"), ("sales_amount", "销售金额", "元"),
    ("amount", "出货金额", "元"), ("gross_profit", "毛利额", "元"),
    ("visits", "拜访次数", "次"), ("stores", "门店数", "家"),
)


def _empty(reason: str) -> dict:
    return {"available": False, "reason": reason, "type": "", "title": "",
            "source_tool": "", "source": "", "caliber": "", "metric": {},
            "dimension": {}, "points": [], "verified": False}


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _label(field: str, value: Any) -> str:
    if field == "month" and isinstance(value, (int, float)):
        return f"{int(value)}月"
    return str(value)


def _preferred_metric(semantic: dict) -> list[str]:
    out: list[str] = []
    for item in semantic.get("metrics") or []:
        for field in item.get("fields") or []:
            if field not in out:
                out.append(field)
    return out


def _ordered(items: list[dict], preferred: list[str], key: str) -> list[dict]:
    rank = {value: index for index, value in enumerate(preferred)}
    return sorted(items, key=lambda item: rank.get(str(item.get(key) or ""), len(rank)))


def build(tool_data: list[dict], checker: dict, execution_status: str,
          semantic: dict | None = None) -> dict:
    if (checker.get("status") not in ("passed", "not_applicable")
            or checker.get("verified") is not True):
        return _empty("Checker 未通过或未确认 verified=true，禁止生成图表。")
    if execution_status not in ("SUCCESS", "FALLBACK"):
        return _empty(f"本次执行状态为 {execution_status or 'UNKNOWN'}，不生成可信图表。")

    preferred = _preferred_metric(semantic or {})
    preferred_dimensions = [item.get("code") for item in (semantic or {}).get("dimensions") or []
                            if item.get("code")]
    preferred_tools = list((semantic or {}).get("route_tools") or [])
    metric_defs = {key: (label, unit) for key, label, unit in METRICS}
    ordered_metrics = preferred + [key for key, _, _ in METRICS if key not in preferred]
    candidate_tools = list(tool_data or [])
    if preferred_tools:
        candidate_tools = [tool for tool in candidate_tools
                           if tool.get("tool") in preferred_tools]
    for tool in _ordered(candidate_tools, preferred_tools, "tool"):
        if not tool.get("ok"):
            continue
        groups = list(tool.get("groups") or [])
        groups.sort(key=lambda group: 0 if any(
            any(row.get(dim) not in (None, "") for row in (group.get("rows") or []))
            for dim in preferred_dimensions) else 1)
        for group in groups:
            rows = [row for row in (group.get("rows") or []) if isinstance(row, dict)]
            if len(rows) < 2:
                continue
            dim_order = preferred_dimensions + [key for key, _ in DIMENSIONS
                                                 if key not in preferred_dimensions]
            dim_labels = dict(DIMENSIONS)
            dim = next(((key, dim_labels[key]) for key in dim_order if key in dim_labels
                        if sum(row.get(key) not in (None, "") for row in rows) >= 2), None)
            if not dim:
                continue
            metric_key = next((key for key in ordered_metrics
                               if key in metric_defs and
                               sum(_number(row.get(key)) for row in rows) >= 2), None)
            if not metric_key:
                continue
            dim_key, dim_label = dim
            metric_label, unit = metric_defs[metric_key]
            points = []
            for row in rows:
                value = row.get(metric_key)
                raw_label = row.get(dim_key)
                if raw_label in (None, "") or not _number(value):
                    continue
                label = _label(dim_key, raw_label)
                followup = f"请继续查询{label}的{metric_label}明细，保持同一指标口径并列出数据来源。"
                points.append({"label": label, "value": value, "unit": unit,
                               "dimension": dim_key, "raw_dimension": raw_label,
                               "followup": followup})
                if len(points) >= 12:
                    break
            if len(points) < 2:
                continue
            chart_type = "line" if dim_key in ("month", "date") else "bar"
            has_independent_checker = checker.get("status") == "passed"
            return {
                "available": True,
                "reason": "",
                "type": chart_type,
                "title": f"{dim_label} · {metric_label}",
                "source_tool": tool.get("tool") or "",
                "source_key": tool.get("key") or "",
                "source_group": group.get("name") or "",
                "source": tool.get("source") or "",
                "caliber": tool.get("caliber") or "",
                "metric": {"field": metric_key, "label": metric_label, "unit": unit},
                "dimension": {"field": dim_key, "label": dim_label},
                "points": points,
                "verified": True,
                "verification_label": (
                    "Checker 已验证" if has_independent_checker else "工具结果已验证"
                ),
                "verification": (
                    "数据点逐项复制自已通过 Checker 的工具结果，前端不重算。"
                    if has_independent_checker
                    else "数据点逐项复制自已验证的工具结果；本场景无独立 Checker，前端不重算。"
                ),
            }
    return _empty("已验证工具结果中没有至少两个可比数据点，暂不生成图表。")
