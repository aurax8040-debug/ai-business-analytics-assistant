"""供应链 Workflow 的独立确定性 Checker。

Checker 不调用 LLM，也不复用 ``sc_queries.forecast`` 的建议量结果作为期望值；
它从需求、库存、在途、安全库存、MOQ 和整包装字段独立复算。
"""
from __future__ import annotations

import math

from server import config

from .supply_chain_workflow import SUPPLY_INTENTS


DEFAULT_TOOL_PLAN = {
    "demand": {"key": "demand", "tool": "forecast_material_demand",
               "args": {"month": config.SC_LAST_MONTH, "window": 2},
               "why": "Checker 定向重查需求", "label": "未来物料需求（重试）"},
    "bom": {"key": "bom", "tool": "query_sku_bom",
            "args": {"sku": "ALL", "effective_date": "2026-11-01"},
            "why": "Checker 定向重查 BOM", "label": "预测期生效 BOM（重试）"},
    "inventory": {"key": "inventory", "tool": "query_material_inventory",
                  "args": {"month": config.SC_LAST_MONTH},
                  "why": "Checker 定向重查库存", "label": "月末库存与在途（重试）"},
    "purchase": {"key": "purchase", "tool": "calculate_purchase_suggestion",
                 "args": {"month": config.SC_LAST_MONTH, "window": 2},
                 "why": "Checker 定向重算采购建议", "label": "确定性采购建议（重试）"},
}


def _item(name: str, ok: bool, detail: str, target: str = "") -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail, "target": target}


def _rows(result: dict, group: str) -> list[dict]:
    return list((result.get("rows") or {}).get(group) or [])


def _by(rows: list[dict], key: str) -> dict:
    return {row.get(key): row for row in rows if row.get(key)}


def _expected_suggestion(row: dict) -> tuple[int, int]:
    net = ((row.get("demand_total") or 0) + (row.get("safety_stock_qty") or 0)
           - (row.get("available_qty") or 0) - (row.get("in_transit_qty") or 0))
    raw = max(0, net)
    if raw <= 0:
        return net, 0
    package = int(row.get("order_package_qty") or 0)
    moq = int(row.get("moq_qty") or 0)
    if package <= 0 or moq <= 0:
        return net, -1
    suggestion = math.ceil(raw / package) * package
    if suggestion < moq:
        suggestion = math.ceil(moq / package) * package
    return net, suggestion


