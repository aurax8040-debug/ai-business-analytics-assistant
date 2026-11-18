"""LangGraph 节点实现：理解 → 计划 → 取数 → 分析 → 报告。

节点边界（也是面试时的「为什么这么拆」）：
- ``understand_question``：只做文本 → 结构，不连库；
- ``plan_analysis``：只决定「查哪些工具、带什么参数」，不执行；
- ``call_business_tools``：唯一连库的节点，工具失败只记录不中断；
- ``analyze_results``：纯函数式，输入是工具结果，输出是结论与证据，**不重新取数**；
- ``generate_report``：唯一接触 LLM 的节点，且输出要过数字白名单校验。

这样做的好处是每一段都能单独测：改提示词不会动到 SQL，改 SQL 不会动到报告结构。
"""
from __future__ import annotations

import json
import time

from server import config

from . import (business_checker, capability, control, direct_workflow, followup, intent, llm,
               llm_orchestration, memory,
               preflight,
               report as report_mod, sections as sections_mod,
               semantics, supervisor, supply_chain_checker, supply_chain_workflow)
from .analysis import cross as cross_mod
from .prompts import cause_messages, intent_messages
from .tools import registry
from .tools._common import ToolError, open_conn, pct, resolve_period

# 出现这些词才算「经营问题」，否则先追问，避免把闲聊当成分析请求。
# 「动销 / 终端 / 业绩 / 趋势 / 下降 / 利润」是 P9 联合分析的常见问法，
# 漏掉它们会把「KA客户C这个月终端动销怎么样」误判成闲聊（V1 的真实缺陷，已补）。
BUSINESS_WORDS = ("销售", "销量", "te", "预算", "达成", "出货", "客户", "战区", "市场部",
                  "门店", "pos", "sfa", "拜访", "陈列", "批发", "档口", "库存", "毛利",
                  "金额", "同比", "环比", "缺口", "分析", "经营", "为什么", "原因",
                  "动销", "终端", "业绩", "表现", "趋势", "下滑", "下降", "增长",
                  "利润", "成本", "渠道", "品类", "ka", "档期", "铺货")
BUSINESS_WORDS = BUSINESS_WORDS + ("包材", "供应链", "采购", "缺料", "短缺", "安全库存",
                                   "在途", "bom", "价格", "生产风险")

# 能力边界里的话题词同样算经营问题：用户问「库存周转」「竞品份额」时要的是
# 「这块我们有没有数据」的正面回答，而不是被当成闲聊请他去补战区。
# 直接复用 capability.TOPICS 的词表，避免两处词表各写一份、日后漂移。
BUSINESS_WORDS = BUSINESS_WORDS + tuple(
    w for k in capability.ORDER for w in capability.TOPICS[k]["words"])

CROSS_REGION_WORDS = ("其他战区", "跨战区", "成功经验", "最佳实践", "借鉴",
                      "全国 sku", "全国sku", "全国 SKU", "复制", "其他区域")


def _cross_region_request(question: str, principal: dict) -> dict:
    requested = any(word.lower() in (question or "").lower() for word in CROSS_REGION_WORDS)
    home = ((principal.get("regions") or [""])[0] if principal else "")
    mentioned = [region for region in intent.REGIONS if region in (question or "")]
    compare = next((region for region in mentioned if region != home), "")
    return {"requested": requested, "home_region": home, "compare_region": compare,
            "mentioned_regions": mentioned}


def _step(state: dict, node: str, status: str, detail: str = "", **extra) -> list[dict]:
    """追加一条执行轨迹（前端用它画「分析过程」时间线）。"""
    steps = list(state.get("steps") or [])
    steps.append({"node": node, "status": status,
                  "at": time.strftime("%H:%M:%S"), "detail": detail, **extra})
    return steps


# ---------------------------------------------------------------- 1. 理解
def understand_question(state: dict) -> dict:
    t0 = time.perf_counter()
    question = (state.get("user_question") or "").strip()
    steps = _step(state, "understand_question", "start", "解析战区内/年份/月份/客户")

    if not question:
        return {"need_clarification": True,
                "clarification": "请描述你想看的经营问题，例如："
                                 "「战区B 2026 年 10 月的达成情况，找出未达成的客户」。",
                "steps": _step({"steps": steps}, "understand_question", "done", "空问题"),
                "timings": {**(state.get("timings") or {}),
                            "understand_question": round((time.perf_counter() - t0) * 1000, 1)}}

    # P10：连续追问。「那这个客户呢」这种句子没有范围，从历史里回读上一轮说过的实体。
    # 只在本句像追问时承接，否则用户换了话题却被旧条件限制，是更难发现的错误。
    fup = followup.build(question, state.get("history") or [],
                         memory.recall(state.get("thread_id") or ""))
    parsed = intent.understand(question, state.get("context"), carry=fup.get("fields"))
    analysis_intent = parsed["analysis_intent"]
    semantic = semantics.resolve(question, state.get("context") or {})
    analysis_intent["semantic_tools"] = list(semantic.get("route_tools") or [])
    analysis_intent["semantic_metrics"] = [
        item.get("code") for item in semantic.get("metrics") or [] if item.get("code")]
    if semantic.get("needs_clarification"):
        return {
            "need_clarification": True,
            "clarification": semantic.get("clarification") or "请先确认指标口径。",
            "analysis_intent": analysis_intent,
            "semantic": semantic,
            "region": parsed.get("region") or "",
            "department": parsed.get("department") or "",
            "customer": parsed.get("customer") or "",
            "year": parsed.get("year") or config.FIXED_YEAR_INT,
            "month": parsed.get("month") or [],
            "understood_by": "rules",
            "steps": _step({"steps": steps}, "understand_question", "done",
                           "指标口径存在歧义，先向用户澄清"),
            "timings": {**(state.get("timings") or {}),
                        "understand_question": round((time.perf_counter() - t0) * 1000, 1)},
        }
    cross_region = _cross_region_request(question, state.get("auth_context") or {})
    if cross_region["requested"]:
        analysis_intent["cross_region_requested"] = True
        analysis_intent["compare_region"] = cross_region["compare_region"]
        # 非集团角色的主分析范围永远是自己的战区；问题中出现的其他战区只作为
        # 聚合对标范围，避免预检把合法的跨区知识查询误判成越权明细查询。
        if cross_region["home_region"]:
            parsed["region"] = cross_region["home_region"]
    carried = fup.get("fields") or {}
    previous_supply = carried.get("supply_intent") or ""
    if previous_supply and any(word in question for word in ("受到影响", "受影响", "在途", "延期")):
        analysis_intent["type"] = "supply_chain_shortage"
    if analysis_intent.get("type") == "supply_chain_growth":
        analysis_intent["category"] = analysis_intent.get("category") or parsed.get("category")
        analysis_intent["pieces_per_box"] = (
            analysis_intent.get("pieces_per_box") or carried.get("pieces_per_box"))
    if (analysis_intent.get("type") == "supply_chain_shortage"
            and "在途" in question and any(word in question for word in ("延期", "延迟", "不能到货"))):
        analysis_intent["exclude_in_transit"] = True
    understood_by = "rules"
    llm_trace = list(state.get("llm_trace") or [])

    # 明显不在经营范围 → 追问，不要硬答。
    # 例外：本句是「那这个客户呢」这类追问时，即使没有业务词也继续——
    # 承接上一轮的实体本身就说明了要分析什么。
    lowered = question.lower()
    if not any(w in lowered for w in BUSINESS_WORDS) and not fup.get("used"):
        return {"need_clarification": True,
                "clarification": "这个助手只能回答经营数据问题（预算达成、客户缺口、"
                                 "POS/SFA/批发表现）。你想查哪个战区、哪个月份？",
                "year": config.FIXED_YEAR_INT, "month": [],
                "analysis_intent": analysis_intent, "understood_by": understood_by,
                "steps": _step({"steps": steps}, "understand_question", "done", "非经营问题"),
                "timings": {**(state.get("timings") or {}),
                            "understand_question": round((time.perf_counter() - t0) * 1000, 1)}}

    # 战区/年份越界：如实说明，不静默改成全国/2026
    if parsed["out_of_scope_region"]:
        r = parsed["out_of_scope_region"]
        return {"need_clarification": True,
                "clarification": f"{r}战区不在当前 BI 范围（只交付战区A/战区B/战区C/战区D/战区E）。"
                                 f"要不要改看这五个战区中的某一个，或者看全国？",
                "analysis_intent": analysis_intent, "understood_by": understood_by,
                "year": config.FIXED_YEAR_INT, "month": parsed["month"],
                "steps": _step({"steps": steps}, "understand_question", "done", f"越界战区 {r}"),
                "timings": {**(state.get("timings") or {}),
                            "understand_question": round((time.perf_counter() - t0) * 1000, 1)}}

    year = parsed["year"] or config.FIXED_YEAR_INT
    if year not in config.YEARS.values() and year != config.FIXED_YEAR_INT:
        return {"need_clarification": True,
                "clarification": f"{year} 年没有可用的 TE 预算（只有 2026 年 1–10 月有），"
                                 f"无法计算达成率。要看 2026 年的情况吗？",
                "analysis_intent": analysis_intent, "understood_by": understood_by,
                "year": config.FIXED_YEAR_INT, "month": parsed["month"],
                "steps": _step({"steps": steps}, "understand_question", "done", f"越界年份 {year}"),
                "timings": {**(state.get("timings") or {}),
                            "understand_question": round((time.perf_counter() - t0) * 1000, 1)}}

    # 期间越界（如 11 月尚未发生业务）：在这里说清楚，而不是等三个工具全报错
    try:
        if not supply_chain_workflow.is_supply_intent(analysis_intent["type"]):
            resolve_period(year, parsed["month"])
    except ToolError as exc:
        return {"need_clarification": True,
                "clarification": f"{exc.message}。{exc.hint}",
                "analysis_intent": analysis_intent, "understood_by": understood_by,
                "year": year, "month": parsed["month"],
                "steps": _step({"steps": steps}, "understand_question", "done",
                               f"期间越界 {parsed['month']}：{exc.code}"),
                "timings": {**(state.get("timings") or {}),
                            "understand_question": round((time.perf_counter() - t0) * 1000, 1)}}

    # 范围完全没给（既没说战区/市场部/客户，也没说「全国」，页面上也没选任何维度）：
    # 先问范围。「给了战区、没说月份」不在此列——那是累计口径，报告里会写明「1–10 月累计」。
    # P8：页面上真实选中的渠道/KA/SKU/门店同样算「有范围」，用户不必再重复一遍。
    if not supply_chain_workflow.is_supply_intent(analysis_intent["type"]) and not any((
            parsed["region"], parsed["department"], parsed["customer"],
            parsed["explicit_national"], parsed.get("channel"),
            parsed.get("category"), parsed.get("ka_system"),
            parsed.get("store"), parsed.get("sku"))):
        return {"need_clarification": True,
                "clarification": "想看哪个范围？可以说战区（战区A / 战区B / 战区C / 战区D / 战区E）、"
                                 "某个市场部（如 河北）、某个客户（如 KA客户C），或者直接说「全国」。"
                                 "带上期间更准，例如「战区B 2026 年 10 月的达成情况」。",
                "analysis_intent": analysis_intent, "understood_by": understood_by,
                "year": year, "month": parsed["month"],
                "page_id": parsed.get("page_id", ""),
                "page_name": parsed.get("page_name", ""),
                "module": parsed.get("module", ""),
                "steps": _step({"steps": steps}, "understand_question", "done", "缺范围，先追问"),
                "timings": {**(state.get("timings") or {}),
                            "understand_question": round((time.perf_counter() - t0) * 1000, 1)}}

    # 有模型时让模型补「意图/关注维度」，实体仍以规则为准（模型与规则冲突时以规则为准）
    if llm.available() and not _skip_llm_intent():
        try:
            intent_attempts = 1
            try:
                guess = llm.chat_json(intent_messages(question, analysis_intent), max_tokens=400)
            except llm.LLMError as first_exc:
                if first_exc.code not in ("EmptyResponse", "InvalidJSON"):
                    raise
                intent_attempts = 2
                guess = llm.chat_json(
                    intent_messages(question, analysis_intent)
                    + [{"role": "user", "content": "只输出完整、紧凑的合法 JSON。"}],
                    max_tokens=400)
            allowed = ("attainment_review", "customer_focus", "pos_review",
                       "sfa_review", "wholesale_review", "overview",
                       "growth_opportunity",
                       "price_anomaly",
                       "supply_chain_forecast", "supply_chain_growth",
                       "supply_chain_shortage")
            kind = str(guess.get("intent") or "")
            # 专业场景由显式关键词高置信命中后锁定；模型只能补充 focus，不能跨场景改路由。
            # 仅 overview/unknown 这类弱分类允许模型纠正。
            if (analysis_intent["type"] in ("overview", "unknown")
                    and kind in allowed and kind != analysis_intent["type"]):
                analysis_intent = {**analysis_intent, "type": kind,
                                   "llm_overridden": True}
            if isinstance(guess.get("wants_cause"), bool):
                analysis_intent["wants_cause"] = guess["wants_cause"]
            extra_focus = [f for f in (guess.get("focus") or [])
                           if f in ("region", "department", "customer", "pos", "sfa", "wholesale")]
            analysis_intent["focus"] = sorted(set(analysis_intent.get("focus") or []) | set(extra_focus))
            understood_by = "rules+llm"
            llm_trace.append({"llm_used": True, "llm_stage": "intent",
                              "fallback_used": False, "fallback_reason": "",
                              "model_name": llm.config_summary().get("model"),
                              "decision_mode": "llm_constrained",
                              "intent": kind or analysis_intent.get("type"),
                              "attempts": intent_attempts})
        except llm.LLMError as exc:
            steps = _step({"steps": steps}, "understand_question", "warn",
                          f"LLM 意图补充失败，已回退规则：{exc.code}")
            llm_trace.append({"llm_used": True, "llm_stage": "intent",
                              "fallback_used": True, "fallback_reason": exc.code,
                              "model_name": llm.config_summary().get("model"),
                              "decision_mode": "deterministic_fallback"})
    elif _skip_llm_intent():
        llm_trace.append({"llm_used": False, "llm_stage": "intent",
                          "fallback_used": True, "fallback_reason": "intent_llm_skipped",
                          "model_name": llm.config_summary().get("model"),
                          "decision_mode": "deterministic_fallback"})
    else:
        llm_trace.append({"llm_used": False, "llm_stage": "intent",
                          "fallback_used": True, "fallback_reason": "llm_not_available",
                          "model_name": llm.config_summary().get("model"),
                          "decision_mode": "deterministic_fallback"})

    timings = {**(state.get("timings") or {}),
               "understand_question": round((time.perf_counter() - t0) * 1000, 1)}
    steps = _step({"steps": steps}, "understand_question", "done",
                  f"意图={analysis_intent['type']}；"
                  f"{parsed['region'] or '全国'}/{parsed['department'] or '全部市场部'}；"
                  f"{year} 年 {parsed['month'] or '累计'}"
                  + (f"；来源页={parsed['page_name']}" if parsed.get("page_name") else ""))
    context = state.get("context") or {}
    entity_sources = {}
    for key, value in (("region", parsed["region"]), ("department", parsed["department"]),
                       ("customer", parsed["customer"]), ("year", year),
                       ("month", parsed["month"])):
        explicit = (str(value) in question if value not in (None, "", []) else False)
        if key == "month" and value:
            explicit = any(f"{m}月" in question or f"{m} 月" in question for m in value)
        if explicit:
            source = control.ENTITY_SOURCE_USER
        elif context.get(key) not in (None, "", []):
            source = control.ENTITY_SOURCE_CONTEXT
        elif value not in (None, "", []):
            source = control.ENTITY_SOURCE_RULE
        else:
            source = control.ENTITY_SOURCE_UNKNOWN
        entity_sources[key] = {"value": value, "source": source}
    return {
        "region": parsed["region"], "department": parsed["department"],
        "customer": parsed["customer"], "year": year, "month": parsed["month"],
        "analysis_intent": analysis_intent, "understood_by": understood_by,
        "semantic": semantic,
        "need_clarification": False, "steps": steps, "timings": timings,
        "llm_available": llm.available(), "llm_trace": llm_trace,
        # P8：页面上下文（只作默认范围，已按「问题优先」合并完）
        "page_id": parsed.get("page_id", ""),
        "page_name": parsed.get("page_name", ""),
        "module": parsed.get("module", ""),
        "channel": parsed.get("channel", ""),
        "category": parsed.get("category", ""),
        "ka_system": parsed.get("ka_system", ""),
        "store": parsed.get("store", ""),
        "sku": parsed.get("sku", ""),
        "segment": parsed.get("segment", ""),
        "page_filters": parsed.get("page_filters", {}),
        # P9：问题触到平台没有的数据（库存周转/竞品/促销ROI/预测），先记下来，
        # 由报告统一说明「有什么、缺什么」，而不是拿相近指标顶上。
        "capability_gaps": capability.detect(question),
        # P10：追问承接说明。前端「本次理解」要显示「承接上一轮：客户=KA客户B」，
        # 让用户能核对 AI 到底按哪个范围答的，也方便当场纠正。
        "followup": fup,
        "entity_sources": entity_sources,
        "control_limits": control.limits(),
    }


