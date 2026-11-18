"""Agent V3 的 Demo RBAC、数据口径与确定性质量治理。

这里不做业务计算，也不调用 LLM。权限只来自后端 ``server_context``；
质量等级只由公开规则产生，供 Checker、Supervisor 和 Reporter 复用。
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from typing import Any


DEMO_USERS = {
    "demo_group_admin": {
        "display_name": "集团管理员（Demo）", "role": "GROUP_ADMIN",
        "regions": [], "departments": [], "stores": [], "domains": ["business", "supply_chain"],
        "knowledge_access": ["CROSS_REGION_AGGREGATE", "BEST_PRACTICE"],
    },
    "demo_north_manager": {
        "display_name": "战区B经理（Demo）", "role": "REGION_MANAGER",
        "regions": ["战区B"], "departments": [], "stores": [], "domains": ["business"],
        "knowledge_access": ["CROSS_REGION_AGGREGATE", "BEST_PRACTICE"],
    },
    "demo_beijing_manager": {
        "display_name": "北京市场部经理（Demo）", "role": "DEPARTMENT_MANAGER",
        "regions": ["战区B"], "departments": ["北京"], "stores": [], "domains": ["business"],
        "knowledge_access": ["CROSS_REGION_AGGREGATE", "BEST_PRACTICE"],
    },
    "demo_account_manager": {
        "display_name": "客户经理（Demo）", "role": "ACCOUNT_MANAGER",
        "regions": ["战区B"], "departments": ["北京"],
        "stores": ["SFA-ST-00077", "SFA-ST-00078"], "domains": ["business"],
        "knowledge_access": ["CROSS_REGION_AGGREGATE", "BEST_PRACTICE"],
    },
}

SUPPLY_TOOLS = {
    "query_material_inventory", "query_sku_bom",
    "forecast_material_demand", "calculate_purchase_suggestion",
}


SOURCE_DICTIONARY = {
    "erp_sell_in": {
        "business_meaning": "ERP 向客户出货（Sell-in）", "grain": "出货明细/客户×SKU×业务月",
        "primary_key": "shipment_no（当前视图可能重复到明细粒度，需结合维度）",
        "join_keys": ["customer_id", "sku_code", "dept_short", "year", "month"],
        "business_date": "year + month；原始出货日字段未在 Agent 视图暴露",
        "update_frequency": "PENDING_CONFIRMATION", "unit": "TE/盒/金额",
        "caliber": "ERP Sell-in 独立统计，不与 POS Sell-out 相加",
        "source": "a_erp / fact_erp_shipment", "limitations": ["刷新频率待确认"],
    },
    "pos_sell_out": {
        "business_meaning": "门店终端销售（Sell-out）", "grain": "门店×SKU×自然日",
        "primary_key": "pos_record_id", "join_keys": ["store_id", "sku_code", "date_key"],
        "business_date": "full_date", "update_frequency": "T+1", "unit": "TE/盒/元",
        "caliber": "仅覆盖 POS 对齐门店；不与 ERP 混算",
        "source": "fact_pos_sales", "limitations": ["退货是否已净额扣除待确认"],
    },
    "sfa_visit": {
        "business_meaning": "客户经理拜访与活动执行事件", "grain": "一次拜访事件",
        "primary_key": "visit_id", "join_keys": ["store_id", "employee_key", "visit_date"],
        "business_date": "visit_date", "update_frequency": "PENDING_CONFIRMATION",
        "unit": "次/分钟/执行状态", "caliber": "月度指标按客户经理与拜访事件统计",
        "source": "fact_sfa_visit", "limitations": ["刷新频率待确认"],
    },
    "sku_master": {
        "business_meaning": "SKU 主数据", "grain": "一行一个 SKU",
        "primary_key": "sku_code", "join_keys": ["sku_code"], "business_date": "不适用",
        "update_frequency": "PENDING_CONFIRMATION", "unit": "规格/粒装/TE 系数",
        "caliber": "UNKNOWN SKU 不参与确定性关联", "source": "dim_sku",
        "limitations": ["主数据更新时间字段未提供"],
    },
    "sku_bom": {
        "business_meaning": "SKU 对水果原料与包材的版本化用量", "grain": "SKU×物料×BOM版本",
        "primary_key": "bom_id", "join_keys": ["sku_code", "item_id"],
        "business_date": "effective_from", "update_frequency": "按生效日期版本管理",
        "unit": "qty_per_unit 对应的物料单位",
        "caliber": "生产/预测日期选择 effective_from 不晚于该日的有效最新版",
        "source": "dim_sku_bom", "limitations": ["无有效版本时阻断计算"],
    },
    "fruit_raw": {
        "business_meaning": "果萃水果原料及月末库存", "grain": "原料批次×库存月",
        "primary_key": "raw_material_id + batch_id + inventory_month",
        "join_keys": ["raw_material_id"], "business_date": "inventory_month",
        "update_frequency": "月末快照", "unit": "粒",
        "caliber": "无采购 PO 与采购参数时只输出需求/库存风险",
        "source": "dim_fruit_raw + fact_fruit_inventory_monthly",
        "limitations": ["非实时库存", "水果在途采购不适用"],
    },
    "packaging_inventory": {
        "business_meaning": "包材月末库存与采购在途", "grain": "物料×仓库×库存月/采购单",
        "primary_key": "material_id + warehouse_id + inventory_month / po_id",
        "join_keys": ["material_id"], "business_date": "inventory_month / order_date",
        "update_frequency": "库存为月末快照；PO刷新频率待确认", "unit": "各物料单位",
        "caliber": "在途按已确认状态与未到货量计算，不跨单位汇总",
        "source": "fact_packaging_inventory_monthly + fact_packaging_po",
        "limitations": ["非实时库存", "部分到货余量状态需按现有V2口径解释"],
    },
    "production_batch": {
        "business_meaning": "生产工单与批次产出", "grain": "一个生产批次",
        "primary_key": "batch_id", "join_keys": ["wo_id", "sku_code", "batch_id"],
        "business_date": "production_date", "update_frequency": "PENDING_CONFIRMATION",
        "unit": "粒/盒", "caliber": "历史批次保留其生产时 BOM 版本",
        "source": "fact_production_batch", "limitations": ["刷新频率待确认"],
    },
    "finished_goods_receipt": {
        "business_meaning": "生产完成后的成品入库", "grain": "一笔成品入库记录",
        "primary_key": "inbound_id", "join_keys": ["production_batch_id", "sku_code"],
        "business_date": "inbound_date", "update_frequency": "PENDING_CONFIRMATION",
        "unit": "qty（盒）", "caliber": "成品入库量与生产批次合格产出关联，不反写历史 BOM",
        "source": "fact_fg_inbound", "limitations": ["刷新频率待确认"],
    },
}


def resolve_principal(server_context: dict | None = None) -> dict:
    ctx = dict(server_context or {})
    user_id = str(ctx.get("demo_user_id") or "demo_group_admin").strip()
    identity_source = str(ctx.get("identity_source") or "backend_default")
    config = DEMO_USERS.get(user_id)
    if config is None:
        return {
            "status": "PERMISSION_DENIED", "user_id": user_id,
            "display_name": "未知 Demo 用户", "role": "UNKNOWN", "demo_only": True,
            "identity_source": identity_source, "regions": [], "departments": [],
            "stores": [], "domains": [], "knowledge_access": [],
            "reason": "后端 Demo 用户配置中不存在该身份",
        }
    return {
        "status": "AVAILABLE", "user_id": user_id, "demo_only": True,
        "identity_source": identity_source, **deepcopy(config),
        "reason": "身份来自后端 Demo 用户配置；不代表生产登录或生产 RBAC",
    }


def _denied(message: str, principal: dict, args: dict) -> dict:
    return {
        "allowed": False, "args": {}, "principal": principal,
        "error": {"type": "PermissionDenied", "message": message,
                  "detail": "权限过滤在业务 SQL 执行前完成；未返回受限数据"},
        "requested_scope": deepcopy(args or {}), "effective_scope": {},
    }


def authorize_tool(name: str, args: dict | None, principal: dict) -> dict:
    """在工具函数执行前验证并收敛范围；不执行数据库查询。"""
    raw = deepcopy(args or {})
    if principal.get("status") != "AVAILABLE":
        return _denied(principal.get("reason") or "身份不可用", principal, raw)
    if name in SUPPLY_TOOLS and "supply_chain" not in principal.get("domains", []):
        return _denied("该 Demo 角色没有企业级供应链数据权限", principal, raw)
    # 跨战区只开放独立聚合工具，不放宽任何现有客户/门店明细工具。
    from .tools import registry
    spec = registry.TOOLS.get(name) or {}
    if spec.get("access_class") == "CROSS_REGION_AGGREGATE":
        if "CROSS_REGION_AGGREGATE" not in principal.get("knowledge_access", []):
            return _denied("该角色没有跨战区汇总知识权限", principal, raw)
        home_regions = principal.get("regions") or []
        requested_home = str(raw.get("home_region") or "").strip()
        if home_regions:
            if requested_home and requested_home not in home_regions:
                return _denied(
                    f"主分析战区 {requested_home} 超出授权范围 {home_regions}", principal, raw)
            raw["home_region"] = home_regions[0]
        elif not requested_home:
            raw["home_region"] = "战区B"
        return {"allowed": True, "args": raw, "principal": principal,
                "requested_scope": deepcopy(args or {}),
                "effective_scope": {**deepcopy(raw),
                                    "access_class": "CROSS_REGION_AGGREGATE",
                                    "sensitive_detail": False}}
    if principal.get("role") == "GROUP_ADMIN":
        return {"allowed": True, "args": raw, "principal": principal,
                "requested_scope": deepcopy(raw), "effective_scope": deepcopy(raw)}

    # 延迟导入避免 registry -> governance -> registry 的模块循环。
    props = ((registry.TOOLS.get(name) or {}).get("parameters") or {}).get("properties") or {}

    regions = principal.get("regions") or []
    if regions:
        requested = raw.get("region")
        if requested and requested not in regions:
            return _denied(f"请求战区 {requested} 超出授权范围 {regions}", principal, raw)
        if "region" not in props:
            return _denied("该工具无法按授权战区在 SQL 前过滤", principal, raw)
        raw["region"] = regions[0]

    departments = principal.get("departments") or []
    if departments:
        requested = raw.get("department")
        if requested and requested not in departments:
            return _denied(f"请求市场部 {requested} 超出授权范围 {departments}", principal, raw)
        if "department" not in props:
            return _denied("该工具无法按授权市场部在 SQL 前过滤", principal, raw)
        raw["department"] = departments[0]

    stores = principal.get("stores") or []
    if stores:
        requested = str(raw.get("store") or "").strip()
        if not requested:
            return _denied("客户经理查询必须明确选择一个负责门店，不能查询更大范围", principal, raw)
        if requested not in stores:
            return _denied(f"请求门店 {requested} 不在该客户经理负责门店内", principal, raw)
        if "store" not in props:
            return _denied("该工具无法按负责门店在 SQL 前过滤", principal, raw)
        raw["store"] = requested

    return {"allowed": True, "args": raw, "principal": principal,
            "requested_scope": deepcopy(args or {}), "effective_scope": deepcopy(raw)}


def permission_preflight(state: dict) -> dict:
    principal = state.get("auth_context") or resolve_principal(None)
    if principal.get("status") != "AVAILABLE":
        return {"status": "PERMISSION_DENIED", "principal": principal,
                "message": principal.get("reason")}
    region, department, store = state.get("region"), state.get("department"), state.get("store")
    if region and principal.get("regions") and region not in principal["regions"]:
        return {"status": "PERMISSION_DENIED", "principal": principal,
                "message": f"请求战区 {region} 超出 {principal['display_name']} 的授权范围"}
    if department and principal.get("departments") and department not in principal["departments"]:
        return {"status": "PERMISSION_DENIED", "principal": principal,
                "message": f"请求市场部 {department} 超出 {principal['display_name']} 的授权范围"}
    if store and principal.get("stores") and store not in principal["stores"]:
        return {"status": "PERMISSION_DENIED", "principal": principal,
                "message": f"请求门店 {store} 不在该客户经理负责门店内"}
    workflow = (state.get("supervisor_decision") or {}).get("workflow") or ""
    if workflow == "supply_chain" and "supply_chain" not in principal.get("domains", []):
        return {"status": "PERMISSION_DENIED", "principal": principal,
                "message": "该 Demo 角色没有企业级供应链数据权限"}
    return {"status": "AVAILABLE", "principal": principal,
            "message": "Demo 权限预检通过；最终范围仍由每个工具在 SQL 前强制校验"}


def _issue(kind: str, source: str, scope: str, *, blocking: bool,
           fallback: str, human: bool, detail: str, severity: str = "MEDIUM") -> dict:
    return {
        "type": kind, "source": source, "impact_scope": scope,
        "blocking": blocking, "fallback": fallback,
        "human_confirmation_required": human, "detail": detail, "severity": severity,
    }


def _iter_rows(rows: Any):
    if isinstance(rows, list):
        yield from rows
    elif isinstance(rows, dict):
        for value in rows.values():
            if isinstance(value, list):
                yield from value


_CROSS_REGION_FORBIDDEN_KEYS = {
    "customer", "customer_id", "store", "store_id", "store_name",
    "shipment_no", "manager_virtual_id", "employee_key",
}


def _find_forbidden_keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _CROSS_REGION_FORBIDDEN_KEYS:
                found.add(key)
            found |= _find_forbidden_keys(child)
    elif isinstance(value, list):
        for child in value:
            found |= _find_forbidden_keys(child)
    return found


def assess_tool_results(state: dict) -> dict:
    """按完整性、时效性、一致性、可追溯性评估已返回工具结果。"""
    plan = list(state.get("analysis_plan") or [])
    results = dict(state.get("tool_results") or {})
    issues: list[dict] = []
    expected = [item.get("key") for item in plan if item.get("key")]
    failed = [key for key in expected if not (results.get(key) or {}).get("ok")]
    if failed:
        blocking = len(failed) == len(expected)
        issues.append(_issue(
            "TOOL_CALL_FAILED", "Agent Business Tools", ",".join(failed),
            blocking=blocking, fallback=("停止分析" if blocking else "使用成功工具做部分分析"),
            human=True, detail=f"失败工具键：{failed}", severity="HIGH" if blocking else "MEDIUM"))
        for key in failed:
            failed_result = results.get(key) or {}
            tool = str(failed_result.get("tool") or "")
            error = failed_result.get("error") or {}
            message = str(error.get("message") or "")
            if tool == "query_sku_bom" and "没有生效 BOM" in message:
                issues.append(_issue(
                    "BOM_VERSION_MISSING", "SKU BOM", message,
                    blocking=True, fallback="阻断采购与生产计算", human=True,
                    detail="指定生产/预测日期没有有效 BOM 版本", severity="HIGH"))
            if "sku 不存在" in message.lower() or "sku不存在" in message.lower():
                issues.append(_issue(
                    "SKU_UNMATCHED", "SKU Master", message,
                    blocking=True, fallback="阻断 SKU 级结论，不自动猜测映射", human=True,
                    detail="业务 SKU 无法匹配主数据", severity="HIGH"))

    successful = {key: value for key, value in results.items()
                  if isinstance(value, dict) and value.get("ok")}
    sources, periods, used_data = [], [], []
    missing_meta = []
    has_erp = has_pos = False
    for key, result in successful.items():
        source = str(result.get("data_source") or "")
        sources.append(source)
        periods.append(result.get("period") or {})
        tool = str(result.get("tool") or "")
        refresh = ("T+1" if tool == "query_pos_performance"
                   else "月末快照" if tool == "query_material_inventory"
                   else "按生效日期版本管理" if tool == "query_sku_bom"
                   else "PENDING_CONFIRMATION")
        used_data.append({
            "tool": tool, "source": source,
            "period": result.get("period") or {},
            "query_generated_at": result.get("generated_at") or "",
            "source_update_frequency": refresh,
            "completeness_status": "AVAILABLE",
        })
        has_erp = has_erp or tool in ("query_erp_actual", "query_customer_performance") or "ERP" in source
        has_pos = has_pos or tool == "query_pos_performance" or "POS" in source
        missing = [field for field in ("data_source", "period", "caliber", "tables")
                   if not result.get(field)]
        if missing:
            missing_meta.append({"key": key, "fields": missing})

        if tool == "query_cross_region_practices":
            scope = result.get("scope") or {}
            leaked = sorted(_find_forbidden_keys(result.get("rows") or {}))
            if (scope.get("aggregation_level") != "REGION_OR_DEPARTMENT"
                    or scope.get("sensitive_detail") is not False):
                issues.append(_issue(
                    "CROSS_REGION_GRAIN_INVALID", source or "跨战区汇总", str(scope),
                    blocking=True, fallback="阻断跨战区结论", human=True,
                    detail="跨战区工具必须声明战区/市场部汇总粒度且不得含敏感明细",
                    severity="HIGH"))
            if leaked:
                issues.append(_issue(
                    "CROSS_REGION_SENSITIVE_DETAIL_LEAK", source or "跨战区汇总",
                    ",".join(leaked), blocking=True, fallback="阻断并清空跨区结果",
                    human=True, detail="跨战区汇总结果包含客户、门店、订单或人员标识",
                    severity="HIGH"))

        metrics = result.get("metrics") or {}
        if tool == "query_pos_performance" and int(metrics.get("records") or 0) == 0:
            issues.append(_issue(
                "POS_T_PLUS_1_NOT_ARRIVED", source or "POS Sell-out", str(result.get("scope") or {}),
                blocking=False, fallback="不把缺记录当作零销售；仅输出其他数据源结论",
                human=True, detail="请求范围 POS 无记录，可能是 T+1 未到或未覆盖门店"))

        if tool == "query_material_inventory":
            months = (result.get("period") or {}).get("months") or []
            request_months = state.get("month") or []
            if months and request_months:
                snapshot = int((result.get("period") or {}).get("year") or state.get("year") or 0) * 12 + max(months)
                requested = int(state.get("year") or 0) * 12 + max(request_months)
                if requested - snapshot > 1:
                    issues.append(_issue(
                        "INVENTORY_SNAPSHOT_STALE", source or "库存", str(result.get("period") or {}),
                        blocking=False, fallback="使用最近月末快照做风险提示，不描述为实时库存",
                        human=True, detail="库存快照早于请求期间超过一个月"))

        if tool == "query_sku_bom":
            seen: dict[tuple, set] = defaultdict(set)
            for row in _iter_rows(result.get("rows")):
                if not isinstance(row, dict):
                    continue
                key_tuple = (row.get("sku_code"), row.get("item_type"), row.get("item_id"),
                             row.get("effective_date"))
                seen[key_tuple].add(row.get("bom_version"))
            conflicts = [key_tuple for key_tuple, versions in seen.items() if len(versions) > 1]
            if conflicts:
                issues.append(_issue(
                    "BOM_VERSION_CONFLICT", source or "SKU BOM", str(conflicts[:5]),
                    blocking=True, fallback="阻断采购与生产计算", human=True,
                    detail="同一 SKU、物料和生效日出现多个 BOM 版本", severity="HIGH"))

    if has_erp and has_pos:
        issues.append(_issue(
            "ERP_POS_CALIBER_DIFFERENCE", "ERP Sell-in + POS Sell-out", "本次联合分析",
            blocking=False, fallback="两个指标独立展示，只比较方向，不相加或互除",
            human=False, detail="Sell-in 与 Sell-out 业务环节和覆盖范围不同", severity="INFO"))

    if missing_meta:
        issues.append(_issue(
            "KEY_METADATA_MISSING", "Agent 工具返回", str(missing_meta),
            blocking=True, fallback="阻断可信结论并要求修复工具元数据", human=True,
            detail="缺少来源、期间、口径或表级追溯字段", severity="HIGH"))

    # 同一门店 ID 映射多个门店名才视为冲突；同一门店多 SKU/多月不算重复。
    store_names: dict[str, set[str]] = defaultdict(set)
    for result in successful.values():
        for row in _iter_rows(result.get("rows")):
            if isinstance(row, dict) and row.get("store_id"):
                name = row.get("store_name") or row.get("store")
                if name:
                    store_names[str(row["store_id"])].add(str(name))
    duplicate_ids = [sid for sid, names in store_names.items() if len(names) > 1]
    if duplicate_ids:
        issues.append(_issue(
            "STORE_ID_CONFLICT", "门店主数据", ",".join(duplicate_ids[:10]),
            blocking=True, fallback="阻断门店级结论，不自动猜测映射", human=True,
            detail="同一门店 ID 对应多个门店名称", severity="HIGH"))

    blocking = any(item["blocking"] for item in issues)
    material = [item for item in issues if item["severity"] != "INFO"]
    partial = any(not item["blocking"] for item in material)
    completeness = "LOW" if failed and len(failed) == len(expected) else ("MEDIUM" if failed else "HIGH")
    timeliness = "MEDIUM" if any(i["type"] in {
        "POS_T_PLUS_1_NOT_ARRIVED", "INVENTORY_SNAPSHOT_STALE"} for i in issues) else "HIGH"
    consistency = "LOW" if any(i["type"] in {
        "BOM_VERSION_CONFLICT", "STORE_ID_CONFLICT"} for i in issues) else "HIGH"
    traceability = "LOW" if missing_meta else "HIGH"
    dimensions = {"completeness": completeness, "timeliness": timeliness,
                  "consistency": consistency, "traceability": traceability}
    level = "LOW" if blocking or list(dimensions.values()).count("LOW") else (
        "MEDIUM" if partial or "MEDIUM" in dimensions.values() else "HIGH")
    conclusion_status = "BLOCKED" if blocking else ("PARTIAL" if partial else "VERIFIED")
    return {
        "level": level, "conclusion_status": conclusion_status, "blocking": blocking,
        "dimensions": dimensions, "issues": issues,
        "sources": sorted({source for source in sources if source}),
        "periods": periods, "used_data": used_data,
        "rules": "DETERMINISTIC_V1",
        "message": ("关键数据质量检查失败，禁止输出确定性结论" if blocking
                    else "存在数据限制，仅允许部分结论" if partial
                    else "数据完整性、时效性、一致性与可追溯性检查通过"),
    }
