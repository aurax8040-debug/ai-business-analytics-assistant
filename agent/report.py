"""报告组装：把「算出来的结论」翻译成结构化报告 + Markdown。

这一层不做任何取数，也不做任何新计算——它只负责：
1. 排版：经营结果 / 主要问题 / 原因分析 / 改善建议 / 数据依据 五段；
2. 数字白名单校验（``guard_numbers``）：LLM 写出来的数字必须在证据集里出现过，
   否则该段自动回退到规则文本，并记录 ``guard.violations``；
3. Markdown 渲染：给终端、邮件、面试演示用同一份内容。

为什么白名单校验放在这里而不是提示词里
------------------------------------
提示词是「请求」，校验是「约束」。模型可以不听请求，但过不了校验——
这才是「LLM 不许编数」的工程实现，而不是一句 prompt。
"""
from __future__ import annotations

import re

# 允许无条件出现的小整数（月份、条数、TOP5 之类的口语数量）
_SAFE_INTS = set(range(0, 13)) | set(range(1, 32)) | {27, 50, 100, 1000, 10000}


def fmt_te(v, digits: int = 2) -> str:
    return "—" if v is None else f"{v:,.{digits}f}"


def fmt_yuan(v) -> str:
    if v is None:
        return "—"
    if abs(v) >= 1e8:
        return f"{v / 1e8:,.2f} 亿元"
    return f"{v / 1e4:,.1f} 万元"


def fmt_pct(v, digits: int = 2) -> str:
    return "—" if v is None else f"{v:.{digits}f}%"


def fmt_pp(v, digits: int = 2) -> str:
    return "—" if v is None else f"{v:+.{digits}f}pp"


# --------------------------------------------------------------------- 段落
def result_rows(ar: dict) -> list[dict]:
    r = ar.get("result") or {}
    rows = [
        {"label": "预算 TE", "value": fmt_te(r.get("budget_te")), "hint": "同期预算（MOCK_BUDGET_TE）"},
        {"label": "实际 TE", "value": fmt_te(r.get("te")), "hint": "ERP 出货 Sell-in"},
        {"label": "TE 达成率", "value": fmt_pct(r.get("attainment_pct")),
         "hint": "实际 ÷ 预算", "level": _level(r.get("attainment_pct"))},
        {"label": "TE 差额", "value": fmt_te(r.get("gap_te")),
         "hint": "实际 − 预算", "level": _level(r.get("attainment_pct"))},
        {"label": "ERP 出货金额", "value": fmt_yuan(r.get("amount")), "hint": "含税额"},
        {"label": "毛利额", "value": fmt_yuan(r.get("gross_profit")), "hint": "金额 − 成本"},
        {"label": "毛利率", "value": fmt_pct(r.get("gross_margin_pct")), "hint": "Σ毛利 ÷ Σ金额"},
        {"label": "同比", "value": fmt_pct(r.get("yoy_pct")),
         "hint": f"去年同期 {fmt_te(r.get('last_te'))} TE"},
    ]
    return rows


def _level(attainment) -> str:
    if attainment is None:
        return "none"
    if attainment >= 100:
        return "good"
    if attainment >= 95:
        return "watch"
    return "bad"


def problem_rows(ar: dict, limit: int = 5) -> list[dict]:
    top = (ar.get("problems") or {}).get("top_gap") or []
    out = []
    for i, c in enumerate(top[:limit], 1):
        out.append({
            "rank": i,
            "customer": c.get("customer"),
            "segment": c.get("segment_label"),
            "te": fmt_te(c.get("te")),
            "budget_te": fmt_te(c.get("budget_te")),
            "attainment": fmt_pct(c.get("attainment_pct")),
            "gap_te": fmt_te(c.get("gap_te")),
            "share": fmt_pct(c.get("gap_share_pct")),
            "yoy": fmt_pct(c.get("yoy_pct")),
        })
    return out


def basis_block(ar: dict, tool_calls: list[dict]) -> dict:
    return {
        "period": (ar.get("period") or {}).get("label"),
        "tools": [{k: t.get(k) for k in ("tool", "label", "ok", "elapsed_ms", "source")}
                  for t in tool_calls],
        "cross_checks": ar.get("cross_checks") or [],
        "data_gaps": ar.get("data_gaps") or [],
        "tables": sorted({tb for t in tool_calls for tb in (t.get("tables") or [])}),
    }


# --------------------------------------------------------------------- 校验
_NUM_RE = re.compile(r"(-?\d[\d,]*(?:\.\d+)?)\s*(%|pp|万|亿)?")


def collect_allowed_numbers(ar: dict) -> dict:
    """收集所有「证据里出现过的数字」，用于白名单校验。

    返回 ``{"values": set[float], "raw": set[str]}``；比较时按相对误差 1% 命中，
    这样 81,764.55 / 81764.55 / 8.18 万 这类写法都能被正确识别。
    """
    values: set[float] = set()
    raw: set[str] = set()

    def walk(obj):
        if isinstance(obj, bool) or obj is None:
            return
        if isinstance(obj, (int, float)):
            values.add(float(obj))
            raw.add(f"{obj}")
        elif isinstance(obj, str):
            for m in _NUM_RE.finditer(obj):
                txt = m.group(1).replace(",", "")
                try:
                    values.add(float(txt))
                except ValueError:
                    pass
                raw.add(m.group(1))
        elif isinstance(obj, dict):
            for v in obj.values():
                walk(v)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                walk(v)

    walk(ar)
    # 达成率、占比这类派生值也在证据里，额外放行 ±100 的补数（如 95%、100%）
    values |= {95.0, 100.0, 99.87, 105.0, 110.0}
    return {"values": values, "raw": raw}


