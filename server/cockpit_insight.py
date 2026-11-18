"""AI 经营洞察 · 规则化诊断引擎（V1）。

为什么是规则引擎而不是在线大模型
------------------------------
1. 这个 Demo 没有接入在线 LLM，任何"AI 写的分析"实质都必须是可复算的规则。
   与其假装调用模型，不如把规则、阈值、证据全部摊开：每句话都能回溯到一个数字。
2. 经营结论必须能被人复核。规则引擎的每条输出都带 ``evidence``（指标 + 取值 + 来源），
   评审时可以直接对着数据库验算。
3. 输出强制三分：``facts``（实测事实）/ ``causes``（推测，带置信度与证伪方式）/
   ``actions``（建议，带责任角色与验证方式）。三者不允许混在一段话里。

数据不充分时（例如筛选后只剩 1 个组合、或缺口不足以定位到客户），
本模块返回 ``insufficient: true`` 与原因，而不是编一个故事。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from . import cockpit_queries as cq

ENGINE = {
    "name": "经营诊断规则引擎 v1",
    "note": "本地规则计算，未接入在线大模型；所有结论均由当前页面筛选条件下的实测数据推导",
    "thresholds": {
        "attainment_alert_pct": 90.0,
        "attainment_watch_pct": 95.0,
        "ka_alert_pct": 85.0,
        "margin_gap_pp": 0.8,
        "mom_alert_pct": 12.0,
        "concentration_note_pct": 50.0,
    },
}


def _fmt_te(v, digits=1):
    return f"{v / 10000:,.{digits}f} 万 TE" if v is not None else "—"


def _fmt_yuan(v, digits=1):
    return f"{v / 10000:,.{digits}f} 万元" if v is not None else "—"


def _fmt_pct(v, digits=2):
    return "—" if v is None else f"{v:.{digits}f}%"


def build(conn: sqlite3.Connection, qs: dict) -> dict:
    core = cq.build(conn, qs)
    meta = core["meta"]
    d = core["data"]
    totals = d["totals"]
    regions = [r for r in d["region"]["items"] if not r["no_data"]]
    kas = d["ka"]["items"]
    months = d["trend"]["months"]
    obs = [m for m in months if m["occurred"]]

    scope = f"{meta['period_label']} · 战区 {meta['region']} · 客户 {meta.get('ka_label', meta['ka'])}"
    facts, findings, causes, actions = [], [], [], []

    # ---------------------------------------------------------------- 事实层
    attainment = totals["attainment_pct"]
    verdict_word = "超出预算" if (attainment or 0) >= 100 else "低于预算"
    facts.append({
        "text": f"同期 TE 实际 {_fmt_te(totals['te'])}，预算 {_fmt_te(totals['budget_te'])}，"
                f"达成率 {_fmt_pct(attainment)}，{verdict_word} {_fmt_te(abs(totals['te'] - totals['budget_te']))}",
        "source": "fact_erp_shipment.shipment_te ÷ budget_te_v1.fact_te_budget_monthly.budget_te",
    })
    facts.append({
        "text": f"ERP 出货金额 {_fmt_yuan(totals['amount'])}，毛利额 {_fmt_yuan(totals['gross_profit'])}，"
                f"毛利率 {_fmt_pct(totals['gross_margin_pct'])}",
        "source": "fact_erp_shipment.sales_amount / gross_profit",
    })
    if regions:
        top = max(regions, key=lambda r: r["te"] or 0)
        worst = min(regions, key=lambda r: r["attainment_pct"] if r["attainment_pct"] is not None else 999)
        facts.append({
            "text": f"{top['name']} 贡献最大：{_fmt_te(top['te'])}，占全国 {_fmt_pct(top['share_of_te_pct'])}，"
                    f"达成 {_fmt_pct(top['attainment_pct'])}；{worst['name']} 达成最低：{_fmt_pct(worst['attainment_pct'])}",
            "source": "按 bi_region_key 聚合，P01 五大战区口径",
        })
    if kas:
        facts.append({
            "text": f"KA 集中度：前 3 家 KA 合计占 TE {_fmt_pct(totals['te_share_top3_pct'])}，"
                    f"首位 {kas[0]['name']} 占 {_fmt_pct(kas[0]['share_pct'])}",
            "source": "KA = ERP customer_type NKA，按 customer_id 聚合",
        })
    mixes = [g for g in d.get("channel", {}).get("groups", []) if (g.get("te") or 0) > 0]
    if len(mixes) > 1:
        seg = "、".join(f"{g['label']} {_fmt_pct(g['share_pct'], 1)}（{g['customers']} 家）" for g in mixes)
        facts.append({
            "text": f"客户结构：{seg}",
            "source": "KA=NKA / 区域商超=商超终端非 NKA / 批发=channel_type 批发，三组互斥",
        })
    facts.append({
        "text": f"全年预测 {_fmt_te(totals['te_forecast_year'])}（1–10 月实际 + 11–12 月模型外推），"
                f"较全年预算 {_fmt_te(totals['budget_annual_te'])} "
                f"{'高' if totals['te_forecast_year'] >= totals['budget_annual_te'] else '低'} "
                f"{_fmt_te(abs(totals['te_forecast_year'] - totals['budget_annual_te']))}",
        "source": "cockpit_queries.FORECAST_MODEL（季节性外推 v1）",
    })

    # ---------------------------------------------------------------- 发现层
    under = [r for r in regions if r["attainment_pct"] is not None and r["attainment_pct"] < 95]
    for r in sorted(under, key=lambda x: x["attainment_pct"])[:2]:
        gap = (r["budget_te"] or 0) - (r["te"] or 0)
        level = "high" if r["attainment_pct"] < 90 else "mid"
        findings.append({
            "id": f"region-{r['name']}",
            "level": level,
            "title": f"{r['name']} TE 达成 {_fmt_pct(r['attainment_pct'])}，缺口 {_fmt_te(gap)}",
            "fact": (f"实际 {_fmt_te(r['te'])} / 预算 {_fmt_te(r['budget_te'])}；"
                     f"该战区占全国预算 {_fmt_pct(r['share_of_budget_pct'])}"),
            "impact": f"拉低全国达成率约 {abs(gap) / totals['budget_te'] * 100:.2f} 个百分点",
            "evidence": [
                {"metric": "战区 TE 达成率", "value": _fmt_pct(r["attainment_pct"]),
                 "source": "fact_erp_shipment ÷ TE 预算"},
                {"metric": "战区缺口", "value": _fmt_te(gap), "source": "预算 − 实际"},
            ],
            "dimension": {"region": r["name"]},
        })

    weak_ka = [k for k in kas if k["attainment_pct"] is not None and k["attainment_pct"] < 85]
    for k in weak_ka[:2]:
        gap = (k["budget"] or 0) - (k["te"] or 0)
        findings.append({
            "id": f"ka-{k['customer_id']}",
            "level": "high" if (k["share_pct"] or 0) >= 5 else "mid",
            "title": f"{k['name']} TE 达成 {_fmt_pct(k['attainment_pct'])}，缺口 {_fmt_te(gap)}",
            "fact": (f"实际 {_fmt_te(k['te'])} / 预算 {_fmt_te(k['budget'])}；"
                     f"占全国 TE {_fmt_pct(k['share_pct'])}"),
            "impact": f"单一客户缺口占全国 TE {abs(gap) / totals['te'] * 100:.2f}%",
            "evidence": [
                {"metric": "客户 TE 达成率", "value": _fmt_pct(k["attainment_pct"]),
                 "source": "fact_erp_shipment ÷ TE 预算"},
                {"metric": "客户毛利率", "value": _fmt_pct(k["gross_margin_pct"]),
                 "source": "Σ毛利 ÷ Σ金额"},
            ],
            "dimension": {"ka": k["customer_id"]},
        })

    gm = totals["gross_margin_pct"]
    for r in regions:
        if r["gross_margin_pct"] is None or gm is None:
            continue
        gap = r["gross_margin_pct"] - gm
        if abs(gap) >= ENGINE["thresholds"]["margin_gap_pp"]:
            findings.append({
                "id": f"margin-{r['name']}",
                "level": "mid",
                "title": f"{r['name']} 毛利率 {_fmt_pct(r['gross_margin_pct'])}，偏离全国 {gap:+.1f}pp",
                "fact": f"毛利额 {_fmt_yuan(r['gross_profit'])} / 金额 {_fmt_yuan(r['amount'])}；全国 {_fmt_pct(gm)}",
                "impact": f"若回到全国毛利率，该战区毛利额约变动 {_fmt_yuan(abs(gap) / 100 * (r['amount'] or 0))}",
                "evidence": [
                    {"metric": "战区毛利率", "value": _fmt_pct(r["gross_margin_pct"]),
                     "source": "Σ毛利 ÷ Σ金额（不加权平均）"},
                ],
                "dimension": {"region": r["name"]},
            })

    if len(obs) >= 2 and obs[-2]["actual_te"]:
        chg = (obs[-1]["actual_te"] - obs[-2]["actual_te"]) / obs[-2]["actual_te"] * 100
        if abs(chg) >= ENGINE["thresholds"]["mom_alert_pct"]:
            findings.append({
                "id": "trend-mom",
                "level": "mid" if chg < 0 else "low",
                "title": f"{obs[-1]['period']} 月 TE 环比 {chg:+.1f}%",
                "fact": f"{obs[-2]['period']} 月 {_fmt_te(obs[-2]['actual_te'])} → "
                        f"{obs[-1]['period']} 月 {_fmt_te(obs[-1]['actual_te'])}",
                "impact": "单月波动，需与上月出货节奏、大客户发货计划一起看",
                "evidence": [{"metric": "月度 TE 环比", "value": f"{chg:+.2f}%",
                              "source": "相邻自然月，不做跨年对比"}],
                "dimension": {},
            })

    findings.sort(key=lambda x: ({"high": 0, "mid": 1, "low": 2}[x["level"]], x["id"]))

    # ---------------------------------------------------------------- 归因层（推测）
    for fd in findings:
        if fd["id"].startswith("region-"):
            region = fd["dimension"]["region"]
            gaps = _region_gap_by_ka(conn, qs, region)
            if gaps:
                top = gaps[0]
                share = top["gap"] / sum(g["gap"] for g in gaps) * 100 if sum(g["gap"] for g in gaps) else 0
                causes.append({
                    "finding_id": fd["id"], "type": "推测", "confidence": "中" if share >= 40 else "低",
                    "statement": (f"{region}的缺口集中在 {top['name']}：该客户缺口 {_fmt_te(top['gap'])}，"
                                  f"占该战区缺口的 {share:.0f}%"),
                    "basis": [f"{top['name']} 实际 {_fmt_te(top['te'])} / 预算 {_fmt_te(top['budget'])}",
                              f"该战区有 {len(gaps)} 个 ERP 客户未达同期预算"],
                    "verify": "调出该客户近 3 个月发货明细，确认是发货节奏后移、还是预算目标偏高",
                })
                price = top.get("price_per_te")
                nat_price = _price_per_te(conn, qs)
                if price and nat_price:
                    if price > nat_price * 1.01:
                        causes.append({
                            "finding_id": fd["id"], "type": "推测", "confidence": "低",
                            "statement": (f"{top['name']} 每 TE 金额 {price:,.0f} 元，高于全国 {nat_price:,.0f} 元，"
                                          "缺口更像「量的结构变化」而不是单纯降价"),
                            "basis": [f"该客户 Σ金额 ÷ ΣTE = {price:,.0f} 元/TE",
                                      f"全国 Σ金额 ÷ ΣTE = {nat_price:,.0f} 元/TE"],
                            "verify": "按 SKU 拆分该客户出货结构，看是否小规格/低单价 SKU 占比变化",
                        })
                    elif price < nat_price * 0.99:
                        causes.append({
                            "finding_id": fd["id"], "type": "推测", "confidence": "低",
                            "statement": (f"{top['name']} 每 TE 金额 {price:,.0f} 元，低于全国 {nat_price:,.0f} 元，"
                                          "可能存在结构性折让或价格让利"),
                            "basis": [f"该客户 Σ金额 ÷ ΣTE = {price:,.0f} 元/TE",
                                      f"全国 = {nat_price:,.0f} 元/TE"],
                            "verify": "核对该客户本期促销/返利政策与执行单据，确认是否有额外让价",
                        })
        elif fd["id"].startswith("margin-"):
            region = fd["dimension"]["region"]
            causes.append({
                "finding_id": fd["id"], "type": "推测", "confidence": "低",
                "statement": f"{region}毛利率偏离全国，可能来自品类或客户结构差异，而非单笔价格调整",
                "basis": ["毛利率按 Σ毛利 ÷ Σ金额计算，结构变化会直接体现在比值上",
                          "本期未采集分品类成本明细，无法进一步拆分价差与结构差"],
                "verify": "按品类/规格拆该战区毛利，确认是结构差（品类占比）还是价差（同规格单价）",
            })
        elif fd["id"].startswith("ka-"):
            causes.append({
                "finding_id": fd["id"], "type": "推测", "confidence": "低",
                "statement": "该客户达成偏低，需区分「预算目标偏高」与「实际发货不足」两种情形",
                "basis": ["预算按同期实际 ÷ 目标达成率生成，目标本身含随机档位",
                          "本页只有 ERP 出货，无法判断终端动销是否同步下降"],
                "verify": "对照 POS 终端销售（另看板）与该客户发货计划，判断是渠道库存积压还是终端需求走弱",
            })

    # ---------------------------------------------------------------- 行动层（建议）
    for fd in findings[:3]:
        if fd["id"].startswith("region-"):
            region = fd["dimension"]["region"]
            actions.append({
                "finding_id": fd["id"], "action": f"对 {region} 做一次缺口复盘：把缺口拆到客户×SKU，确认可追回部分",
                "owner": f"{region} 区域销售负责人 + 需求计划", "horizon": "2 周内",
                "expected": "给出「可追回 / 结构性 / 目标偏高」三类归因与金额",
                "verify": "下次月度经营会复核该战区达成率是否回到 95% 以上",
            })
        elif fd["id"].startswith("ka-"):
            name = fd["title"].split(" TE 达成")[0]
            actions.append({
                "finding_id": fd["id"], "action": f"与 {name} 对齐 11–12 月发货计划，确认缺口是否有补货窗口",
                "owner": "KA 客户经理", "horizon": "1 周内",
                "expected": "拿到书面发货计划，明确可补数量与时间点",
                "verify": "两周后看该客户 TE 实际与计划偏差是否收窄到 10% 以内",
            })
        elif fd["id"].startswith("margin-"):
            region = fd["dimension"]["region"]
            actions.append({
                "finding_id": fd["id"], "action": f"复核 {region} 的价格政策与品类结构，定位毛利率偏离来源",
                "owner": "区域商务 + 财务 BP", "horizon": "本月内",
                "expected": "输出价差/结构差分解，明确是否需要调整促销投入",
                "verify": "下月毛利率与该战区的偏离是否收敛到 ±0.5pp 以内",
            })

    # ---------------------------------------------------------------- 证据与边界
    evidence = [
        {"metric": "同期 TE 实际", "value": _fmt_te(totals["te"]), "caliber": "ERP Sell-in，Σ shipment_te",
         "source": "fact_erp_shipment"},
        {"metric": "同期 TE 预算", "value": _fmt_te(totals["budget_te"]), "caliber": "模拟预算（MOCK_BUDGET_TE），非真实业务预算",
         "source": f"budget_te_v1.db · {meta['budget_batch']['batch_id']}"},
        {"metric": "TE 达成率", "value": _fmt_pct(attainment), "caliber": "实际 ÷ 预算（同期口径）",
         "source": "cockpit_queries.build"},
        {"metric": "ERP 出货金额", "value": _fmt_yuan(totals["amount"]), "caliber": "Σ sales_amount，未做含税/未税换算",
         "source": "fact_erp_shipment"},
        {"metric": "毛利额 / 毛利率", "value": f"{_fmt_yuan(totals['gross_profit'])} / {_fmt_pct(gm)}",
         "caliber": "Σ gross_profit ÷ Σ sales_amount", "source": "fact_erp_shipment"},
        {"metric": "全年预测", "value": _fmt_te(totals["te_forecast_year"]),
         "caliber": "1–10 月实际 + 11–12 月季节性外推", "source": "FORECAST_MODEL"},
    ]

    caveats = [
        {"level": "high", "text": "TE 预算是模拟数据（MOCK_BUDGET_TE），只能用于演示分析链路，"
                                  "不能作为考核或对外披露依据。"},
        {"level": "high", "text": "11–12 月为模型预测，不是实际；预测模型只看 2025 同月基数、"
                                  "同比规模与近三月动能，不含促销计划与新品因素。"},
        {"level": "mid", "text": "本页只含 ERP Sell-in，不含 POS Sell-out；"
                                 "因此不能据此判断终端动销与渠道库存。"},
        {"level": "mid", "text": "KA = ERP customer_type NKA 的 7 家客户（KA客户A/KA客户B/KA客户C/KA客户D/KA客户E/"
                                 "KA客户F/KA客户G）；批发与区域商超单列，不并入 KA 口径。"},
        {"level": "low", "text": f"数据截止 2026-10-31；筛选条件：{scope}。"},
    ]

    insufficient = None
    if meta["entity_count"] <= 1:
        insufficient = ("当前筛选只剩 1 个客户组织组合，无法做战区/客户结构对比，"
                        "建议回到「全部战区」再看结论。")
    elif not findings:
        insufficient = "当前筛选下所有指标都在阈值内，没有需要单独解释的异常；这不等于没有问题，只说明本页指标未触发规则。"

    return {
        "ok": True,
        "meta": {
            "engine": ENGINE,
            "filters": {"period": meta["period_label"], "month": meta["month"],
                        "region": meta["region"], "ka": meta["ka"]},
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "data_source": "ERP Sell-in + MOCK_BUDGET_TE",
            "disclaimer": "事实来自页面同口径实测；推测是规则推断，必须按「证伪方式」验证后才可作为决策依据。",
        },
        "data": {
            "insufficient": insufficient,
            "summary": {
                "headline": f"同期 TE 达成 {_fmt_pct(attainment)}，"
                            f"{'高于' if (attainment or 0) >= 100 else '低于'}预算 "
                            f"{_fmt_te(abs(totals['te'] - totals['budget_te']))}",
                "facts": facts,
            },
            "findings": findings,
            "causes": causes,
            "actions": actions,
            "evidence": evidence,
            "caveats": caveats,
        },
    }


# --------------------------------------------------------------------------- 归因辅助
def _region_gap_by_ka(conn: sqlite3.Connection, qs: dict, region: str) -> list[dict]:
    """把某战区缺口拆到客户，用于归因（只算有缺口的客户）。"""
    q = dict(qs)
    q["region"] = region
    core = cq.build(conn, q)
    rows = []
    seen = set()
    # 三个客户组都看：批发的缺口也要能被点名，不能因为不在 KA 榜里就漏掉
    for g in core["data"]["channel"]["groups"]:
        for k in g["items"]:
            if k["customer_id"] in seen:
                continue
            seen.add(k["customer_id"])
            gap = (k["budget"] or 0) - (k["te"] or 0)
            if gap > 0:
                rows.append({"name": k["name"], "customer_id": k["customer_id"], "gap": gap,
                             "te": k["te"], "budget": k["budget"],
                             "price_per_te": (k["amount"] / k["te"]) if k["te"] else None})
    rows.sort(key=lambda r: -r["gap"])
    return rows


def _price_per_te(conn: sqlite3.Connection, qs: dict) -> float | None:
    core = cq.build(conn, qs)
    t = core["data"]["totals"]
    return (t["amount"] / t["te"]) if t["te"] else None
