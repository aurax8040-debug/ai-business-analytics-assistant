"""Agent KPI 字典、运行日志与确定性聚合。"""
from __future__ import annotations

import json
import math
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
RUN_LOG_PATH = ROOT / "logs" / "agent_runs.jsonl"
EVAL_RESULT_PATH = ROOT / "reports" / "agent_v2_quality_eval_result.json"
EVAL_SET_PATH = ROOT / "tests" / "agent_v2_quality_eval_set.json"
_LOCK = threading.Lock()

STATUSES = {"MEASURED", "OFFLINE_EVALUATED", "MOCK_VALIDATED", "PENDING_FEEDBACK",
            "PENDING_BASELINE", "PENDING_BUSINESS_DATA"}

SCENE_MAP = {
    "DIAGNOSIS": ["intent_accuracy", "route_accuracy", "tool_accuracy",
                  "drill_effectiveness", "budget_gap_precision", "analysis_time_reduction"],
    "OPPORTUNITY": ["intent_accuracy", "route_accuracy", "tool_accuracy",
                    "high_growth_sku_accuracy", "opportunity_precision", "adoption_rate", "sku_velocity_lift"],
    "PRICE": ["intent_accuracy", "route_accuracy", "price_precision", "price_recall",
              "price_misjudgment_rate", "adoption_rate"],
    "SUPPLY_CHAIN": ["intent_accuracy", "route_accuracy", "shortage_recall",
                     "shortage_missed_count"],
    "PROCUREMENT": ["intent_accuracy", "route_accuracy", "procurement_accuracy",
                    "modified_rate", "packaging_obsolescence_rate", "procurement_overage_risk"],
    "SIMULATION": ["intent_accuracy", "route_accuracy", "simulation_accuracy",
                   "material_consistency", "average_latency_ms", "average_tokens"],
}

DICTIONARY = {
    "intent_accuracy": ("AI能力", "意图识别准确率", "正确意图数/有效离线题数"),
    "route_accuracy": ("AI能力", "场景路由准确率", "正确场景路由数/有效路由题数"),
    "tool_accuracy": ("AI能力", "工具选择准确率", "包含全部预期工具的题数/有工具金标题数"),
    "drill_effectiveness": ("AI能力", "钻取路径有效率", "命中专家关键差距的诊断题数/有专家标注诊断题数"),
    "budget_gap_precision": ("AI能力", "预算缺口 Precision", "正确缺口实体/Agent识别缺口实体"),
    "budget_gap_recall": ("AI能力", "预算缺口 Recall", "正确缺口实体/金标缺口实体"),
    "budget_gap_topn": ("AI能力", "关键缺口 Top-N 命中率", "Top-N命中金标关键缺口数/N"),
    "clarification_rate": ("AI能力", "用户追问率", "需补充信息任务/全部运行任务；另列权限拒绝"),
    "adoption_rate": ("建议质量", "建议采纳率", "ACCEPTED/已完成决策"),
    "modified_rate": ("建议质量", "建议修改率", "MODIFIED/已完成决策"),
    "rejection_rate": ("建议质量", "建议驳回率", "REJECTED/已完成决策"),
    "price_misjudgment_rate": ("建议质量", "价格误判率", "FP/(TP+FP)，需同KA/SKU/时间/样本金标"),
    "price_precision": ("建议质量", "价格 Precision", "TP/(TP+FP)"),
    "price_recall": ("建议质量", "价格 Recall", "TP/(TP+FN)"),
    "shortage_missed_count": ("建议质量", "短缺漏判数", "金标短缺物料中Agent未识别数量"),
    "shortage_recall": ("建议质量", "短缺识别率", "TP/(TP+FN)"),
    "average_latency_ms": ("运行效率", "平均响应时间", "成功/部分成功请求总时延均值"),
    "p50_latency_ms": ("运行效率", "P50响应时间", "成功/部分成功请求时延P50"),
    "p95_latency_ms": ("运行效率", "P95响应时间", "成功/部分成功请求时延P95"),
    "max_latency_ms": ("运行效率", "最大响应时间", "成功/部分成功请求时延最大值"),
    "average_tokens": ("运行效率", "单次Token消耗", "有usage记录请求的total_tokens均值"),
    "average_cost": ("运行效率", "单次模型调用成本", "按配置价格计算；未配置则为空"),
    "average_tool_calls": ("运行效率", "平均工具调用次数", "成功/部分成功请求工具调用数均值"),
    "analysis_time_reduction": ("运行效率", "人工分析时长下降", "人工基线-使用Agent后人工时长"),
    "budget_attainment": ("业务价值", "预算达成率", "ERP Sell-in实际TE/同期预算TE"),
    "stockout_rate": ("业务价值", "缺货率", "缺货SKU-门店/应有货SKU-门店"),
    "packaging_obsolescence_rate": ("业务价值", "包材呆滞率", "呆滞包材库存金额/包材库存总金额"),
    "sku_velocity_lift": ("业务价值", "SKU动销提升", "(实施后-实施前)/实施前"),
    "opportunity_precision": ("建议质量", "机会识别Precision", "需专家机会金标"),
    "high_growth_sku_accuracy": ("AI能力", "高增长SKU识别准确率", "正确高增长SKU/金标高增长SKU"),
    "procurement_accuracy": ("建议质量", "采购建议计算准确率", "Checker独立复算一致的物料/全部物料"),
    "simulation_accuracy": ("建议质量", "模拟计算准确率", "Checker验证通过的模拟/已执行模拟"),
    "material_consistency": ("建议质量", "物料变化计算一致性", "前后需求差与增量重算一致"),
    "procurement_overage_risk": ("建议质量", "采购过量风险", "建议量超过经确认需求与库存边界的物料占比"),
}


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict): rows.append(value)
        except json.JSONDecodeError:
            continue
    return rows