def _procurement_checks(tool_results: dict) -> tuple[list[dict], int]:
    demand = tool_results.get("demand") or {}
    bom = tool_results.get("bom") or {}
    inventory = tool_results.get("inventory") or {}
    purchase = tool_results.get("purchase") or {}
    checks: list[dict] = []

    required = {"demand": demand, "bom": bom, "inventory": inventory, "purchase": purchase}
    failed_tools = [key for key, result in required.items() if not result.get("ok")]
    checks.append(_item("工具完整性", not failed_tools,
                        "四个供应链工具均成功" if not failed_tools else f"失败工具：{failed_tools}",
                        failed_tools[0] if failed_tools else ""))
    if failed_tools:
        return checks, 0

    dmonths = (demand.get("period") or {}).get("months") or []
    pmonths = (purchase.get("period") or {}).get("months") or []
    checks.append(_item("需求期间一致性", dmonths == pmonths == [11, 12],
                        f"需求月份={dmonths}，采购月份={pmonths}", "demand"))

    bom_rows = _rows(bom, "bom_items")
    bom_skus = {row.get("sku_code") for row in bom_rows}
    bom_dates = {row.get("effective_date") for row in bom_rows}
    checks.append(_item("SKU 与 BOM 匹配", len(bom_skus) == 72 and bool(bom_rows),
                        f"覆盖 {len(bom_skus)} 个 SKU / {len(bom_rows)} 行", "bom"))
    checks.append(_item("BOM 生效版本", bom_dates == {"2026-11-01"},
                        f"生效日={sorted(bom_dates)}", "bom"))

    demand_rows = _by(_rows(demand, "packaging_demand"), "material_id")
    inventory_rows = _by(_rows(inventory, "packaging_inventory"), "material_id")
    purchase_rows = _rows(purchase, "procurement_items")
    unit_ok = all((demand_rows.get(row.get("material_id")) or {}).get("unit") ==
                  (inventory_rows.get(row.get("material_id")) or {}).get("unit")
                  for row in purchase_rows)
    checks.append(_item("物料单位一致", unit_ok,
                        "需求与库存的 8 种包材单位逐项一致" if unit_ok else "存在单位不一致",
                        "inventory"))

    basis_ok = True
    param_ok = True
    rounding_ok = True
    mismatch_ids = []
    for row in purchase_rows:
        mid = row.get("material_id")
        drow, irow = demand_rows.get(mid) or {}, inventory_rows.get(mid) or {}
        if (row.get("demand_total") != drow.get("demand_total")
                or row.get("available_qty") != irow.get("ending_qty")
                or row.get("in_transit_qty") != irow.get("in_transit_qty")):
            basis_ok = False
        if any(row.get(key) is None for key in (
                "safety_stock_qty", "attrition_rate_pct", "moq_qty", "order_package_qty")):
            param_ok = False
        expected_net, expected_suggestion = _expected_suggestion(row)
        if (row.get("net_requirement") != expected_net
                or row.get("suggested_order_qty") != expected_suggestion):
            rounding_ok = False
            mismatch_ids.append(mid)

    checks.append(_item("库存和在途扣减", basis_ok,
                        "采购建议与需求、库存、在途逐物料对齐" if basis_ok else "存在基础量不一致",
                        "purchase"))
    checks.append(_item("安全库存和损耗率", param_ok,
                        "8 种包材参数完整" if param_ok else "存在参数缺失", "purchase"))
    checks.append(_item("MOQ 和整包装取整", rounding_ok,
                        "全部满足 MOQ 与整包装规则" if rounding_ok else f"不一致：{mismatch_ids}",
                        "purchase"))
    checks.append(_item("建议采购数量独立复算", rounding_ok,
                        f"独立复算 {len(purchase_rows)} 种包材" if rounding_ok else f"复算失败：{mismatch_ids}",
                        "purchase"))

    notes_ok = all(result.get("notes") for result in required.values())
    checks.append(_item("缺失数据披露", notes_ok,
                        "四工具均包含口径/边界说明" if notes_ok else "工具缺少边界说明"))
    mock_ok = all(result.get("data_source") == "MOCK_SUPPLY_CHAIN"
                  and result.get("mock_supply_chain") is True for result in required.values())
    checks.append(_item("MOCK 数据来源声明", mock_ok,
                        "四工具均明确 MOCK_SUPPLY_CHAIN" if mock_ok else "MOCK 标识缺失"))
    return checks, len(purchase_rows)