def _skip_llm_intent() -> bool:
    import os
    return (os.environ.get("AGENT_SKIP_LLM_INTENT") or "").strip() in ("1", "true", "yes")


# ---------------------------------------------------------------- 1.5 Supervisor
def supervise_task(state: dict) -> dict:
    t0 = time.perf_counter()
    decision = supervisor.decide(
        state.get("user_question") or "", state.get("analysis_intent") or {})
    # 能力边界类问题需要经过完整分析，把“有/没有什么数据”写进报告；
    # 不能因分类器碰巧命中某个模块就直达单工具。
    if state.get("capability_gaps") and decision.get("route") == "direct":
        decision = {"route": "planned", "workflow": "business_analysis", "tool": "",
                    "requires_planner": True,
                    "reason": "问题触达能力边界，需要完整披露可用数据与缺失口径"}
    if ((state.get("analysis_intent") or {}).get("cross_region_requested")
            and decision.get("route") == "direct"):
        decision = {"route": "planned", "workflow": "business_analysis", "tool": "",
                    "requires_planner": True,
                    "reason": "用户明确要求跨战区汇总对标，需要调用脱敏知识工具"}
    timings = {**(state.get("timings") or {}),
               "supervise_task": round((time.perf_counter() - t0) * 1000, 1)}
    steps = _step(state, "supervise_task", "done",
                  f"{decision['route']} · {decision['workflow']}：{decision['reason']}")
    return {"supervisor_decision": decision, "plan_mode": decision["route"],
            "steps": steps, "timings": timings}


def preflight_check(state: dict) -> dict:
    t0 = time.perf_counter()
    result = preflight.check(state)
    timings = {**(state.get("timings") or {}),
               "preflight_check": round((time.perf_counter() - t0) * 1000, 1)}
    status = "done" if result.get("status") in ("AVAILABLE", "PARTIAL") else "warn"
    steps = _step(state, "preflight_check", status, result.get("message") or "预检完成")
    return {"data_availability": result, "task_status": "PLANNING",
            "steps": steps, "timings": timings}


def plan_direct_query(state: dict) -> dict:
    """为简单问题生成一个只读工具调用，不启动复杂 Planner。"""
    t0 = time.perf_counter()
    decision = state.get("supervisor_decision") or {}
    tool = decision.get("tool") or ""
    dims = {
        "region": state.get("region") or "", "department": state.get("department") or "",
        "customer": state.get("customer") or "", "year": state.get("year"),
        "months": state.get("month") or [], "segment": state.get("segment") or "",
        "channel": state.get("channel") or "", "category": state.get("category") or "",
        "ka_system": state.get("ka_system") or "", "store": state.get("store") or "",
        "sku": state.get("sku") or "", "top_n": 10,
    }
    ka_from_customer = ""
    if tool == "query_pos_performance" and state.get("customer"):
        from .intent import POS_KA_SYSTEMS
        customer = state.get("customer") or ""
        for ka in sorted(POS_KA_SYSTEMS, key=len, reverse=True):
            if ka in customer or customer in ka:
                dims["ka_system"] = ka
                ka_from_customer = ka
                break
    args = registry.filter_args(tool, dims)
    plan = [{"key": "direct", "tool": tool, "args": args,
             "why": decision.get("reason") or "简单查询直接调用工具",
             "label": decision.get("workflow") or tool}]
    timings = {**(state.get("timings") or {}),
               "plan_direct_query": round((time.perf_counter() - t0) * 1000, 1)}
    steps = _step(state, "plan_direct_query", "done", f"直达工具：{tool}")
    return {"analysis_plan": plan, "plan_mode": "direct",
            "plan_rationale": decision.get("reason") or "简单查询直达",
            "scope_dims": {k: v for k, v in dims.items() if v not in ("", [], None)},
            "ka_from_customer": ka_from_customer,
            "steps": steps, "timings": timings}


# ---------------------------------------------------------------- 2. 计划
def _deterministic_plan_analysis(state: dict) -> dict:
    t0 = time.perf_counter()
    it = state.get("analysis_intent") or {}
    kind = it.get("type") or "attainment_review"
    wants_cause = bool(it.get("wants_cause"))
    segments = it.get("segments") or []
    region = state.get("region") or ""
    dept = state.get("department") or ""
    customer = state.get("customer") or ""
    year = state.get("year") or config.FIXED_YEAR_INT
    months = state.get("month") or []

    # 统一「当前范围」：页面上下文 + 问题解析已经合并过，这里只负责按工具裁剪。
    # 直接透传会触发 UnknownArgument（例如预算没有渠道维度、POS 没有客户维度）。
    # 跨模块场景（cross）先算出来：它决定「客户类型」要不要下推。
    cross = it.get("cross") or ""
    dims: dict = {"region": region, "department": dept, "year": year}
    if months:
        dims["months"] = months
    if customer:
        dims["customer"] = customer
    # 「是 KA 还是批发的问题」这句话里同时出现两类客户：绝不能只筛其中一类，
    # 否则回答会变成「批发 125%，没问题」，把真正要对比的另一半丢掉。
    if len(segments) == 1 and not cross:
        dims["segment"] = segments[0]
    for key in ("channel", "category", "ka_system", "store", "sku"):
        if not state.get(key):
            continue
        # 同理：「KA 和批发这两个渠道各做了多少」里的「批发」会被解析成渠道，
        # 一旦下推成 channel='批发'，ERP 就只剩批发段，对比直接变成「批发 100%」。
        # 对比类场景要的是两边都在，所以渠道维度不下推。
        if cross == "ka_vs_wholesale" and key == "channel":
            continue
        dims[key] = state[key]

    # 客户名 ↔ POS KA 系统：ERP 没有 KA 维度（映射 PENDING），
    # 只在「名称能对上」时做一次显式映射，并在报告里标注这是近似映射，不是已确认关系。
    ka_from_customer = ""
    if customer:
        # 情况一：问题里既点名客户、又命中了同名 KA（「KA客户C」），这本身就是一次名称映射
        if dims.get("ka_system") and (dims["ka_system"] in customer
                                      or customer in dims["ka_system"]):
            ka_from_customer = dims["ka_system"]
        elif not dims.get("ka_system"):
            from .intent import POS_KA_SYSTEMS
            for ka in sorted(POS_KA_SYSTEMS, key=len, reverse=True):
                if ka in customer:
                    ka_from_customer = ka
                    dims["ka_system"] = ka
                    break

    def args_for(tool: str, extra: dict | None = None) -> dict:
        return registry.filter_args(tool, {**dims, **(extra or {})})

    plan: list[dict] = []

    def add(key: str, tool: str, args: dict, why: str, label: str = "") -> None:
        plan.append({"key": key, "tool": tool, "args": args, "why": why,
                     "label": label or tool})

    if supply_chain_workflow.is_supply_intent(kind):
        plan = supply_chain_workflow.build_plan(it)
        timings = {**(state.get("timings") or {}),
                   "plan_analysis": round((time.perf_counter() - t0) * 1000, 1)}
        steps = _step(state, "plan_analysis", "done",
                      f"供应链 Workflow 计划 {len(plan)} 个只读工具")
        return {
            "analysis_plan": plan,
            "plan_mode": "planned",
            "plan_rationale": "供应链专业 Workflow：需求 → BOM → 库存/在途 → 确定性采购建议 → Checker。",
            "scope_dims": {"observation_month": config.SC_LAST_MONTH,
                           "forecast_window": 2},
            "ka_from_customer": "",
            "steps": steps,
            "timings": timings,
        }

    if kind == "growth_opportunity":
        plan = [{"key": "growth_opportunity",
                 "tool": "query_sku_distribution_opportunities",
                 "args": {"region": region, "top_n": 10},
                 "why": "复用 SKU Gap 确定性引擎识别高增长 SKU、门店三态与 P50 增量",
                 "label": "高增长 SKU 铺货机会"}]
        timings = {**(state.get("timings") or {}),
                   "plan_analysis": round((time.perf_counter() - t0) * 1000, 1)}
        steps = _step(state, "plan_analysis", "done", "增长机会 Workflow：1 个确定性工具")
        return {"analysis_plan": plan, "plan_mode": "planned",
                "plan_rationale": "高增长阈值 → 连续两月 POS 缺口 → SFA 三态 → P50 增量。",
                "scope_dims": {"region": region}, "ka_from_customer": "",
                "steps": steps, "timings": timings}

    if kind == "price_anomaly":
        plan = [{"key": "price_anomaly", "tool": "query_store_price_anomalies",
                 "args": {"region": region, "year": year, "months": months,
                          "threshold_pct": 10, "min_sample": 3, "top_n": 20},
                 "why": "按同 SKU/规格/门店类型/等级计算组内中位价与异常阈值",
                 "label": "同款 SKU 门店价格异常"}]
        timings = {**(state.get("timings") or {}),
                   "plan_analysis": round((time.perf_counter() - t0) * 1000, 1)}
        steps = _step(state, "plan_analysis", "done", "价格异常 Workflow：1 个确定性工具")
        return {"analysis_plan": plan, "plan_mode": "planned",
                "plan_rationale": "同 SKU/规格 → 可比门店分组 → 中位价 → 阈值与样本检查。",
                "scope_dims": {"region": region, "year": year, "months": months},
                "ka_from_customer": "", "steps": steps, "timings": timings}

    # 动态预算诊断首轮只取整体预算与 ERP；客户/POS/SFA 必须由 Supervisor
    # 根据已验证证据再决定，不能在第一次规划时固定全查。
    add("budget", "query_budget", args_for("query_budget"), "先拿到同期预算口径")
    add("erp", "query_erp_actual", args_for("query_erp_actual"),
        "再拿到 ERP 实际出货与毛利")
    if (state.get("supervisor_decision") or {}).get("workflow") == "budget_diagnosis":
        rationale = "动态诊断首轮：只验证整体预算与 ERP Sell-in，校验后再决定是否下钻。"
        timings = {**(state.get("timings") or {}),
                   "plan_analysis": round((time.perf_counter() - t0) * 1000, 1)}
        steps = _step(state, "plan_analysis", "done",
                      "动态首轮仅调用 query_budget、query_erp_actual")
        return {"analysis_plan": plan, "plan_mode": "planned",
                "plan_rationale": rationale, "scope_dims": dict(dims),
                "ka_from_customer": ka_from_customer, "task_status": "EXECUTING",
                "decision_log": list(state.get("decision_log") or []) + [{
                    "stage": "initial_plan", "rule": "MINIMUM_EVIDENCE_FIRST",
                    "action": "query_budget + query_erp_actual",
                    "reason": "先验证问题前提，避免无依据调用所有辅助工具"}],
                "steps": steps, "timings": timings}

    # 其他经营分析沿用既有组合计划。
    add("customer", "query_customer_performance",
        args_for("query_customer_performance", {"top_n": 5 if not wants_cause else 8}),
        "客户维度算缺口与贡献，用于定位「谁没达成」")

    if kind == "pos_review" or wants_cause:
        add("pos", "query_pos_performance", args_for("query_pos_performance", {"top_n": 8}),
            "终端动销是解释「出货为什么少」的辅助证据（不与 ERP 相加）")
    if kind == "sfa_review" or wants_cause:
        add("sfa", "query_sfa_visit", args_for("query_sfa_visit"),
            "拜访/陈列执行是解释「终端有没有跟上」的辅助证据")
    if kind == "wholesale_review" or "wholesale" in segments or wants_cause:
        # 批发工具本身只覆盖批发业务，不接受 segment 参数
        add("wholesale", "query_wholesale_performance",
            args_for("query_wholesale_performance"), "批发客户的出库与采购，用于判断是提货端还是下游端的问题")

    # ---- P9：跨模块联合分析，按场景补齐工具（已有的只改参数，不重复调）
    def ensure(key: str, tool: str, extra: dict, why: str, label: str = "") -> None:
        for item in plan:
            if item["key"] == key:
                item["args"] = registry.filter_args(tool, {**dims, **extra})
                item["why"] = why
                return
        add(key, tool, registry.filter_args(tool, {**dims, **extra}), why, label)

    if cross == "ka_vs_wholesale":
        ensure("wholesale", "query_wholesale_performance", {},
               "批发是独立一段（下游出库），与 ERP 出货分开统计")
    elif cross == "erp_vs_pos":
        ensure("pos", "query_pos_performance", {"top_n": 10},
               "终端侧同口径对照，只比方向、不做比率换算")
    elif cross == "sfa_vs_pos":
        ensure("sfa", "query_sfa_visit", {"full_store_ranking": True},
               "要按门店做交集，必须拿全量门店而不是 Top15")
        ensure("pos", "query_pos_performance", {"full_store_ranking": True},
               "同上：POS 也要全量门店，两个 Top10 求交集会得出错误结论")
    elif cross == "profit_mix":
        ensure("erp", "query_erp_actual", {},
               "毛利需要 ERP 的出货金额与成本，已在主查询里一并取回")

    # 单月问题补一条「近三月」趋势：解释「这个月是偶发还是持续」，
    # 但它只作为趋势参考，不参与达成率计算。
    if len(months) == 1 and wants_cause:
        m = months[0]
        window = [x for x in (m - 2, m - 1, m) if x >= 1]
        if len(window) >= 2:
            add("erp_trend", "query_erp_actual",
                args_for("query_erp_actual", {"months": window}),
                f"近 {len(window)} 个月出货趋势，判断是单月波动还是持续下滑",
                label="近三月趋势（参考）")

    rationale = _plan_text(kind, wants_cause, plan)
    timings = {**(state.get("timings") or {}),
               "plan_analysis": round((time.perf_counter() - t0) * 1000, 1)}
    steps = _step(state, "plan_analysis", "done",
                  f"计划调用 {len(plan)} 个工具：{'、'.join(p['tool'] for p in plan)}")
    return {"analysis_plan": plan, "plan_mode": "planned", "plan_rationale": rationale,
            "scope_dims": dict(dims),
            "ka_from_customer": ka_from_customer,
            "steps": steps, "timings": timings}


