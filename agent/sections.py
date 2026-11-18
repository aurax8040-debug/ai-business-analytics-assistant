"""P10 · 结构化经营分析报告（七段式）。

为什么要有这一层
----------------
V1 的报告是「五段散文 + 两张表」，读起来顺，但有三类毛病在面试里会被追问：

1. **事实、推测、建议混在一起**：「出货下降可能是铺货不足」和「实际 38.9 万 TE」
   排在同一层，读者分不清哪句能直接引用、哪句只是假设；
2. **相关性被说成因果**：「拜访多、动销差」被写成「因为拜访没做好」；
3. **限制藏在最后**：数据只覆盖 27 家门店这件事，写在第五节最后一行，
   而读者在第一节就已经把结论记下来了。

所以这里把报告固定成七段，并对**每一条**打标签：

| 段 | 内容 | 允许的标签 |
| --- | --- | --- |
| 一 经营结论 | 一句话结论 + 交叉判断 | 事实（含数字必有出处） |
| 二 核心指标 | 预算/实际/达成/缺口/金额/毛利/同比 | 事实 |
| 三 异常发现 | 未达成客户、集中度、门店与品类异常 | 事实（描述性，不做归因） |
| 四 原因分析 | 已验证 / 可能原因 / 暂无数据支持 | 事实 / 推测 / 缺数据 |
| 五 数据证据 | 工具、表、口径自校验、期间 | 事实 |
| 六 业务建议 | 动作 + 责任人 + 验证方式 | 建议 |
| 七 数据限制 | 能力边界、覆盖范围、演示数据声明 | 限制 |

`kind` 只有三个值：``fact`` / ``inference`` / ``advice``（限制类统一记 ``limit``）。
前端按 `kind` 上色，Markdown 按 `kind` 加前缀，两种呈现不会说法不一。
"""
from __future__ import annotations

from .report import fmt_pct, fmt_te, fmt_yuan

KIND_LABEL = {"fact": "事实", "inference": "推测", "advice": "建议", "limit": "限制"}

TITLES = (
    ("conclusion", "经营结论"),
    ("metrics", "核心指标"),
    ("anomalies", "异常发现"),
    ("causes", "原因分析"),
    ("evidence", "数据证据"),
    ("actions", "业务建议"),
    ("limits", "数据限制"),
)


def _item(text: str, kind: str = "fact", source: str = "") -> dict:
    return {"kind": kind, "label": KIND_LABEL[kind], "text": text, "source": source}


def _section(key: str, items: list[dict], *, table: dict | None = None,
             note: str = "") -> dict:
    no = [k for k, _ in TITLES].index(key) + 1
    title = dict(TITLES)[key]
    return {"no": no, "key": key, "title": title, "items": items,
            "table": table or {}, "note": note}


# ------------------------------------------------------------------ 各段
def _conclusion(ar: dict, headline: str, cross: dict | None) -> dict:
    items = [_item(headline, "fact", "见「二、核心指标」与「五、数据证据」")]
    if cross:
        from .analysis.cross import summarize
        text = summarize(cross)
        if text:
            items.append(_item(text, "fact", f"跨模块对照：{cross.get('title')}"))
    r = ar.get("result") or {}
    if r.get("yoy_pct") is not None and r.get("te") is not None:
        direction = "增长" if r["yoy_pct"] >= 0 else "下降"
        items.append(_item(
            f"同比{direction} {abs(r['yoy_pct']):.2f}%（去年同期 {fmt_te(r.get('last_te'))} TE），"
            f"这是与去年同期的对比，不是与竞品的对比。", "fact",
            "ERP 出货 Sell-in；竞品数据本平台没有"))
    return _section("conclusion", items)


def _metrics(ar: dict) -> dict:
    r = ar.get("result") or {}
    rows = [
        ["预算 TE", fmt_te(r.get("budget_te")), "同期预算（MOCK_BUDGET_TE，演示数据）"],
        ["实际 TE", fmt_te(r.get("te")), "ERP 出货 Sell-in"],
        ["TE 达成率", fmt_pct(r.get("attainment_pct")), "实际 ÷ 预算"],
        ["TE 差额", fmt_te(r.get("gap_te")), "实际 − 预算"],
        ["出货金额", fmt_yuan(r.get("amount")), "含税额"],
        ["毛利额", fmt_yuan(r.get("gross_profit")), "出货金额 − 成本"],
        ["毛利率", fmt_pct(r.get("gross_margin_pct")), "Σ毛利 ÷ Σ出货金额（金额口径）"],
        ["同比", fmt_pct(r.get("yoy_pct")), f"去年同期 {fmt_te(r.get('last_te'))} TE"],
    ]
    return _section("metrics", [_item("以下数字全部来自 SQL 计算，未经模型改写。", "fact")],
                    table={"columns": ["指标", "数值", "口径"], "rows": rows})