def _growth_checks(state: dict) -> tuple[list[dict], int]:
    """不调用 Workflow 计算函数，直接从四工具结果独立复算增长情景。"""
    tool_results = state.get("tool_results") or {}
    checks, _ = _procurement_checks(tool_results)
    if any(not item["ok"] for item in checks):
        return checks, 0
    intent = state.get("analysis_intent") or {}
    growth = float(intent.get("growth_pct") or 0)
    category = intent.get("category") or ""
    pieces = int(intent.get("pieces_per_box") or 0)
    scope_ok = growth > 0
    checks.append(_item("情景输入完整", scope_ok,
                        (f"范围={category or '全部品类'}/"
                         f"{(str(pieces) + '粒装') if pieces else '全部规格'}，增长={growth}%"),
                        "demand"))

    demand = tool_results["demand"]
    bom = tool_results["bom"]
    inventory = tool_results["inventory"]
    purchase = tool_results["purchase"]
    sku_rows = [row for row in _rows(demand, "sku_forecast")
                if (not category or row.get("category") == category)
                and (not pieces or int(row.get("pieces_per_box") or 0) == pieces)]
    expected_skus: dict[str, dict] = {}
    deltas: list[tuple[str, int]] = []
    factor = 1 + growth / 100.0
    for row in sku_rows:
        base = int(row.get("forecast_boxes") or 0)
        adjusted = int(round(base * factor))
        delta = adjusted - base
        sku = row["sku_code"]
        agg = expected_skus.setdefault(sku, {"baseline": 0, "adjusted": 0, "delta": 0})
        agg["baseline"] += base
        agg["adjusted"] += adjusted
        agg["delta"] += delta
        deltas.append((sku, delta))

    actual = state.get("workflow_result") or {}
    actual_skus = {row["sku_code"]: row for row in actual.get("selected_skus") or []}
    sku_ok = set(actual_skus) == set(expected_skus)
    for sku, expected in expected_skus.items():
        row = actual_skus.get(sku) or {}
        sku_ok = sku_ok and all((row.get("baseline_boxes"), row.get("adjusted_boxes"),
                                 row.get("incremental_boxes"))[i] == value
                                for i, value in enumerate(expected.values()))
    checks.append(_item("SKU 需求独立复算", sku_ok and bool(expected_skus),
                        f"复算 {len(expected_skus)} 个目标 SKU", "demand"))

    bom_by_sku: dict[str, list[dict]] = {}
    for row in _rows(bom, "bom_items"):
        bom_by_sku.setdefault(row["sku_code"], []).append(row)
    raw_inc: dict[tuple[str, str], float] = {}
    for sku, delta in deltas:
        for row in bom_by_sku.get(sku, []):
            key = (row["item_type"], row["item_id"])
            raw_inc[key] = raw_inc.get(key, 0.0) + delta * row["qty_per_unit"]

    drows = demand.get("rows") or {}
    fruit_base = _by(drows.get("fruit_demand") or [], "material_id")
    pack_base = _by(drows.get("packaging_demand") or [], "material_id")
    irows = inventory.get("rows") or {}
    fruit_inv = _by(irows.get("fruit_inventory") or [], "raw_material_id")
    pack_inv = _by(irows.get("packaging_inventory") or [], "material_id")
    purchase_rows = _by(_rows(purchase, "procurement_items"), "material_id")
    expected_materials = {}
    for (material_type, mid), raw in raw_inc.items():
        if material_type == "fruit":
            baseline = int((fruit_base.get(mid) or {}).get("demand_total") or 0)
            increment = int(round(raw))
            coverage = int((fruit_inv.get(mid) or {}).get("ending_qty") or 0)
            base_net, adjusted_net = max(0, baseline - coverage), max(0, baseline + increment - coverage)
        else:
            prow = purchase_rows.get(mid) or {}
            attrition = float(prow.get("attrition_rate_pct") or 0)
            increment = int(round(raw * (1 + attrition / 100.0)))
            baseline = int((pack_base.get(mid) or {}).get("demand_total") or 0)
            inv = pack_inv.get(mid) or {}
            coverage = int(inv.get("ending_qty") or 0) + int(inv.get("in_transit_qty") or 0)
            safety = int(prow.get("safety_stock_qty") or 0)
            base_net = max(0, baseline + safety - coverage)
            adjusted_net = max(0, baseline + increment + safety - coverage)
        expected_materials[(material_type, mid)] = {
            "baseline_demand": baseline, "adjusted_demand": baseline + increment,
            "incremental_demand": increment, "inventory_coverage_qty": coverage,
            "incremental_net_requirement": adjusted_net - base_net,
        }
    actual_materials = {(row["material_type"], row["material_id"]): row
                        for row in actual.get("scenario_materials") or []}
    material_ok = set(actual_materials) == set(expected_materials)
    for key, expected in expected_materials.items():
        row = actual_materials.get(key) or {}
        material_ok = material_ok and all(row.get(field) == value
                                          for field, value in expected.items())
    checks.append(_item("情景物料需求独立复算", material_ok,
                        f"复算 {len(expected_materials)} 种受影响物料",
                        "demand" if not material_ok else ""))
    readonly_ok = actual.get("calculation_mode") == "in_memory_only"
    checks.append(_item("内存只读情景", readonly_ok,
                        "情景结果标记为 in_memory_only" if readonly_ok else "缺少只读情景标记"))
    return checks, len(expected_materials)