def plan_analysis(state: dict) -> dict:
    """DeepSeek 生成受约束计划；任何校验失败均原样回退现有确定性计划。"""
    fallback = _deterministic_plan_analysis(state)
    it = state.get("analysis_intent") or {}
    if it.get("cross_region_requested"):
        kind = it.get("type") or "attainment_review"
        scenario = ("OPPORTUNITY" if kind == "growth_opportunity"
                    else "PRICE" if kind == "price_anomaly"
                    else "SUPPLY_CHAIN" if kind == "supply_chain_shortage"
                    else "PROCUREMENT" if kind == "supply_chain_forecast"
                    else "SIMULATION" if kind == "supply_chain_growth"
                    else "DIAGNOSIS")
        principal = state.get("auth_context") or {}
        home = state.get("region") or ((principal.get("regions") or [""])[0]) or "战区B"
        cross_item = {
            "key": "cross_region", "tool": "query_cross_region_practices",
            "args": {"home_region": home,
                     "compare_region": it.get("compare_region") or "",
                     "year": state.get("year") or config.FIXED_YEAR_INT,
                     "months": state.get("month") or [], "scenario": scenario, "top_n": 10},
            "why": "查询其他战区授权汇总指标与脱敏最佳实践，评估经验迁移条件",
            "label": "跨战区汇总与成功经验",
        }
        plan = [item for item in (fallback.get("analysis_plan") or [])
                if item.get("tool") != "query_cross_region_practices"] + [cross_item]
        fallback = {**fallback, "analysis_plan": plan,
                    "plan_rationale": ((fallback.get("plan_rationale") or "")
                                       + "；追加跨战区汇总和最佳实践检索。")}
    result, trace = llm_orchestration.plan({**state, **fallback}, fallback)
    # 跨区范围属于权限与业务口径硬约束：LLM 可以解释为什么查，但不能改写
    # home/compare/period/scenario。否则可能把“其他战区”误收窄成本区，或改变期间。
    if it.get("cross_region_requested"):
        deterministic_cross = next(
            (item for item in (fallback.get("analysis_plan") or [])
             if item.get("tool") == "query_cross_region_practices"), None)
        if deterministic_cross:
            locked_plan = []
            for item in (result.get("analysis_plan") or []):
                if item.get("tool") == "query_cross_region_practices":
                    item = {**item, "args": dict(deterministic_cross.get("args") or {})}
                locked_plan.append(item)
            result = {**result, "analysis_plan": locked_plan}
            trace = {**trace, "cross_region_scope_revalidated": True}
    return {**result, "llm_trace": list(state.get("llm_trace") or []) + [trace]}


def _plan_text(kind: str, wants_cause: bool, plan: list[dict]) -> str:
    label = {"attainment_review": "预算达成复盘", "customer_focus": "客户缺口定位",
             "pos_review": "终端动销复盘", "sfa_review": "拜访执行复盘",
             "wholesale_review": "批发经营复盘", "overview": "经营概况"}.get(kind, "经营分析")
    tail = "，并补充 POS/SFA 等辅助证据" if wants_cause else ""
    return f"按「{label}」执行：先预算再实际，再下钻到客户缺口{tail}；共 {len(plan)} 步。"


# ---------------------------------------------------------------- 3. 取数
def call_business_tools(state: dict) -> dict:
    t0 = time.perf_counter()
    retry_plan = state.get("retry_plan") or []
    next_plan = state.get("next_plan") or []
    plan = retry_plan or next_plan or state.get("analysis_plan") or []
    incremental = bool(retry_plan or next_plan)
    results: dict[str, dict] = dict(state.get("tool_results") or {}) if incremental else {}
    calls: list[dict] = list(state.get("tool_calls") or []) if incremental else []
    errors: list[dict] = list(state.get("errors") or [])
    steps = list(state.get("steps") or [])

    conn = None
    try:
        conn = open_conn()
        for item in plan:
            key, tool = item["key"], item["tool"]
            started = time.perf_counter()
            res = registry.run(tool, item.get("args") or {}, conn,
                               permission_context=state.get("auth_context") or {})
            elapsed = round((time.perf_counter() - started) * 1000, 1)
            results[key] = res
            calls.append({
                "key": key, "tool": tool, "label": item.get("label") or tool,
                "ok": bool(res.get("ok")), "elapsed_ms": elapsed,
                "source": res.get("data_source"),
                "tables": res.get("tables") or [],
                "args": res.get("args") or item.get("args") or {},
                "why": item.get("why", ""),
                "error": res.get("error"),
                "summary": _brief(res),
            })
            if not res.get("ok"):
                errors.append({"node": "call_business_tools", "tool": tool,
                               **({"error": res["error"]} if res.get("error") else {}),
                               **({"code": "ToolFailed"} if not res.get("error") else {})})
                steps = _step({"steps": steps}, "call_business_tools", "warn",
                              f"{tool} 失败：{(res.get('error') or {}).get('message', '未知错误')}")
            else:
                steps = _step({"steps": steps}, "call_business_tools", "ok",
                              f"{tool} 完成（{elapsed}ms）：{_brief(res)}")
    finally:
        if conn is not None:
            conn.close()

    timings = {**(state.get("timings") or {}),
               "call_business_tools": round((time.perf_counter() - t0) * 1000, 1)}
    return {"tool_results": results, "tool_calls": calls, "errors": errors,
            "retry_plan": [], "next_plan": [], "task_status": "CHECKING",
            "steps": steps, "timings": timings}


# ---------------------------------------------------------------- 3.5 校验
def check_results(state: dict) -> dict:
    """供应链和预算诊断走各自的确定性 Checker。"""
    t0 = time.perf_counter()
    kind = (state.get("analysis_intent") or {}).get("type") or ""
    workflow_result = dict(state.get("workflow_result") or {})
    if kind == "supply_chain_growth":
        workflow_result = supply_chain_workflow.calculate_growth(
            state.get("tool_results") or {}, state.get("analysis_intent") or {})
    elif kind == "supply_chain_shortage":
        workflow_result = supply_chain_workflow.calculate_shortage(
            state.get("tool_results") or {}, state.get("analysis_intent") or {})
    from . import data_governance
    data_quality = data_governance.assess_tool_results(
        {**state, "workflow_result": workflow_result})
    checker_state = {**state, "workflow_result": workflow_result,
                     "data_quality": data_quality}
    if (state.get("supervisor_decision") or {}).get("workflow") in (
            "budget_diagnosis", "growth_opportunity", "price_anomaly"):
        checker = business_checker.check(checker_state)
    else:
        checker = supply_chain_checker.check(checker_state)
    if data_quality.get("blocking"):
        checker = {**checker, "status": "blocked", "verified": False,
                   "message": data_quality.get("message"),
                   "data_quality": data_quality}
    else:
        checker = {**checker, "data_quality": data_quality}
    status = checker.get("status")
    retry_count = int(state.get("retry_count") or 0)
    errors = list(state.get("errors") or [])
    if status == "retry":
        retry_count += 1
    elif status == "blocked":
        errors.append({"node": "check_results", "code": "CheckerFailed",
                       "error": {"type": "CheckerFailed", "message": checker.get("message")}})
    timings = {**(state.get("timings") or {}),
               "check_results": round((time.perf_counter() - t0) * 1000, 1)}
    steps = _step(state, "check_results",
                  "warn" if status in ("retry", "blocked") else "done",
                  checker.get("message") or "Checker 完成")
    return {"checker": checker, "workflow_result": workflow_result,
            "data_quality": data_quality,
            "retry_plan": checker.get("retry_plan") or [],
            "retry_count": retry_count, "checker_blocked": status == "blocked",
            "errors": errors, "steps": steps, "timings": timings}


def _deterministic_supervisor_review(state: dict) -> dict:
    t0 = time.perf_counter()
    outcome = supervisor.review(state)
    action = outcome.get("action") or "stop"
    log = list(state.get("decision_log") or [])
    log.append({"stage": f"review_{int(state.get('drill_depth') or 0)}",
                "rule": "EVIDENCE_DRIVEN_DRILLDOWN", "action": action,
                "reason": outcome.get("decision") or ""})
    timings = {**(state.get("timings") or {}),
               "supervisor_review": round((time.perf_counter() - t0) * 1000, 1)}
    steps = _step(state, "supervisor_review", "done",
                  f"{action}：{outcome.get('decision') or ''}")
    return {"next_plan": outcome.get("next_plan") or [],
            "drill_records": outcome.get("drill_records") or [],
            "drill_depth": int(state.get("drill_depth") or 0) + (1 if action == "continue" else 0),
            "stop_reason": outcome.get("stop_reason") or "",
            "confidence": outcome.get("confidence") or "low",
            "decision_log": log,
            "task_status": "EXECUTING" if action == "continue" else "COMPLETED",
            "steps": steps, "timings": timings}


def supervisor_review(state: dict) -> dict:
    """DeepSeek 审阅真实工具结果；下一步只能从规则引擎允许的候选中选择。"""
    fallback = _deterministic_supervisor_review(state)
    result, trace = llm_orchestration.supervise({**state, **fallback}, fallback)
    return {**result, "llm_trace": list(state.get("llm_trace") or []) + [trace]}


def _brief(res: dict) -> str:
    """一句话摘要，给前端的工具卡片用。宁可短，也不要含糊。"""
    if not res.get("ok"):
        return "失败"
    m = res.get("metrics") or {}
    if "materials_to_order" in m:
        return (f"{m.get('materials')} 种包材，{m.get('materials_to_order')} 种建议采购，"
                f"{m.get('high_risk_materials')} 种高风险")
    if "attainment_pct" in m and m.get("attainment_pct") is not None:
        return f"达成 {m['attainment_pct']:.2f}%（实际 {m.get('te')} / 预算 {m.get('budget_te')} TE）"
    if "budget_te" in m:
        return f"预算 {m['budget_te']:,.2f} TE"
    if "visits" in m:
        return f"拜访 {m['visits']} 次 / {m.get('stores')} 家门店"
    if "stalls" in m:                                   # 批发
        return f"出库 {m.get('sales_te'):,.2f} TE / {m.get('stalls')} 个档口"
    if "sales_te" in m:                                 # POS
        return f"终端 {m['sales_te']:,.2f} TE / {m.get('stores')} 家门店"
    if "te" in m:                                       # ERP 实际
        gm = m.get("gross_margin_pct")
        tail = f"，毛利率 {gm:.2f}%" if gm is not None else ""
        return f"实际 {m['te']:,.2f} TE{tail}"
    return "已取数"