def _anomalies(ar: dict, cross: dict | None) -> dict:
    p = ar.get("problems") or {}
    items: list[dict] = []
    if p.get("unmet_count"):
        items.append(_item(
            f"{p['customers_total']} 家客户中 {p['unmet_count']} 家未达成，"
            f"缺口合计 {fmt_te(abs(p.get('unmet_gap_te') or 0))} TE"
            f"（相当于同期预算的 {fmt_pct(abs(p.get('unmet_gap_vs_budget_pct') or 0))}）。", "fact",
            "预算 vs ERP 实际，逐客户对齐"))
    if p.get("concentration_top3_pct") is not None:
        items.append(_item(
            f"未达成缺口集中度：前 3 家占 {fmt_pct(p['concentration_top3_pct'])}，"
            + ("问题集中在少数客户。" if p["concentration_top3_pct"] >= 50
               else "缺口比较分散，不是单点问题。"),
            "fact", "客户缺口绝对值排序与 Top3 占比"))
    ws = p.get("worst_segment") or {}
    if ws.get("segment"):
        items.append(_item(
            f"最差客户类型：{ws.get('label') or ws['segment']}，缺口 {fmt_te(abs(ws.get('gap_te') or 0))} TE。",
            "fact", "按客户类型汇总"))
    pos = ar.get("pos") or {}
    if pos:
        items.append(_item(
            f"终端：POS {pos.get('stores')} 家对齐门店合计 {fmt_te(pos.get('sales_te'))} TE，"
            f"覆盖有限，只作辅助证据（不与 ERP 相加）。", "fact", "fact_pos_sales"))
    sfa = ar.get("sfa") or {}
    if sfa:
        items.append(_item(
            f"执行：SFA 拜访 {sfa.get('visits')} 次，完成率 "
            f"{fmt_pct(sfa.get('completed_rate_pct'))}，陈列记录率 "
            f"{fmt_pct(sfa.get('display_recorded_rate_pct'))}。", "fact", "fact_sfa_visit"))

    table: dict = {}
    if cross and cross.get("rows"):
        rows = _cross_table(cross)
        table = rows
        items.append(_item(f"跨模块对照：{cross.get('title')}，结论只用并列与交集，"
                           f"不做相加或相除。", "fact", "见下表"))
    return _section("anomalies", items, table=table,
                    note="本段只陈述「发生了什么」，归因放在下一段，避免把相关性写成因果。")


def _cross_table(cross: dict) -> dict:
    sc = cross.get("scenario")
    rows = cross.get("rows") or []
    if sc == "ka_vs_wholesale":
        return {"columns": ["分段", "TE", "份额", "毛利率", "口径"],
                "rows": [[r.get("part"), fmt_te(r.get("te")), fmt_pct(r.get("share_pct")),
                          fmt_pct(r.get("gross_margin_pct")), r.get("caliber")]
                         for r in rows]}
    if sc == "erp_vs_pos":
        return {"columns": ["环节", "TE", "变化", "基期", "判断"],
                "rows": [[r.get("side"), fmt_te(r.get("te")), fmt_pct(r.get("change_pct")),
                          r.get("basis"), cross.get("verdict")] for r in rows]}
    if sc == "sfa_vs_pos":
        counts = cross.get("counts") or {}
        return {"columns": ["门店", "KA", "拜访次数", "完成率", "POS TE", "环比", "判断"],
                "rows": [[r.get("store"), r.get("ka_system"), r.get("visits"),
                          fmt_pct(r.get("completed_rate_pct")), fmt_te(r.get("sales_te")),
                          fmt_pct(r.get("mom_pct")), r.get("flag")] for r in rows],
                "footer": f"可对上 {counts.get('matched', 0)} 家；"
                          f"仅 SFA {counts.get('sfa_only', 0)} 家；仅 POS {counts.get('pos_only', 0)} 家"}
    if sc == "profit_mix":
        return {"columns": ["维度", "名称", "TE", "占比", "毛利率", "与整体差", "判断"],
                "rows": [[r.get("dimension"), r.get("name"), fmt_te(r.get("te")),
                          fmt_pct(r.get("te_share_pct")),
                          fmt_pct(r.get("gross_margin_pct")),
                          f"{r.get('gap_vs_overall_pp'):+.2f}pp" if r.get("gap_vs_overall_pp") is not None else "—",
                          r.get("flag") or "—"] for r in rows]}
    return {}


