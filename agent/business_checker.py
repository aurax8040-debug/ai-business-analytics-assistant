"""经营分析的确定性 Checker。

当前先覆盖预算达成诊断。它不调用 LLM，也不复用报告层结果，而是直接从
预算、ERP 和客户工具的原始返回值独立复算 TE 达成率与差额。
"""
from __future__ import annotations


TOLERANCE_TE = 0.05


def _item(name: str, ok: bool, detail: str, target: str = "") -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail, "target": target}


def _same_period(*results: dict) -> bool:
    values = []
    for result in results:
        period = result.get("period") or {}
        values.append((period.get("year"), tuple(period.get("months") or [])))
    return bool(values) and len(set(values)) == 1


def _same_scope(*results: dict) -> bool:
    """只在同一业务范围内做跨工具总量对账。"""
    keys = ("region", "department", "customer", "year", "months", "segment")
    normalized = []
    for result in results:
        args = result.get("args") or {}
        normalized.append(tuple(tuple(args.get(k) or []) if k == "months"
                                else (args.get(k) or "") for k in keys))
    return bool(normalized) and len(set(normalized)) == 1


def _retry_item(state: dict, target: str) -> dict:
    for item in state.get("analysis_plan") or []:
        if item.get("key") == target:
            return dict(item)
    result = (state.get("tool_results") or {}).get(target) or {}
    tools = {"budget": "query_budget", "erp": "query_erp_actual",
             "customer": "query_customer_performance"}
    return {"key": target, "tool": tools[target],
            "args": dict(result.get("args") or {}),
            "why": f"Checker 定向重查 {target}",
            "label": f"{target}（Checker 重试）"}


def _check_growth_opportunity(state: dict) -> dict:
    result = (state.get("tool_results") or {}).get("growth_opportunity") or {}
    rows = (result.get("rows") or {}).get("opportunities") or []
    checks = [
        _item("机会工具成功", bool(result.get("ok")), "确定性机会工具已返回",
              "growth_opportunity"),
        _item("数据来源声明", result.get("data_source") == "AI_ANALYSIS_LAYER_V1",
              f"数据来源={result.get('data_source')}", "growth_opportunity"),
        _item("机会清单非空", bool(rows), f"返回 {len(rows)} 个机会 SKU",
              "growth_opportunity"),
    ]
    threshold_ok = all((row.get("yoy_pct") or 0) >= 20
                       and (row.get("y26_amount_wan") or 0) >= 100 for row in rows)
    checks.append(_item("高增长与规模门槛", threshold_ok,
                        "YoY≥20% 且 Y26 销售额≥100万元",
                        "growth_opportunity" if not threshold_ok else ""))
    coverage_ok = all((row.get("gap_stores") or 0) + (row.get("sold_stores") or 0)
                      == (row.get("responsible_stores") or 0) for row in rows)
    checks.append(_item("门店覆盖守恒", coverage_ok, "缺口门店+在售门店=责任门店",
                        "growth_opportunity" if not coverage_ok else ""))
    tri_ok = all((row.get("suspect_oos") or 0) + (row.get("to_verify") or 0)
                 == (row.get("gap_stores") or 0) for row in rows)
    checks.append(_item("SFA 三态守恒", tri_ok, "疑似缺货+待核实=缺口门店",
                        "growth_opportunity" if not tri_ok else ""))
    low_velocity_ok = all(
        (row.get("low_velocity_stores") or 0)
        == len(row.get("low_velocity_detail") or [])
        and all((store.get("monthly_boxes") or 0) > 0
                and store.get("monthly_boxes") < (row.get("p25_boxes") or 0)
                and store.get("status") == "已售但低动销"
                for store in row.get("low_velocity_detail") or [])
        for row in rows)
    checks.append(_item("已铺货低动销复核", low_velocity_ok,
                        "已有销售且单店月均箱数<P25，不与缺口门店混用",
                        "growth_opportunity" if not low_velocity_ok else ""))
    math_ok = all(row.get("p50_boxes") is None
                  or row.get("inc_p50_boxes") == round(
                      row["gap_stores"] * row["p50_boxes"], 1) for row in rows)
    checks.append(_item("P50 增量独立复算", math_ok, f"复算 {len(rows)} 个 SKU",
                        "growth_opportunity" if not math_ok else ""))
    boundary_ok = any("无 POS 记录 ≠ 未上架" in note for note in result.get("notes") or [])
    checks.append(_item("未铺货边界披露", boundary_ok,
                        "无 POS 记录不直接认定未上架",
                        "growth_opportunity" if not boundary_ok else ""))
    failed = [item for item in checks if not item["ok"]]
    if not failed:
        return {"status": "passed", "verified": True, "checks": checks,
                "retry_plan": [], "opportunities_checked": len(rows),
                "message": "增长机会确定性 Checker 全部通过"}
    if int(state.get("retry_count") or 0) < 1:
        plan = next((dict(item) for item in state.get("analysis_plan") or []
                     if item.get("key") == "growth_opportunity"),
                    {"key": "growth_opportunity",
                     "tool": "query_sku_distribution_opportunities",
                     "args": {"region": state.get("region") or "", "top_n": 10},
                     "why": "Checker 定向重查增长机会", "label": "增长机会（重试）"})
        return {"status": "retry", "verified": False, "checks": checks,
                "retry_plan": [plan], "opportunities_checked": len(rows),
                "message": "增长机会 Checker 未通过，定向重查机会工具"}
    return {"status": "blocked", "verified": False, "checks": checks,
            "retry_plan": [], "opportunities_checked": len(rows),
            "message": "增长机会 Checker 重查后仍未通过，禁止输出机会测算"}