def _price_cost(usage: dict) -> float | None:
    raw_in, raw_out = os.environ.get("LLM_INPUT_PRICE_PER_MILLION"), os.environ.get("LLM_OUTPUT_PRICE_PER_MILLION")
    if not raw_in or not raw_out:
        return None
    return round((usage.get("input_tokens", 0) * float(raw_in)
                  + usage.get("output_tokens", 0) * float(raw_out)) / 1_000_000, 8)


def build_run_record(payload: dict) -> dict:
    data = payload.get("data") or {}
    traces = data.get("llm_trace") or []
    usage = data.get("llm_usage") or {}
    has_usage = int(usage.get("call_count") or 0) > 0
    permission = (data.get("governance") or {}).get("permission") or {}
    checker = (data.get("report") or {}).get("checker") or {}
    return {
        "request_id": data.get("run_id"), "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "scenario": (data.get("scene") or {}).get("code"),
        "intent": (data.get("understood") or {}).get("intent"), "expected_intent": None,
        "selected_route": (data.get("supervisor") or {}).get("workflow"), "expected_route": None,
        "planner_status": (data.get("plan") or {}).get("generation_mode") or (data.get("plan") or {}).get("mode"),
        "supervisor_decision": (data.get("governance") or {}).get("stop_reason") or (data.get("supervisor") or {}).get("reason"),
        "tools_called": [x.get("tool") for x in data.get("tools") or []],
        "drill_down_count": (data.get("governance") or {}).get("drill_depth", 0),
        "checker_status": checker.get("status"),
        "quality_status": ((data.get("governance") or {}).get("data_quality") or {}).get("conclusion_status"),
        "llm_provider": (data.get("llm") or {}).get("provider"), "model_name": (data.get("llm") or {}).get("model"),
        "llm_used": any(x.get("llm_used") for x in traces),
        "fallback_used": any(x.get("fallback_used") for x in traces),
        "input_tokens": usage.get("input_tokens") if has_usage else None,
        "output_tokens": usage.get("output_tokens") if has_usage else None,
        "total_tokens": usage.get("total_tokens") if has_usage else None,
        "llm_call_count": usage.get("call_count") if has_usage else 0,
        "llm_retry_count": sum(max(0, int(x.get("attempts") or 1) - 1)
                               for x in traces if x.get("llm_used")),
        "model_cost": _price_cost(usage) if has_usage else None,
        "total_latency_ms": (data.get("timings") or {}).get("total"),
        "execution_status": data.get("execution_status") or (
            "PERMISSION_DENIED" if permission.get("status") == "PERMISSION_DENIED"
            else "NEEDS_CLARIFICATION" if data.get("needs_clarification")
            else "BLOCKED" if ((data.get("governance") or {}).get("data_quality") or {}).get("blocking")
            else "SUCCESS"),
        "parent_request_id": data.get("parent_request_id") or None,
        "final_report_status": "GENERATED" if (data.get("report") or {}).get("headline") else "NOT_GENERATED",
        "budget_attainment_pct": ((data.get("report") or {}).get("unified") or {}).get("key_metrics", {}).get("attainment_pct"),
    }


