"""运行入口：把请求变成一次图执行，并整理成稳定的响应结构。

两个入口：
- ``run()``：一次性返回完整结果（/api/agent/chat 用）；
- ``stream()``：逐节点产出事件（/api/agent/stream 用 SSE），前端据此展示
  「正在理解问题 → 正在查预算 → 正在查客户 → 正在写报告」。

两者共用同一张图、同一份状态构造，避免「流式和非流式结果不一致」这种经典坑。
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Iterator

from . import charts, data_governance, demo_contract, failure_handling, kpi, llm, memory, tools
from .graph import GRAPH
from .schemas import AgentEnvelope
from .state import new_state

ENGINE = "langgraph"


def _state(question: str, thread_id: str, context: dict | None,
           history: list | None, server_context: dict | None = None) -> dict:
    state = dict(new_state(question, thread_id=thread_id, history=history,
                           context=context))
    state["auth_context"] = data_governance.resolve_principal(server_context)
    return state


def _understood(final: dict) -> dict:
    it = final.get("analysis_intent") or {}
    period = ((final.get("analysis_result") or {}).get("period") or {})
    return {
        "intent": it.get("type") or "unknown",
        "wants_cause": bool(it.get("wants_cause")),
        "focus": it.get("focus") or [],
        "region": final.get("region") or "",
        "department": final.get("department") or "",
        "customer": final.get("customer") or "",
        "year": final.get("year") or 0,
        "months": final.get("month") or [],
        # P8：页面上下文与业务维度，前端「本次理解」要逐项显示，便于核对
        "page_id": final.get("page_id") or "",
        "page_name": final.get("page_name") or "",
        "module": final.get("module") or "",
        "channel": final.get("channel") or "",
        "category": final.get("category") or "",
        "ka_system": final.get("ka_system") or "",
        "store": final.get("store") or "",
        "sku": final.get("sku") or "",
        "segment": final.get("segment") or "",
        "growth_pct": it.get("growth_pct"),
        "pieces_per_box": it.get("pieces_per_box"),
        "ka_from_customer": final.get("ka_from_customer") or "",
        "period_label": period.get("label") or "",
        "by": final.get("understood_by") or "rules",
        "llm_overridden": bool(it.get("llm_overridden")),
    }


def _plan(final: dict) -> dict:
    return {
        "mode": final.get("plan_mode") or "",
        "generation_mode": final.get("planner_generation_mode") or "",
        "rationale": final.get("plan_rationale") or "",
        "items": [{k: item.get(k) for k in ("key", "tool", "label", "why")}
                  for item in (final.get("analysis_plan") or [])],
    }


MAX_ROWS_PER_GROUP = 40   # 前端表格可滚动，超过这个数只截断展示，不影响任何计算结果


def _flat_metrics(metrics: dict | None) -> dict:
    """把指标里的嵌套日期区间压成可读字符串，其余标量原样保留。"""
    out: dict[str, Any] = {}
    for k, v in (metrics or {}).items():
        if isinstance(v, dict):
            if "from" in v or "to" in v:
                out[k] = f"{v.get('from') or '—'} ~ {v.get('to') or '—'}"
        elif isinstance(v, list):
            continue
        else:
            out[k] = v
    return out


def _tool_data(final: dict) -> list[dict]:
    """把每个工具查回来的数据行整理成前端能直接渲染的表。

    这是「数据核对」区的数据源：报告里的每个数字，都能在这里找到对应的行。
    只做截断与扁平化，不做任何加工，避免出现「页面展示」和「报告结论」两套口径。
    """
    calls = {c.get("key"): c for c in (final.get("tool_calls") or [])}
    out: list[dict] = []
    for key, res in (final.get("tool_results") or {}).items():
        if not isinstance(res, dict):
            continue
        call = calls.get(key) or {}
        rows = res.get("rows")
        groups: list[dict] = []
        if isinstance(rows, dict):
            for name, items in rows.items():
                if isinstance(items, list) and items:
                    groups.append({"name": name, "total": len(items),
                                   "rows": items[:MAX_ROWS_PER_GROUP]})
        elif isinstance(rows, list) and rows:
            groups.append({"name": "rows", "total": len(rows),
                           "rows": rows[:MAX_ROWS_PER_GROUP]})
        out.append({
            "key": key,
            "tool": call.get("tool") or "",
            "label": call.get("label") or call.get("tool") or key,
            "ok": bool(res.get("ok")),
            "summary": call.get("summary") or "",
            "args": res.get("args") or call.get("args") or {},
            "period": res.get("period") or {},
            "scope": res.get("scope") or {},
            "caliber": res.get("caliber") or "",
            "source": res.get("data_source") or "",
            "tables": res.get("tables") or [],
            "metrics": _flat_metrics(res.get("metrics")),
            "groups": groups,
            "notes": res.get("notes") or [],
            "error": res.get("error"),
        })
    return out


def _envelope(final: dict, started: float, run_id: str,
              parent_request_id: str = "") -> dict:
    scene = demo_contract.scene(final)
    report = dict(final.get("report") or {})
    quality = final.get("data_quality") or {}
    permission = ((final.get("data_availability") or {}).get("permission") or {})
    failures = failure_handling.build_failure_handling(
        final, run_id, parent_request_id=parent_request_id)
    report = failure_handling.apply_output_guard(report, final, failures)
    safe_tool_calls, safe_tool_data = failure_handling.apply_tool_output_guard(
        final.get("tool_calls") or [], _tool_data(final), failures)
    safe_evidence, safe_steps = failure_handling.apply_trace_output_guard(
        final.get("evidence") or [], final.get("steps") or [], failures)
    report["governance"] = {
        "permission": permission,
        "data_quality": quality,
        "confidence": quality.get("level") or final.get("confidence") or "LOW",
    }
    if quality.get("issues"):
        limits = list(report.get("data_limits") or [])
        for issue in quality["issues"]:
            limits.append({
                "label": issue.get("type") or "DATA_QUALITY",
                "have": issue.get("detail") or "已识别数据限制",
                "missing": issue.get("impact_scope") or "待确认范围",
                "offer": issue.get("fallback") or "请人工确认",
            })
        report["data_limits"] = limits
    report["unified"] = demo_contract.unified(
        {**final, "report": report, "steps": safe_steps, "evidence": safe_evidence}, scene)
    chart = charts.build(safe_tool_data, final.get("checker") or {},
                         failures["execution_status"], final.get("semantic") or {})
    data = {
        "thread_id": final.get("thread_id") or "",
        "run_id": run_id,
        "parent_request_id": parent_request_id or "",
        "execution_status": failures["execution_status"],
        "failure_handling": failures,
        "question": final.get("user_question") or "",
        "understood": _understood(final),
        "semantic": final.get("semantic") or {},
        "supervisor": final.get("supervisor_decision") or {},
        "scene": scene,
        "governance": {
            "entity_sources": final.get("entity_sources") or {},
            "data_availability": final.get("data_availability") or {},
            "control_limits": final.get("control_limits") or {},
            "task_status": final.get("task_status") or "",
            "drill_depth": final.get("drill_depth") or 0,
            "drill_records": final.get("drill_records") or [],
            "stop_reason": final.get("stop_reason") or "",
            "confidence": final.get("confidence") or "low",
            "decision_log": final.get("decision_log") or [],
            "permission": ((final.get("data_availability") or {}).get("permission") or {}),
            "data_quality": final.get("data_quality") or {},
        },
        "plan": _plan(final),
        "tools": safe_tool_calls,
        "tool_data": safe_tool_data,
        "chart": chart,
        "report": report,
        "evidence": safe_evidence,
        "cross_checks": final.get("cross_checks") or [],
        "steps": safe_steps,
        "errors": final.get("errors") or [],
        "timings": {**(final.get("timings") or {}),
                    "total": round((time.perf_counter() - started) * 1000, 1)},
        "llm": llm.config_summary(),
        "llm_usage": llm.usage_summary(),
        "llm_trace": final.get("llm_trace") or [],
        "needs_clarification": bool(final.get("need_clarification")),
    }
    meta = {"engine": ENGINE, "graph_nodes": 12,
            "tools_available": tools.registry.public_count()}
    payload = {"ok": True, "data": data, "meta": meta}

    # 结构自检：模型字段漂移时这里会报出来（不阻塞返回，但记在 meta 里）
    try:
        AgentEnvelope.model_validate(payload)
    except Exception as exc:                                  # pragma: no cover
        meta["schema_warning"] = str(exc)[:200]
    return payload


def run(question: str, thread_id: str = "", context: dict | None = None,
        history: list | None = None, *, as_of: str = "",
        server_context: dict | None = None, parent_request_id: str = "") -> dict:
    """执行一次完整分析。异常一律收敛为 ``ok=false``，不把栈抛给前端。"""
    started = time.perf_counter()
    llm.reset_usage()
    run_id = as_of or uuid.uuid4().hex[:12]
    state = _state(question, thread_id, context, history, server_context)
    try:
        final = GRAPH.invoke(state)
    except Exception as exc:                                  # pragma: no cover
        kpi.record_failure(run_id, latency_ms=(time.perf_counter() - started) * 1000,
                           error_type=type(exc).__name__)
        return {"ok": False,
                "error": {"type": type(exc).__name__, "message": str(exc)},
                "meta": {"engine": ENGINE, "run_id": run_id}}
    payload = _envelope(final, started, run_id, parent_request_id)
    kpi.record_run(payload)
    # P10：只为本轮会话记住「范围 + 结论焦点」，供下一句的指代消解使用
    memory.remember(thread_id or final.get("thread_id") or "", payload["data"])
    return payload


def stream(question: str, thread_id: str = "", context: dict | None = None,
           history: list | None = None, server_context: dict | None = None,
           request_id: str = "", parent_request_id: str = "") -> Iterator[dict]:
    """逐节点产出事件；最后一个事件是 ``done``，带完整 envelope。

    事件格式刻意保持扁平、可 JSON 序列化，SSE 层只负责加 ``data:`` 前缀。
    """
    started = time.perf_counter()
    llm.reset_usage()
    run_id = request_id or uuid.uuid4().hex[:12]
    state = _state(question, thread_id, context, history, server_context)
    yield {"event": "start", "run_id": run_id, "llm": llm.config_summary()}

    final: dict[str, Any] = dict(state)
    try:
        for chunk in GRAPH.stream(state, stream_mode="updates"):
            for node, update in (chunk or {}).items():
                if isinstance(update, dict):
                    final.update(update)
                yield {"event": "node", "node": node,
                       "detail": _node_detail(node, update)}
                if node == "call_business_tools":
                    for call in (final.get("tool_calls") or []):
                        if call.get("key") not in (state.get("_emitted") or []):
                            state.setdefault("_emitted", []).append(call.get("key"))
                            yield {"event": "tool", "tool": call.get("tool"),
                                   "label": call.get("label"), "ok": call.get("ok"),
                                   "elapsed_ms": call.get("elapsed_ms"),
                                   "summary": call.get("summary")}
    except Exception as exc:                                  # pragma: no cover
        kpi.record_failure(run_id, latency_ms=(time.perf_counter() - started) * 1000,
                           error_type=type(exc).__name__)
        yield {"event": "error", "error": {"type": type(exc).__name__,
                                           "message": str(exc)}}
        return

    payload = _envelope(final, started, run_id, parent_request_id)
    kpi.record_run(payload)
    memory.remember(thread_id or final.get("thread_id") or "", payload["data"])
    yield {"event": "report", "report": payload["data"]["report"]}
    yield {"event": "done", "data": payload["data"], "meta": payload["meta"]}


def _node_detail(node: str, update: Any) -> str:
    if not isinstance(update, dict):
        return ""
    if node == "understand_question":
        return "解析问题"
    if node == "plan_analysis":
        return f"计划 {len(update.get('analysis_plan') or [])} 个查询"
    if node == "supervise_task":
        decision = update.get("supervisor_decision") or {}
        return f"Supervisor：{decision.get('route') or 'planned'}"
    if node == "preflight_check":
        return f"执行前检查：{(update.get('data_availability') or {}).get('status') or '完成'}"
    if node == "plan_direct_query":
        return "简单查询直达一个工具"
    if node == "call_business_tools":
        return f"取数 {len(update.get('tool_calls') or [])} 项"
    if node == "check_results":
        checker = update.get("checker") or {}
        return f"确定性校验：{checker.get('status') or '完成'}"
    if node == "supervisor_review":
        return "Supervisor：继续下钻" if update.get("next_plan") else "Supervisor：证据充分，停止下钻"
    if node == "analyze_results":
        return "计算达成率、缺口与证据"
    if node == "generate_report":
        return "生成结构化报告"
    return ""


def health() -> dict:
    """健康检查：模型是否可用、工具有哪些、图是否编译成功。"""
    return {
        "ok": True,
        "engine": ENGINE,
        "graph_compiled": GRAPH is not None,
        "llm": llm.config_summary(),
        "tools": [{"name": t["name"], "description": t["description"],
                   "required": t["required"], "source": t["source"]}
                  for t in tools.registry.catalog()],
        "endpoints": ["/api/agent/health", "/api/agent/chat", "/api/agent/stream",
                      "/api/agent/tools", "/api/agent/feedback", "/api/agent/actions"],
    }
