"""供应链专业 Workflow：计划编排与确定性结果组装。

本模块不连库；输入只能是 Phase 1 工具返回。采购数量由工具给出，Checker
独立复算后，本模块才把通过校验的数字交给 Reporter。
"""
from __future__ import annotations

import math

from server import config


SUPPLY_INTENTS = {
    "supply_chain_forecast",
    "supply_chain_growth",
    "supply_chain_shortage",
}


def is_supply_intent(kind: str) -> bool:
    return kind in SUPPLY_INTENTS


def build_plan(intent: dict) -> list[dict]:
    """按供应链场景生成有序工具计划；工具执行仍由主图统一节点负责。"""
    kind = intent.get("type") or "supply_chain_forecast"
    if kind not in SUPPLY_INTENTS:
        return []
    args = {"month": config.SC_LAST_MONTH, "window": 2}
    return [
        {"key": "demand", "tool": "forecast_material_demand", "args": args,
         "why": "先取得未来 SKU 对应的水果与包材需求", "label": "未来物料需求"},
        {"key": "bom", "tool": "query_sku_bom",
         "args": {"sku": "ALL", "effective_date": "2026-11-01"},
         "why": "核对预测月生效的水果与包材 BOM 版本", "label": "预测期生效 BOM"},
        {"key": "inventory", "tool": "query_material_inventory",
         "args": {"month": config.SC_LAST_MONTH},
         "why": "取得月末库存与包材在途，保持水果和包材单位分离", "label": "月末库存与在途"},
        {"key": "purchase", "tool": "calculate_purchase_suggestion", "args": args,
         "why": "调用供应链 V2 确定性公式取得净需求与建议采购量", "label": "确定性采购建议"},
    ]


def _by(rows: list[dict], key: str) -> dict:
    return {row.get(key): row for row in rows if row.get(key)}


def _rounded_suggestion(net: int, moq: int, package: int) -> int:
    if net <= 0:
        return 0
    value = math.ceil(net / package) * package
    return max(value, math.ceil(moq / package) * package)