def record_run(payload: dict) -> dict:
    row = build_run_record(payload)
    if not row.get("request_id") or os.environ.get("AGENT_RUN_LOG_DISABLED") in ("1", "true"):
        return row
    RUN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        with RUN_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def record_failure(run_id: str, *, latency_ms: float, error_type: str) -> dict:
    row = {"request_id": run_id, "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
           "scenario": None, "intent": None, "expected_intent": None,
           "selected_route": None, "expected_route": None, "planner_status": None,
           "supervisor_decision": None, "tools_called": [], "drill_down_count": 0,
           "checker_status": None, "quality_status": None,
           "llm_provider": None, "model_name": None, "llm_used": False, "fallback_used": False,
           **usage_summary_for_failure(), "total_latency_ms": round(latency_ms, 1),
           "execution_status": "FAILED", "final_report_status": "NOT_GENERATED",
           "error_type": error_type, "model_cost": None, "budget_attainment_pct": None}
    if os.environ.get("AGENT_RUN_LOG_DISABLED") not in ("1", "true"):
        RUN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with RUN_LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def usage_summary_for_failure() -> dict:
    from . import llm
    usage = llm.usage_summary()
    return {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
            "total_tokens": usage.get("total_tokens"), "llm_call_count": usage.get("call_count"),
            "llm_retry_count": max(0, int(usage.get("call_count") or 0) - 1)}


def classification_metrics(predicted: Iterable[str], gold: Iterable[str]) -> dict:
    p, g = set(predicted), set(gold)
    tp, fp, fn = len(p & g), len(p - g), len(g - p)
    return {"tp": tp, "fp": fp, "fn": fn,
            "precision": None if tp + fp == 0 else round(tp / (tp + fp) * 100, 2),
            "recall": None if tp + fn == 0 else round(tp / (tp + fn) * 100, 2),
            "missed_count": fn}


