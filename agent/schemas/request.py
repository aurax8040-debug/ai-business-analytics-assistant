"""请求模型（pydantic）：接口边界校验。

只做「形状校验」：字段类型、长度、范围。语义校验（战区是否在范围内、月份是否
已发生）在工具层做，因为那需要读库，不该放在 API 边界。

V2：``AgentContext`` 从「5 个筛选」扩展为「页面身份 + 页面筛选」。
为什么保留全部默认值：老前端（不带 page_id 的调用）必须继续工作，
新增字段一律可选、缺省为空，不做必填，避免破坏已有 473 条测试与线上请求。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class AgentContext(BaseModel):
    """前端当前页面的上下文（只作默认分析范围，不覆盖用户明确说出的条件）。"""

    # ---- 页面身份（P8 新增）
    page_id: str = Field(default="", max_length=32)
    page_name: str = Field(default="", max_length=40)
    module: str = Field(default="", max_length=20)

    # ---- 通用范围
    region: str = ""
    department: str = ""
    customer: str = ""
    year: int | None = None
    months: list[int] = Field(default_factory=list)

    # ---- 业务维度（P8 新增；不同页面只有部分字段真实存在）
    channel: str = Field(default="", max_length=40)
    ka_system: str = Field(default="", max_length=40)
    sku: str = Field(default="", max_length=80)
    store: str = Field(default="", max_length=80)
    category: str = Field(default="", max_length=20)
    segment: str = Field(default="", max_length=20)

    # ---- 原始控件快照（用于「用户看到什么 = AI 收到什么」的核对）
    filters: dict[str, Any] = Field(default_factory=dict)
    date_range: dict[str, Any] = Field(default_factory=dict)

    @field_validator("months")
    @classmethod
    def _months(cls, v: list[int]) -> list[int]:
        out = sorted({int(m) for m in (v or []) if 1 <= int(m) <= 12})
        return out


class AgentRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500,
                          description="用户的自然语言问题")
    thread_id: str = Field(default="", max_length=64,
                           description="会话标识；只把多轮展示串起来，不共享筛选状态")
    context: AgentContext | None = Field(default=None, description="可选：当前页面上下文")
    history: list[dict[str, Any]] = Field(default_factory=list,
                                          description="可选：历史消息（仅展示与追问指代用）")
    request_id: str = Field(default="", max_length=64,
                            pattern=r"^[A-Za-z0-9._:-]*$",
                            description="可选：恢复执行时复用原请求标识")
    parent_request_id: str = Field(default="", max_length=64,
                                   pattern=r"^[A-Za-z0-9._:-]*$",
                                   description="可选：新请求与原失败请求的追溯关系")

    @field_validator("question")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("问题不能为空")
        return v