def calculate_growth(tool_results: dict, intent: dict) -> dict:
    """在内存中计算 SKU 需求增长情景；不修改物化预测或库存。"""
    growth_pct = float(intent.get("growth_pct") or 0)
    category = intent.get("category") or ""
    pieces = int(intent.get("pieces_per_box") or 0)
    factor = 1 + growth_pct / 100.0
    demand = tool_results.get("demand") or {}
    bom = tool_results.get("bom") or {}
    inventory = tool_results.get("inventory") or {}
    purchase = tool_results.get("purchase") or {}
    drows = demand.get("rows") or {}

    selected_forecasts = [row for row in drows.get("sku_forecast") or []
                          if (not category or row.get("category") == category)
                          and (not pieces or int(row.get("pieces_per_box") or 0) == pieces)]
    selected: dict[str, dict] = {}
    deltas: list[tuple[str, str, int]] = []
    for row in selected_forecasts:
        base = int(row.get("forecast_boxes") or 0)
        adjusted = int(round(base * factor))
        delta = adjusted - base
        sku = row["sku_code"]
        item = selected.setdefault(sku, {
            "sku_code": sku, "sku_name": row.get("sku_name"),
            "category": row.get("category"),
            "pieces_per_box": int(row.get("pieces_per_box") or 0),
            "baseline_boxes": 0, "adjusted_boxes": 0,
            "incremental_boxes": 0, "growth_pct": growth_pct,
        })
        item["baseline_boxes"] += base
        item["adjusted_boxes"] += adjusted
        item["incremental_boxes"] += delta
        deltas.append((sku, row["forecast_month"], delta))

    bom_rows = list((bom.get("rows") or {}).get("bom_items") or [])
    bom_by_sku: dict[str, list[dict]] = {}
    for row in bom_rows:
        bom_by_sku.setdefault(row["sku_code"], []).append(row)
    raw_increment: dict[tuple[str, str], float] = {}
    material_info: dict[tuple[str, str], dict] = {}
    for sku, _month, delta_boxes in deltas:
        for row in bom_by_sku.get(sku, []):
            key = (row["item_type"], row["item_id"])
            raw_increment[key] = raw_increment.get(key, 0.0) + delta_boxes * row["qty_per_unit"]
            material_info[key] = row

    fruit_base = _by(drows.get("fruit_demand") or [], "material_id")
    pack_base = _by(drows.get("packaging_demand") or [], "material_id")
    irows = inventory.get("rows") or {}
    fruit_inv = _by(irows.get("fruit_inventory") or [], "raw_material_id")
    pack_inv = _by(irows.get("packaging_inventory") or [], "material_id")
    purchase_rows = _by((purchase.get("rows") or {}).get("procurement_items") or [],
                        "material_id")
    materials = []
    for key in sorted(raw_increment):
        material_type, mid = key
        info = material_info[key]
        if material_type == "fruit":
            base_row, inv_row = fruit_base.get(mid) or {}, fruit_inv.get(mid) or {}
            incremental = int(round(raw_increment[key]))
            baseline = int(base_row.get("demand_total") or 0)
            available = int(inv_row.get("ending_qty") or 0)
            baseline_net = max(0, baseline - available)
            adjusted = baseline + incremental
            adjusted_net = max(0, adjusted - available)
            materials.append({
                "material_type": "fruit", "material_id": mid,
                "material_name": info.get("item_name"), "unit": info.get("unit") or "粒",
                "baseline_demand": baseline, "adjusted_demand": adjusted,
                "incremental_demand": incremental, "inventory_coverage_qty": available,
                "baseline_net_requirement": baseline_net,
                "adjusted_net_requirement": adjusted_net,
                "incremental_net_requirement": adjusted_net - baseline_net,
                "suggested_order_qty": None,
                "note": "水果无采购参数，不生成采购建议",
            })
        else:
            base_row, inv_row = pack_base.get(mid) or {}, pack_inv.get(mid) or {}
            prow = purchase_rows.get(mid) or {}
            attrition = float(prow.get("attrition_rate_pct") or 0)
            incremental = int(round(raw_increment[key] * (1 + attrition / 100.0)))
            baseline = int(base_row.get("demand_total") or 0)
            adjusted = baseline + incremental
            available = int(inv_row.get("ending_qty") or 0) + int(inv_row.get("in_transit_qty") or 0)
            safety = int(prow.get("safety_stock_qty") or 0)
            baseline_net_raw = baseline + safety - available
            adjusted_net_raw = adjusted + safety - available
            baseline_net = max(0, baseline_net_raw)
            adjusted_net = max(0, adjusted_net_raw)
            moq = int(prow.get("moq_qty") or 0)
            package = int(prow.get("order_package_qty") or 0)
            adjusted_suggest = _rounded_suggestion(adjusted_net_raw, moq, package)
            base_suggest = int(prow.get("suggested_order_qty") or 0)
            materials.append({
                "material_type": "packaging", "material_id": mid,
                "material_name": info.get("item_name"), "unit": info.get("unit") or "件",
                "baseline_demand": baseline, "adjusted_demand": adjusted,
                "incremental_demand": incremental, "inventory_coverage_qty": available,
                "baseline_net_requirement": baseline_net,
                "adjusted_net_requirement": adjusted_net,
                "incremental_net_requirement": adjusted_net - baseline_net,
                "baseline_suggested_order_qty": base_suggest,
                "adjusted_suggested_order_qty": adjusted_suggest,
                "incremental_suggested_order_qty": adjusted_suggest - base_suggest,
                "attrition_rate_pct": attrition, "safety_stock_qty": safety,
                "moq_qty": moq, "order_package_qty": package,
            })
    selected_rows = [selected[key] for key in sorted(selected)]
    sales_impact = {
        "baseline_boxes": sum(row["baseline_boxes"] for row in selected_rows),
        "adjusted_boxes": sum(row["adjusted_boxes"] for row in selected_rows),
        "incremental_boxes": sum(row["incremental_boxes"] for row in selected_rows),
    }
    return {
        "scenario": "supply_chain_growth", "growth_pct": growth_pct,
        "category": category, "pieces_per_box": pieces,
        "scope_mode": ("filtered_skus" if category or pieces else "all_forecast_skus"),
        "selected_skus": selected_rows, "sales_impact": sales_impact,
        "scenario_materials": materials,
        "calculation_mode": "in_memory_only",
    }


