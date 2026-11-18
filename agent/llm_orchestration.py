"""DeepSeek constrained orchestration for Planner, Supervisor and Reporter."""
from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from . import llm, report as report_mod
from .tools import registry


class PlanTask(BaseModel):
    task_id: str = Field(min_length=1, max_length=40)
    task_goal: str = Field(min_length=1, max_length=200)
    selected_tool: str = Field(min_length=1, max_length=80)
    parameters: dict[str, Any]
    expected_evidence: list[str] = Field(min_length=1, max_length=6)
    dependencies: list[str] = Field(default_factory=list, max_length=8)
    completion_criteria: str = Field(min_length=1, max_length=200)


class PlanOutput(BaseModel):
    tasks: list[PlanTask] = Field(min_length=1, max_length=12)
    rationale: str = Field(min_length=1, max_length=300)


class SupervisorOutput(BaseModel):
    action: Literal["CONTINUE", "NEXT_TOOL", "REPLAN", "STOP", "CLARIFY", "PARTIAL"]
    reason: str = Field(min_length=1, max_length=800)
    evidence_refs: list[str] = Field(min_length=1, max_length=8)
    next_tool: str = Field(default="", max_length=80)
    parameters: dict[str, Any] = Field(default_factory=dict)


class ReporterOutput(BaseModel):
    core_conclusion: str = Field(min_length=1, max_length=500)
    key_data: list[str] = Field(min_length=1, max_length=8)
    issues_or_opportunities: list[str] = Field(default_factory=list, max_length=8)
    data_evidence: list[str] = Field(min_length=1, max_length=8)
    risks: list[str] = Field(default_factory=list, max_length=8)
    recommended_actions: list[str] = Field(default_factory=list, max_length=8)
    human_confirmation: list[str] = Field(default_factory=list, max_length=8)


def _chat_json_with_one_retry(messages: list[dict], **kwargs) -> tuple[dict, int]:
    """空响应、非法 JSON 和瞬时 API 故障最多重试一次。"""
    try:
        return llm.chat_json(messages, **kwargs), 1
    except llm.LLMError as exc:
        retryable = ("EmptyResponse", "InvalidJSON", "APITimeoutError",
                     "APIConnectionError", "RateLimitError", "InternalServerError",
                     "TimeoutError")
        if exc.code not in retryable:
            exc.attempts = 1
            raise
        retry = messages + [{"role": "user", "content":
                             "上次响应为空或不完整。请压缩内容，并只输出完整合法 JSON。"}]
        try:
            return llm.chat_json(retry, **kwargs), 2
        except llm.LLMError as retry_exc:
            retry_exc.attempts = 2
            raise


def _normalize_reporter(raw: dict) -> dict:
    """兼容模型偶发把列表写成对象；仅做格式归一，不创造业务内容。"""
    normalized = dict(raw)
    list_fields = ("key_data", "issues_or_opportunities", "data_evidence", "risks",
                   "recommended_actions", "human_confirmation")
    for field in list_fields:
        value = normalized.get(field)
        if isinstance(value, dict):
            normalized[field] = [f"{key}：{item}" for key, item in value.items()]
        elif isinstance(value, str):
            normalized[field] = [value]
    return normalized


def _normalize_supervisor(raw: dict) -> dict:
    normalized = dict(raw)
    normalized["action"] = str(normalized.get("action") or "").upper()
    if normalized.get("next_tool") is None:
        normalized["next_tool"] = ""
    if normalized.get("parameters") is None:
        normalized["parameters"] = {}
    refs = normalized.get("evidence_refs") or []
    normalized["evidence_refs"] = [
        (f"{item.get('key') or item.get('tool') or 'evidence'}："
         f"{item.get('summary') or item.get('value') or ''}")
        if isinstance(item, dict) else str(item)
        for item in refs
    ]
    return normalized


