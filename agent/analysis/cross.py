"""P9 · 跨业务模块联合分析。

四个分析器共用一条纪律：
**只做「并列、对照、交集」，绝不把不同口径的数字相加或相除。**

| 分析器 | 允许的比较 | 禁止的操作 |
| --- | --- | --- |
| `ka_vs_wholesale` | ERP 内部的 KA 段 vs 批发段（同为 Sell-in，可对比、可相加） | 把「批发出库（下游）」加到 ERP 出货上 |
| `erp_vs_pos` | 方向一致性、覆盖范围 | 用 ERP 除 POS 算「消化率」、把两者金额相加 |
| `sfa_vs_pos` | 门店交集、拜访与动销的**同店**对照 | 用拜访次数推算销量 |
| `profit_mix` | ERP 内部的金额 / 成本 / 毛利 | 用 TE 代替金额算毛利率 |

所有输出都带 `comparable` 与 `limits`，报告层会把它们原样写进「数据限制」。
"""
from __future__ import annotations

from typing import Any

SEG_LABEL = {"clubz": "ClubZ", "nka": "NKA", "lka": "LKA", "wholesale": "批发客户",
             "others": "其他"}
KA_SEGMENTS = ("clubz", "nka", "lka")


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _pct(a, b) -> float | None:
    if not b:
        return None
    return round((a / b - 1) * 100, 2)


def _ok(res: dict | None) -> bool:
    return bool(res and res.get("ok"))


# ---------------------------------------------------------------- ① KA vs 批发
def ka_vs_wholesale(tools: dict) -> dict:
    """KA 与批发分开看：ERP 内部两段可对比，批发出库是第三段、独立核算。"""
    erp, wh = tools.get("erp"), tools.get("wholesale")
    rows: list[dict] = []
    limits: list[str] = []

    if _ok(erp):
        segs = {r["segment"]: r for r in (erp.get("rows") or {}).get("by_segment") or []}
        ka_te = sum(_f(segs.get(s, {}).get("te")) for s in KA_SEGMENTS)
        ka_gp = sum(_f(segs.get(s, {}).get("gross_profit")) for s in KA_SEGMENTS)
        ka_amt = sum(_f(segs.get(s, {}).get("amount")) for s in KA_SEGMENTS)
        wh_row = segs.get("wholesale") or {}
        total = _f((erp.get("metrics") or {}).get("te"))
        rows += [
            {"part": "KA（ClubZ + NKA + LKA）", "te": round(ka_te, 2),
             "share_pct": round(ka_te / total * 100, 1) if total else None,
             "gross_margin_pct": round(ka_gp / ka_amt * 100, 2) if ka_amt else None,
             "caliber": "ERP Sell-in"},
            {"part": "批发客户（ERP 出货）", "te": round(_f(wh_row.get("te")), 2),
             "share_pct": round(_f(wh_row.get("te")) / total * 100, 1) if total else None,
             "gross_margin_pct": wh_row.get("gross_margin_pct"),
             "caliber": "ERP Sell-in"},
        ]
        if not any(s in segs for s in KA_SEGMENTS):
            limits.append("本次范围内没有 KA 段记录（可能只筛了批发客户）。")
    else:
        limits.append("ERP 出货数据不可用，无法给出 KA 段与批发段的对比。")

    if _ok(wh):
        m = wh.get("metrics") or {}
        rows.append({"part": "批发出库（下游档口/二批）", "te": m.get("sales_te"),
                     "share_pct": None, "gross_margin_pct": m.get("gross_margin_pct"),
                     "caliber": "批发进销存（独立一段，勿与上两行相加）"})
        limits.append("批发出库是「批发客户 → 下游」的第二段流转，与 ERP 出货给批发客户不是同一笔，"
                      "两者相加会重复计算。")
    else:
        limits.append("批发进销存数据未取到，本次只比较了 ERP 内部两段。")

    total_erp = _f(((tools.get("erp") or {}).get("metrics") or {}).get("te"))
    return {
        "scenario": "ka_vs_wholesale",
        "title": "KA 与批发分开看",
        "rows": rows,
        "comparable": False,
        "note": "前两行同为 ERP Sell-in，可以对比、份额可相加为 100%；"
                "第三行是另一段业务，只作方向参考。" if total_erp else "ERP 总量缺失，份额不可用。",
        "limits": limits,
    }