def _check_price_anomaly(state: dict) -> dict:
    result = (state.get("tool_results") or {}).get("price_anomaly") or {}
    metrics = result.get("metrics") or {}
    groups = (result.get("rows") or {}).get("group_summary") or []
    anomalies = (result.get("rows") or {}).get("anomalies") or []
    min_sample = int(metrics.get("min_sample") or 0)
    threshold = float(metrics.get("threshold_pct") or 0)
    checks = [
        _item("价格工具成功", bool(result.get("ok")), "价格工具已返回", "price_anomaly"),
        _item("可比组样本量", bool(groups) and all(
            int(row.get("sample_size") or 0) >= min_sample for row in groups),
              f"{len(groups)} 个组，最小样本={min_sample}", "price_anomaly"),
        _item("价格单位与可比维度", "元/盒" in (result.get("caliber") or "")
              and "同 SKU" in (result.get("caliber") or ""),
              "单位=元/盒；同 SKU+规格+门店类型+等级", "price_anomaly"),
    ]
    group_math = all((row.get("min_price") or 0) <= (row.get("median_price") or 0)
                     <= (row.get("max_price") or 0) for row in groups)
    checks.append(_item("中位价区间复核", group_math, f"复核 {len(groups)} 个价格组",
                        "price_anomaly" if not group_math else ""))
    anomaly_math = all((row.get("price_premium_pct") or 0) > threshold
                       and int(row.get("sample_size") or 0) >= min_sample
                       for row in anomalies)
    checks.append(_item("异常阈值独立复核", anomaly_math,
                        f"复核 {len(anomalies)} 条异常，阈值>{threshold}%",
                        "price_anomaly" if not anomaly_math else ""))
    count_ok = len(anomalies) == int(metrics.get("anomaly_stores") or 0)
    checks.append(_item("异常数量一致", count_ok,
                        f"明细={len(anomalies)}，指标={metrics.get('anomaly_stores')}",
                        "price_anomaly" if not count_ok else ""))
    boundary_ok = any("因果" in note for note in result.get("notes") or [])
    checks.append(_item("因果边界披露", boundary_ok, "价格异常不直接归因为销量变化",
                        "price_anomaly" if not boundary_ok else ""))
    failed = [item for item in checks if not item["ok"]]
    if not failed:
        return {"status": "passed", "verified": True, "checks": checks,
                "retry_plan": [], "groups_checked": len(groups),
                "message": "价格异常确定性 Checker 全部通过"}
    if int(state.get("retry_count") or 0) < 1:
        plan = next((dict(item) for item in state.get("analysis_plan") or []
                     if item.get("key") == "price_anomaly"),
                    {"key": "price_anomaly", "tool": "query_store_price_anomalies",
                     "args": {"region": state.get("region") or "",
                              "year": state.get("year"), "months": state.get("month") or [],
                              "threshold_pct": 10, "min_sample": 3},
                     "why": "Checker 定向重查价格异常", "label": "价格异常（重试）"})
        return {"status": "retry", "verified": False, "checks": checks,
                "retry_plan": [plan], "groups_checked": len(groups),
                "message": "价格 Checker 未通过，定向重查价格工具"}
    return {"status": "blocked", "verified": False, "checks": checks,
            "retry_plan": [], "groups_checked": len(groups),
            "message": "价格 Checker 重查后仍未通过，禁止输出异常门店"}


