"""响应模型（pydantic）：给接口层做一次结构自检，也是前端字段的唯一契约。

注意：``report`` 内部是自由的（前端按 section 渲染），这里只锁定外层字段，
避免为了加一个报告段落就改模型。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolCallInfo(BaseModel):
    key: str
    tool: str
    label: str = ""
    ok: bool
    elapsed_ms: float | None = None
    source: str | None = None
    summary: str | None = None
    error: dict | None = None


class Understood(BaseModel):
    intent: str
    region: str = ""
    department: str = ""
    customer: str = ""
    year: int
    months: list[int] = Field(default_factory=list)
    period_label: str = ""
    by: Literal["rules", "rules+llm"] = "rules"


class ToolData(BaseModel):
    """单个工具查回来的数据行，供前端「数据核对」区渲染。

    与 ``ToolCallInfo`` 的区别：那个只描述「调了什么、结果摘要」，
    这个带真实的查询结果行，用来核对报告里的每个数字。
    """

    key: str
    tool: str = ""
    label: str = ""
    ok: bool = True
    summary: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    period: dict[str, Any] = Field(default_factory=dict)
    scope: dict[str, Any] = Field(default_factory=dict)
    caliber: str = ""
    source: str = ""
    tables: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    groups: list[dict] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    error: dict | None = None


class AgentResponse(BaseModel):
    """成功响应的 data 部分。字段贴合前端渲染需要。

    ``tool_data`` 带工具查询结果行（已截断展示条数），用于「数据核对」；
    不含原始 SQL 语句。
    """

    thread_id: str = ""
    run_id: str
    parent_request_id: str = ""
    execution_status: Literal["SUCCESS", "PARTIAL", "FALLBACK", "BLOCKED",
                              "PERMISSION_DENIED", "NEEDS_CLARIFICATION"] = "SUCCESS"
    failure_handling: dict[str, Any] = Field(default_factory=dict)
    question: str
    understood: Understood
    semantic: dict[str, Any] = Field(default_factory=dict)
    scene: dict[str, Any] = Field(default_factory=dict)
    plan: dict[str, Any]
    tools: list[ToolCallInfo] = Field(default_factory=list)
    tool_data: list[ToolData] = Field(default_factory=list)
    chart: dict[str, Any] = Field(default_factory=dict)
    report: dict[str, Any]
    evidence: list[dict] = Field(default_factory=list)
    cross_checks: list[dict] = Field(default_factory=list)
    steps: list[dict] = Field(default_factory=list)
    errors: list[dict] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)
    llm: dict[str, Any] = Field(default_factory=dict)
    llm_usage: dict[str, Any] = Field(default_factory=dict)
    llm_trace: list[dict[str, Any]] = Field(default_factory=list)
    needs_clarification: bool = False


class AgentEnvelope(BaseModel):
    ok: bool = True
    data: AgentResponse | None = None
    error: dict | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