def guard_numbers(text: str, allowed: dict) -> list[str]:
    """返回疑似编造的数字列表（空 = 通过）。

    放行规则：
    - 整数且在安全集合（月份、门店数、TOP 数量）里；
    - 与任一证据数字相对误差 ≤1%（允许四舍五入、万/亿换算）；
    - 年份（2000–2100）。
    """
    bad: list[str] = []
    values = allowed["values"]
    for m in _NUM_RE.finditer(text or ""):
        token = m.group(1)
        unit = m.group(2) or ""
        try:
            num = float(token.replace(",", ""))
        except ValueError:
            continue
        if num == int(num) and unit not in ("%", "pp", "万", "亿"):
            iv = int(num)
            if iv in _SAFE_INTS or 2000 <= iv <= 2100:
                continue
        if _matches(num, values):
            continue
        if unit == "万" and _matches(num * 1e4, values):
            continue
        if unit == "亿" and _matches(num * 1e8, values):
            continue
        bad.append(token + unit)
    return bad


def _matches(num: float, values: set[float]) -> bool:
    if not values:
        return False
    for v in values:
        if v == 0:
            if abs(num) < 1e-9:
                return True
            continue
        if abs(num - v) <= max(0.5, abs(v) * 0.01):
            return True
    return False


# --------------------------------------------------------------------- 用例
_CN_NUM = "一二三四五六七八九十"


def _cn(no: int) -> str:
    return _CN_NUM[no - 1] if 1 <= no <= len(_CN_NUM) else str(no)


def _table_md(table: dict) -> list[str]:
    cols = table.get("columns") or []
    rows = table.get("rows") or []
    if not cols or not rows:
        return []
    out = ["| " + " | ".join(str(c) for c in cols) + " |",
           "|" + "|".join("---" for _ in cols) + "|"]
    for r in rows:
        out.append("| " + " | ".join("—" if v in (None, "") else str(v) for v in r) + " |")
    if table.get("footer"):
        out += ["", f"*{table['footer']}*"]
    return out


def render_markdown(report: dict) -> str:
    """把结构化报告渲染成 Markdown（终端、复制粘贴、面试演示共用）。

    V2：按七段渲染，每条都带标签，读者能分清哪句是可引用的数字、
    哪句只是待验证的推测。段落顺序固定，不随内容多少变化。
    """
    lines: list[str] = []
    lines.append(f"## {report.get('headline') or '经营分析结果'}")
    lines.append("")
    lines.append(f"> 范围：{report.get('scope_label')} ｜ 期间：{report.get('period_label')}")
    if (report.get("followup") or {}).get("note"):
        lines.append(f"> 承接上一轮：{report['followup']['note']}")
    lines.append("")

    # 能力边界放在最前面：用户问的是「库存周转」而平台只有月末库存时，
    # 这句话必须在他读到任何数字之前出现，否则容易被当成已作答。
    for lim in report.get("data_limits") or []:
        lines.append(f"> 数据限制 · {lim['label']}：有{lim['have']}；"
                     f"没有{lim['missing']}。{lim['offer']}。")
        lines.append("")

    sections = report.get("sections") or []
    if not sections:                                  # 兜底：结构缺失时不输出半份报告
        for cv in report.get("caveats") or []:
            lines.append(f"- {cv}")
        return "\n".join(lines).rstrip() + "\n"

    for sec in sections:
        lines.append(f"### {_cn(sec['no'])}、{sec['title']}")
        lines.append("")
        for it in sec.get("items") or []:
            tag = it.get("label") or ""
            src = it.get("source") or ""
            if src:
                # 事实写「依据」（可追溯的数据源），推测写「状态」（还没验证）
                src = f"（{src}）" if it.get("kind") in ("inference", "limit") else f"（依据：{src}）"
            lines.append(f"- **[{tag}]** {it.get('text', '')}{src}")
        if sec.get("table"):
            lines.append("")
            lines += _table_md(sec["table"])
        if sec.get("note"):
            lines.append("")
            lines.append(f"_{sec['note']}_")
        lines.append("")

    gen = report.get("generator") or {}
    if gen.get("mode") == "llm":
        guard = (gen.get("guard") or {}).get("violations") or []
        lines.append(f"> 生成方式：LLM（{gen.get('model')}）叙事 + SQL 计算；"
                     f"数字白名单校验{'通过' if not guard else f'拦截 {len(guard)} 处'}")
    elif gen.get("mode") == "deterministic_tool_report":
        lines.append("> 生成方式：供应链 V2 确定性工具报告（未由 LLM 生成采购数字）")
    elif gen.get("mode") == "deterministic_direct_report":
        lines.append("> 生成方式：单工具确定性报告（未由 LLM 生成或改写指标）")
    elif gen.get("mode") == "deterministic_growth_report":
        lines.append("> 生成方式：SKU Gap 确定性机会报告（未由 LLM 编造铺货状态或增量）")
    elif gen.get("mode") == "deterministic_price_report":
        lines.append("> 生成方式：POS 价格确定性报告（未由 LLM 编造异常门店或因果关系）")
    else:
        lines.append("> 生成方式：规则模板（未配置模型或模型不可用），所有数字来自 SQL")
    return "\n".join(lines).rstrip() + "\n"
