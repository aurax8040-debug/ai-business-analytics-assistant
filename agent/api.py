"""Agent 的 HTTP 接口层：把 runner 的结果包成和现有 BI 接口一致的 envelope。

响应格式与项目其它接口保持一致：``{"ok": true, "data": ..., "meta": ...}``，
错误一律 ``{"ok": false, "error": {...}}``，前端只需一套处理逻辑。

SSE 流式接口单独在 ``server/app.py`` 里写响应，因为要一小段一小段 flush；
本模块提供 ``sse_lines()`` 把事件编码成 SSE 文本，逻辑仍集中在这里。
"""
from __future__ import annotations

import json
from typing import Iterator

from pydantic import ValidationError

from . import actions, feedback, kpi, llm, pages, runner, tools
from .schemas import AgentRequest


def health() -> dict:
    return {"ok": True, "data": runner.health(),
            "meta": {"page": "agent", "engine": runner.ENGINE}}


def page_catalog() -> dict:
    """GET /api/agent/pages：页面注册表。

    前端自带同一份注册表（要同步可用），这个接口用于两件事：
    1. 新增页面时前后端对得上（测试逐项比对）；
    2. 将来做「页面级权限/开关」时，前端不必改代码。
    """
    return {"ok": True,
            "data": {"pages": pages.catalog(),
                     "modules": {k: list(v) for k, v in pages.MODULE_DIMENSIONS.items()}},
            "meta": {"page": "agent", "count": len(pages.PAGES)}}


def tool_catalog() -> dict:
    return {"ok": True,
            "data": {"tools": tools.registry.catalog(),
                     "graph": {"nodes": ["understand_question", "supervise_task",
                                         "plan_direct_query", "plan_analysis",
                                         "call_business_tools", "check_results", "analyze_results",
                                         "generate_report"],
                               "mermaid": _mermaid()}},
            "meta": {"page": "agent", "count": tools.registry.public_count()}}


def kpi_summary(scene: str = "") -> dict:
    return {"ok": True, "data": kpi.aggregate(scene),
            "meta": {"page": "agent", "source": "offline_eval+run_log+feedback"}}


def chat(raw_body: dict, server_context: dict | None = None) -> tuple[int, dict]:
    """POST /api/agent/chat：一次性返回完整分析结果。"""
    try:
        req = AgentRequest.model_validate(raw_body or {})
    except ValidationError as exc:
        return 400, {"ok": False,
                     "error": {"type": "RequestValidationError",
                               "message": "请求参数不合法",
                               "details": exc.errors(include_url=False)},
                     "meta": {"page": "agent"}}

    result = runner.run(
        req.question, thread_id=req.thread_id,
        context=(req.context.model_dump() if req.context else None),
        history=req.history,
        server_context=server_context,
        as_of=req.request_id,
        parent_request_id=req.parent_request_id,
    )
    if not result.get("ok"):
        return 500, result
    return 200, result


def record_feedback(raw_body: dict) -> tuple[int, dict]:
    """记录人工决策；只写独立日志，不触发任何业务动作。"""
    try:
        row = feedback.record(raw_body or {})
    except feedback.FeedbackError as exc:
        return 400, {"ok": False, "error": {"type": "FeedbackError",
                                              "message": str(exc)}}
    return 200, {"ok": True, "data": row,
                 "meta": {"effect": "feedback_only_no_business_data_write"}}


def action_tasks(run_id: str = "") -> dict:
    """读取 Demo 行动项；只读 JSONL，不连接任何业务库。"""
    rows = actions.list_tasks(run_id=run_id)
    return {"ok": True, "data": {"tasks": rows},
            "meta": {"count": len(rows),
                     "effect": "demo_only_no_business_data_write",
                     "external_notification": False}}


def action_command(raw_body: dict) -> tuple[int, dict]:
    """人工确认创建或推进 Demo 行动项；严格状态机且不执行经营动作。"""
    operation = str((raw_body or {}).get("operation") or "create").strip().lower()
    try:
        if operation == "create":
            row = actions.create(raw_body or {})
        elif operation == "transition":
            row = actions.transition(raw_body or {})
        else:
            raise actions.ActionError("operation 必须是 create 或 transition")
    except actions.ActionError as exc:
        return 400, {"ok": False, "error": {"type": "ActionError", "message": str(exc)}}
    return 200, {"ok": True, "data": row,
                 "meta": {"effect": "demo_only_no_business_data_write",
                          "external_notification": False}}


def sse_lines(question: str, thread_id: str = "", context: dict | None = None,
              history: list | None = None,
              server_context: dict | None = None, request_id: str = "",
              parent_request_id: str = "") -> Iterator[str]:
    """把 runner.stream 的事件编码成 SSE 文本行。

    SSE 的 ``event:`` 名与前端 ``addEventListener`` 一一对应；
    每个事件都是单行 JSON（内部换行已转义），避免前端解析歧义。
    """
    for evt in runner.stream(question, thread_id=thread_id, context=context,
                             history=history, server_context=server_context,
                             request_id=request_id,
                             parent_request_id=parent_request_id):
        name = evt.pop("event", "message")
        yield f"event: {name}\ndata: {json.dumps(evt, ensure_ascii=False)}\n\n"
    yield "event: end\ndata: {}\n\n"


def _mermaid() -> str:
    from .graph import mermaid
    return mermaid()


def llm_summary() -> dict:
    return llm.config_summary()