def _percentile(values: list[float], q: float) -> float | None:
    if not values: return None
    values = sorted(values); pos = (len(values) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return round(values[lo] if lo == hi else values[lo] + (values[hi] - values[lo]) * (pos - lo), 2)


def _metric(key: str, value, status: str, *, numerator=None, denominator=None, note="") -> dict:
    group, name, formula = DICTIONARY[key]
    return {"key": key, "group": group, "name": name, "value": value, "status": status,
            "numerator": numerator, "denominator": denominator, "formula": formula, "note": note}


def _offline() -> dict:
    if not EVAL_RESULT_PATH.exists(): return {}
    result = json.loads(EVAL_RESULT_PATH.read_text(encoding="utf-8"))
    rows = result.get("results") or []
    valid = [r for r in rows if not r.get("expected_clarification")]
    intent_ok = sum((r.get("actual_result") or {}).get("intent") == r.get("expected_intent") for r in valid)
    tool_rows = [r for r in valid if r.get("expected_tools")]
    tool_ok = sum(set(r.get("expected_tools") or []) <= set((r.get("actual_result") or {}).get("tools") or []) for r in tool_rows)
    return {"intent": (intent_ok, len(valid)), "route": (intent_ok, len(valid)),
            "tool": (tool_ok, len(tool_rows)), "mode": result.get("actual_mode"),
            "total_cases": len(rows), "clarification_cases": len(rows) - len(valid)}


def aggregate(scene: str = "") -> dict:
    runs_by_id = {r.get("request_id"): r for r in _read_jsonl(RUN_LOG_PATH) if r.get("request_id")}
    runs = list(runs_by_id.values())
    if scene: runs = [r for r in runs if r.get("scenario") == scene]
    completed = [r for r in runs if r.get("execution_status") in
                 ("SUCCESS", "PARTIAL", "FALLBACK", "BLOCKED")]
    latencies = [float(r["total_latency_ms"]) for r in completed if r.get("total_latency_ms") is not None]
    tokens = [float(r["total_tokens"]) for r in completed if r.get("total_tokens") is not None]
    costs = [float(r["model_cost"]) for r in completed if r.get("model_cost") is not None]
    tools = [len(r.get("tools_called") or []) for r in completed]
    clarifications = [r for r in runs if r.get("execution_status") == "NEEDS_CLARIFICATION"]
    denied = [r for r in runs if r.get("execution_status") == "PERMISSION_DENIED"]

    from . import feedback
    fb = _read_jsonl(feedback.FEEDBACK_PATH)
    latest = {(r.get("run_id"), r.get("suggestion_id") or "report"): r for r in fb}
    decisions = [r for r in latest.values() if (r.get("feedback_status") or "") in {"ACCEPTED", "MODIFIED", "REJECTED"}]
    counts = {s: sum(r.get("feedback_status") == s for r in decisions) for s in ("ACCEPTED", "MODIFIED", "REJECTED")}

    metrics = []
    off = _offline()
    for key, source in (("intent_accuracy", "intent"), ("route_accuracy", "route"), ("tool_accuracy", "tool")):
        n, d = off.get(source, (0, 0)); metrics.append(_metric(key, None if not d else round(n/d*100, 2),
            "OFFLINE_EVALUATED" if d else "PENDING_BUSINESS_DATA", numerator=n, denominator=d,
            note="36题离线评测；不代表线上真实用户准确率" if d else "尚无离线结果"))
    for key in ("drill_effectiveness", "budget_gap_precision", "budget_gap_recall", "budget_gap_topn"):
        metrics.append(_metric(key, None, "PENDING_FEEDBACK", note="缺少专家关键差距/实体金标，已提供标注与计算接口"))
    metrics.append(_metric("clarification_rate", None if not runs else round(len(clarifications)/len(runs)*100, 2),
                           "MEASURED" if runs else "PENDING_BUSINESS_DATA", numerator=len(clarifications), denominator=len(runs),
                           note=f"权限拒绝 {len(denied)} 次，未计为无效追问"))
    for key, status in (("adoption_rate", "ACCEPTED"), ("modified_rate", "MODIFIED"), ("rejection_rate", "REJECTED")):
        metrics.append(_metric(key, None if not decisions else round(counts[status]/len(decisions)*100, 2),
                               "MEASURED" if decisions else "PENDING_FEEDBACK", numerator=counts[status], denominator=len(decisions),
                               note="仅统计互斥且已完成的人工决策"))
    for key in ("price_misjudgment_rate", "price_precision", "price_recall", "shortage_missed_count", "shortage_recall"):
        metrics.append(_metric(key, None, "MOCK_VALIDATED", note="计算函数与测试接口已验证；暂无专家/真实业务金标，不展示比率"))
    metrics += [
        _metric("average_latency_ms", None if not latencies else round(sum(latencies)/len(latencies),2), "MEASURED" if latencies else "PENDING_BUSINESS_DATA", denominator=len(latencies)),
        _metric("p50_latency_ms", _percentile(latencies,.5), "MEASURED" if latencies else "PENDING_BUSINESS_DATA", denominator=len(latencies)),
        _metric("p95_latency_ms", _percentile(latencies,.95), "MEASURED" if latencies else "PENDING_BUSINESS_DATA", denominator=len(latencies)),
        _metric("max_latency_ms", max(latencies) if latencies else None, "MEASURED" if latencies else "PENDING_BUSINESS_DATA", denominator=len(latencies)),
        _metric("average_tokens", None if not tokens else round(sum(tokens)/len(tokens),2), "MEASURED" if tokens else "PENDING_BUSINESS_DATA", denominator=len(tokens)),
        _metric("average_cost", None if not costs else round(sum(costs)/len(costs),8), "MEASURED" if costs else "PENDING_BASELINE", denominator=len(costs), note="未配置模型价格时只展示Token"),
        _metric("average_tool_calls", None if not tools else round(sum(tools)/len(tools),2), "MEASURED" if tools else "PENDING_BUSINESS_DATA", denominator=len(tools)),
        _metric("analysis_time_reduction", None, "PENDING_BASELINE", note="需记录传统人工耗时与Agent后人工耗时"),
    ]
    attainment = [r["budget_attainment_pct"] for r in completed if r.get("budget_attainment_pct") is not None]
    metrics.append(_metric("budget_attainment", attainment[-1] if attainment else None,
                           "MOCK_VALIDATED" if attainment else "PENDING_BUSINESS_DATA",
                           note="仅为Mock预算与ERP计算，不代表Agent造成的提升"))
    for key, note in (("stockout_rate", "缺少完整应有货清单与确认缺货状态"),
                      ("packaging_obsolescence_rate", "缺少库龄、金额与呆滞规则"),
                      ("sku_velocity_lift", "缺少建议执行记录和实施前后可比窗口")):
        metrics.append(_metric(key, None, "PENDING_BUSINESS_DATA", note=note))
    metrics += [
        _metric("high_growth_sku_accuracy", None, "PENDING_FEEDBACK", note="需专家高增长SKU金标"),
        _metric("opportunity_precision", None, "PENDING_FEEDBACK", note="需门店机会专家标注"),
        _metric("procurement_accuracy", None, "MOCK_VALIDATED", note="V2公式与Checker已验证；不代表真实采购执行准确率"),
        _metric("simulation_accuracy", None, "MOCK_VALIDATED", note="Mock情景计算与Checker已验证"),
        _metric("material_consistency", None, "MOCK_VALIDATED", note="前后需求与增量公式由Checker复核"),
        _metric("procurement_overage_risk", None, "PENDING_BUSINESS_DATA", note="缺少真实采购审批与后续库存结果"),
    ]
    return {"generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "scene_filter": scene or "ALL", "run_count": len(runs), "metrics": metrics,
            "scene_mapping": SCENE_MAP, "statuses": sorted(STATUSES)}


def dictionary() -> list[dict]:
    return [{"key": k, "group": v[0], "name": v[1], "formula": v[2]} for k,v in DICTIONARY.items()]