def _causes(causes: dict) -> dict:
    items: list[dict] = []
    for v in causes.get("verified") or []:
        text = v["text"] if isinstance(v, dict) else str(v)
        src = v.get("source", "") if isinstance(v, dict) else ""
        items.append(_item(text, "fact", src))
    for v in causes.get("possible") or []:
        text = v.get("text") if isinstance(v, dict) else str(v)
        why = v.get("why", "") if isinstance(v, dict) else ""
        verify = v.get("verify", "") if isinstance(v, dict) else ""
        extra = "；".join(x for x in (why and f"依据：{why}", verify and f"验证方式：{verify}") if x)
        items.append(_item(text + (f"（{extra}）" if extra else ""), "inference",
                           "尚未验证，需要业务确认"))
    for v in causes.get("unsupported") or []:
        items.append(_item(str(v), "limit", "无对应数据源，本次不下结论"))
    if not items:
        items.append(_item("本次没有足够证据做原因分析。", "limit"))
    return _section("causes", items,
                    note="「事实」可直接引用；「推测」必须经业务验证后才能当作原因；"
                         "两者都成立也不等于因果。")


def _evidence(ar: dict, tool_calls: list, period_label: str, scope_label: str) -> dict:
    items = [_item(f"范围：{scope_label}；期间：{period_label}", "fact")]
    rows = []
    for t in tool_calls:
        rows.append([t.get("label") or t.get("tool"), "成功" if t.get("ok") else "失败",
                     t.get("source") or "—", f"{t.get('elapsed_ms')}ms"])
    checks = ar.get("cross_checks") or []
    if checks:
        ok = all(c.get("ok") for c in checks)
        names = "、".join(c.get("name", "") for c in checks)
        items.append(_item(f"口径自校验 {len(checks)} 项：{'全部通过' if ok else '存在差异，已在下方标注'}"
                           f"（{names}）。", "fact"))
    for g in ar.get("data_gaps") or []:
        items.append(_item(f"数据缺口：{g}", "limit"))
    return _section("evidence", items,
                    table={"columns": ["工具", "状态", "数据源", "耗时"], "rows": rows})


def _actions(actions: list) -> dict:
    items = []
    for a in actions or []:
        items.append(_item(
            f"{a.get('action')}｜责任：{a.get('owner', '—')}｜验证：{a.get('check', '—')}", "advice"))
    if not items:
        items.append(_item("证据不足，本次不建议采取动作。", "advice"))
    return _section("actions", items,
                    note="建议是「基于当前证据的可执行动作」，不是已确定的对策；"
                         "执行后请按验证方式回收效果，再判断建议是否成立。")


def _limits(ar: dict, caveats: list, cross: dict | None) -> dict:
    items: list[dict] = []
    for d in ar.get("data_limits") or []:
        items.append(_item(f"{d['label']}：有{d['have']}；没有{d['missing']}。{d['offer']}", "limit"))
    if cross:
        for lim in cross.get("limits") or []:
            items.append(_item(lim, "limit", f"跨模块对照：{cross.get('title')}"))
    for c in caveats or []:
        items.append(_item(c, "limit"))
    if not items:
        items.append(_item("本次未触发已知能力边界；仍受演示数据范围限制。", "limit"))
    return _section("limits", items)


# ------------------------------------------------------------------ 组装
def build(ar: dict, *, headline: str, period_label: str, scope_label: str,
          causes: dict, actions: list, caveats: list, tool_calls: list) -> list[dict]:
    cross = ar.get("cross")
    return [
        _conclusion(ar, headline, cross),
        _metrics(ar),
        _anomalies(ar, cross),
        _causes(causes),
        _evidence(ar, tool_calls, period_label, scope_label),
        _actions(actions),
        _limits(ar, caveats, cross),
    ]


def counts(sections: list[dict]) -> dict:
    """各类标签条数，前端用来显示「3 条事实 / 2 条推测」，也用于测试断言。"""
    out = {"fact": 0, "inference": 0, "advice": 0, "limit": 0}
    for s in sections:
        for it in s.get("items") or []:
            if it.get("kind") in out:
                out[it["kind"]] += 1
    return out