def _shortage_checks(state: dict) -> tuple[list[dict], int]:
    """从工具原始字段独立复算短缺量与比例影响，不调用 Workflow 函数。"""
    tool_results = state.get("tool_results") or {}
    checks, _ = _procurement_checks(tool_results)
    if any(not item["ok"] for item in checks):
        return checks, 0
    demand = tool_results["demand"]
    bom = tool_results["bom"]
    inventory = tool_results["inventory"]
    actual = state.get("workflow_result") or {}
    exclude_in_transit = bool((state.get("analysis_intent") or {}).get("exclude_in_transit"))
    drows = demand.get("rows") or {}
    irows = inventory.get("rows") or {}
    fruit_inv = _by(irows.get("fruit_inventory") or [], "raw_material_id")
    pack_inv = _by(irows.get("packaging_inventory") or [], "material_id")
    sku_boxes: dict[str, int] = {}
    for row in drows.get("sku_forecast") or []:
        sku = row.get("sku_code")
        sku_boxes[sku] = sku_boxes.get(sku, 0) + int(row.get("forecast_boxes") or 0)
    affected: dict[tuple[str, str], set[str]] = {}
    for row in _rows(bom, "bom_items"):
        affected.setdefault((row.get("item_type"), row.get("item_id")), set()).add(
            row.get("sku_code"))

    expected: dict[tuple[str, str], dict] = {}
    for material_type, rows, inv_by in (
            ("fruit", drows.get("fruit_demand") or [], fruit_inv),
            ("packaging", drows.get("packaging_demand") or [], pack_inv)):
        for row in rows:
            mid = row.get("material_id")
            inv = inv_by.get(mid) or {}
            stock = int(inv.get("ending_qty") or 0)
            transit = 0 if material_type == "fruit" else int(inv.get("in_transit_qty") or 0)
            demand_qty = int(row.get("demand_total") or 0)
            counted_transit = 0 if (material_type == "fruit" or exclude_in_transit) else transit
            available = stock + counted_transit
            shortage = max(0, demand_qty - available)
            if shortage <= 0:
                continue
            skus = sorted(x for x in affected.get((material_type, mid), set()) if x)
            related_boxes = sum(sku_boxes.get(sku, 0) for sku in skus)
            expected[(material_type, mid)] = {
                "forecast_demand": demand_qty, "inventory_qty": stock,
                "in_transit_qty": transit, "available_qty": available,
                "counted_in_transit_qty": counted_transit,
                "shortage_qty": shortage, "affected_skus": skus,
                "estimated_impacted_boxes": int(round(
                    related_boxes * shortage / demand_qty)) if demand_qty else 0,
                "impact_method": "proportional_allocation_estimate",
            }
    actual_rows = ((actual.get("fruit_shortages") or [])
                   + (actual.get("packaging_shortages") or []))
    actual_by = {(row.get("material_type"), row.get("material_id")): row
                 for row in actual_rows}
    values_ok = set(actual_by) == set(expected)
    for key, fields in expected.items():
        row = actual_by.get(key) or {}
        values_ok = values_ok and all(row.get(name) == value
                                      for name, value in fields.items())
    checks.append(_item("短缺数量独立复算", values_ok,
                        f"复算 {len(expected)} 种短缺物料", "demand" if not values_ok else ""))
    unit_ok = all(row.get("unit") for row in actual_rows) and "total_shortage_qty" not in actual
    checks.append(_item("原料与包材单位隔离", unit_ok,
                        "逐物料保留单位，未生成跨单位短缺合计", "demand" if not unit_ok else ""))
    impact_ok = all(row.get("impact_method") == "proportional_allocation_estimate"
                    and row.get("affected_skus") for row in actual_rows)
    checks.append(_item("影响生产量独立复算", impact_ok and values_ok,
                        "按各物料短缺比例对关联 SKU 预测盒数作估算",
                        "bom" if not impact_ok else ""))
    return checks, len(expected)


def check(state: dict) -> dict:
    kind = (state.get("analysis_intent") or {}).get("type") or ""
    if kind not in SUPPLY_INTENTS:
        return {"status": "not_applicable", "verified": True, "checks": [],
                "materials_checked": 0, "retry_plan": [], "message": "非供应链场景"}

    if kind == "supply_chain_growth":
        checks, materials = _growth_checks(state)
    elif kind == "supply_chain_shortage":
        checks, materials = _shortage_checks(state)
    else:
        checks, materials = _procurement_checks(state.get("tool_results") or {})
    failed = [item for item in checks if not item["ok"]]
    if not failed:
        return {"status": "passed", "verified": True, "checks": checks,
                "materials_checked": materials, "retry_plan": [],
                "message": "确定性 Checker 全部通过"}

    retry_count = int(state.get("retry_count") or 0)
    targets = []
    for item in failed:
        target = item.get("target")
        if target and target not in targets:
            targets.append(target)
    if kind == "supply_chain_growth" and any(
            item["name"] in ("SKU 需求独立复算", "情景物料需求独立复算")
            for item in failed):
        targets = [key for key in ("demand", "bom", "inventory", "purchase")
                   if key not in targets] + targets
    if retry_count < 1 and targets:
        return {"status": "retry", "verified": False, "checks": checks,
                "materials_checked": materials,
                "retry_plan": [dict(DEFAULT_TOOL_PLAN[target]) for target in targets],
                "message": f"Checker 未通过，定向重算：{'、'.join(targets)}"}
    return {"status": "blocked", "verified": False, "checks": checks,
            "materials_checked": materials, "retry_plan": [],
            "message": "Checker 重算后仍未通过，禁止输出未经验证的采购数量"}