# ---------------------------------------------------------------- ② ERP vs POS
def erp_vs_pos(tools: dict) -> dict:
    """出货与终端的方向是否一致；不做任何比率换算。"""
    erp, pos = tools.get("erp"), tools.get("pos")
    rows: list[dict] = []
    limits: list[str] = []
    verdict = "无法判断"

    erp_yoy = None
    if _ok(erp):
        erp_yoy = (erp.get("metrics") or {}).get("yoy_pct")
        rows.append({"side": "ERP 出货（Sell-in）",
                     "te": (erp.get("metrics") or {}).get("te"),
                     "change_pct": erp_yoy,
                     "basis": "同比（去年同期）"})
    else:
        limits.append("ERP 出货不可用。")

    pos_mom = None
    if _ok(pos):
        pm = pos.get("metrics") or {}
        # 工具在单月查询时已经算好「同筛选条件下的上月对比」，优先用它：
        # 口径与本期完全一致（同战区/同 KA），不会出现「本月KA客户A vs 上月全部」的漂移。
        pos_mom = pm.get("mom_pct")
        base_month = pm.get("mom_base_month")
        if pos_mom is not None and base_month:
            basis = f"环比（{base_month} → 本期）"
        else:
            by_month = (pos.get("rows") or {}).get("by_month") or []
            if len(by_month) >= 2:
                pos_mom = _pct(_f(by_month[-1].get("sales_te")), _f(by_month[-2].get("sales_te")))
                basis = f"环比（{by_month[-2]['month']} → {by_month[-1]['month']} 月）"
            else:
                basis = "本期无可比基期"
        rows.append({"side": "POS 终端（Sell-out）",
                     "te": pm.get("sales_te"),
                     "change_pct": pos_mom,
                     "basis": basis})
        limits.append(f"POS 只覆盖 {pm.get('stores')} 家与 SFA 对齐的门店，"
                      "不代表全国终端；ERP 与 POS 环节不同，本工具不做相加或相除。")
    else:
        limits.append("POS 终端数据未取到，无法判断终端是否同步。")

    if erp_yoy is not None and pos_mom is not None:
        if erp_yoy > 0 and pos_mom > 0:
            verdict = "同向增长"
        elif erp_yoy < 0 and pos_mom < 0:
            verdict = "同向下降"
        elif erp_yoy > 0 > pos_mom:
            verdict = "背离：出货增、终端降"
        else:
            verdict = "背离：出货降、终端增"
    limits.append("「方向一致」不等于因果；出货与终端之间还隔着库存、铺货与促销，"
                  "需要门店级库存才能进一步验证。")

    return {"scenario": "erp_vs_pos", "title": "出货与终端是否同步",
            "rows": rows, "verdict": verdict, "comparable": False, "limits": limits}