def check(state: dict) -> dict:
    """复核预算诊断；非该 Workflow 返回无损旁路。"""
    workflow = (state.get("supervisor_decision") or {}).get("workflow") or ""
    if workflow == "growth_opportunity":
        return _check_growth_opportunity(state)
    if workflow == "price_anomaly":
        return _check_price_anomaly(state)
    if workflow != "budget_diagnosis":
        return {"status": "not_applicable", "verified": True, "checks": [],
                "retry_plan": [], "message": "非预算诊断场景"}

    results = state.get("tool_results") or {}
    budget = results.get("budget") or {}
    erp = results.get("erp") or {}
    customer = results.get("customer") or {}
    checks: list[dict] = []

    has_customer_stage = "customer" in results
    required = [("budget", budget), ("erp", erp)]
    if has_customer_stage:
        required.append(("customer", customer))
    failed = [key for key, result in required if not result.get("ok")]
    checks.append(_item("必需工具完整性", not failed,
                        (("预算、ERP、客户工具均成功" if has_customer_stage
                          else "动态首轮预算与 ERP 工具均成功")
                         if not failed else f"失败工具：{failed}"),
                        failed[0] if failed else ""))

    bm, em, cm = (budget.get("metrics") or {}, erp.get("metrics") or {},
                  customer.get("metrics") or {})
    budget_te, actual_te = bm.get("budget_te"), em.get("te")
    customer_budget, customer_actual = cm.get("budget_te"), cm.get("te")

    periods_ok = not failed and _same_period(*([budget, erp, customer]
                                                if has_customer_stage else [budget, erp]))
    checks.append(_item("期间一致性", periods_ok,
                        "预算、ERP、客户查询期间一致" if periods_ok else "查询期间不一致",
                        "budget" if not periods_ok else ""))

    te_present = budget_te is not None and actual_te is not None
    checks.append(_item("TE 主口径完整", te_present,
                        "预算 TE 与 ERP Sell-in TE 均存在" if te_present else "预算或实际 TE 缺失",
                        "budget" if budget_te is None else ("erp" if actual_te is None else "")))

    if has_customer_stage:
        comparable_scope = _same_scope(budget, erp, customer)
        actual_match = (not comparable_scope or
                        (actual_te is not None and customer_actual is not None
                         and abs(actual_te - customer_actual) < TOLERANCE_TE))
        checks.append(_item("实际 TE 独立对账", actual_match,
                            (f"ERP={actual_te}，客户汇总={customer_actual}"
                             if comparable_scope else "客户为定向下钻范围，不与整体 ERP 强行对账"),
                            "customer" if not actual_match else ""))
        budget_match = (not comparable_scope or
                        (budget_te is not None and customer_budget is not None
                         and abs(budget_te - customer_budget) < TOLERANCE_TE))
        checks.append(_item("预算 TE 独立对账", budget_match,
                            (f"预算={budget_te}，客户汇总={customer_budget}"
                             if comparable_scope else "客户为定向下钻范围，不与整体预算强行对账"),
                            "customer" if not budget_match else ""))

        reconciliation = (customer.get("rows") or {}).get("reconciliation") or {}
        reconciliation_ok = bool(reconciliation.get("ok"))
        checks.append(_item("客户明细合计", reconciliation_ok,
                            "客户明细与范围合计一致" if reconciliation_ok else "客户明细与范围合计不一致",
                            "customer" if not reconciliation_ok else ""))

    amount = em.get("amount")
    amount_ok = amount is not None
    checks.append(_item("销售额辅助口径", amount_ok,
                        "销售额仅取 ERP Sell-in，未混入 POS Sell-out" if amount_ok else "ERP 销售额缺失",
                        "erp" if not amount_ok else ""))
    source_ok = (bool(budget.get("data_source")) and bool(erp.get("data_source"))
                 and (not has_customer_stage or bool(customer.get("data_source"))))
    checks.append(_item("数据来源可追溯", source_ok,
                        (("三个主工具" if has_customer_stage else "首轮两个主工具")
                         + "均返回数据来源" if source_ok else "存在数据来源缺失")))

    attainment = (round(actual_te / budget_te * 100, 2)
                   if actual_te is not None and budget_te not in (None, 0) else None)
    gap = (round(actual_te - budget_te, 2)
           if actual_te is not None and budget_te is not None else None)
    asks_unmet = any(word in (state.get("user_question") or "")
                     for word in ("未完成", "没完成", "没有完成", "未达成"))
    premise_supported = (attainment is not None and attainment < 100) if asks_unmet else True
    premise = {"asked_unmet": asks_unmet, "supported": premise_supported,
               "status": "supported" if premise_supported else "contradicted",
               "detail": ("实际 TE 低于预算" if premise_supported
                          else "实际 TE 不低于预算，未达成前提与数据矛盾")}
    recalculated = {"budget_te": budget_te, "actual_te": actual_te,
                    "attainment_pct": attainment, "gap_te": gap,
                    "erp_amount": amount}

    failed_checks = [item for item in checks if not item["ok"]]
    if not failed_checks:
        return {"status": "passed", "verified": True, "checks": checks,
                "retry_plan": [], "recalculated": recalculated, "premise": premise,
                "message": "预算诊断确定性 Checker 全部通过"}

    targets: list[str] = []
    for item in failed_checks:
        target = item.get("target")
        if target and target not in targets:
            targets.append(target)
    if int(state.get("retry_count") or 0) < 1 and targets:
        return {"status": "retry", "verified": False, "checks": checks,
                "retry_plan": [_retry_item(state, target) for target in targets],
                "recalculated": recalculated, "premise": premise,
                "message": f"预算 Checker 未通过，定向重查：{'、'.join(targets)}"}
    return {"status": "blocked", "verified": False, "checks": checks,
            "retry_plan": [], "recalculated": recalculated, "premise": premise,
            "message": "预算 Checker 重查后仍未通过，禁止输出未经验证的诊断数字"}