def _scrub_unverified_numbers(payload: dict, violations: list[str],
                              deterministic: dict) -> dict:
    """移除含未验证数字的模型句；核心结论用确定性 headline 覆盖。"""
    safe = dict(payload)
    fallback_text = deterministic.get("headline") or "以下结论以确定性计算结果为准"
    if any(token in str(safe.get("core_conclusion") or "") for token in violations):
        safe["core_conclusion"] = fallback_text
    for field in ("key_data", "issues_or_opportunities", "data_evidence", "risks",
                  "recommended_actions", "human_confirmation"):
        items = safe.get(field) or []
        safe[field] = [item for item in items
                       if not any(token in str(item) for token in violations)]
    if not safe.get("key_data"):
        safe["key_data"] = [fallback_text]
    if not safe.get("data_evidence"):
        safe["data_evidence"] = ["数值以 Checker 已验证的确定性结果为准"]
    return safe


def _trace(stage: str, *, used: bool, fallback: bool, reason: str = "",
           decision_mode: str = "llm_constrained", detail: dict | None = None) -> dict:
    return {"llm_used": used, "llm_stage": stage, "fallback_used": fallback,
            "fallback_reason": reason, "model_name": llm.config_summary().get("model"),
            "decision_mode": decision_mode, **(detail or {})}


def _safe_catalog(names: list[str]) -> list[dict]:
    out = []
    for name in names:
        spec = registry.TOOLS.get(name) or {}
        out.append({"name": name, "description": spec.get("description", ""),
                    "parameters": (spec.get("parameters") or {}).get("properties", {}),
                    "required": (spec.get("parameters") or {}).get("required", [])})
    return out


def plan(state: dict, fallback: dict) -> tuple[dict, dict]:
    if not llm.available():
        return fallback, _trace("planner", used=False, fallback=True,
                                reason="llm_not_available", decision_mode="deterministic_fallback")
    candidates = fallback.get("analysis_plan") or []
    names = [item.get("tool") for item in candidates if item.get("tool")]
    if not names:
        return fallback, _trace("planner", used=False, fallback=True,
                                reason="no_safe_candidate_tools", decision_mode="deterministic_fallback")
    prompt = {"question": state.get("user_question"),
              "intent": state.get("analysis_intent"),
              "scope": {k: state.get(k) for k in ("region", "department", "customer", "year", "month")},
              "safe_candidate_tools": _safe_catalog(names),
              "deterministic_candidate_plan": candidates,
              "verified_evidence": (state.get("evidence") or [])[-8:]}
    try:
        raw, attempts = _chat_json_with_one_retry([
            {"role": "system", "content": "你是受约束的经营分析Planner。只能使用safe_candidate_tools。输出紧凑JSON：{tasks:[{task_id,task_goal,selected_tool,parameters,expected_evidence,dependencies,completion_criteria}],rationale}。任务数必须与候选计划相同；所有说明字段不超过50字、expected_evidence最多2项。不要输出思维链。"},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, default=str)}],
            temperature=0, max_tokens=2400)
        parsed = PlanOutput.model_validate(raw)
        if len(parsed.tasks) != len(candidates):
            raise ValueError("task_count_mismatch")
        allowed = set(names)
        ids = {task.task_id for task in parsed.tasks}
        candidate_by_tool = {item.get("tool"): item for item in candidates}
        if len(candidate_by_tool) != len(candidates):
            raise ValueError("duplicate_candidate_tool")
        task_key_by_id = {
            task.task_id: candidate_by_tool[task.selected_tool].get("key")
            for task in parsed.tasks if task.selected_tool in candidate_by_tool
        }
        plan_rows = []
        for task in parsed.tasks:
            if task.selected_tool not in allowed:
                raise ValueError(f"tool_not_allowed:{task.selected_tool}")
            if any(dep not in ids for dep in task.dependencies):
                raise ValueError(f"unknown_dependency:{task.task_id}")
            spec = registry.TOOLS[task.selected_tool]["parameters"]
            unknown = set(task.parameters) - set(spec["properties"])
            missing = set(spec["required"]) - set(task.parameters)
            if unknown or missing:
                raise ValueError(f"invalid_parameters:{task.selected_tool}:unknown={unknown}:missing={missing}")
            clean = registry.filter_args(task.selected_tool, task.parameters)
            candidate = candidate_by_tool[task.selected_tool]
            plan_rows.append({"key": candidate.get("key"), "tool": task.selected_tool,
                              "args": clean, "why": task.task_goal,
                              "label": candidate.get("label") or task.selected_tool,
                              "llm_task_id": task.task_id,
                              "expected_evidence": task.expected_evidence,
                              "dependencies": [task_key_by_id[dep] for dep in task.dependencies],
                              "completion_criteria": task.completion_criteria})
        result = {**fallback, "analysis_plan": plan_rows, "planner_generation_mode": "llm_constrained",
                  "plan_rationale": parsed.rationale}
        return result, _trace("planner", used=True, fallback=False,
                              detail={"task_ids": [x.task_id for x in parsed.tasks],
                                      "selected_tools": [x.selected_tool for x in parsed.tasks],
                                      "attempts": attempts})
    except (llm.LLMError, ValidationError, ValueError, KeyError) as exc:
        return fallback, _trace("planner", used=True, fallback=True,
                                reason=f"{type(exc).__name__}:{str(exc)[:180]}",
                                decision_mode="deterministic_fallback",
                                detail={"attempts": int(getattr(exc, "attempts", 1))})