# ---------------------------------------------------------------- ③ SFA vs POS
def sfa_vs_pos(tools: dict) -> dict:
    """门店级交集：有拜访但动销差的门店，以及两边对不上的门店数量。"""
    sfa, pos = tools.get("sfa"), tools.get("pos")
    limits: list[str] = []
    if not _ok(sfa) or not _ok(pos):
        return {"scenario": "sfa_vs_pos", "title": "拜访与动销对照",
                "rows": [], "matched": 0, "comparable": False,
                "limits": ["SFA 或 POS 数据缺失，无法做门店级对照。"]}

    sfa_map = {r["store_id"]: r for r in (sfa.get("rows") or {}).get("by_store") or []
               if r.get("store_id")}
    pos_map = {r["store_id"]: r for r in (pos.get("rows") or {}).get("by_store") or []
               if r.get("store_id")}
    matched = sorted(set(sfa_map) & set(pos_map))

    te_list = [_f(pos_map[s].get("sales_te")) for s in matched]
    median = sorted(te_list)[len(te_list) // 2] if te_list else 0

    rows = []
    for sid in matched:
        s, p = sfa_map[sid], pos_map[sid]
        te = _f(p.get("sales_te"))
        mom = p.get("mom_pct")
        low = te < median or (mom is not None and mom < 0)
        rows.append({
            "store_id": sid, "store": p.get("store") or s.get("store"),
            "ka_system": p.get("ka_system"),
            "visits": s.get("visits"), "completed_rate_pct": s.get("completed_rate_pct"),
            "sales_te": te, "mom_pct": mom,
            "flag": "有拜访但动销偏弱" if low else "拜访与动销都正常",
        })
    rows.sort(key=lambda r: (r["flag"] != "有拜访但动销偏弱", r["sales_te"] or 0))

    only_sfa = sorted(set(sfa_map) - set(pos_map))
    only_pos = sorted(set(pos_map) - set(sfa_map))
    limits += [
        f"SFA 有拜访记录 {len(sfa_map)} 家、POS 有销售记录 {len(pos_map)} 家，"
        f"按门店编码可对上 {len(matched)} 家。",
        f"{len(only_sfa)} 家门店有 SFA 拜访但不在 POS 对齐范围（无法判断动销）；"
        f"{len(only_pos)} 家门店有 POS 销售但本期无 SFA 拜访记录（不代表没拜访，可能不在 SFA 覆盖内）。",
        "「动销偏弱」= 本期 TE 低于同组中位数，或环比为负；这是同店横向/纵向比较，不是目标达成判断。",
    ]
    return {"scenario": "sfa_vs_pos", "title": "哪些门店拜访正常但动销偏弱",
            "rows": rows, "matched": len(matched),
            "counts": {"sfa_only": len(only_sfa), "pos_only": len(only_pos),
                       "matched": len(matched)},
            "comparable": False, "limits": limits}


# ---------------------------------------------------------------- ④ 利润结构
def profit_mix(tools: dict, focus: list[str] | None = None) -> dict:
    """按战区/品类/客户看「量大但毛利率低」。

    ``focus`` 决定拆到哪一层：问「哪个品类最赚钱」就按品类拆，
    问「哪个客户」就按客户拆。拿客户维度去回答品类问题，是另一种形式的编造
    （数字都对，但答的不是这个问题）。
    """
    erp = tools.get("erp")
    if not _ok(erp):
        return {"scenario": "profit_mix", "title": "利润结构", "rows": [],
                "comparable": True, "limits": ["ERP 出货与成本数据不可用。"]}

    metrics = erp.get("metrics") or {}
    overall_gm = metrics.get("gross_margin_pct")
    total_te = _f(metrics.get("te"))
    rows: list[dict] = []
    limits = [
        "毛利率 = Σ毛利 ÷ Σ出货金额（金额口径），不是 TE 口径，也不是单笔平均。",
        "成本来自演示补充库（fact_shipment_ext），与 ETL 出货同一批单据；"
        "若剔除补充库，毛利会缺失而不是变小。",
    ]

    focus = list(focus or [])
    dims: list[tuple[str, str, str]] = []
    if "category" in focus:
        dims.append(("by_category", "品类", "category"))
    if "department" in focus:
        dims.append(("by_department", "战区市场部", "department"))
    if not dims:      # 没指定维度：按市场部（战区）+ 客户给两层，覆盖面最广
        dims = [("by_department", "战区市场部", "department"),
                ("by_customer", "客户", "customer")]

    for key, label, field in dims:
        for r in (erp.get("rows") or {}).get(key) or []:
            name = r.get(field) or r.get("department") or r.get("customer") or "—"
            te = _f(r.get("te"))
            gm = r.get("gross_margin_pct")
            if gm is None or not total_te:
                continue
            rows.append({
                "dimension": label, "name": name, "te": r.get("te"),
                "amount": r.get("amount"), "gross_profit": r.get("gross_profit"),
                "gross_margin_pct": gm,
                "te_share_pct": round(te / total_te * 100, 1),
                "gap_vs_overall_pp": round(gm - (overall_gm or 0), 2),
                "flag": ("量大但毛利率低于整体" if te / total_te >= 0.08 and gm < (overall_gm or 0)
                         else ""),
            })

    rows.sort(key=lambda r: (r["flag"] == "", r["gross_margin_pct"]))
    return {"scenario": "profit_mix", "title": "量大但利润偏薄", "rows": rows[:12],
            "dimensions": [label for _, label, _ in dims],
            "overall_gross_margin_pct": overall_gm, "comparable": True, "limits": limits}


BUILDERS = {
    "ka_vs_wholesale": ka_vs_wholesale,
    "erp_vs_pos": erp_vs_pos,
    "sfa_vs_pos": sfa_vs_pos,
    "profit_mix": profit_mix,
}


def build(scenario: str, tools: dict, focus: list[str] | None = None) -> dict | None:
    fn = BUILDERS.get(scenario or "")
    if not fn:
        return None
    try:
        # 只有利润结构需要 focus（决定拆到品类还是客户层），其余分析器不接受该参数
        if scenario == "profit_mix":
            return fn(tools or {}, focus=focus)
        return fn(tools or {})
    except Exception as exc:                     # pragma: no cover
        return {"scenario": scenario, "title": "联合分析失败",
                "rows": [], "comparable": False,
                "limits": [f"分析器异常：{type(exc).__name__}: {exc}"]}


def summarize(cross: dict | None) -> str:
    """给 markdown / 报告用的一句话结论（只陈述对比结果，不做因果断言）。"""
    if not cross:
        return ""
    sc = cross.get("scenario")
    if sc == "erp_vs_pos":
        return f"出货与终端：{cross.get('verdict')}。"
    if sc == "sfa_vs_pos":
        weak = [r for r in cross.get("rows") or [] if r.get("flag") == "有拜访但动销偏弱"]
        return (f"门店可对上 {cross.get('matched')} 家，其中 {len(weak)} 家有拜访但动销偏弱。")
    if sc == "ka_vs_wholesale":
        return "KA 段与批发段已分开列出；批发出库为独立一段，未与出货相加。"
    if sc == "profit_mix":
        flagged = [r for r in cross.get("rows") or [] if r.get("flag")]
        return f"毛利偏低且占比不低的项 {len(flagged)} 个。"
    return ""