# ---------------------------------------------------------------- 4. 分析
def analyze_results(state: dict) -> dict:
    t0 = time.perf_counter()
    tr = state.get("tool_results") or {}
    if state.get("plan_mode") == "direct":
        decision = state.get("supervisor_decision") or {}
        tool = decision.get("tool") or ""
        analysis_result = direct_workflow.analyze(tool, tr.get("direct") or {})
        evidence = [{"id": f"DIRECT-{tool}", "claim": analysis_result["metric_label"],
                     "value": analysis_result.get("metric_value"),
                     "unit": analysis_result.get("metric_unit"),
                     "source": "、".join(analysis_result.get("tables") or []),
                     "data_source": analysis_result.get("data_source")}]
        timings = {**(state.get("timings") or {}),
                   "analyze_results": round((time.perf_counter() - t0) * 1000, 1)}
        steps = _step(state, "analyze_results", "done",
                      f"简单查询：{analysis_result['metric_label']}")
        return {"analysis_result": analysis_result, "evidence": evidence,
                "cross_checks": [], "steps": steps, "timings": timings}
    if (state.get("supervisor_decision") or {}).get("workflow") == "growth_opportunity":
        result = tr.get("growth_opportunity") or {}
        rows = (result.get("rows") or {}).get("opportunities") or []
        checker = state.get("checker") or {}
        analysis_result = {
            "scenario": "growth_opportunity", "verified": bool(checker.get("verified")),
            "period": result.get("period") or {}, "scope": result.get("scope") or {},
            "metrics": result.get("metrics") or {}, "opportunities": rows,
            "data_source": result.get("data_source") or "",
            "caliber": result.get("caliber") or "", "tables": result.get("tables") or [],
            "notes": result.get("notes") or [], "checker": checker,
            "cross_checks": checker.get("checks") or [], "data_gaps": [],
        }
        evidence = [{"id": f"GROWTH-{row.get('sku_code')}",
                     "claim": (f"{row.get('sku_code')} YoY {row.get('yoy_pct')}%，"
                               f"{row.get('gap_stores')} 家门店需核实"),
                     "value": row.get("inc_p50_boxes"), "unit": "盒/月",
                     "source": "metric_pos_yoy + fact_pos_sales + fact_sfa_visit",
                     "data_source": result.get("data_source")} for row in rows]
        timings = {**(state.get("timings") or {}),
                   "analyze_results": round((time.perf_counter() - t0) * 1000, 1)}
        steps = _step(state, "analyze_results", "done",
                      f"识别 {len(rows)} 个高增长 SKU 门店机会")
        return {"analysis_result": analysis_result, "evidence": evidence,
                "cross_checks": checker.get("checks") or [],
                "steps": steps, "timings": timings}
    if (state.get("supervisor_decision") or {}).get("workflow") == "price_anomaly":
        result = tr.get("price_anomaly") or {}
        anomalies = (result.get("rows") or {}).get("anomalies") or []
        groups = (result.get("rows") or {}).get("group_summary") or []
        checker = state.get("checker") or {}
        analysis_result = {
            "scenario": "price_anomaly", "verified": bool(checker.get("verified")),
            "period": result.get("period") or {}, "scope": result.get("scope") or {},
            "metrics": result.get("metrics") or {}, "anomalies": anomalies,
            "group_summary": groups, "data_source": result.get("data_source") or "",
            "caliber": result.get("caliber") or "", "tables": result.get("tables") or [],
            "notes": result.get("notes") or [], "checker": checker,
            "cross_checks": checker.get("checks") or [], "data_gaps": [],
        }
        evidence = [{"id": f"PRICE-{row.get('store_id')}-{row.get('sku_code')}",
                     "claim": (f"{row.get('store_name')} {row.get('sku_code')} 高于组内中位价 "
                               f"{row.get('price_premium_pct')}%"),
                     "value": row.get("unit_price"), "unit": "元/盒",
                     "source": "fact_pos_sales + dim_store + dim_sku",
                     "data_source": result.get("data_source")} for row in anomalies]
        timings = {**(state.get("timings") or {}),
                   "analyze_results": round((time.perf_counter() - t0) * 1000, 1)}
        steps = _step(state, "analyze_results", "done",
                      f"检查 {len(groups)} 个可比价格组，发现 {len(anomalies)} 条异常")
        return {"analysis_result": analysis_result, "evidence": evidence,
                "cross_checks": checker.get("checks") or [],
                "steps": steps, "timings": timings}
    if supply_chain_workflow.is_supply_intent(
            (state.get("analysis_intent") or {}).get("type") or ""):
        return _analyze_supply_forecast(state, tr, state.get("checker") or {}, t0)
    budget = tr.get("budget") or {}
    erp = tr.get("erp") or {}
    cust = tr.get("customer") or {}
    pos = tr.get("pos") or {}
    sfa = tr.get("sfa") or {}
    whole = tr.get("wholesale") or {}
    trend = tr.get("erp_trend") or {}

    data_gaps: list[str] = []
    cross_checks: list[dict] = []

    period = (erp.get("period") or budget.get("period") or cust.get("period")
              or {"year": state.get("year"), "months": state.get("month") or [],
                  "label": f"{state.get('year')} 年"})
    scope = (erp.get("scope") or cust.get("scope") or budget.get("scope") or {})

    # 实际：优先 ERP 工具；ERP 挂了就用客户工具的直接汇总（同一口径）
    te = (erp.get("metrics") or {}).get("te")
    if te is None and cust.get("ok"):
        te = (cust.get("metrics") or {}).get("te")
        data_gaps.append("ERP 明细工具调用失败，实际 TE 取客户汇总口径")
    budget_te = (budget.get("metrics") or {}).get("budget_te")
    if budget_te is None and cust.get("ok"):
        budget_te = (cust.get("metrics") or {}).get("budget_te")
    if not budget.get("ok"):
        data_gaps.append("预算工具未返回有效数据，达成率不可计算")
    if te is None:
        data_gaps.append("实际出货数据缺失，无法计算达成率")

    attainment = pct(te, budget_te)
    gap = None if (te is None or budget_te is None) else round(te - budget_te, 2)
    m_erp = erp.get("metrics") or {}

    result = {
        "te": te, "budget_te": budget_te, "attainment_pct": attainment,
        "gap_te": gap,
        "last_te": m_erp.get("last_year_te"),
        "yoy_pct": m_erp.get("yoy_pct"),
        "amount": m_erp.get("amount"), "cost": m_erp.get("cost"),
        "gross_profit": m_erp.get("gross_profit"),
        "gross_margin_pct": m_erp.get("gross_margin_pct"),
        "shipments": m_erp.get("shipments"),
        "boxes": m_erp.get("boxes"),
    }

    # ---- 口径自校验：两三个工具算同一个数，必须对得上
    if erp.get("ok") and cust.get("ok"):
        cm = cust.get("metrics") or {}
        cross_checks.append({
            "name": "实际 TE：ERP 工具 vs 客户工具汇总",
            "a": m_erp.get("te"), "b": cm.get("te"),
            "diff": round((m_erp.get("te") or 0) - (cm.get("te") or 0), 2),
            "ok": abs((m_erp.get("te") or 0) - (cm.get("te") or 0)) < 0.05,
        })
    if budget.get("ok") and cust.get("ok"):
        bm = budget.get("metrics") or {}
        cm = cust.get("metrics") or {}
        cross_checks.append({
            "name": "预算 TE：预算工具 vs 客户工具汇总",
            "a": bm.get("budget_te"), "b": cm.get("budget_te"),
            "diff": round((bm.get("budget_te") or 0) - (cm.get("budget_te") or 0), 2),
            "ok": abs((bm.get("budget_te") or 0) - (cm.get("budget_te") or 0)) < 0.05,
        })
    if cust.get("ok"):
        rc = (cust.get("rows") or {}).get("reconciliation") or {}
        cross_checks.append({"name": "客户明细合计 = 范围内实际合计",
                             "a": rc.get("customer_sum_te"), "b": rc.get("direct_sum_te"),
                             "diff": rc.get("diff"), "ok": bool(rc.get("ok"))})
    if erp.get("ok"):
        seg_sum = sum((r.get("te") or 0) for r in (erp.get("rows") or {}).get("by_segment") or [])
        cross_checks.append({
            "name": "按客户类型合计 = 实际合计",
            "a": round(seg_sum, 2), "b": m_erp.get("te"),
            "diff": round(seg_sum - (m_erp.get("te") or 0), 2),
            "ok": abs(seg_sum - (m_erp.get("te") or 0)) < 0.05,
        })

    # ---- 问题层：缺口排序与集中度
    rows = (cust.get("rows") or {}).get("by_customer") or []
    top_gap = (cust.get("rows") or {}).get("top_gap") or []
    top_over = (cust.get("rows") or {}).get("top_over") or []
    unmet = [r for r in rows if (r.get("gap_te") or 0) < 0 and r.get("budget_te")]
    unmet_gap = round(sum(r["gap_te"] for r in unmet), 2)
    top3 = round(sum(abs(r["gap_te"]) for r in top_gap[:3]), 2) if top_gap else 0
    # 集中度分母 = 未达成客户的缺口绝对值之和（不是全体客户），
    # 否则区域整体超额时正缺口会把分母撑大，看起来「缺口很分散」，那是假象。
    pool = sum(abs(r.get("gap_te") or 0) for r in unmet) or 0
    concentration = round(top3 / pool * 100, 2) if pool else None
    unmet_gap_vs_budget = (round(abs(unmet_gap) / budget_te * 100, 4)
                           if budget_te and unmet_gap else None)

    worst_segment = None
    seg_gap: dict[str, float] = {}
    for r in rows:
        if r.get("gap_te") is None:
            continue
        seg_gap[r.get("segment") or "unknown"] = round(
            seg_gap.get(r.get("segment") or "unknown", 0) + r["gap_te"], 2)
    if seg_gap:
        key = min(seg_gap, key=lambda k: seg_gap[k])
        worst_segment = {"segment": key,
                         "label": next((r.get("segment_label") for r in rows
                                        if r.get("segment") == key), key),
                         "gap_te": seg_gap[key]}

    problems = {
        "total_gap_te": gap,
        "unmet_count": len(unmet), "met_count": len(rows) - len(unmet),
        "unmet_gap_te": unmet_gap,
        "unmet_gap_vs_budget_pct": unmet_gap_vs_budget,
        "top_gap": top_gap, "top_over": top_over,
        "concentration_top3_pct": concentration,
        "worst_segment": worst_segment,
        "customers_total": len(rows),
    }

    # ---- 趋势
    trend_block = {}
    by_month = (trend.get("rows") or {}).get("by_month") or []
    if len(by_month) >= 2:
        last, prev = by_month[-1], by_month[-2]
        trend_block = {
            "months": by_month,
            "mom_pct": pct((last.get("te") or 0) - (prev.get("te") or 0), prev.get("te")),
            "last": last, "prev": prev,
        }

    # ---- 规则版事实与假设（LLM 只在此基础上润色，不从零编）
    verified, possible, unsupported = _rule_causes(result, problems, trend_block,
                                                   pos, sfa, whole, data_gaps)

    if not pos.get("ok"):
        unsupported.append("本轮没有调用 POS：Sell-out 仅作辅助证据，不与 ERP 相加；"
                           "当前已验证证据足够，无法据此判断终端动销好坏")
    if not sfa.get("ok"):
        unsupported.append("没有可用的 SFA 拜访数据，无法判断终端执行情况")
    if not whole.get("ok"):
        unsupported.append("没有取批发出库与下游库存，无法判断是渠道压货还是真实动销")

    # ---- P9：能力边界。用户问的是「库存周转」就直说没有周转天数，
    # 而不是把批发出库当成库存周转的答案。这段放在 unsupported 最前面，
    # 因为它是本次回答最重要的限制条件。
    gap_keys = state.get("capability_gaps") or []
    data_limits = capability.describe(gap_keys)
    unsupported = [item["limit"] for item in data_limits] + unsupported

    budget_workflow = ((state.get("supervisor_decision") or {}).get("workflow")
                       == "budget_diagnosis")
    checker = state.get("checker") or {}
    analysis_result = {
        "period": period, "scope": scope, "result": result, "problems": problems,
        "trend": trend_block,
        "pos": (pos.get("metrics") if pos.get("ok") else None),
        "sfa": (sfa.get("metrics") if sfa.get("ok") else None),
        "wholesale": (whole.get("metrics") if whole.get("ok") else None),
        # P9：跨模块联合分析结果（scenario 为空时为 None，不产生空段）
        "cross": cross_mod.build((state.get("analysis_intent") or {}).get("cross") or "", tr,
                                 focus=(state.get("analysis_intent") or {}).get("focus")),
        "causes_rule": {"verified": verified, "possible": possible,
                        "unsupported": _dedup(unsupported)},
        # P9：能力边界（有/缺/能给什么），报告中单独成段，模型不能改写
        "data_limits": data_limits,
        "cross_checks": cross_checks, "data_gaps": _dedup(data_gaps),
        "checker": checker,
        "premise": checker.get("premise") or {},
        "drill_records": state.get("drill_records") or [],
        "stop_reason": state.get("stop_reason") or "",
        "confidence": state.get("confidence") or "low",
        "decision_log": state.get("decision_log") or [],
    }
    if budget_workflow:
        analysis_result["scenario"] = "budget_diagnosis"

    evidence = _evidence(analysis_result)
    timings = {**(state.get("timings") or {}),
               "analyze_results": round((time.perf_counter() - t0) * 1000, 1)}
    steps = _step(state, "analyze_results", "done",
                  f"达成率 {attainment if attainment is not None else '—'}%，"
                  f"未达成客户 {len(unmet)} 家，缺口合计 {unmet_gap:,.2f} TE")
    return {"analysis_result": analysis_result, "evidence": evidence,
            "cross_checks": cross_checks, "steps": steps, "timings": timings}


def _analyze_supply_forecast(state: dict, tool_results: dict,
                             checker: dict, t0: float) -> dict:
    """组装供应链 Workflow 结果；采购数字必须先通过 Checker。"""
    kind = (state.get("analysis_intent") or {}).get("type") or ""
    if kind == "supply_chain_growth":
        analysis_result = supply_chain_workflow.analyze_growth(
            tool_results, state.get("workflow_result") or {}, checker)
    elif kind == "supply_chain_shortage":
        analysis_result = supply_chain_workflow.analyze_shortage(
            tool_results, state.get("workflow_result") or {}, checker)
    else:
        analysis_result = supply_chain_workflow.analyze_forecast(tool_results, checker)
    all_items = analysis_result.get("procurement_items") or []
    source = "、".join(analysis_result.get("tables") or [])
    evidence = [{
        "id": f"SC-FC-{row.get('material_id')}",
        "claim": row.get("basis") or row.get("note") or "供应链 V2 预测结果",
        "material_id": row.get("material_id"),
        "material_name": row.get("material_name"),
        "suggested_order_qty": row.get("suggested_order_qty"),
        "risk_level": row.get("risk_level"),
        "source": source,
        "data_source": analysis_result.get("data_source"),
    } for row in all_items]
    evidence.extend({
        "id": f"SC-FRUIT-{row.get('material_id')}",
        "claim": row.get("note"), "material_id": row.get("material_id"),
        "material_name": row.get("material_name"), "shortage_qty": row.get("shortage_qty"),
        "risk_level": row.get("risk_level"), "source": source,
        "data_source": analysis_result.get("data_source"),
    } for row in analysis_result.get("fruit_risk") or [])
    timings = {**(state.get("timings") or {}),
               "analyze_results": round((time.perf_counter() - t0) * 1000, 1)}
    if kind == "supply_chain_growth":
        detail = f"完成 {analysis_result.get('growth_pct')}% 内存情景模拟"
    elif kind == "supply_chain_shortage":
        detail = (f"识别 {len(analysis_result.get('fruit_shortages') or [])} 种水果、"
                  f"{len(analysis_result.get('packaging_shortages') or [])} 种包材短缺风险")
    else:
        detail = f"Checker 通过后采用 {len(all_items)} 种包材建议；水果仅做风险分析"
    steps = _step(state, "analyze_results", "done", detail)
    return {"analysis_result": analysis_result, "evidence": evidence,
            "cross_checks": analysis_result.get("cross_checks") or [],
            "steps": steps, "timings": timings}


