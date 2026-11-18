"""Agent V2 Supervisor：确定性判断直达查询或复杂规划。"""
from __future__ import annotations

from . import control
from .supply_chain_workflow import is_supply_intent
from .tools import registry


DIRECT_BY_KIND = {
    "pos_review": ("direct_pos", "query_pos_performance"),
    "sfa_review": ("direct_sfa", "query_sfa_visit"),
    "wholesale_review": ("direct_wholesale", "query_wholesale_performance"),
}


def decide(question: str, analysis_intent: dict) -> dict:
    """只输出路由决策；不连库、不计算指标、不修改输入。"""
    q = (question or "").lower()
    kind = analysis_intent.get("type") or "attainment_review"
    semantic_tools = [name for name in (analysis_intent.get("semantic_tools") or [])
                      if name in registry.TOOLS and registry.TOOLS[name].get("public", True)]
    if (len(semantic_tools) == 1 and semantic_tools[0] != "query_customer_performance"
            and not analysis_intent.get("wants_cause")
            and not analysis_intent.get("cross")):
        tool = semantic_tools[0]
        return {"route": "direct", "workflow": "semantic_direct", "tool": tool,
                "requires_planner": False,
                "reason": "统一语义层已将指标映射到单一公开工具"}
    if is_supply_intent(kind):
        return {"route": "planned", "workflow": "supply_chain", "tool": "",
                "requires_planner": True,
                "reason": "供应链需要需求、BOM、库存/在途、确定性计算和 Checker"}
    if kind == "growth_opportunity":
        return {"route": "planned", "workflow": "growth_opportunity", "tool": "",
                "requires_planner": True,
                "reason": "需要高增长筛选、门店覆盖比对、SFA 三态和增量测算"}
    if kind == "price_anomaly":
        return {"route": "planned", "workflow": "price_anomaly", "tool": "",
                "requires_planner": True,
                "reason": "需要同 SKU/规格可比组、中位价、样本量与销量证据"}
    if (kind in ("attainment_review", "customer_focus")
            and (analysis_intent.get("wants_cause")
                 or ("预算" in q and any(word in q for word in
                                          ("实际", "差", "缺口", "达成", "目标"))))):
        return {"route": "planned", "workflow": "budget_diagnosis", "tool": "",
                "requires_planner": True,
                "reason": "预算未达成诊断需要预算、ERP 实际、客户下钻与辅助证据"}
    if analysis_intent.get("cross") or analysis_intent.get("wants_cause"):
        return {"route": "planned", "workflow": "business_diagnosis", "tool": "",
                "requires_planner": True, "reason": "原因诊断或跨模块问题需要多工具计划"}
    if kind in DIRECT_BY_KIND:
        workflow, tool = DIRECT_BY_KIND[kind]
        return {"route": "direct", "workflow": workflow, "tool": tool,
                "requires_planner": False, "reason": "单模块单指标查询"}

    diagnosis_words = ("达成", "完成", "缺口", "目标", "为什么", "原因", "诊断", "对比")
    if ("预算" in q and not any(word in q for word in diagnosis_words)
            and not any(word in q for word in ("实际", "出货"))):
        return {"route": "direct", "workflow": "direct_budget", "tool": "query_budget",
                "requires_planner": False, "reason": "单一预算指标查询"}
    if (any(word in q for word in ("erp", "实际出货", "实际销售", "出货多少"))
            and "预算" not in q and not any(word in q for word in diagnosis_words)):
        return {"route": "direct", "workflow": "direct_erp", "tool": "query_erp_actual",
                "requires_planner": False, "reason": "单一 ERP 实际指标查询"}
    return {"route": "planned", "workflow": "business_analysis", "tool": "",
            "requires_planner": True, "reason": "需要组合预算、实际或维度下钻"}


def _record(level: str, target: str, scope: dict, tools: list[str],
            metrics: dict, evidence: list[dict], finding: str,
            contribution=None, next_action: str = "", decision_reason: str = "",
            stop_reason: str = "") -> dict:
    return {
        "drill_level": level, "analysis_target": target, "query_scope": scope,
        "source_tool": tools, "data_source": [e.get("data_source") for e in evidence
                                                if e.get("data_source")],
        "key_metrics": metrics, "evidence": evidence, "finding": finding,
        "contribution_to_gap": contribution, "next_action": next_action,
        "decision_reason": decision_reason, "stop_reason": stop_reason,
    }


def _plan_item(key: str, tool: str, args: dict, why: str, label: str) -> dict:
    return {"key": key, "tool": tool, "args": registry.filter_args(tool, args),
            "why": why, "label": label}


def _record_evidence_signature(record: dict) -> tuple:
    evidence = record.get("evidence") or []
    return tuple(sorted((str(item.get("claim") or ""), str(item.get("value") or ""),
                         str(item.get("data_source") or item.get("source") or ""))
                        for item in evidence if isinstance(item, dict)))


