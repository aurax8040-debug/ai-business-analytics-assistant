"""P10 · 会话记忆：记住「上一轮到底在说谁」，只用于指代消解。

为什么需要
----------
用户问「那这个客户为什么掉这么多」时，「这个客户」指的是**上一轮答案里的那个客户**
（例如缺口最大的KA客户E）。这个信息只存在于上一轮的结果里，不在用户说过的话里。
纯靠重放历史文本无法还原，所以需要一份按 ``thread_id`` 索引的最小记忆。

边界（很重要，面试会被问）
------------------------
- **只存指代所需的最小信息**：已解析的范围 + 上一轮结论里的焦点实体，
  不存 SQL 结果、不存报告全文，避免变成「隐形状态」难以解释；
- **不跨会话**：``thread_id`` 为空时不记忆，不同 thread 互不可见；
- **进程内**：这是演示级实现，重启即清空；生产应换成 Redis/DB，
  键与 TTL 与这里的语义一致即可，不影响上层逻辑；
- **不参与取数**：记忆只喂给指代消解（followup），取数仍只认本轮解析出的范围。
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Any

_MAX_THREADS = 200          # 防止长时间运行后无限增长

_STORE: "OrderedDict[str, dict]" = OrderedDict()


def _entity_from(report: dict) -> dict:
    """从上一轮报告里取「焦点实体」：用户说「这个客户/这家店」时最可能指的那个。"""
    out: dict = {}
    rows = ((report.get("problems") or {}).get("rows") or [])
    if rows and rows[0].get("customer"):
        out["customer"] = rows[0]["customer"]
    cross = report.get("cross") or {}
    if cross.get("scenario") == "sfa_vs_pos":
        cross_rows = cross.get("rows") or []
        if cross_rows:
            out["store"] = cross_rows[0].get("store") or ""
    elif cross.get("scenario") == "profit_mix":
        cross_rows = cross.get("rows") or []
        if cross_rows:
            dim = cross_rows[0].get("dimension") or ""
            if "品类" in dim:
                out["category"] = cross_rows[0].get("name")
            elif "客户" in dim:
                out["customer"] = cross_rows[0].get("name")
    return {k: v for k, v in out.items() if v}


def remember(thread_id: str, data: dict) -> None:
    """记下本轮解析出的范围与结论焦点。``data`` 是 envelope 里的 ``data``。"""
    if not thread_id:
        return
    understood = data.get("understood") or {}
    report = data.get("report") or {}
    fields: dict[str, Any] = {}
    for key, src in (("region", "region"), ("department", "department"),
                     ("customer", "customer"), ("year", "year"),
                     ("channel", "channel"), ("category", "category"),
                     ("ka_system", "ka_system"), ("store", "store"),
                     ("sku", "sku"), ("segment", "segment")):
        value = understood.get(src)
        if value not in ("", None, 0, []):
            fields[key] = value
    if understood.get("months"):
        fields["month"] = list(understood["months"])
    scenario = report.get("scenario") or ""
    if scenario.startswith("supply_chain_"):
        fields["supply_intent"] = scenario
    if scenario == "supply_chain_growth":
        selected = report.get("selected_skus") or []
        if selected:
            fields["category"] = selected[0].get("category") or fields.get("category")
            fields["pieces_per_box"] = selected[0].get("pieces_per_box")

    prev = _STORE.get(thread_id) or {}
    _STORE[thread_id] = {
        "fields": fields,
        "entity": _entity_from(report) or (prev.get("entity") or {}),
        "turn": int(prev.get("turn") or 0) + 1,
        "scope_label": report.get("scope_label") or "",
    }
    _STORE.move_to_end(thread_id)
    while len(_STORE) > _MAX_THREADS:
        _STORE.popitem(last=False)


def recall(thread_id: str) -> dict:
    if not thread_id:
        return {}
    item = _STORE.get(thread_id)
    if item:
        _STORE.move_to_end(thread_id)
    return dict(item or {})


def clear(thread_id: str = "") -> None:
    """测试用：清空某个会话或全部。"""
    if thread_id:
        _STORE.pop(thread_id, None)
    else:
        _STORE.clear()
