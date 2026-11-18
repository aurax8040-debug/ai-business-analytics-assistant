"""Agent 全局状态（LangGraph State）。

字段命名对齐任务书要求（``user_question`` / ``analysis_intent`` / ``region`` /
``department`` / ``customer`` / ``year`` / ``month`` / ``analysis_plan`` /
``tool_results`` / ``evidence`` / ``analysis_result`` / ``final_response`` /
``errors``），额外补充了运行期需要的字段（``steps`` / ``timings`` / ``llm``）。

约定：
- 所有节点只做「读取自己的输入字段 + 写入自己的输出字段」，不在节点里顺手改别人的字段；
- ``errors`` 只追加不覆盖，保留完整失败链，便于面试时复盘；
- 数值字段一律由工具层算好，节点不做第二套计算（避免同一指标两种算法）。
"""
from __future__ import annotations

from typing import Any, Literal, TypedDict

Intent = Literal[
    "supply_chain_forecast",  # 供应链 V2 包材采购预测
    "supply_chain_growth",    # SKU 需求增长情景模拟
    "supply_chain_shortage",  # 水果原料与包材短缺风险
    "growth_opportunity",  # 高增长 SKU 铺货机会
    "price_anomaly",  # 同款 SKU 门店价格异常
    "attainment_review",   # 预算达成 / 缺口 / 未达成客户
    "customer_focus",      # 单客户或客户排名
    "pos_review",          # 终端动销
    "sfa_review",          # 拜访与陈列执行
    "wholesale_review",    # 批发经营
    "overview",            # 整体概况
    "unknown",
]


class AgentState(TypedDict, total=False):
    """LangGraph 状态。``total=False`` 便于分阶段填充，缺字段时节点自行兜底。"""

    # ---------------------------------------------------------------- 输入
    user_question: str
    thread_id: str
    history: list[dict]            # 历史轮次（只用于上下文展示，不参与取数）
    context: dict                  # 前端显式传入的页面上下文（P8 起含 page_id 等）

    # ---------------------------------------------------------------- 页面上下文
    page_id: str                   # pos / dashboard / analysis / sfa_visit …
    page_name: str                 # 人话页面名，报告里要写明「你是在哪一页问的」
    module: str                    # 业务模块：pos / sfa / wholesale / erp / analysis
    channel: str                   # 渠道（商超/终端 · 电商 · 批发）
    category: str                  # 品类（品类A / 品类B / 品类C）
    ka_system: str                 # KA 系统（仅 POS / SFA 有该字段）
    store: str                     # 门店（仅 POS / SFA）
    sku: str                       # SKU（ERP / POS / 批发）
    segment: str                   # 客户类型（clubz / nka / lka / wholesale）
    page_filters: dict             # 原始控件快照，用于「看到什么 = 收到什么」核对

    # ---------------------------------------------------------------- 理解
    analysis_intent: dict          # {"type": ..., "focus": [...], "wants_cause": bool}
    semantic: dict                 # 统一指标/维度/同义词/工具路由与口径澄清
    region: str
    department: str
    customer: str
    year: int
    month: list[int]               # 月份列表；空列表 = 全年累计
    missing_params: list[str]
    capability_gaps: list[str]     # 问题触达的能力边界 key（P9，见 agent/capability.py）
    followup: dict                 # 追问承接（P10，{"fields","note","from_turn","used"}）
    need_clarification: bool
    clarification: str
    understood_by: str             # "rules" | "rules+llm"
    supervisor_decision: dict      # direct/planned + workflow/tool/reason
    entity_sources: dict           # 实体值的 USER_EXPLICIT/CONTEXT_INHERITED/RULE_DERIVED
    data_availability: dict        # 执行前数据覆盖与只读权限检查
    auth_context: dict             # 后端注入的 Demo 用户与行级授权范围
    data_quality: dict             # 完整性/时效性/一致性/可追溯性确定性评估

    # ---------------------------------------------------------------- 计划
    analysis_plan: list[dict]      # [{"tool": "query_budget", "args": {...}, "why": "..."}]
    plan_mode: str                 # direct / planned
    planner_generation_mode: str  # llm_constrained / deterministic_fallback
    plan_rationale: str
    scope_dims: dict               # 计划层最终采用的范围（已按工具裁剪前的全集）
    ka_from_customer: str          # 客户名 → POS KA 的近似映射（空 = 未映射）

    # ---------------------------------------------------------------- 执行
    tool_results: dict[str, Any]   # {tool_name: 标准返回结构}
    tool_calls: list[dict]         # 执行轨迹（供前端展示「调用了哪些工具」）
    errors: list[dict]
    checker: dict                  # 确定性质量检查结果
    retry_plan: list[dict]         # Checker 生成的定向重查计划
    next_plan: list[dict]          # Supervisor 基于本轮证据生成的下一层计划
    retry_count: int               # 有限重试次数（供应链最多 1）
    checker_blocked: bool          # 重试后仍失败，禁止输出计算结果
    workflow_result: dict          # 供应链情景的内存计算结果（不落库）
    drill_depth: int               # 动态下钻深度
    drill_records: list[dict]      # 每层范围、证据、发现、下一动作与停止原因
    stop_reason: str               # 集中定义的停止原因
    task_status: str               # PLANNING/EXECUTING/CHECKING/COMPLETED/BLOCKED
    decision_log: list[dict]       # 可审计决策摘要，不记录隐式思维过程
    confidence: str                # high/mid/low（规则计算）
    control_limits: dict           # 本次运行采用的集中治理阈值

    # ---------------------------------------------------------------- 分析
    evidence: list[dict]           # 证据链，每条形如 {claim, value, source}
    analysis_result: dict          # 结构化结论：结果 / 问题 / 原因 / 建议
    cross_checks: list[dict]       # 工具之间的口径自校验

    # ---------------------------------------------------------------- 输出
    report: dict                   # 最终结构化报告（前端渲染）
    final_response: str            # Markdown 版最终回答
    generator: dict                # {"mode": "llm|template", "model": ..., "guard": {...}}
    llm_trace: list[dict]          # 意图/Planner/Supervisor/Reporter 的真实模型与降级轨迹

    # ---------------------------------------------------------------- 运行期
    steps: list[dict]
    timings: dict
    llm_available: bool


def new_state(question: str, thread_id: str = "",
              history: list[dict] | None = None,
              context: dict | None = None) -> AgentState:
    """构造一份干净的初始状态。每次请求独立构造，不共享可变的默认值。"""
    return AgentState(
        user_question=(question or "").strip(),
        thread_id=thread_id or "",
        history=list(history or []),
        context=dict(context or {}),
        page_id="", page_name="", module="",
        channel="", category="", ka_system="", store="", sku="", segment="",
        page_filters={},
        analysis_intent={}, semantic={},
        region="", department="", customer="",
        year=0, month=[],
        missing_params=[], capability_gaps=[], followup={},
        need_clarification=False, clarification="",
        understood_by="rules", entity_sources={}, data_availability={},
        auth_context={}, data_quality={},
        supervisor_decision={}, analysis_plan=[], plan_mode="", planner_generation_mode="",
        plan_rationale="",
        tool_results={}, tool_calls=[], errors=[],
        checker={}, retry_plan=[], next_plan=[], retry_count=0, checker_blocked=False,
        workflow_result={}, drill_depth=0, drill_records=[], stop_reason="",
        task_status="PLANNING", decision_log=[], confidence="low", control_limits={},
        evidence=[], analysis_result={}, cross_checks=[],
        report={}, final_response="", generator={}, llm_trace=[],
        steps=[], timings={}, llm_available=False,
    )