def _rule_causes(result: dict, problems: dict, trend: dict, pos: dict, sfa: dict,
                 whole: dict, data_gaps: list[str]) -> tuple[list, list, list]:
    """规则层原因假设：每条都带「验证方式」，因为它只是假设不是结论。"""
    verified: list[dict] = []
    possible: list[dict] = []
    unsupported: list[str] = []

    te, bud = result.get("te"), result.get("budget_te")
    att, gap = result.get("attainment_pct"), result.get("gap_te")
    if te is not None and bud:
        verified.append({
            "text": f"实际 {te:,.2f} TE、预算 {bud:,.2f} TE、达成 {att:.2f}%、"
                    f"差额 {'+' if gap >= 0 else '−'}{abs(gap):,.2f} TE",
            "source": "fact_erp_shipment ÷ budget_te_v1.fact_te_budget_monthly"})
    if result.get("gross_margin_pct") is not None:
        verified.append({
            "text": f"ERP 出货金额 {result['amount'] / 1e4:,.1f} 万元，毛利额 "
                    f"{result['gross_profit'] / 1e4:,.1f} 万元，"
                    f"毛利率 {result['gross_margin_pct']:.2f}%",
            "source": "fact_erp_shipment.sales_amount / gross_profit"})
    if result.get("yoy_pct") is not None:
        verified.append({
            "text": f"同期同比 {'+' if result['yoy_pct'] >= 0 else '−'}"
                    f"{abs(result['yoy_pct']):.2f}%（去年同期 {result['last_te']:,.2f} TE）",
            "source": "同一批月份的 2025 年实际出货"})

    top = problems.get("top_gap") or []
    if top:
        first = top[0]
        verified.append({
            "text": f"缺口最大的是 {first['customer']}（{first.get('segment_label')}）："
                    f"实际 {first['te']:,.2f} TE / 预算 {first['budget_te']:,.2f} TE，"
                    f"达成 {first['attainment_pct']:.2f}%，缺口 {abs(first['gap_te']):,.2f} TE",
            "source": "客户维度预算 vs 实际（同一套过滤条件）"})
        verified.append({
            "text": f"共 {problems['unmet_count']} 家客户未达成，缺口合计 "
                    f"{abs(problems['unmet_gap_te']):,.2f} TE；前 3 家占未达成缺口的 "
                    f"{problems['concentration_top3_pct']:.2f}%",
            "source": "缺口绝对值加权，避免正负抵消"})

    if trend.get("mom_pct") is not None:
        word = "下滑" if trend["mom_pct"] < 0 else "回升"
        verified.append({
            "text": f"环比{word} {abs(trend['mom_pct']):.2f}%（"
                    f"{trend['prev']['month']} 月 {trend['prev']['te']:,.2f} TE → "
                    f"{trend['last']['month']} 月 {trend['last']['te']:,.2f} TE）",
            "source": "ERP 近三月出货趋势"})

    if pos.get("ok"):
        pm = pos.get("metrics") or {}
        verified.append({
            "text": f"POS 终端（{pm.get('stores')} 家对齐门店）同期销售 {pm.get('sales_te'):,.2f} TE、"
                    f"金额 {pm.get('sales_amount') / 1e4:,.1f} 万元",
            "source": "fact_pos_sales（Sell-out，不与 ERP 相加）"})
    if sfa.get("ok"):
        sm = sfa.get("metrics") or {}
        dr = sm.get("date_range") or {}
        verified.append({
            "text": f"SFA 期间内拜访 {sm.get('visits')} 次（{sm.get('stores')} 家门店、"
                    f"{sm.get('employees')} 名业代），完成率 {_p1(sm.get('completed_rate_pct'))}，"
                    f"陈列记录率 {_p1(sm.get('display_recorded_rate_pct'))}；"
                    f"记录日期 {dr.get('from')} ~ {dr.get('to')}",
            "source": "fact_sfa_visit（人员现场反馈，不是销量）"})
    if whole.get("ok"):
        wm = whole.get("metrics") or {}
        verified.append({
            "text": f"批发出库 {wm.get('sales_te'):,.2f} TE、金额 {wm.get('sales_amount') / 1e4:,.1f} 万元、"
                    f"毛利率 {_p1(wm.get('gross_margin_pct'))}，同期采购 {wm.get('purchase_te'):,.2f} TE，"
                    f"覆盖 {wm.get('customers')} 个批发客户 / {wm.get('stalls')} 个档口",
            "source": "fact_wholesale_sales / fact_wholesale_purchase"})

    # ---- 假设（全部标注验证方式）
    if top:
        worst = top[0]
        yoy = worst.get("yoy_pct")
        if yoy is not None and yoy <= -10:
            possible.append({
                "text": f"{worst['customer']} 同比下滑 {abs(yoy):.2f}%，更像持续性掉量而非单月波动",
                "why": "同比降幅明显，且当期达成率低于 100%",
                "verify": "拉该客户近 6 个月出货与订单频次"})
        if worst.get("segment") == "wholesale":
            possible.append({
                "text": f"{worst['customer']} 属批发客户，缺口可能来自下游提货节奏或价格倒挂",
                "why": "批发客户的出货由下游分销决定，单月波动通常大于 KA",
                "verify": "核对批发出库明细与下游档口库存/售价"})
        if worst.get("budget_missing"):
            possible.append({
                "text": f"{worst['customer']} 只有实际没有预算，达成率不可比，需确认预算归属",
                "why": "预算只下达到部分客户",
                "verify": "与财务/计划确认该客户预算口径"})

    # 以下三条是「有数据依据的推测」，条件不满足就不写——宁可少一条，也不凑数
    conc = problems.get("concentration_top3_pct")
    if top and conc is not None and conc >= 50:
        possible.append({
            "text": f"缺口前 3 家占 {conc:.2f}%，更像少数客户的单点问题（订单节奏、排面或断货），"
                    "而不是整体需求走弱",
            "why": "缺口高度集中，若是系统性问题，缺口应更均匀地分布在客户之间",
            "verify": "逐个核对前 3 家的订单频次与门店缺货记录"})

    pm = (pos.get("metrics") or {}) if pos.get("ok") else {}
    if pm.get("mom_pct") is not None and result.get("yoy_pct") is not None:
        erp_up = result["yoy_pct"] > 0
        pos_up = pm["mom_pct"] > 0
        if erp_up != pos_up:
            possible.append({
                "text": "出货同比与终端环比方向不一致，中间环节（渠道库存、在途、铺货）可能发生了变化，"
                        "即「货发出去了但没卖到终端」或反之",
                "why": f"ERP 同比 {result['yoy_pct']:+.2f}%、POS 环比 {pm['mom_pct']:+.2f}%，两者方向相反",
                "verify": "调取该范围的门店库存与在途数据；本平台没有按天出库流水，无法直接验证"})

    sm = (sfa.get("metrics") or {}) if sfa.get("ok") else {}
    if (sm.get("completed_rate_pct") or 0) >= 90 and pm.get("mom_pct") is not None \
            and pm["mom_pct"] < 0:
        possible.append({
            "text": "拜访完成率处于高位但终端环比为负，问题可能不在执行动作，而在铺货结构、"
                    "价格或竞品挤压",
            "why": f"拜访完成率 {sm['completed_rate_pct']:.1f}%，POS 环比 {pm['mom_pct']:+.2f}%",
            "verify": "抽取 5 家门店做陈列与价格巡检，与竞品同规格对比"})
    if sfa.get("ok"):
        dr = sfa["metrics"]
        if dr.get("completed_rate_pct") is not None and dr["completed_rate_pct"] < 95:
            possible.append({
                "text": f"拜访完成率仅 {dr['completed_rate_pct']:.2f}%，终端执行可能没跟上",
                "why": "SFA 记录显示部分拜访未完成",
                "verify": "抽查未完成门店的执行照片与陈列记录"})
        if dr.get("display_recorded_rate_pct") is not None and dr["display_recorded_rate_pct"] < 50:
            possible.append({
                "text": f"陈列记录率仅 {dr['display_recorded_rate_pct']:.2f}%，"
                        f"可能是陈列执行不到位或业代漏记录",
                "why": "多数拜访没有记录陈列位置/形式",
                "verify": "对比同区域优秀门店的陈列标准照片"})
    if trend.get("mom_pct") is not None and trend["mom_pct"] < -10:
        possible.append({
            "text": "环比降幅超过 10%，需排查是否有大客户调整提货节奏或断货",
            "why": "单月环比大幅下滑",
            "verify": "按客户拉本月 vs 上月出货差异，锁定变动最大的客户"})

    for g in data_gaps:
        unsupported.append(g)
    # 「暂无数据」是报告里最有价值的一段：它明确了这次分析的能力边界。
    if pos.get("ok"):
        stores = (pos.get("metrics") or {}).get("stores") or 0
        unsupported.append(f"POS 只覆盖 {stores} 家对齐门店，无法据此推断该范围整体的终端动销")
    unsupported.append("未取得竞品价格、促销活动与渠道库存水位，外部因素无法判断")
    return verified, possible, unsupported


def _evidence(ar: dict) -> list[dict]:
    """证据链：把结论用到的关键数值摊平，供前端展开与数字校验使用。"""
    out: list[dict] = []
    for i, item in enumerate((ar.get("causes_rule") or {}).get("verified") or [], 1):
        out.append({"id": i, "claim": item["text"], "source": item.get("source")})
    for r in ((ar.get("problems") or {}).get("top_gap") or [])[:5]:
        out.append({"id": len(out) + 1,
                    "claim": f"{r['customer']} 缺口 {r['gap_te']:,.2f} TE"
                             f"（达成 {r['attainment_pct']:.2f}%）",
                    "source": "客户维度预算 vs 实际"})
    return out


def _dedup(items: list[str]) -> list[str]:
    seen, out = set(), []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _p1(v) -> str:
    """百分比统一保留 1 位：22.2222% 这种数字在报告里只会干扰阅读。"""
    return "—" if v is None else f"{v:.1f}%"


# ---------------------------------------------------------------- 5. 报告
def _deterministic_generate_report(state: dict) -> dict:
    t0 = time.perf_counter()
    ar = state.get("analysis_result") or {}
    it = state.get("analysis_intent") or {}
    steps = list(state.get("steps") or [])

    if ar.get("scenario") == "supply_chain_growth":
        return _generate_supply_growth_report(state, ar, steps, t0)
    if ar.get("scenario") == "supply_chain_shortage":
        return _generate_supply_shortage_report(state, ar, steps, t0)
    if ar.get("scenario") == "supply_chain_forecast":
        return _generate_supply_report(state, ar, steps, t0)
    if ar.get("scenario") == "direct_query":
        return _generate_direct_report(state, ar, steps, t0)
    if ar.get("scenario") == "growth_opportunity":
        return _generate_growth_opportunity_report(state, ar, steps, t0)
    if ar.get("scenario") == "price_anomaly":
        return _generate_price_anomaly_report(state, ar, steps, t0)

    allowed = report_mod.collect_allowed_numbers(ar)
    cause = None
    generator = {"mode": "template", "model": None, "guard": {"violations": []}}
    fallback_reason = ""

    legacy_llm_enabled = False
    if legacy_llm_enabled and llm.available():
        try:
            payload = llm.chat_json(cause_messages(
                _digest(ar), _facts_text(ar), "；".join(ar.get("data_gaps") or []) or "无"))
            violations = _check_payload(payload, allowed)
            if violations:
                generator["guard"]["violations"] = violations
                fallback_reason = "numeric_guard"
                steps = _step({"steps": steps}, "generate_report", "warn",
                              f"LLM 输出含未在证据中出现的数字，已回退规则文本：{violations[:3]}")
            else:
                cause = payload
                generator = {"mode": "llm", "model": llm.config_summary()["model"],
                             "guard": {"violations": [], "checked": True}}
        except llm.LLMError as exc:
            fallback_reason = f"llm_error:{exc.code}"
            steps = _step({"steps": steps}, "generate_report", "warn",
                          f"模型调用失败，已降级为规则模板：{exc.code}")
    else:
        fallback_reason = "not_configured"

    rule = ar.get("causes_rule") or {}
    headline = (cause or {}).get("headline") or _headline(ar, it)
    if (ar.get("scenario") == "budget_diagnosis"
            and (ar.get("premise") or {}).get("status") == "contradicted"):
        headline = f"问题前提不成立：{headline}"
    causes = {
        "verified": rule.get("verified") or [],
        "possible": (cause or {}).get("possible") or rule.get("possible") or [],
        "unsupported": ((cause or {}).get("unsupported") or []) + (rule.get("unsupported") or []),
    }
    # 去重，并保证「暂无数据」至少有一条口径说明
    causes["unsupported"] = _dedup(causes["unsupported"])

    actions = (cause or {}).get("actions") or _rule_actions(ar)
    caveats = _dedup(list((cause or {}).get("caveats") or []) + _rule_caveats(ar))

    period_label = (ar.get("period") or {}).get("label") or "—"
    scope = ar.get("scope") or {}
    scope_label = _scope_label(scope)
    # P8：页面上下文里的维度（KA 系统 / 门店 / 渠道 / 品类 / SKU）不一定出现在工具 scope 里，
    # 但用户必须能一眼看出「这次回答到底按哪些条件筛的」。
    for value in (state.get("ka_system"), state.get("store"), state.get("channel"),
                  state.get("category"), state.get("sku")):
        if value and value not in scope_label:
            scope_label = f"{scope_label} · {value}"
    if state.get("page_name"):
        scope_label = f"{scope_label}（来自 {state['page_name']}）"

    report = {
        **({"scenario": "budget_diagnosis",
            "checker": ar.get("checker") or {},
            "premise": ar.get("premise") or {},
            "drill_records": ar.get("drill_records") or [],
            "stop_reason": ar.get("stop_reason") or "",
            "confidence": ar.get("confidence") or "low",
            "decision_log": ar.get("decision_log") or []}
           if ar.get("scenario") == "budget_diagnosis" else {}),
        "headline": headline,
        "period_label": period_label,
        "scope_label": scope_label,
        # P8：回答的「来源页面 + 默认范围」，让用户能核对 AI 是不是按他看到的这一屏答的
        "page_context": {
            "page_id": state.get("page_id") or "",
            "page_name": state.get("page_name") or "",
            "module": state.get("module") or "",
            "filters": state.get("page_filters") or {},
            "scope_label": scope_label,
            "rule": "页面筛选只作默认范围；问题里明确说出的条件优先",
        },
        "result": {"rows": report_mod.result_rows(ar)},
        "problems": {
            "summary": _problem_summary(ar),
            "rows": report_mod.problem_rows(ar, 5),
        },
        "causes": causes,
        "actions": actions,
        "caveats": caveats,
        # P9：跨模块对照（无场景时为 None；前端只在有值时渲染）
        "cross": ar.get("cross"),
        # P9：能力边界（有/缺/能给什么）。这是「不编」的工程实现，不是提示词约束
        "data_limits": ar.get("data_limits") or [],
        "basis": report_mod.basis_block(ar, state.get("tool_calls") or []),
        "generator": {**generator, "fallback_reason": fallback_reason,
                      "understood_by": state.get("understood_by") or "rules"},
        "question": state.get("user_question"),
        # P10：追问承接（未承接时为空对象），报告里要写明「这一轮继承了哪些条件」
        "followup": state.get("followup") or {},
    }
    # P10：七段式报告。每条都带「事实 / 推测 / 建议 / 限制」标签，
    # 前端按标签上色、Markdown 按标签加前缀，两种呈现不会说法不一。
    report["sections"] = sections_mod.build(
        ar, headline=headline, period_label=period_label, scope_label=scope_label,
        causes=causes, actions=actions, caveats=caveats,
        tool_calls=state.get("tool_calls") or [])
    report["kind_counts"] = sections_mod.counts(report["sections"])
    report["markdown"] = report_mod.render_markdown(report)

    timings = {**(state.get("timings") or {}),
               "generate_report": round((time.perf_counter() - t0) * 1000, 1)}
    steps = _step({"steps": steps}, "generate_report", "done",
                  f"生成方式：{'LLM' if generator['mode'] == 'llm' else '规则模板'}")
    return {"report": report, "final_response": report["markdown"],
            "generator": generator, "steps": steps, "timings": timings}