def _worst(rows_a: list[dict], rows_b: list[dict], dim: str,
           actual_key: str = "te", budget_key: str = "budget_te") -> dict | None:
    budget = {row.get(dim): row.get(budget_key) for row in rows_b}
    candidates = []
    for row in rows_a:
        name = row.get(dim)
        actual = row.get(actual_key)
        target = budget.get(name)
        if name and actual is not None and target is not None:
            candidates.append({dim: name, "actual_te": actual, "budget_te": target,
                               "gap_te": round(actual - target, 2)})
    return min(candidates, key=lambda item: item["gap_te"], default=None)


def review(state: dict) -> dict:
    """依据已验证证据决定继续下钻或停止，不调用 LLM。"""
    workflow = (state.get("supervisor_decision") or {}).get("workflow") or ""
    if workflow != "budget_diagnosis":
        return {"action": "stop", "next_plan": [], "stop_reason": "EVIDENCE_SUFFICIENT",
                "drill_records": list(state.get("drill_records") or []),
                "confidence": "high" if (state.get("checker") or {}).get("verified") else "mid",
                "decision": "专业 Workflow 已完成确定性校验"}

    records = list(state.get("drill_records") or [])
    results = state.get("tool_results") or {}
    calls = state.get("tool_calls") or []
    depth = int(state.get("drill_depth") or 0)
    limits = state.get("control_limits") or control.limits()
    if (len(records) >= 2
            and _record_evidence_signature(records[-1])
            and _record_evidence_signature(records[-1]) == _record_evidence_signature(records[-2])):
        records[-1]["stop_reason"] = control.STOP_NO_NEW_EVIDENCE
        return {"action": "stop", "next_plan": [],
                "stop_reason": control.STOP_NO_NEW_EVIDENCE,
                "drill_records": records, "confidence": "mid",
                "decision": "连续下钻未产生新证据，停止继续调用工具"}
    if len(calls) >= int(limits["max_tool_calls"]):
        return {"action": "stop", "next_plan": [],
                "stop_reason": control.STOP_MAX_TOOL_CALLS,
                "drill_records": records, "confidence": "mid",
                "decision": "达到最大工具调用次数，停止继续下钻"}
    if depth >= int(limits["max_drill_depth"]):
        return {"action": "stop", "next_plan": [], "stop_reason": control.STOP_MAX_DEPTH,
                "drill_records": records, "confidence": "mid",
                "decision": "达到最大下钻深度"}

    budget, erp = results.get("budget") or {}, results.get("erp") or {}
    bm, em = budget.get("metrics") or {}, erp.get("metrics") or {}
    budget_te, actual_te = bm.get("budget_te"), em.get("te")
    attainment = (round(actual_te / budget_te * 100, 2)
                   if actual_te is not None and budget_te not in (None, 0) else None)
    gap = (round(actual_te - budget_te, 2)
           if actual_te is not None and budget_te is not None else None)
    scope = {"region": state.get("region") or "全国", "year": state.get("year"),
             "months": state.get("month") or []}

    if not records:
        contradicted = bool(attainment is not None and attainment >= 100 and any(
            word in (state.get("user_question") or "")
            for word in ("未完成", "没完成", "没有完成", "未达成")))
        wants_detail = bool((state.get("analysis_intent") or {}).get("wants_cause"))
        finding = (f"整体达成率 {attainment:.2f}%，用户的未完成前提不成立"
                   if contradicted else f"整体达成率 {attainment:.2f}%")
        next_action = ("下钻内部客户缺口"
                       if wants_detail and attainment is not None and attainment >= 100 else "")
        records.append(_record(
            "overall", state.get("region") or "全国", scope,
            ["query_budget", "query_erp_actual"],
            {"budget_te": budget_te, "actual_te": actual_te,
             "attainment_pct": attainment, "gap_te": gap},
            [{"claim": "同期预算 TE", "value": budget_te,
              "data_source": budget.get("data_source")},
             {"claim": "ERP Sell-in 实际 TE", "value": actual_te,
              "data_source": erp.get("data_source")}],
            finding, next_action=next_action,
            decision_reason=(("整体前提被数据推翻，但用户仍要求主要缺口，故只补查内部客户"
                              if contradicted else "整体已达成，但用户要求原因分析，补查客户结构")
                             if next_action else "整体证据足以回答当前问题")))
        if next_action:
            args = {"region": state.get("region") or "", "year": state.get("year"),
                    "months": state.get("month") or [], "top_n": 8}
            return {"action": "continue", "next_plan": [_plan_item(
                "customer", "query_customer_performance", args,
                "整体虽超额，但按用户要求识别内部未达成客户", "内部客户缺口")],
                "stop_reason": "", "drill_records": records, "confidence": "mid",
                "decision": ("前提矛盾；有限下钻内部客户缺口" if contradicted
                             else "整体达成；按原因分析要求有限下钻客户结构")}
        if contradicted:
            records[-1]["stop_reason"] = control.STOP_PREMISE_CONTRADICTED
            return {"action": "stop", "next_plan": [],
                    "stop_reason": control.STOP_PREMISE_CONTRADICTED,
                    "drill_records": records, "confidence": "high",
                    "decision": "整体达成，纠正前提后停止"}

        worst_dept = _worst((erp.get("rows") or {}).get("by_department") or [],
                            (budget.get("rows") or {}).get("by_department") or [],
                            "department")
        if not worst_dept or worst_dept["gap_te"] >= -float(limits["stop_threshold_te"]):
            records[-1]["stop_reason"] = control.STOP_NO_MATERIAL_GAP
            return {"action": "stop", "next_plan": [],
                    "stop_reason": control.STOP_NO_MATERIAL_GAP,
                    "drill_records": records, "confidence": "high",
                    "decision": "未发现达到阈值的部门缺口"}
        dept = worst_dept["department"]
        records[-1]["next_action"] = f"下钻 {dept} 市场部"
        records[-1]["decision_reason"] = f"{dept} 的 TE 缺口最负"
        args = {"region": state.get("region") or "", "department": dept,
                "year": state.get("year"), "months": state.get("month") or []}
        return {"action": "continue", "next_plan": [
            _plan_item("budget_drill", "query_budget", args, "复核主缺口市场部预算", f"{dept}预算"),
            _plan_item("erp_drill", "query_erp_actual", args, "复核主缺口市场部实际", f"{dept}实际")],
            "stop_reason": "", "drill_records": records, "confidence": "mid",
            "decision": f"选择缺口最大的 {dept} 市场部继续下钻"}

    customer = results.get("customer") or {}
    if customer.get("ok"):
        top_gap = (customer.get("rows") or {}).get("top_gap") or []
        gap_pool = round(sum(abs(row.get("gap_te") or 0) for row in top_gap), 2)
        asked_unmet = any(word in (state.get("user_question") or "")
                          for word in ("未完成", "没完成", "没有完成", "未达成"))
        final_stop = (control.STOP_PREMISE_CONTRADICTED_WITH_INTERNAL_GAPS
                      if attainment is not None and attainment >= 100 and asked_unmet
                      else control.STOP_EVIDENCE_SUFFICIENT)
        records.append(_record(
            "customer", "内部未达成客户", customer.get("scope") or scope,
            ["query_customer_performance"], customer.get("metrics") or {},
            [{"claim": f"{row.get('customer')} 客户缺口", "value": row.get("gap_te"),
              "data_source": customer.get("data_source")} for row in top_gap[:5]],
            f"识别 {len(top_gap)} 个主要未达成客户，Top 缺口池 {gap_pool:.2f} TE",
            contribution=top_gap[:5], next_action="停止下钻",
            decision_reason="整体已超额，客户缺口已足以说明内部结构，不需调用 POS/SFA",
            stop_reason=final_stop))
        return {"action": "stop", "next_plan": [],
                "stop_reason": final_stop,
                "drill_records": records, "confidence": "high",
                "decision": "前提纠正与内部缺口证据均已充分"}

    budget_drill, erp_drill = results.get("budget_drill") or {}, results.get("erp_drill") or {}
    if budget_drill.get("ok") and erp_drill.get("ok"):
        dept = (budget_drill.get("scope") or {}).get("department") or "主缺口市场部"
        worst_segment = _worst((erp_drill.get("rows") or {}).get("by_segment") or [],
                               (budget_drill.get("rows") or {}).get("by_segment") or [],
                               "segment")
        if not worst_segment:
            return {"action": "stop", "next_plan": [],
                    "stop_reason": control.STOP_EVIDENCE_SUFFICIENT,
                    "drill_records": records, "confidence": "mid",
                    "decision": "市场部数据可用，但无可比客户类型"}
        segment = worst_segment["segment"]
        records.append(_record(
            "department_channel", f"{dept}/{segment}",
            {"region": state.get("region"), "department": dept, "segment": segment,
             "year": state.get("year"), "months": state.get("month") or []},
            ["query_budget", "query_erp_actual"], worst_segment,
            [{"claim": "客户类型缺口", "value": worst_segment["gap_te"],
              "data_source": erp_drill.get("data_source")}],
            f"{dept} 中 {segment} 客户类型缺口最负",
            next_action="下钻客户", decision_reason="选择缺口最负的可比客户类型"))
        args = {"region": state.get("region") or "", "department": dept,
                "segment": segment, "year": state.get("year"),
                "months": state.get("month") or [], "top_n": 8}
        return {"action": "continue", "next_plan": [_plan_item(
            "customer", "query_customer_performance", args,
            "定位主缺口市场部与客户类型内的客户贡献", "客户缺口")],
            "stop_reason": "", "drill_records": records, "confidence": "mid",
            "decision": f"从 {dept}/{segment} 下钻到客户"}

    return {"action": "stop", "next_plan": [], "stop_reason": control.STOP_EVIDENCE_SUFFICIENT,
            "drill_records": records, "confidence": "mid",
            "decision": "现有证据已达到最小要求"}
