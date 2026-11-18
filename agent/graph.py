"""LangGraph 图：状态 + 节点 + 条件路由。

```
            ┌──────────────────┐
START ──────► understand_question│
            └────────┬─────────┘
                     │ need_clarification?
        ┌────────────┴─────────────┐
       是                          否
        │                           ▼
        │                  ┌────────────────┐
        │                  │ plan_analysis  │
        │                  └───────┬────────┘
        │                          ▼
        │                  ┌──────────────────────┐
        │                  │ call_business_tools  │
        │                  └───────┬──────────────┘
        │                          │ 全部失败?
        │              ┌───────────┴────────────┐
        │             是                        否
        │              │                         ▼
        │              │                ┌────────────────┐
        │              │                │ analyze_results│
        │              │                └───────┬────────┘
        │              │                        ▼
        │              │                ┌────────────────┐
        │              │                │ generate_report│
        │              │                └───────┬────────┘
        ▼              ▼                        ▼
   ask_clarification  handle_failure          END
```

图只负责「走到哪个节点」，业务逻辑都在 ``nodes.py``；路由函数只看状态里的
布尔/计数，不做二次业务判断，方便单测逐个覆盖。
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from . import nodes
from .state import AgentState


def route_after_understand(state: AgentState) -> str:
    return "clarify" if state.get("need_clarification") else "supervise"


def route_after_supervisor(state: AgentState) -> str:
    return "direct" if (state.get("supervisor_decision") or {}).get("route") == "direct" \
        else "planned"


def route_after_preflight(state: AgentState) -> str:
    status = (state.get("data_availability") or {}).get("status")
    if status in ("UNAVAILABLE", "PERMISSION_DENIED"):
        return "failed"
    return route_after_supervisor(state)


def route_after_tools(state: AgentState) -> str:
    """所有工具都失败才走失败分支；部分失败仍继续（降级出报告，并标明缺什么）。"""
    results = state.get("tool_results") or {}
    if not results:
        return "failed"
    if all(not (r or {}).get("ok") for r in results.values()):
        return "failed"
    return "analyze"


def route_after_check(state: AgentState) -> str:
    checker = state.get("checker") or {}
    if checker.get("status") == "retry":
        return "retry"
    if checker.get("status") == "blocked" or state.get("checker_blocked"):
        return "failed"
    # 简单单指标查询仍可直达；六类复杂场景统一经过 Supervisor Review。
    # 非预算专业 Workflow 的确定性 Supervisor 只允许 STOP，不会额外制造工具调用。
    return "review" if state.get("plan_mode") == "planned" else "analyze"


def route_after_review(state: AgentState) -> str:
    return "continue" if state.get("next_plan") else "analyze"


def build_graph():
    g = StateGraph(AgentState)

    g.add_node("understand_question", nodes.understand_question)
    g.add_node("supervise_task", nodes.supervise_task)
    g.add_node("preflight_check", nodes.preflight_check)
    g.add_node("plan_direct_query", nodes.plan_direct_query)
    g.add_node("plan_analysis", nodes.plan_analysis)
    g.add_node("call_business_tools", nodes.call_business_tools)
    g.add_node("check_results", nodes.check_results)
    g.add_node("supervisor_review", nodes.supervisor_review)
    g.add_node("analyze_results", nodes.analyze_results)
    g.add_node("generate_report", nodes.generate_report)
    g.add_node("ask_clarification", nodes.ask_clarification)
    g.add_node("handle_failure", nodes.handle_failure)

    g.add_edge(START, "understand_question")
    g.add_conditional_edges("understand_question", route_after_understand,
                            {"clarify": "ask_clarification", "supervise": "supervise_task"})
    g.add_edge("supervise_task", "preflight_check")
    g.add_conditional_edges("preflight_check", route_after_preflight,
                            {"direct": "plan_direct_query", "planned": "plan_analysis",
                             "failed": "handle_failure"})
    g.add_edge("plan_direct_query", "call_business_tools")
    g.add_edge("plan_analysis", "call_business_tools")
    g.add_conditional_edges("call_business_tools", route_after_tools,
                            {"analyze": "check_results", "failed": "handle_failure"})
    g.add_conditional_edges("check_results", route_after_check,
                            {"retry": "call_business_tools", "review": "supervisor_review",
                             "analyze": "analyze_results",
                             "failed": "handle_failure"})
    g.add_conditional_edges("supervisor_review", route_after_review,
                            {"continue": "call_business_tools", "analyze": "analyze_results"})
    g.add_edge("analyze_results", "generate_report")
    g.add_edge("generate_report", END)
    g.add_edge("ask_clarification", END)
    g.add_edge("handle_failure", END)
    return g.compile()


GRAPH = build_graph()


def mermaid() -> str:
    """给文档用的图定义（与 build_graph 严格对应，改图记得同步）。"""
    return "\n".join([
        "graph TD",
        "  START([开始]) --> UQ[understand_question<br/>解析战区/年份/月份/客户]",
        "  UQ -->|需要追问| CL[ask_clarification<br/>明确说明缺什么]",
        "  UQ -->|信息足够| SV[supervise_task<br/>直达或复杂规划]",
        "  SV --> PF[preflight_check<br/>数据覆盖与只读权限]",
        "  PF -->|简单单指标| DP[plan_direct_query<br/>一个只读工具]",
        "  PF -->|复杂任务| PA[plan_analysis<br/>结构化任务计划]",
        "  DP --> CT",
        "  PA --> CT[call_business_tools<br/>十三个工具按需调用]",
        "  CT -->|全部失败| HF[handle_failure<br/>不编结论]",
        "  CT -->|至少一个成功| CK[check_results<br/>确定性校验/有限重试]",
        "  CK -->|定向重算一次| CT",
        "  CK -->|通过| SR[supervisor_review<br/>继续下钻或停止]",
        "  SR -->|定向下钻| CT",
        "  SR -->|证据充分| AR[analyze_results<br/>缺口排序/口径自校验/证据链]",
        "  CK -->|重试后失败| HF",
        "  AR --> GR[generate_report<br/>LLM 叙事 + 数字白名单校验]",
        "  GR --> E([结束])",
        "  CL --> E",
        "  HF --> E",
    ])