def supervise(state: dict, fallback: dict) -> tuple[dict, dict]:
    if not llm.available():
        return fallback, _trace("supervisor_review", used=False, fallback=True,
                                reason="llm_not_available", decision_mode="deterministic_fallback")
    candidates = fallback.get("next_plan") or []
    candidate_names = [x.get("tool") for x in candidates if x.get("tool")]
    evidence = [{"key": c.get("key"), "tool": c.get("tool"), "ok": c.get("ok"),
                 "summary": c.get("summary")} for c in (state.get("tool_calls") or [])]
    prompt = {"question": state.get("user_question"), "intent": state.get("analysis_intent"),
              "checker": state.get("checker"), "evidence": evidence,
              "safe_next_tools": _safe_catalog(candidate_names),
              "hard_limits": state.get("control_limits"),
              "deterministic_safe_decision": fallback}
    try:
        raw, attempts = _chat_json_with_one_retry([
            {"role": "system", "content": "你是受约束的Supervisor Review。根据真实证据输出JSON：{action,reason,evidence_refs,next_tool,parameters}。action只能是CONTINUE/NEXT_TOOL/REPLAN/STOP/CLARIFY/PARTIAL，不要输出思维链。"},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, default=str)}],
            temperature=0, max_tokens=500)
        decision = SupervisorOutput.model_validate(_normalize_supervisor(raw))
        if candidates:
            if decision.action not in ("CONTINUE", "NEXT_TOOL", "REPLAN"):
                raise ValueError("hard_rule_requires_safe_continuation")
            selected = decision.next_tool or candidate_names[0]
            if selected not in candidate_names:
                raise ValueError(f"next_tool_not_allowed:{selected}")
            next_plan = [x for x in candidates if x.get("tool") == selected]
            result = {**fallback, "next_plan": next_plan,
                      "task_status": "EXECUTING", "llm_decision": decision.model_dump()}
        else:
            if decision.action not in ("STOP", "PARTIAL", "CLARIFY"):
                raise ValueError("no_safe_next_tool")
            result = {**fallback, "next_plan": [], "task_status": "COMPLETED",
                      "llm_decision": decision.model_dump()}
        return result, _trace("supervisor_review", used=True, fallback=False,
                              detail={"action": decision.action,
                                      "evidence_refs": decision.evidence_refs,
                                      "reason": decision.reason, "attempts": attempts})
    except (llm.LLMError, ValidationError, ValueError, KeyError) as exc:
        return fallback, _trace("supervisor_review", used=True, fallback=True,
                                reason=f"{type(exc).__name__}:{str(exc)[:180]}",
                                decision_mode="deterministic_fallback",
                                detail={"attempts": int(getattr(exc, "attempts", 1))})


