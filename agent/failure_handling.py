"""确定性失败事件、终态与输出护栏。

本模块不调用 LLM、不连数据库，只把已经发生的权限、数据质量、模型降级与
Supervisor 停止原因收敛为统一契约。这样页面、KPI 和恢复入口不会各自猜状态。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


EXECUTION_STATUSES = {
    "SUCCESS", "PARTIAL", "FALLBACK", "BLOCKED",
    "PERMISSION_DENIED", "NEEDS_CLARIFICATION",
}

_PRIORITY = {
    "SUCCESS": 0, "FALLBACK": 1, "PARTIAL": 2, "BLOCKED": 3,
    "NEEDS_CLARIFICATION": 4, "PERMISSION_DENIED": 5,
}

_BLOCKING_CONFLICTS = {
    "BOM_VERSION_CONFLICT", "BOM_VERSION_MISSING", "SKU_UNMATCHED",
    "STORE_ID_CONFLICT", "KEY_METADATA_MISSING",
}

_TOOL_BY_CODE = {
    "POS_T_PLUS_1_NOT_ARRIVED": "query_pos_performance",
    "BOM_VERSION_CONFLICT": "query_sku_bom",
    "BOM_VERSION_MISSING": "query_sku_bom",
    "SKU_UNMATCHED": "query_sku_bom",
    "INVENTORY_SNAPSHOT_STALE": "query_material_inventory",
}


def _event(request_id: str, *, failure_type: str, failure_code: str,
           failure_stage: str, source_tool: str = "",
           affected_entities: list[str] | None = None, severity: str = "MEDIUM",
           retry_count: int = 0, fallback_used: bool = False,
           fallback_reason: str = "", execution_status: str,
           recovery_action: str, requires_human_review: bool = False,
           detail: str = "") -> dict:
    return {
        "request_id": request_id,
        "failure_type": failure_type,
        "failure_code": failure_code,
        "failure_stage": failure_stage,
        "source_tool": source_tool,
        "affected_entities": list(affected_entities or []),
        "severity": severity,
        "retry_count": int(retry_count or 0),
        "fallback_used": bool(fallback_used),
        "fallback_reason": fallback_reason or "",
        "execution_status": execution_status,
        "recovery_action": recovery_action,
        "requires_human_review": bool(requires_human_review),
        "detail": detail or "",
    }


def _quality_event(request_id: str, issue: dict) -> dict:
    code = str(issue.get("type") or "DATA_QUALITY_ISSUE")
    blocking = bool(issue.get("blocking"))
    if code == "POS_T_PLUS_1_NOT_ARRIVED":
        status, failure_type = "PARTIAL", "DATA_FRESHNESS"
        recovery = "POS T+1 数据更新后，使用同一 request_id 重新执行 Sell-out 分析"
    elif code in _BLOCKING_CONFLICTS:
        status, failure_type = "BLOCKED", "DATA_CONFLICT"
        recovery = "修复 BOM、SKU 映射或业务日期冲突并经人工确认后重新计算"
    elif code == "TOOL_CALL_FAILED":
        status, failure_type = ("BLOCKED" if blocking else "PARTIAL"), "TOOL_FAILURE"
        recovery = "恢复数据工具后重新执行；关键工具仍失败时继续阻断结论"
    else:
        status, failure_type = ("BLOCKED" if blocking else "PARTIAL"), "DATA_QUALITY"
        recovery = str(issue.get("fallback") or "修复数据后重新执行")
    scope = str(issue.get("impact_scope") or "").strip()
    return _event(
        request_id, failure_type=failure_type, failure_code=code,
        failure_stage="data_quality_check", source_tool=_TOOL_BY_CODE.get(code, ""),
        affected_entities=[scope] if scope else [],
        severity=str(issue.get("severity") or ("HIGH" if blocking else "MEDIUM")),
        fallback_used=not blocking, fallback_reason=str(issue.get("fallback") or ""),
        execution_status=status, recovery_action=recovery,
        requires_human_review=bool(issue.get("human_confirmation_required")),
        detail=str(issue.get("detail") or ""))


def _model_code(reason: str) -> str:
    lower = reason.lower()
    if ("checker_not_passed" in lower or "non_report_terminal_state" in lower
            or "no_safe_candidate" in lower):
        return "LLM_SKIPPED_BY_GUARD"
    if "timeout" in lower or "超时" in lower:
        return "LLM_TIMEOUT"
    if "emptyresponse" in lower or "空" in lower:
        return "LLM_EMPTY_RESPONSE"
    if "invalidjson" in lower or "validationerror" in lower or "schema" in lower:
        return "LLM_SCHEMA_INVALID"
    if "not_available" in lower or "notconfigured" in lower:
        return "LLM_NOT_AVAILABLE"
    return "LLM_RETRY_EXHAUSTED"


def build_failure_handling(final: dict, request_id: str,
                           parent_request_id: str = "") -> dict:
    """从最终 state 生成统一失败事件与确定性终态。"""
    events: list[dict] = []
    availability = final.get("data_availability") or {}
    permission = availability.get("permission") or {}
    if (availability.get("status") == "PERMISSION_DENIED"
            or permission.get("status") == "PERMISSION_DENIED"):
        principal = permission.get("principal") or {}
        detail = str(permission.get("reason") or permission.get("message")
                     or availability.get("message") or "请求超出授权范围")
        events.append(_event(
            request_id, failure_type="AUTHORIZATION", failure_code="PERMISSION_DENIED",
            failure_stage="permission_preflight", affected_entities=[detail], severity="HIGH",
            execution_status="PERMISSION_DENIED",
            recovery_action="改为本人授权范围；跨战区仅可使用脱敏汇总或最佳实践知识",
            requires_human_review=False,
            detail=f"{principal.get('display_name') or '当前用户'}：{detail}"))

    if final.get("need_clarification"):
        clarification = str(final.get("clarification") or
                            (final.get("report") or {}).get("problems", {}).get("summary") or
                            "缺少必要业务实体")
        events.append(_event(
            request_id, failure_type="ANALYSIS_PATH", failure_code="MISSING_REQUIRED_ENTITY",
            failure_stage="intent_and_entity_resolution", severity="MEDIUM",
            execution_status="NEEDS_CLARIFICATION",
            recovery_action="补充必要的时间、范围、SKU 或门店实体后继续执行",
            detail=clarification))

    for issue in (final.get("data_quality") or {}).get("issues") or []:
        # INFO 是口径披露，不是失败；仍会保留在 governance/data_limits 中。
        if str(issue.get("severity") or "").upper() == "INFO":
            continue
        events.append(_quality_event(request_id, issue))

    for trace in final.get("llm_trace") or []:
        if not trace.get("fallback_used"):
            continue
        reason = str(trace.get("fallback_reason") or "模型调用失败")
        attempts = int(trace.get("attempts") or 1)
        events.append(_event(
            request_id, failure_type="MODEL_FAILURE", failure_code=_model_code(reason),
            failure_stage=str(trace.get("llm_stage") or "llm"), source_tool="DeepSeek",
            affected_entities=[str(trace.get("llm_stage") or "llm")], severity="MEDIUM",
            retry_count=max(0, attempts - 1), fallback_used=True,
            fallback_reason=reason, execution_status="FALLBACK",
            recovery_action="模型恢复后使用同一 request_id 重新执行；当前仅采用确定性规则或模板",
            detail=("真实模型调用失败，已明确降级" if trace.get("llm_used")
                    else "模型未调用，已使用确定性降级")))

    stop_reason = str(final.get("stop_reason") or "")
    if stop_reason in {"MAX_DRILL_DEPTH_REACHED", "MAX_TOOL_CALLS_REACHED",
                       "NO_NEW_EVIDENCE"}:
        events.append(_event(
            request_id, failure_type="ANALYSIS_PATH", failure_code=stop_reason,
            failure_stage="supervisor_review", severity="MEDIUM",
            execution_status="PARTIAL",
            recovery_action="补充新证据或缩小分析范围后继续；不会无限调用工具",
            detail="已达到确定性停止条件，保留已有证据支持的部分结论"))

    # 兜住未被数据质量模块展开的业务工具异常。
    known_codes = {event["failure_code"] for event in events}
    for error in final.get("errors") or []:
        detail = error.get("error") or {}
        code = str(detail.get("type") or error.get("code") or "TOOL_CALL_FAILED")
        if code in known_codes or code == "CheckerFailed":
            continue
        events.append(_event(
            request_id, failure_type="TOOL_FAILURE", failure_code=code,
            failure_stage=str(error.get("node") or "tool_execution"),
            source_tool=str(error.get("tool") or ""), severity="HIGH",
            execution_status="BLOCKED",
            recovery_action="恢复关键工具后重新执行",
            requires_human_review=True, detail=str(detail.get("message") or detail)))

    status = max((event["execution_status"] for event in events),
                 key=lambda value: _PRIORITY[value], default="SUCCESS")
    recovery_actions = list(dict.fromkeys(
        event["recovery_action"] for event in events if event.get("recovery_action")))
    return {
        "state_machine": "DETERMINISTIC_FAILURE_V1",
        "execution_status": status,
        "events": events,
        "can_continue": status in {"SUCCESS", "PARTIAL", "FALLBACK"},
        "fallback_mode": ("DETERMINISTIC_RULE_OR_TEMPLATE"
                          if any(event["fallback_used"] for event in events) else "NONE"),
        "requires_human_review": any(event["requires_human_review"] for event in events),
        "recovery_actions": recovery_actions,
        "parent_request_id": parent_request_id or "",
        "retry_with_request_id": request_id if events else "",
    }


def apply_output_guard(report: dict, final: dict, handling: dict) -> dict:
    """在响应边界移除不能安全展示的数字，不改变任何计算或源数据。"""
    guarded = deepcopy(report or {})
    codes = {event.get("failure_code") for event in handling.get("events") or []}
    if codes & _BLOCKING_CONFLICTS:
        guarded.update({
            "headline": "数据冲突，已阻断受影响的采购与需求计算",
            "result": {"rows": []},
            "llm_report": {},
            "sections": [],
            "actions": [{"action": "修复冲突并经人工确认后重新计算",
                         "owner": "数据管理员/采购人员",
                         "check": "Checker 重新通过后才允许展示采购数量"}],
            "blocked_output": True,
        })
    if "POS_T_PLUS_1_NOT_ARRIVED" in codes:
        successful_tools = {
            str(result.get("tool") or "") for result in (final.get("tool_results") or {}).values()
            if isinstance(result, dict) and result.get("ok")
        }
        if successful_tools <= {"query_pos_performance"}:
            checker = guarded.get("checker") or {}
            generator = guarded.get("generator") or {"mode": "data_guard"}
            guarded = {
                "headline": "POS T+1 数据未到，暂不能判断门店 Sell-out",
                "period_label": guarded.get("period_label") or "—",
                "scope_label": guarded.get("scope_label") or "—",
                "result": {"rows": []},
                "sections": [],
                "llm_report": {},
                "problems": {"summary": "请求期间 POS 无记录；该状态不能解释为零销售。",
                             "rows": []},
                "causes": {"verified": [], "possible": [],
                           "unsupported": ["等待 POS T+1 数据更新后重查"]},
                "actions": [{"action": "数据更新后重新执行 Sell-out 分析",
                             "owner": "数据管理员/分析人员",
                             "check": "POS 工具返回有效记录"}],
                "basis": {"period": guarded.get("period_label") or "—", "tools": [],
                          "cross_checks": [], "data_gaps": ["POS T+1 数据未到"]},
                "checker": checker,
                "generator": generator,
                "question": guarded.get("question") or final.get("user_question") or "",
                "markdown": ("### POS T+1 数据未到\n\n请求期间没有可验证的 Sell-out 记录。"
                             "无记录不等于零销售，待数据更新后重新执行。"),
            }
        limits = list(guarded.get("data_limits") or [])
        limits.append({
            "label": "POS_T_PLUS_1_NOT_ARRIVED",
            "have": "其他已通过质量检查的数据仍可用于部分分析",
            "missing": "请求期间 POS Sell-out 记录；无记录不等于零销售",
            "offer": "Sell-in 与 Sell-out 保持独立口径；POS 更新后重新执行",
        })
        guarded["data_limits"] = limits
    guarded["execution_status"] = handling.get("execution_status") or "SUCCESS"
    return guarded


def apply_tool_output_guard(tool_calls: list[dict], tool_data: list[dict],
                            handling: dict) -> tuple[list[dict], list[dict]]:
    """避免失败结果在工具卡片/证据表中重新泄露成确定性数字。"""
    codes = {event.get("failure_code") for event in handling.get("events") or []}
    calls = deepcopy(tool_calls or [])
    data = deepcopy(tool_data or [])
    if "POS_T_PLUS_1_NOT_ARRIVED" in codes:
        for call in calls:
            if call.get("tool") == "query_pos_performance":
                call["summary"] = "POS T+1 数据未到；无记录不等于零销售"
        for item in data:
            if item.get("tool") == "query_pos_performance":
                item["summary"] = "POS T+1 数据未到；无记录不等于零销售"
                item["metrics"] = {"records": None, "data_status": "T_PLUS_1_PENDING"}
                item["groups"] = []
                item["notes"] = list(item.get("notes") or []) + [
                    "请求期间无 POS 记录，未展示零值指标；等待 T+1 数据后重查。"]
    if codes & _BLOCKING_CONFLICTS:
        for item in data:
            if item.get("tool") == "calculate_purchase_suggestion":
                item["metrics"] = {"calculation_status": "BLOCKED_BY_DATA_CONFLICT"}
                item["groups"] = []
                item["notes"] = ["BOM/SKU/日期冲突未解决，采购数量已从响应中移除。"]
    return calls, data


def apply_trace_output_guard(evidence: list[dict], steps: list[dict],
                             handling: dict) -> tuple[list[dict], list[dict]]:
    """清理报告外层证据与执行轨迹中的误导性零值摘要。"""
    codes = {event.get("failure_code") for event in handling.get("events") or []}
    safe_evidence = deepcopy(evidence or [])
    safe_steps = deepcopy(steps or [])
    if "POS_T_PLUS_1_NOT_ARRIVED" in codes:
        safe_evidence = [item for item in safe_evidence
                         if "POS" not in str(item.get("source") or item.get("data_source") or "")]
        for step in safe_steps:
            if (step.get("node") == "call_business_tools"
                    and "query_pos_performance" in str(step.get("detail") or "")):
                step["detail"] = "query_pos_performance：POS T+1 数据未到，未生成零值结论"
    return safe_evidence, safe_steps