def generate_report(state: dict) -> dict:
    """六类场景统一 Reporter：只接收确定性报告与 Checker 已验证结果。"""
    fallback = _deterministic_generate_report(state)
    fallback = _augment_cross_region_report(state, fallback)
    result, trace = llm_orchestration.report({**state, **fallback}, fallback)
    return {**result, "llm_trace": list(state.get("llm_trace") or []) + [trace]}


def _augment_cross_region_report(state: dict, fallback: dict) -> dict:
    """把已通过工具与治理门禁的跨区证据加入所有六类报告。"""
    result = (state.get("tool_results") or {}).get("cross_region") or {}
    if not result.get("ok"):
        return fallback
    rows = result.get("rows") or {}
    cases = list(rows.get("success_cases") or [])
    benchmarks = list(rows.get("region_benchmarks") or [])
    rankings = list(rows.get("sku_growth_ranking") or [])
    prices = list(rows.get("price_benchmarks") or [])
    cross_region = {
        "scope": result.get("scope") or {},
        "region_benchmarks": benchmarks,
        "success_cases": cases,
        "sku_growth_ranking": rankings,
        "price_benchmarks": prices,
        "caliber": result.get("caliber") or "",
        "notes": result.get("notes") or [],
        "permission_rule": "本区明细按RBAC；其他战区仅返回授权汇总和脱敏最佳实践",
    }
    report = dict(fallback.get("report") or {})
    report["cross_region"] = cross_region
    section_items: list[dict] = []
    for row in benchmarks[:5]:
        section_items.append({
            "kind": "fact", "label": "事实",
            "text": (f"{row.get('region')}同期实际 {row.get('actual_te')} TE、预算 "
                     f"{row.get('budget_te')} TE、达成率 {row.get('attainment_pct')}%。"),
            "source": "v_budget + a_erp；战区汇总",
        })
    for case in cases[:3]:
        perf = case.get("success_performance") or {}
        section_items.append({
            "kind": "fact", "label": "事实",
            "text": (f"{case.get('source_region')}{case.get('source_department')}市场部同期达成率 "
                     f"{perf.get('attainment_pct')}%，可作为对标案例。"),
            "source": "同期预算与ERP Sell-in汇总",
        })
        factors = case.get("related_factors") or []
        if factors:
            section_items.append({
                "kind": "inference", "label": "推测",
                "text": ("可借鉴的关联因素：" + "；".join(
                    str(x.get("factor")) for x in factors[:3])
                    + "。这些是相关发现，不代表因果。"),
                "source": "跨战区结构与POS同期增长对照",
            })
        section_items.append({
            "kind": "advice", "label": "建议",
            "text": (f"复制程度 {case.get('replicability')}；迁移前需满足："
                     + "；".join(case.get("applicable_conditions") or [])
                     + "。"),
            "source": "确定性迁移规则",
        })
    section_items.append({
        "kind": "limit", "label": "限制",
        "text": "其他战区不返回客户、门店、订单或人员明细；促销、陈列和价格策略缺少因果实验，需人工核验。",
        "source": "后端RBAC与数据质量门禁",
    })
    sections = list(report.get("sections") or [])
    sections.append({"no": len(sections) + 1, "key": "cross_region",
                     "title": "跨战区对比与经验迁移", "items": section_items,
                     "table": {}, "note": "汇总可比较，敏感明细不跨区开放。"})
    report["sections"] = sections
    report["kind_counts"] = sections_mod.counts(sections)
    report["markdown"] = report_mod.render_markdown(report)
    return {**fallback, "report": report, "final_response": report["markdown"]}


def _generate_supply_report(state: dict, ar: dict, steps: list[dict], t0: float) -> dict:
    """生成确定性供应链报告，不让 LLM 生成或改写采购数字。"""
    items = ar.get("procurement_items") or []
    metrics = ar.get("metrics") or {}
    high = metrics.get("high_risk_materials") or 0
    rows = []
    for row in items:
        rows.append([
            row.get("material_name"), row.get("material_id"),
            row.get("unit") or "—", f"{row.get('demand_total') or 0:,}",
            f"{row.get('available_qty') or 0:,}", f"{row.get('in_transit_qty') or 0:,}",
            f"{row.get('safety_stock_qty') or 0:,}", f"{row.get('attrition_rate_pct') or 0:g}%",
            f"{row.get('net_requirement') or 0:,}", f"{row.get('moq_qty') or 0:,}",
            f"{row.get('order_package_qty') or 0:,}",
            f"{row.get('suggested_order_qty') or 0:,}", row.get("risk_level") or "—",
        ])
    formula = ar.get("formula") or {}
    source = "、".join(ar.get("tables") or [])
    headline = (f"未来两个月有 {len(items)} 种包材建议采购，"
                f"其中 {high} 种为高风险")
    period_label = (ar.get("period") or {}).get("label") or "—"
    scope_label = "包材采购预测（MOCK_SUPPLY_CHAIN）"
    sections = [
        {"no": 1, "title": "采购建议", "items": [{
            "kind": "fact", "label": "事实", "text": headline,
            "source": "calculate_purchase_suggestion / Supply Chain V2",
        }], "table": {
            "columns": ["包材", "ID", "单位", "预测需求", "库存", "在途", "安全库存",
                        "损耗率", "净需求", "MOQ", "整包装", "建议采购", "风险"],
            "rows": rows,
            "footer": "不同包材计量单位不同，不跨物料汇总采购数量。",
        }},
        {"no": 2, "title": "计算口径", "items": [
            {"kind": "fact", "label": "事实", "text": text, "source": source}
            for text in formula.values()
        ]},
        {"no": 3, "title": "证据与边界", "items": [
            {"kind": "fact", "label": "事实",
             "text": f"确定性 Checker：{(ar.get('checker') or {}).get('message', '—')}。",
             "source": "agent.supply_chain_checker"},
            {"kind": "limit", "label": "限制",
             "text": "全部数据均为固定种子 MOCK_SUPPLY_CHAIN 模拟数据，不代表真实企业经营数据。",
             "source": "供应链 V2 元数据"},
            {"kind": "limit", "label": "限制",
             "text": "库存为 2026-10 月末快照，不是实时可用库存；水果原料无采购参数，仅展示需求与库存风险。",
             "source": "query_material_inventory"},
            {"kind": "limit", "label": "限制",
             "text": "结果仅供人工复核；Agent 不会自动下采购单，也不会修改库存、BOM 或采购参数。",
             "source": "只读工具边界"},
        ]},
    ]
    generator = {"mode": "deterministic_tool_report", "model": None,
                 "guard": {"violations": [], "checked": True},
                 "fallback_reason": "", "understood_by": state.get("understood_by") or "rules"}
    report = {
        "scenario": "supply_chain_forecast",
        "data_source": ar.get("data_source") or "MOCK_SUPPLY_CHAIN",
        "checker": ar.get("checker") or {},
        "fruit_risk": ar.get("fruit_risk") or [],
        "headline": headline,
        "period_label": period_label,
        "scope_label": scope_label,
        "result": {"rows": rows},
        "problems": {"summary": "", "rows": []},
        "causes": {"verified": [], "possible": [], "unsupported": []},
        "actions": [],
        "caveats": ar.get("notes") or [],
        "data_limits": [],
        "basis": {"tables": ar.get("tables") or [],
                  "tool_calls": state.get("tool_calls") or [],
                  "forecast_basis": ar.get("forecast_basis") or ""},
        "generator": generator,
        "question": state.get("user_question"),
        "followup": state.get("followup") or {},
        "sections": sections,
        "kind_counts": sections_mod.counts(sections),
    }
    report["markdown"] = report_mod.render_markdown(report)
    timings = {**(state.get("timings") or {}),
               "generate_report": round((time.perf_counter() - t0) * 1000, 1)}
    steps = _step({"steps": steps}, "generate_report", "done",
                  "生成供应链 V2 确定性工具报告（未调用 LLM 生成采购数字）")
    return {"report": report, "final_response": report["markdown"],
            "generator": generator, "steps": steps, "timings": timings}


def _generate_price_anomaly_report(state: dict, ar: dict,
                                   steps: list[dict], t0: float) -> dict:
    anomalies = ar.get("anomalies") or []
    metrics = ar.get("metrics") or {}
    groups = ar.get("group_summary") or []
    if anomalies:
        headline = (f"发现 {len(anomalies)} 条同款 SKU 门店价格偏高记录，"
                    f"阈值为高于组内中位价 {metrics.get('threshold_pct')}%")
    else:
        headline = (f"检查 {metrics.get('groups_checked') or 0} 个可比价格组，未发现门店价格异常")
    rows = [[row.get("store_name"), row.get("sku_code"), row.get("sku_name"),
             row.get("ka_system"), row.get("store_type"), row.get("store_level"), row.get("sample_size"),
             row.get("unit_price"), row.get("benchmark_median_price"),
             row.get("price_premium_pct"), row.get("sales_boxes")]
            for row in anomalies]
    source = "AI_ANALYSIS_LAYER_V1：fact_pos_sales + dim_store + dim_sku"
    result_items = [{"kind": "fact", "label": "事实", "text": headline, "source": source}]
    if not anomalies:
        result_items.append({"kind": "fact", "label": "事实",
                             "text": "当前 POS 数据同一 SKU 标价固定，最大跨店价差为 0.0%。",
                             "source": source})
    sections = [
        {"no": 1, "title": "价格异常结果", "items": result_items,
        "table": ({"columns": ["门店", "SKU", "名称", "KA", "类型", "等级", "样本量",
                                "门店价(元/盒)", "中位价", "偏高%", "销量(盒)"],
                    "rows": rows} if rows else {})},
        {"no": 2, "title": "计算口径", "items": [{
            "kind": "fact", "label": "事实", "text": ar.get("caliber") or "—",
            "source": source,
        }]},
        {"no": 3, "title": "校验与限制", "items": [
            {"kind": "fact", "label": "事实",
             "text": f"确定性 Checker：{(ar.get('checker') or {}).get('message', '—')}。",
             "source": "agent.business_checker"},
            {"kind": "limit", "label": "限制",
             "text": "未发现跨店价差不代表价格不会影响销量，只表示当前固定标价数据无法支持价格执行差异分析。",
             "source": source},
            {"kind": "limit", "label": "限制",
             "text": "部分门店等级未采集，只能在同类型的‘等级未采集’组内比较；小于最小样本量的组不判异常。",
             "source": "dim_store"},
        ]},
        {"no": 4, "title": "核查建议", "items": [
            {"kind": "advice", "label": "建议",
             "text": "如需演示真实价格异常，应补充门店实际成交价、促销折扣与日期，而不是修改现有 POS 原始数据。",
             "source": "数据改进建议"},
            {"kind": "limit", "label": "限制",
             "text": "即使发现价格偏高，也不得认定为销量变化的因果原因；需联合促销、库存和陈列验证。",
             "source": "因果边界"},
        ]},
    ]
    generator = {"mode": "deterministic_price_report", "model": None,
                 "guard": {"violations": [], "checked": True},
                 "fallback_reason": "", "understood_by": state.get("understood_by") or "rules"}
    report = {
        "scenario": "price_anomaly", "data_source": ar.get("data_source"),
        "checker": ar.get("checker") or {}, "anomalies": anomalies,
        "group_summary": groups, "headline": headline,
        "period_label": (ar.get("period") or {}).get("label") or "—",
        "scope_label": _scope_label(ar.get("scope") or {}), "result": {"rows": rows},
        "problems": {"summary": "", "rows": []},
        "causes": {"verified": [], "possible": [], "unsupported": []},
        "actions": [], "caveats": ar.get("notes") or [], "data_limits": [],
        "basis": {"tables": ar.get("tables") or [],
                  "tool_calls": state.get("tool_calls") or [],
                  "caliber": ar.get("caliber") or ""},
        "generator": generator, "question": state.get("user_question"),
        "followup": state.get("followup") or {}, "sections": sections,
        "kind_counts": sections_mod.counts(sections),
    }
    report["markdown"] = report_mod.render_markdown(report)
    timings = {**(state.get("timings") or {}),
               "generate_report": round((time.perf_counter() - t0) * 1000, 1)}
    steps = _step({"steps": steps}, "generate_report", "done", "生成价格异常确定性报告")
    return {"report": report, "final_response": report["markdown"],
            "generator": generator, "steps": steps, "timings": timings}


def _generate_growth_opportunity_report(state: dict, ar: dict,
                                        steps: list[dict], t0: float) -> dict:
    """增长机会确定性报告：不把“无 POS”写成“确认未铺货”。"""
    items = ar.get("opportunities") or []
    table_rows = []
    for row in items:
        stores = "、".join(x.get("store_name") or "" for x in row.get("gap_detail") or [])
        low_stores = "、".join(
            f"{x.get('store_name')}({x.get('monthly_boxes'):g})"
            for x in row.get("low_velocity_detail") or [])
        table_rows.append([
            row.get("sku_code"), row.get("sku_name"), f"{row.get('yoy_pct') or 0:g}%",
            row.get("responsible_stores"), row.get("gap_stores"), row.get("suspect_oos"),
            row.get("to_verify"), row.get("low_velocity_stores"), low_stores or "—",
            row.get("p50_boxes"), row.get("inc_p50_boxes"),
            row.get("inc_p50_amount_wan"), stores or "—",
        ])
    scope = ar.get("scope") or {}
    headline = (f"{scope.get('region') or '当前范围'}识别出 {len(items)} 个高增长 SKU 机会；"
                "缺口门店需经 SFA/库存核实")
    source = "AI_ANALYSIS_LAYER_V1：metric_pos_yoy + fact_pos_sales + fact_sfa_visit"
    sections = [
        {"no": 1, "title": "机会清单", "items": [{
            "kind": "fact", "label": "事实", "text": headline, "source": source,
        }], "table": {
            "columns": ["SKU", "名称", "YoY", "责任门店", "连续两月无 POS", "疑似缺货",
                        "待核实", "已售低动销", "低动销门店(盒/月)", "P50 单店盒/月",
                        "中性增量盒/月", "中性增量万元/月", "缺口门店"],
            "rows": table_rows,
            "footer": "无 POS 记录 ≠ 未上架；表中门店均需人工核实后再决定铺货。",
        }},
        {"no": 2, "title": "计算口径", "items": [{
            "kind": "fact", "label": "事实", "text": ar.get("caliber") or "—",
            "source": source,
        }]},
        {"no": 3, "title": "检查与风险", "items": [
            {"kind": "fact", "label": "事实",
             "text": f"确定性 Checker：{(ar.get('checker') or {}).get('message', '—')}。",
             "source": "agent.business_checker"},
            {"kind": "limit", "label": "限制",
             "text": "无 POS 记录 ≠ 未上架；目前只能区分疑似缺货与待核实，不能输出确认未铺货。",
             "source": "SKU Gap 三态口径"},
            {"kind": "fact", "label": "关联发现",
             "text": "已有 POS 销售但低于同 SKU P25 的门店列为已售低动销，转入动销核查，不重复建议铺货。",
             "source": source},
            {"kind": "limit", "label": "限制",
             "text": "POS 仅覆盖 27 家对齐门店；城市和门店等级未采集，基准降级为责任范围内在售门店分位数。",
             "source": source},
        ]},
        {"no": 4, "title": "建议与人工确认", "items": [
            {"kind": "advice", "label": "建议",
             "text": "优先核实中性增量高的门店：待核实门店补做上架/库存/扫码检查，疑似缺货门店先查库存与扫码。",
             "source": "执行后由销售负责人决定是否铺货"},
            {"kind": "limit", "label": "限制",
             "text": "Agent 只生成机会与核实清单，不自动创建拜访任务，不自动调整铺货。",
             "source": "人机协同边界"},
        ]},
    ]
    generator = {"mode": "deterministic_growth_report", "model": None,
                 "guard": {"violations": [], "checked": True},
                 "fallback_reason": "", "understood_by": state.get("understood_by") or "rules"}
    report = {
        "scenario": "growth_opportunity", "data_source": ar.get("data_source"),
        "checker": ar.get("checker") or {}, "opportunities": items,
        "headline": headline, "period_label": (ar.get("period") or {}).get("label") or "—",
        "scope_label": _scope_label(scope), "result": {"rows": table_rows},
        "problems": {"summary": "", "rows": []},
        "causes": {"verified": [], "possible": [], "unsupported": []},
        "actions": [], "caveats": ar.get("notes") or [], "data_limits": [],
        "basis": {"tables": ar.get("tables") or [],
                  "tool_calls": state.get("tool_calls") or [],
                  "caliber": ar.get("caliber") or ""},
        "generator": generator, "question": state.get("user_question"),
        "followup": state.get("followup") or {}, "sections": sections,
        "kind_counts": sections_mod.counts(sections),
    }
    report["markdown"] = report_mod.render_markdown(report)
    timings = {**(state.get("timings") or {}),
               "generate_report": round((time.perf_counter() - t0) * 1000, 1)}
    steps = _step({"steps": steps}, "generate_report", "done", "生成增长机会确定性报告")
    return {"report": report, "final_response": report["markdown"],
            "generator": generator, "steps": steps, "timings": timings}