def analyze_growth(tool_results: dict, workflow_result: dict, checker: dict) -> dict:
    if not checker.get("verified"):
        return {"scenario": "supply_chain_growth", "verified": False,
                "checker": checker, "selected_skus": [], "scenario_materials": [],
                "data_gaps": [checker.get("message") or "情景结果未通过校验"]}
    demand = tool_results.get("demand") or {}
    return {**workflow_result, "verified": True, "checker": checker,
            "period": demand.get("period") or {},
            "scope": {"business": "SKU 需求增长情景模拟"},
            "data_source": "MOCK_SUPPLY_CHAIN", "mock_supply_chain": True,
            "tables": sorted({table for result in tool_results.values()
                              for table in (result.get("tables") or [])}),
            "notes": ["情景模拟仅在内存中计算，不修改 fact_production_forecast 或库存数据"],
            "cross_checks": checker.get("checks") or [], "data_gaps": []}


def calculate_shortage(tool_results: dict, intent: dict | None = None) -> dict:
    """按物料独立计算短缺与生产影响；不同单位永不汇总。"""
    exclude_in_transit = bool((intent or {}).get("exclude_in_transit"))
    demand = tool_results.get("demand") or {}
    bom = tool_results.get("bom") or {}
    inventory = tool_results.get("inventory") or {}
    drows = demand.get("rows") or {}
    irows = inventory.get("rows") or {}
    fruit_inv = _by(irows.get("fruit_inventory") or [], "raw_material_id")
    pack_inv = _by(irows.get("packaging_inventory") or [], "material_id")
    sku_forecast: dict[str, int] = {}
    for row in drows.get("sku_forecast") or []:
        sku = row.get("sku_code")
        sku_forecast[sku] = sku_forecast.get(sku, 0) + int(row.get("forecast_boxes") or 0)
    affected: dict[tuple[str, str], set[str]] = {}
    names: dict[tuple[str, str], str] = {}
    for row in (bom.get("rows") or {}).get("bom_items") or []:
        key = (row.get("item_type"), row.get("item_id"))
        affected.setdefault(key, set()).add(row.get("sku_code"))
        names[key] = row.get("item_name")

    def risk_row(material_type: str, row: dict) -> dict | None:
        mid = row.get("material_id")
        need = int(row.get("demand_total") or 0)
        if material_type == "fruit":
            inv = fruit_inv.get(mid) or {}
            stock, transit = int(inv.get("ending_qty") or 0), 0
        else:
            inv = pack_inv.get(mid) or {}
            stock = int(inv.get("ending_qty") or 0)
            transit = int(inv.get("in_transit_qty") or 0)
        counted_transit = 0 if (material_type == "fruit" or exclude_in_transit) else transit
        available = stock + counted_transit
        shortage = max(0, need - available)
        if shortage <= 0:
            return None
        skus = sorted(x for x in affected.get((material_type, mid), set()) if x)
        related_boxes = sum(sku_forecast.get(sku, 0) for sku in skus)
        ratio = shortage / need if need else 0
        return {
            "material_type": material_type, "material_id": mid,
            "material_name": row.get("material_name") or names.get((material_type, mid)),
            "unit": row.get("unit") or inv.get("unit") or ("粒" if material_type == "fruit" else "件"),
            "forecast_demand": need, "inventory_qty": stock,
            "in_transit_qty": transit, "available_qty": available,
            "counted_in_transit_qty": counted_transit,
            "shortage_qty": shortage,
            "coverage_pct": round(available / need * 100, 2) if need else None,
            "affected_skus": skus, "affected_sku_count": len(skus),
            "estimated_impacted_boxes": int(round(related_boxes * ratio)),
            "impact_method": "proportional_allocation_estimate",
            "risk_level": "high" if available / need < 0.8 else "medium",
        }

    fruit = [value for row in drows.get("fruit_demand") or []
             if (value := risk_row("fruit", row))]
    packaging = [value for row in drows.get("packaging_demand") or []
                 if (value := risk_row("packaging", row))]
    return {
        "scenario": "supply_chain_shortage",
        "fruit_shortages": sorted(fruit, key=lambda row: -row["shortage_qty"]),
        "packaging_shortages": sorted(packaging, key=lambda row: -row["shortage_qty"]),
        "impact_method": "proportional_allocation_estimate",
        "calculation_mode": "read_only_deterministic",
        "exclude_in_transit": exclude_in_transit,
    }


