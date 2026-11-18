"""六类面试 Demo 的统一场景与报告契约。"""
from __future__ import annotations

from typing import Any


SCENES = {
    "budget_diagnosis": ("DIAGNOSIS", "预算未达成诊断"),
    "growth_opportunity": ("OPPORTUNITY", "高增长 SKU 机会挖掘"),
    "price_anomaly": ("PRICE", "门店价格竞争力分析"),
    "supply_chain_shortage": ("SUPPLY_CHAIN", "供应链短缺风险"),
    "supply_chain_forecast": ("PROCUREMENT", "包材采购预测"),
    "supply_chain_growth": ("SIMULATION", "经营情景模拟"),
}

SECONDARY = {
    "DIAGNOSIS": ["ATTAINMENT", "INTERNAL_GAP", "CUSTOMER"],
    "OPPORTUNITY": ["SKU_GROWTH", "DISTRIBUTION_GAP", "STORE_BENCHMARK"],
    "PRICE": ["COMPARABLE_STORE", "PRICE_OUTLIER", "POS_ASSOCIATION"],
    "SUPPLY_CHAIN": ["MATERIAL_SHORTAGE", "AFFECTED_SKU", "PRODUCTION_RISK"],
    "PROCUREMENT": ["MATERIAL_DEMAND", "INVENTORY", "MOQ_ROUNDING"],
    "SIMULATION": ["DEMAND_CHANGE", "MATERIAL_IMPACT", "INCREMENTAL_GAP"],
}


def scene(final: dict) -> dict:
    report = final.get("report") or {}
    internal = (final.get("analysis_intent") or {}).get("type") or ""
    scenario = report.get("scenario") or {
        "attainment_review": "budget_diagnosis",
        "customer_focus": "budget_diagnosis",
        "growth_opportunity": "growth_opportunity",
        "price_anomaly": "price_anomaly",
        "supply_chain_shortage": "supply_chain_shortage",
        "supply_chain_forecast": "supply_chain_forecast",
        "supply_chain_growth": "supply_chain_growth",
    }.get(internal, "")
    code, label = SCENES.get(scenario, ("OTHER", "经营分析"))
    return {
        "code": code, "label": label, "primary_intent": code,
        "secondary_intents": SECONDARY.get(code, []),
        "internal_intent": internal,
        "workflow": (final.get("supervisor_decision") or {}).get("workflow") or "",
    }


def _section_items(report: dict, kinds: set[str]) -> list[str]:
    out = []
    for section in report.get("sections") or []:
        for item in section.get("items") or []:
            if item.get("kind") in kinds and item.get("text"):
                out.append(item["text"])
    return out


def _metrics(final: dict, scene_code: str) -> dict[str, Any]:
    report = final.get("report") or {}
    results = final.get("tool_results") or {}
    if scene_code == "DIAGNOSIS":
        checked = report.get("checker") or {}
        return dict(checked.get("recalculated") or {})
    if scene_code == "OPPORTUNITY":
        rows = report.get("opportunities") or []
        return {"opportunity_skus": len(rows),
                "gap_stores": sum(row.get("gap_stores") or 0 for row in rows),
                "incremental_p50_boxes": sum(row.get("inc_p50_boxes") or 0 for row in rows)}
    if scene_code == "PRICE":
        return {"groups_checked": len(report.get("group_summary") or []),
                "anomaly_records": len(report.get("anomalies") or [])}
    if scene_code == "SUPPLY_CHAIN":
        return {"fruit_shortages": len(report.get("fruit_shortages") or []),
                "packaging_shortages": len(report.get("packaging_shortages") or [])}
    if scene_code == "PROCUREMENT":
        items = ((results.get("purchase") or {}).get("rows") or {}).get("procurement_items") or []
        pm_box = next((row.get("suggested_order_qty") for row in items
                       if row.get("material_id") == "PM-BOX"), None)
        return {"materials_to_order": len(items), "pm_box_suggested_order_qty": pm_box}
    if scene_code == "SIMULATION":
        return {"growth_pct": report.get("growth_pct"),
                "affected_skus": len(report.get("selected_skus") or []),
                **(report.get("sales_impact") or {})}
    return {}


def unified(final: dict, scene_info: dict) -> dict:
    report = final.get("report") or {}
    calls = final.get("tool_calls") or []
    sources = []
    for call in calls:
        source = call.get("source")
        if source and source not in sources:
            sources.append(source)
    facts = _section_items(report, {"fact"})
    risks = _section_items(report, {"inference", "limit"})
    actions = _section_items(report, {"advice"})
    human = {
        "DIAGNOSIS": ["由业务负责人确认客户缺口的真实经营根因"],
        "OPPORTUNITY": ["核实门店真实上架、库存与扫码状态后再决定铺货"],
        "PRICE": ["核实实际成交价、促销、库存和陈列，不直接认定价格因果"],
        "SUPPLY_CHAIN": ["供应链人员复核库存快照、在途状态和排产影响"],
        "PROCUREMENT": ["采购人员复核建议量并人工审批；Agent 不自动下单"],
        "SIMULATION": ["业务与供应链共同确认增长假设后再调整正式计划"],
    }.get(scene_info["code"], [])
    base = {
        "user_question": final.get("user_question") or "",
        "business_scene": scene_info,
        "analysis_scope": {"period": report.get("period_label") or "",
                           "scope": report.get("scope_label") or "",
                           "entities": final.get("entity_sources") or {}},
        "core_conclusion": report.get("headline") or "",
        "key_metrics": _metrics(final, scene_info["code"]),
        "execution_path": [{"node": step.get("node"), "status": step.get("status"),
                            "detail": step.get("detail")}
                           for step in (final.get("steps") or [])],
        "data_sources": sources,
        "main_findings": facts[:8],
        "risks_or_opportunities": risks[:8],
        "recommended_actions": actions[:6],
        "human_confirmation": human,
        "checker": report.get("checker") or final.get("checker") or {},
    }
    # 第三阶段九段式经营报告。保留上面的既有字段，避免破坏前端与历史调用方。
    base.update({
        "business_conclusion": base["core_conclusion"],
        "core_metrics": base["key_metrics"],
        "data_evidence": facts[:8],
        "analysis_summary": base["execution_path"],
        "issues_or_opportunities": risks[:8],
        "impact_scope": base["analysis_scope"],
        "data_sources_and_period": {
            "sources": sources,
            "period": report.get("period_label") or "",
            "caliber": (report.get("basis") or {}).get("caliber") or "",
        },
    })
    return base