def _generate_direct_report(state: dict, ar: dict,
                            steps: list[dict], t0: float) -> dict:
    """单工具直达报告：只复述工具事实，不做跨系统原因推断。"""
    value = ar.get("metric_value")
    unit = ar.get("metric_unit") or ""
    value_text = "—" if value is None else (
        f"{value:,.2f}" if isinstance(value, float) else f"{value:,}")
    headline = f"{ar.get('metric_label')}：{value_text} {unit}".strip()
    if ar.get("tool") == "query_pos_performance":
        headline += "（Sell-out，不与 ERP 相加）"
    source = "、".join(ar.get("tables") or [])
    notes = ar.get("notes") or []
    sections = [
        {"no": 1, "title": "查询结果", "items": [{
            "kind": "fact", "label": "事实", "text": headline,
            "source": source or ar.get("data_source") or "业务工具",
        }]},
        {"no": 2, "title": "计算口径", "items": [{
            "kind": "fact", "label": "事实", "text": ar.get("caliber") or "按工具原口径返回",
            "source": ar.get("data_source") or source,
        }]},
        {"no": 3, "title": "数据边界", "items": [
            {"kind": "limit", "label": "限制", "text": note,
             "source": ar.get("data_source") or source} for note in notes
        ] or [{"kind": "limit", "label": "限制",
               "text": "本回答仅复述单一工具结果，不进行跨系统因果判断。",
               "source": ar.get("data_source") or source}]},
    ]
    generator = {"mode": "deterministic_direct_report", "model": None,
                 "guard": {"violations": [], "checked": True},
                 "fallback_reason": "", "understood_by": state.get("understood_by") or "rules"}
    scope_label = _scope_label(ar.get("scope") or {})
    for value in (state.get("ka_system"), state.get("store"), state.get("channel"),
                  state.get("category"), state.get("sku")):
        if value and value not in scope_label:
            scope_label = f"{scope_label} · {value}"
    if state.get("page_name"):
        scope_label = f"{scope_label}（来自 {state['page_name']}）"
    report = {
        "scenario": "direct_query", "tool": ar.get("tool"),
        "headline": headline, "period_label": (ar.get("period") or {}).get("label") or "—",
        "scope_label": scope_label,
        "page_context": {
            "page_id": state.get("page_id") or "", "page_name": state.get("page_name") or "",
            "module": state.get("module") or "", "filters": state.get("page_filters") or {},
            "scope_label": scope_label,
            "rule": "页面筛选只作默认范围；问题里明确说出的条件优先",
        },
        "metric": {"label": ar.get("metric_label"), "value": value, "unit": unit},
        "sections": sections, "kind_counts": sections_mod.counts(sections),
        "result": {"rows": [[ar.get("metric_label"), value_text, unit]]},
        "problems": {"summary": "", "rows": []},
        "causes": {"verified": [], "possible": [], "unsupported": []},
        "actions": [], "caveats": notes, "data_limits": [],
        "basis": {"tables": ar.get("tables") or [],
                  "tool_calls": state.get("tool_calls") or [],
                  "caliber": ar.get("caliber") or ""},
        "generator": generator, "question": state.get("user_question"),
        "followup": state.get("followup") or {},
    }
    report["markdown"] = report_mod.render_markdown(report)
    timings = {**(state.get("timings") or {}),
               "generate_report": round((time.perf_counter() - t0) * 1000, 1)}
    steps = _step({"steps": steps}, "generate_report", "done", "生成单工具确定性报告")
    return {"report": report, "final_response": report["markdown"],
            "generator": generator, "steps": steps, "timings": timings}


def _generate_supply_growth_report(state: dict, ar: dict,
                                   steps: list[dict], t0: float) -> dict:
    """生成内存情景模拟报告；不调用 LLM 改写计算数字。"""
    selected = ar.get("selected_skus") or []
    materials = ar.get("scenario_materials") or []
    growth = ar.get("growth_pct") or 0
    scope_mode = ar.get("scope_mode") or "filtered_skus"
    scope_text = ("全量预测 SKU" if scope_mode == "all_forecast_skus" else
                  " ".join(x for x in (ar.get("category") or "",
                                        f"{ar.get('pieces_per_box')} 粒装"
                                        if ar.get("pieces_per_box") else "") if x))
    sales_impact = ar.get("sales_impact") or {}
    headline = (f"{scope_text}：未来需求增加 {growth:g}%：影响 {len(selected)} 个 SKU、"
                f"新增 {sales_impact.get('incremental_boxes') or 0:,} 盒需求、"
                f"{len(materials)} 种物料")
    table_rows = [[
        row.get("material_name"), row.get("material_id"),
        "水果原料" if row.get("material_type") == "fruit" else "包材",
        row.get("unit"), f"{row.get('baseline_demand') or 0:,}",
        f"{row.get('adjusted_demand') or 0:,}", f"{row.get('incremental_demand') or 0:,}",
        f"{row.get('inventory_coverage_qty') or 0:,}",
        f"{row.get('incremental_net_requirement') or 0:,}",
    ] for row in materials]
    source = "、".join(ar.get("tables") or [])
    sections = [
        {"no": 1, "title": "情景结论", "items": [
            {"kind": "fact", "label": "事实", "text": headline,
             "source": "内存情景计算 + 独立 Checker"},
        ], "table": {"columns": ["物料", "ID", "类型", "单位", "原始预测需求",
                                      "调整后需求", "新增物料需求", "库存可覆盖数量",
                                      "新增净采购需求"], "rows": table_rows,
                     "footer": "水果与包材单位不同，不跨物料汇总。"}},
        {"no": 2, "title": "SKU 调整范围", "items": [{
            "kind": "fact", "label": "事实",
            "text": (f"仅调整 {len(selected)} 个匹配 SKU；每个预测月按原需求 × "
                     f"{1 + growth / 100:.2f} 四舍五入。"),
            "source": "fact_production_forecast + dim_sku",
        }], "table": {"columns": ["SKU", "名称", "原始盒数", "调整后盒数", "新增盒数"],
                     "rows": [[row.get("sku_code"), row.get("sku_name"),
                               f"{row.get('baseline_boxes') or 0:,}",
                               f"{row.get('adjusted_boxes') or 0:,}",
                               f"{row.get('incremental_boxes') or 0:,}"] for row in selected]}},
        {"no": 3, "title": "校验与边界", "items": [
            {"kind": "fact", "label": "事实",
             "text": f"确定性 Checker：{(ar.get('checker') or {}).get('message', '—')}。",
             "source": "agent.supply_chain_checker"},
            {"kind": "limit", "label": "限制",
             "text": "这是内存情景模拟，不会修改正式预测表、库存、BOM 或采购参数。",
             "source": source},
            {"kind": "limit", "label": "限制",
             "text": "水果原料没有采购参数，只展示新增需求与库存缺口，不生成水果采购建议。",
             "source": "供应链业务边界"},
            {"kind": "limit", "label": "限制",
             "text": "全部数据为 MOCK_SUPPLY_CHAIN；库存是 2026-10 月末快照，不是实时库存。",
             "source": "工具元数据"},
        ]},
    ]
    generator = {"mode": "deterministic_tool_report", "model": None,
                 "guard": {"violations": [], "checked": True},
                 "fallback_reason": "", "understood_by": state.get("understood_by") or "rules"}
    report = {
        "scenario": "supply_chain_growth", "data_source": "MOCK_SUPPLY_CHAIN",
        "growth_pct": growth, "scope_mode": scope_mode,
        "sales_impact": sales_impact,
        "headline": headline, "period_label": (ar.get("period") or {}).get("label") or "—",
        "scope_label": f"{scope_text} · +{growth:g}%",
        "selected_skus": selected, "scenario_materials": materials,
        "checker": ar.get("checker") or {}, "sections": sections,
        "kind_counts": sections_mod.counts(sections), "data_limits": [],
        "caveats": ar.get("notes") or [], "generator": generator,
        "question": state.get("user_question"), "followup": state.get("followup") or {},
        "basis": {"tables": ar.get("tables") or [], "tool_calls": state.get("tool_calls") or []},
    }
    report["markdown"] = report_mod.render_markdown(report)
    timings = {**(state.get("timings") or {}),
               "generate_report": round((time.perf_counter() - t0) * 1000, 1)}
    steps = _step({"steps": steps}, "generate_report", "done",
                  "生成确定性内存情景报告（未调用 LLM 生成数字）")
    return {"report": report, "final_response": report["markdown"],
            "generator": generator, "steps": steps, "timings": timings}


def _generate_supply_shortage_report(state: dict, ar: dict,
                                     steps: list[dict], t0: float) -> dict:
    """生成原料/包材分区的短缺风险报告，影响盒数明确标为估算。"""
    fruit = ar.get("fruit_shortages") or []
    packaging = ar.get("packaging_shortages") or []
    exclude_in_transit = bool(ar.get("exclude_in_transit"))

    def table_rows(rows: list[dict]) -> list[list[str]]:
        return [[
            row.get("material_name"), row.get("material_id"), row.get("unit"),
            f"{row.get('forecast_demand') or 0:,}",
            f"{row.get('inventory_qty') or 0:,}",
            f"{row.get('in_transit_qty') or 0:,}",
            f"{row.get('available_qty') or 0:,}",
            f"{row.get('shortage_qty') or 0:,}",
            f"{row.get('coverage_pct') or 0:g}%",
            str(row.get("affected_sku_count") or 0),
            f"{row.get('estimated_impacted_boxes') or 0:,}",
            row.get("risk_level") or "—",
        ] for row in rows]

    transit_column = "在途（未计入）" if exclude_in_transit else "在途"
    columns = ["物料", "ID", "单位", "预测需求", "月末库存", transit_column, "可用量",
               "短缺量", "覆盖率", "关联 SKU 数", "估算影响盒数", "风险"]
    prefix = "在途延期情景下，" if exclude_in_transit else ""
    headline = (f"{prefix}未来两个月识别到 {len(fruit)} 种水果原料和 "
                f"{len(packaging)} 种包材存在短缺风险")
    source = "、".join(ar.get("tables") or [])
    sections = [
        {"no": 1, "title": "水果原料短缺风险", "items": [{
            "kind": "fact", "label": "事实",
            "text": f"{len(fruit)} 种水果原料的预测需求高于月末库存。",
            "source": "forecast_material_demand + query_material_inventory",
        }], "table": {"columns": columns, "rows": table_rows(fruit),
                       "footer": "水果无采购参数、无可确认在途口径，不生成采购建议。"}},
        {"no": 2, "title": "包材短缺风险", "items": [{
            "kind": "fact", "label": "事实",
            "text": (f"{len(packaging)} 种包材在在途暂不计入可用量时存在缺口。"
                     if exclude_in_transit else
                     f"{len(packaging)} 种包材在计入工具返回的在途后仍存在缺口。"),
            "source": "forecast_material_demand + query_material_inventory",
        }], "table": {"columns": columns, "rows": table_rows(packaging),
                       "footer": ("在途延期情景中包材可用量=月末库存，在途仅披露、暂不计入。"
                                  if exclude_in_transit else
                                  "不同物料单位不直接汇总；包材可用量=月末库存+在途。")}},
        {"no": 3, "title": "影响测算与边界", "items": [
            {"kind": "fact", "label": "事实",
             "text": f"确定性 Checker：{(ar.get('checker') or {}).get('message', '—')}。",
             "source": "agent.supply_chain_checker"},
            {"kind": "limit", "label": "限制",
             "text": "估算影响盒数采用比例分摊估算：关联 SKU 预测盒数 × 物料短缺量/物料需求量；不能作为精确排产承诺。",
             "source": source},
            {"kind": "limit", "label": "限制",
             "text": "库存为 2026-10 月末库存快照，不是实时库存；在途采用模拟数据返回口径，未进一步确认到货状态。",
             "source": "query_material_inventory"},
            {"kind": "limit", "label": "限制",
             "text": "全部数据为固定种子 MOCK_SUPPLY_CHAIN；结果仅供人工复核，不会自动下采购单或修改生产计划。",
             "source": "供应链 V2 元数据"},
        ]},
    ]
    generator = {"mode": "deterministic_tool_report", "model": None,
                 "guard": {"violations": [], "checked": True},
                 "fallback_reason": "", "understood_by": state.get("understood_by") or "rules"}
    report = {
        "scenario": "supply_chain_shortage", "data_source": "MOCK_SUPPLY_CHAIN",
        "headline": headline, "period_label": (ar.get("period") or {}).get("label") or "—",
        "scope_label": "水果原料与包材短缺风险（MOCK_SUPPLY_CHAIN）",
        "fruit_shortages": fruit, "packaging_shortages": packaging,
        "exclude_in_transit": exclude_in_transit,
        "checker": ar.get("checker") or {}, "sections": sections,
        "kind_counts": sections_mod.counts(sections), "data_limits": [],
        "caveats": ar.get("notes") or [], "generator": generator,
        "question": state.get("user_question"), "followup": state.get("followup") or {},
        "basis": {"tables": ar.get("tables") or [], "tool_calls": state.get("tool_calls") or [],
                  "impact_method": ar.get("impact_method")},
    }
    report["markdown"] = report_mod.render_markdown(report)
    timings = {**(state.get("timings") or {}),
               "generate_report": round((time.perf_counter() - t0) * 1000, 1)}
    steps = _step({"steps": steps}, "generate_report", "done",
                  "生成确定性短缺风险报告（影响盒数为比例估算）")
    return {"report": report, "final_response": report["markdown"],
            "generator": generator, "steps": steps, "timings": timings}