def analyze_shortage(tool_results: dict, workflow_result: dict, checker: dict) -> dict:
    if not checker.get("verified"):
        return {"scenario": "supply_chain_shortage", "verified": False,
                "checker": checker, "fruit_shortages": [], "packaging_shortages": [],
                "data_gaps": [checker.get("message") or "短缺结果未通过校验"]}
    demand = tool_results.get("demand") or {}
    exclude_in_transit = bool(workflow_result.get("exclude_in_transit"))
    transit_note = (
        "在途延期情景：包材在途数量仅披露、暂不计入可用量"
        if exclude_in_transit else
        "包材可用量包含工具返回的在途数量；在途状态精度受模拟数据口径限制")
    return {**workflow_result, "verified": True, "checker": checker,
            "period": demand.get("period") or {},
            "scope": {"business": "供应链短缺风险"},
            "data_source": "MOCK_SUPPLY_CHAIN", "mock_supply_chain": True,
            "tables": sorted({table for result in tool_results.values()
                              for table in (result.get("tables") or [])}),
            "notes": [
                "库存为 2026-10 月末快照，不是实时库存",
                transit_note,
                "影响盒数按短缺比例分摊估算，不能作为精确排产承诺",
            ],
            "cross_checks": checker.get("checks") or [], "data_gaps": []}


def analyze_forecast(tool_results: dict, checker: dict) -> dict:
    """组装 Workflow A 结果；只接受 Checker 已验证的采购数字。"""
    demand = tool_results.get("demand") or {}
    inventory = tool_results.get("inventory") or {}
    purchase = tool_results.get("purchase") or {}
    if not checker.get("verified"):
        return {
            "scenario": "supply_chain_forecast", "verified": False,
            "checker": checker, "procurement_items": [], "fruit_risk": [],
            "data_gaps": [checker.get("message") or "采购建议未通过确定性校验"],
        }

    demand_rows = demand.get("rows") or {}
    inventory_rows = inventory.get("rows") or {}
    fruit_inventory = _by(inventory_rows.get("fruit_inventory") or [], "raw_material_id")
    fruit_risk = []
    for row in demand_rows.get("fruit_demand") or []:
        stock = (fruit_inventory.get(row["material_id"]) or {}).get("ending_qty") or 0
        need = row.get("demand_total") or 0
        shortage = max(0, need - stock)
        fruit_risk.append({
            "material_id": row["material_id"], "material_name": row.get("material_name"),
            "unit": row.get("unit") or "粒", "forecast_demand": need,
            "available_qty": stock, "shortage_qty": shortage,
            "coverage_pct": round(stock / need * 100, 2) if need else None,
            "risk_level": "high" if shortage > 0 else "low",
            "note": "水果原料无采购参数，仅展示生产需求与月末库存风险，不生成采购建议",
        })

    items = list((purchase.get("rows") or {}).get("procurement_items") or [])
    packaging_demand = _by(demand_rows.get("packaging_demand") or [], "material_id")
    packaging_inventory = _by(inventory_rows.get("packaging_inventory") or [], "material_id")
    enriched = []
    for row in items:
        mid = row.get("material_id")
        drow, irow = packaging_demand.get(mid) or {}, packaging_inventory.get(mid) or {}
        enriched.append({**row, "unit": drow.get("unit") or irow.get("unit") or "件"})
    enriched.sort(key=lambda row: ({"high": 0, "medium": 1, "low": 2}.get(
        row.get("risk_level"), 9), -(row.get("suggested_order_qty") or 0)))

    tables = sorted({table for result in tool_results.values()
                     for table in (result.get("tables") or [])})
    notes = [note for result in tool_results.values() for note in (result.get("notes") or [])]
    return {
        "scenario": "supply_chain_forecast", "verified": True,
        "period": purchase.get("period") or demand.get("period") or {},
        "scope": purchase.get("scope") or {},
        "data_source": "MOCK_SUPPLY_CHAIN", "mock_supply_chain": True,
        "metrics": purchase.get("metrics") or {},
        "procurement_items": enriched, "fruit_risk": fruit_risk,
        "formula": purchase.get("formula") or {},
        "forecast_basis": purchase.get("forecast_basis") or demand.get("forecast_basis") or "",
        "tables": tables, "notes": list(dict.fromkeys(notes)),
        "checker": checker, "cross_checks": checker.get("checks") or [], "data_gaps": [],
    }