def _allowed_from_report(report: dict) -> dict:
    values: set[float] = set()
    raw: set[str] = set()
    def walk(value):
        if isinstance(value, dict):
            for item in value.values(): walk(item)
        elif isinstance(value, list):
            for item in value: walk(item)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            values.add(float(value)); raw.add(str(value))
        elif isinstance(value, str):
            for token in re.findall(r"(?<![A-Za-z])[-+]?\d+(?:,\d{3})*(?:\.\d+)?%?", value):
                raw.add(token)
                try:
                    values.add(float(token.rstrip("%").replace(",", "")))
                except ValueError:
                    pass
    walk(report)
    return {"values": values, "raw": raw}


def report(state: dict, fallback: dict) -> tuple[dict, dict]:
    deterministic = fallback.get("report") or {}
    checker = deterministic.get("checker") or state.get("checker") or {}
    if checker and checker.get("status") not in ("passed",):
        return fallback, _trace("reporter", used=False, fallback=True,
                                reason="checker_not_passed", decision_mode="deterministic_fallback")
    if deterministic.get("needs_clarification") or deterministic.get("error"):
        return fallback, _trace("reporter", used=False, fallback=True,
                                reason="non_report_terminal_state", decision_mode="deterministic_fallback")
    if not llm.available():
        return fallback, _trace("reporter", used=False, fallback=True,
                                reason="llm_not_available", decision_mode="deterministic_fallback")
    safe_input = {"question": state.get("user_question"),
                  "scene": deterministic.get("scenario"),
                  "verified_checker": checker,
                  "deterministic_report": deterministic}
    try:
        raw, attempts = _chat_json_with_one_retry([
            {"role": "system", "content": "你是经营分析Reporter。只能复述输入中的已验证事实，不得新增数字或因果。输出紧凑JSON：{core_conclusion:string,key_data:string[],issues_or_opportunities:string[],data_evidence:string[],risks:string[],recommended_actions:string[],human_confirmation:string[]}。每个数组最多4项，每项最多80字。不要输出思维链。"},
            {"role": "user", "content": json.dumps(safe_input, ensure_ascii=False, default=str)}],
            temperature=0.1, max_tokens=1800)
        raw = _normalize_reporter(raw)
        narrative = ReporterOutput.model_validate(raw)
        violations = report_mod.guard_numbers(json.dumps(raw, ensure_ascii=False),
                                               _allowed_from_report(deterministic))
        if violations:
            corrected = _scrub_unverified_numbers(narrative.model_dump(), violations,
                                                  deterministic)
            narrative = ReporterOutput.model_validate(corrected)
        merged = dict(deterministic)
        merged["headline"] = narrative.core_conclusion
        merged["llm_report"] = narrative.model_dump()
        merged["generator"] = {"mode": "llm", "model": llm.config_summary()["model"],
                               "guard": {"checked": True, "violations": violations,
                                         "corrected": bool(violations)},
                               "fallback_reason": ""}
        sections = [dict(section) for section in (merged.get("sections") or [])]
        if sections:
            items = list(sections[0].get("items") or [])
            if items: items[0] = {**items[0], "text": narrative.core_conclusion}
            sections[0]["items"] = items
            merged["sections"] = sections
        merged["markdown"] = report_mod.render_markdown(merged)
        result = {**fallback, "report": merged, "final_response": merged["markdown"],
                  "generator": merged["generator"]}
        return result, _trace("reporter", used=True, fallback=False,
                              detail={"guard_checked": True, "guard_corrections": violations,
                                      "attempts": attempts})
    except (llm.LLMError, ValidationError, ValueError, KeyError) as exc:
        report_copy = dict(deterministic)
        report_copy["generator"] = {**(report_copy.get("generator") or {}),
                                    "fallback_used": True,
                                    "fallback_reason": f"{type(exc).__name__}:{str(exc)[:180]}"}
        return {**fallback, "report": report_copy}, _trace(
            "reporter", used=True, fallback=True,
            reason=f"{type(exc).__name__}:{str(exc)[:180]}",
            decision_mode="deterministic_fallback",
            detail={"attempts": int(getattr(exc, "attempts", 1))})