def _check_payload(payload: dict, allowed: dict) -> list[str]:
    """对 LLM 产出的每一段文本做数字校验，任何一段越界都整段作废。"""
    texts: list[str] = []
    if isinstance(payload.get("headline"), str):
        texts.append(payload["headline"])
    for item in payload.get("possible") or []:
        if isinstance(item, dict):
            texts += [str(item.get(k) or "") for k in ("text", "why", "verify")]
    for item in payload.get("actions") or []:
        if isinstance(item, dict):
            texts += [str(item.get(k) or "") for k in ("action", "check", "owner")]
    for item in payload.get("unsupported") or []:
        texts.append(str(item))
    for item in payload.get("caveats") or []:
        texts.append(str(item))
    bad: list[str] = []
    for t in texts:
        bad += report_mod.guard_numbers(t, allowed)
    return _dedup(bad)


def _digest(ar: dict) -> str:
    """给模型看的证据摘要（数字都在这里，模型只能引用不能新造）。"""
    r = ar.get("result") or {}
    p = ar.get("problems") or {}
    lines = [
        f"期间：{(ar.get('period') or {}).get('label')}；"
        f"范围：{_scope_label(ar.get('scope') or {})}",
        f"预算 TE：{r.get('budget_te')}；实际 TE：{r.get('te')}；"
        f"达成率：{r.get('attainment_pct')}%；差额：{r.get('gap_te')} TE",
        f"出货金额：{r.get('amount')}；毛利额：{r.get('gross_profit')}；"
        f"毛利率：{r.get('gross_margin_pct')}%；同比：{r.get('yoy_pct')}%",
        f"未达成客户数：{p.get('unmet_count')}；未达成缺口合计：{p.get('unmet_gap_te')} TE；"
        f"前 3 家集中度：{p.get('concentration_top3_pct')}%",
    ]
    for i, c in enumerate(p.get("top_gap") or [], 1):
        lines.append(
            f"缺口{i}：{c.get('customer')}（{c.get('segment_label')}）"
            f"实际 {c.get('te')} / 预算 {c.get('budget_te')} TE，"
            f"达成 {c.get('attainment_pct')}%，缺口 {c.get('gap_te')} TE，"
            f"占缺口池 {c.get('gap_share_pct')}%，同比 {c.get('yoy_pct')}%")
    if ar.get("trend"):
        t = ar["trend"]
        lines.append(f"近三月环比：{t.get('mom_pct')}%")
    if ar.get("pos"):
        pm = ar["pos"]
        lines.append(f"POS：{pm.get('stores')} 家门店，{pm.get('sales_te')} TE，"
                     f"金额 {pm.get('sales_amount')}")
    if ar.get("sfa"):
        sm = ar["sfa"]
        lines.append(f"SFA：拜访 {sm.get('visits')} 次，完成率 {sm.get('completed_rate_pct')}%，"
                     f"陈列记录率 {sm.get('display_recorded_rate_pct')}%，"
                     f"覆盖 {(sm.get('date_range') or {}).get('from')} ~ "
                     f"{(sm.get('date_range') or {}).get('to')}")
    if ar.get("wholesale"):
        wm = ar["wholesale"]
        lines.append(f"批发：出库 {wm.get('sales_te')} TE，采购 {wm.get('purchase_te')} TE，"
                     f"毛利率 {wm.get('gross_margin_pct')}%，{wm.get('stalls')} 个档口")
    return "\n".join(lines)


def _facts_text(ar: dict) -> str:
    return "\n".join(f"- {x['text']}" for x in
                     (ar.get("causes_rule") or {}).get("verified") or [])


def _headline(ar: dict, it: dict) -> str:
    r = ar.get("result") or {}
    p = ar.get("problems") or {}
    if r.get("attainment_pct") is None:
        return "数据不足，无法给出达成结论"
    # 用完整范围（战区 · 市场部 · 客户 · 客户类型）：只写战区会把「KA客户C一家」
    # 的数字标成「全国」，是最容易被误读的一种写法
    scope = _scope_label(ar.get("scope") or {})
    period = (ar.get("period") or {}).get("label") or ""
    att = r["attainment_pct"]
    gap = abs(r.get("gap_te") or 0)
    if att >= 100:
        # 整体超额时不能说「缺口集中」，要先说结论再说隐忧
        base = f"{scope} {period} TE 达成 {att:.2f}%，超预算 {gap:,.0f} TE"
        if p.get("unmet_count"):
            base += f"；但仍有 {p['unmet_count']} 家客户未达成"
        return base + _dim_tail(ar, it)
    base = f"{scope} {period} TE 达成 {att:.2f}%，差预算 {gap:,.0f} TE"
    if p.get("top_gap"):
        base += f"；缺口主要在 {p['top_gap'][0]['customer']}"
    return base + _dim_tail(ar, it)


def _f(v, default: float = 0.0) -> float:
    """工具指标可能是 None（该范围没有业务），格式化前必须兜底。

    ``dict.get(k, 0)`` 只在「键不存在」时生效；键存在但值为 None 时照样炸，
    这类 500 只在空范围（如某品类在某月没有出货）才复现，最容易漏到线上。
    """
    try:
        return default if v is None else float(v)
    except (TypeError, ValueError):
        return default


def _dim_tail(ar: dict, it: dict) -> str:
    """问的是 POS / SFA / 批发的，就把那个维度的数字放进第一行。

    否则用户问「终端动销怎么样」，第一行却在讲 ERP 达成率，看着就是答非所问。
    这三个数字各自独立成句，不与 ERP 相加，也不换算成达成率。
    """
    kind = it.get("type")
    if kind == "pos_review" and ar.get("pos"):
        m = ar["pos"]
        return (f"｜POS 终端销售 {_f(m.get('sales_te')):,.2f} TE、"
                f"{_f(m.get('stores')):.0f} 家门店（Sell-out，不与 ERP 相加）")
    if kind == "sfa_review" and ar.get("sfa"):
        m = ar["sfa"]
        rng = (m.get("date_range") or {})
        span = f"{rng.get('from')}~{rng.get('to')}" if rng.get("from") else "期间未覆盖"
        return (f"｜SFA 拜访 {_f(m.get('visits')):.0f} 次、完成率 {_f(m.get('completed_rate_pct')):.2f}%、"
                f"陈列记录率 {_f(m.get('display_recorded_rate_pct')):.2f}%（记录日期 {span}，不是销量）")
    if kind == "wholesale_review" and ar.get("wholesale"):
        m = ar["wholesale"]
        return (f"｜批发出库 {_f(m.get('sales_te')):,.2f} TE、采购 {_f(m.get('purchase_te')):,.2f} TE、"
                f"毛利率 {_f(m.get('gross_margin_pct')):.2f}%（{_f(m.get('customers')):.0f} 家批发客户，不与 ERP 相加）")
    return ""


def _problem_summary(ar: dict) -> str:
    p = ar.get("problems") or {}
    r = ar.get("result") or {}
    if not p.get("unmet_count"):
        return "该范围内没有未达成客户（或客户维度数据缺失）。"
    tail = ""
    if r.get("gap_te") is not None and r["gap_te"] >= 0:
        tail = (f"整体虽然超预算 {r['gap_te']:,.2f} TE，但这些客户的缺口仍被超额部分掩盖，"
                f"需要单独跟进。")
    return (f"范围内 {p.get('customers_total')} 家客户中 {p.get('unmet_count')} 家未达成，"
            f"缺口合计 {abs(p.get('unmet_gap_te') or 0):,.2f} TE"
            f"（相当于同期预算的 {p.get('unmet_gap_vs_budget_pct') or 0:.2f}%）；"
            f"前 3 家占未达成缺口的 {p.get('concentration_top3_pct')}%。{tail}")


def _scope_label(scope: dict) -> str:
    region = scope.get("region") or "全国（五大战区）"
    dept = scope.get("department") or ""
    cust = scope.get("customer") or ""
    seg = (scope.get("segment") or "").strip()
    parts = [region]
    if dept and dept != "全部":
        parts.append(dept)
    if cust and cust != "全部客户":
        parts.append(cust)
    # 按客户类型筛选时必须写出来，否则「战区B 125.66%」会被误读成整个战区
    if seg and seg not in ("全部", "全部类型"):
        parts.append({"clubz": "ClubZ", "nka": "NKA", "lka": "LKA",
                      "wholesale": "批发客户"}.get(seg, seg))
    return " · ".join(parts)


def _rule_actions(ar: dict) -> list[dict]:
    p = ar.get("problems") or {}
    acts: list[dict] = []
    for c in (p.get("top_gap") or [])[:3]:
        acts.append({
            "action": f"跟 {c['customer']} 对账：确认缺口 {abs(c['gap_te']):,.2f} TE 是提货节奏、"
                      f"价格还是库存问题，本周内给出补量计划",
            "owner": "区域经理 / 客户负责人",
            "check": "下周出货 TE 是否回到预算节奏，缺口是否收窄",
        })
    if ar.get("sfa") and (ar["sfa"].get("display_recorded_rate_pct") or 100) < 50:
        acts.append({"action": "补齐陈列记录：对未记录陈列的门店做一次巡检并补拍标准陈列",
                     "owner": "城市主管", "check": "陈列记录率是否提升到 80% 以上"})
    if ar.get("wholesale"):
        acts.append({"action": "核查批发出库与下游库存，判断是渠道压货还是终端真实动销",
                     "owner": "批发渠道负责人", "check": "批发出库 TE 与下游库存周转天数是否同步改善"})
    if not acts:
        acts.append({"action": "拉齐该范围的客户级预算与实际明细，确认口径后再定跟进对象",
                     "owner": "销售运营", "check": "客户明细合计与战区合计一致"})
    return acts[:4]


def _rule_caveats(ar: dict) -> list[str]:
    out = ["预算为演示用模拟数据（MOCK_BUDGET_TE），不是真实业务预算，不能用于考核。"]
    if (ar.get("pos") or {}).get("stores"):
        out.append(f"POS 只覆盖 {ar['pos']['stores']} 家与 SFA 对齐的门店，样本不代表全国终端。")
    if ar.get("sfa"):
        dr = ar["sfa"].get("date_range") or {}
        if dr.get("from"):
            out.append(f"SFA 记录只覆盖 {dr['from']} ~ {dr['to']}，不代表整月执行情况。")
    return out


# ---------------------------------------------------------------- 终点
def ask_clarification(state: dict) -> dict:
    q = state.get("clarification") or "请补充战区、年份和月份。"
    report = {
        "headline": "需要补充信息",
        "period_label": f"{(state.get('year') or config.FIXED_YEAR_INT)} 年",
        "scope_label": "—",
        "result": {"rows": []},
        "problems": {"summary": q, "rows": []},
        "causes": {"verified": [], "possible": [], "unsupported": []},
        "actions": [],
        "caveats": [],
        "basis": {"period": "—", "tools": [], "cross_checks": [], "data_gaps": []},
        "generator": {"mode": "clarification", "guard": {"violations": []}},
        "needs_clarification": True,
        "question": state.get("user_question"),
    }
    report["markdown"] = f"### 需要补充信息\n\n{q}"
    steps = _step(state, "ask_clarification", "done", q)
    return {"report": report, "final_response": report["markdown"], "steps": steps,
            "need_clarification": True, "clarification": q,
            "generator": {"mode": "clarification"}}


def handle_failure(state: dict) -> dict:
    errs = [e for e in (state.get("errors") or []) if e.get("node") == "call_business_tools"]
    checker = state.get("checker") or {}
    checker_blocked = checker.get("status") == "blocked" or state.get("checker_blocked")
    msg = (checker.get("message") if checker_blocked else "") or \
        "；".join((e.get("error") or {}).get("message", "") for e in errs) or "所有业务工具均未返回数据"
    hint = next(((e.get("error") or {}).get("hint") for e in errs
                 if (e.get("error") or {}).get("hint")), "")
    kinds = [(e.get("error") or {}).get("type") for e in errs]
    # 失败也要说人话：把工具错误类型翻成一句结论，而不是只丢一句「失败」
    headline = "证据不足，已停止输出结论" if checker_blocked else {
        "PeriodNotObserved": "该期间尚未发生业务",
        "UnsupportedYear": "该年份没有可用的预算数据",
        "InvalidRegion": "战区不在当前范围",
        "InvalidDepartment": "市场部不存在",
        "MissingArgument": "缺少必要的筛选条件",
        "DatabaseError": "数据源不可读",
    }.get(kinds[0] if kinds else "", "数据查询失败")
    internal = (state.get("analysis_intent") or {}).get("type") or ""
    scenario = {"price_anomaly": "price_anomaly", "growth_opportunity": "growth_opportunity",
                "supply_chain_shortage": "supply_chain_shortage",
                "supply_chain_forecast": "supply_chain_forecast",
                "supply_chain_growth": "supply_chain_growth"}.get(internal)
    report = {
        "headline": headline,
        "period_label": (state.get("year") and f"{state.get('year')} 年") or "—",
        "scope_label": state.get("region") or "全国",
        "result": {"rows": []},
        "problems": {"summary": msg, "rows": []},
        "causes": {"verified": [], "possible": [],
                   "unsupported": ["Checker 未通过，本次不给出未经验证的结论。"
                                   if checker_blocked else
                                   "工具执行失败，本次不给出任何结论，避免编数。"]},
        "actions": [{"action": ("扩大可比样本或补充数据后重新分析" if checker_blocked else
                                hint or "检查数据库/预算库是否就绪后重试"),
                     "owner": "提问方" if hint else "系统运维",
                     "check": "重新提问后工具返回 ok=true"}],
        "caveats": [],
        "basis": {"period": "—", "tools": state.get("tool_calls") or [],
                  "cross_checks": [], "data_gaps": [msg]},
        "generator": {"mode": "error"},
        "checker": checker,
        **({"scenario": scenario} if scenario else {}),
        "error": {"message": msg, "details": errs},
        "question": state.get("user_question"),
    }
    report["markdown"] = (f"### {headline}\n\n{msg}\n\n"
                          + (f"{hint}\n\n" if hint else "") + "本次不给出任何结论。")
    steps = _step(state, "handle_failure", "done", msg)
    return {"report": report, "final_response": report["markdown"], "steps": steps,
            "generator": {"mode": "error"}}
