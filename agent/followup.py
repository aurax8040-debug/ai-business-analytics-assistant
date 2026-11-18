"""P10 · 连续追问：把「那这个客户呢」接上上一轮说过的范围。

问题
----
多轮对话里，第二句往往是「那这个客户为什么掉这么多」——没有战区、没有月份、
没有客户名。V1 的处理是重新解析这一句，结果什么都解析不到，
要么回到「请补充范围」，要么默默按页面默认重答一遍，答非所问。

做法
----
**从历史里回读实体，而不是维护一份服务端会话状态。**

理由：
- 历史消息就是用户真实说过的话，重新解析得到的结果可复现、可解释，
  不依赖进程内存（重启不丢），也不依赖前端额外传结构化状态；
- 前端现在只传 ``{role, content}``，无需改协议；
- 万一解析错了，用户能在「本次理解」里看到「承接上一轮：客户=KA客户B」并直接纠正。

优先级（与 P8 一致，只是多了一层）：
    **本轮问题里明确说的 > 上一轮说过的 > 当前页面上下文**
"""
from __future__ import annotations

from . import intent

# 指代词：出现这些才认为「这一句是在接着上一轮说」
PRONOUNS = ("这个客户", "该客户", "这家客户", "这家店", "这个门店", "该门店",
            "这个战区", "该战区", "这个品类", "该品类", "这个市场部", "该市场部",
            "他们", "它们", "它", "上述", "上面", "刚才", "刚说", "接着", "继续", "那",
            "再看", "还有呢", "然后呢")

# 可以承接的实体字段（与 intent.understand 的返回一致）
FIELDS = ("region", "department", "customer", "year", "month",
          "channel", "category", "ka_system", "store", "sku", "segment",
          "pieces_per_box", "supply_intent")

LABELS = {"region": "战区", "department": "市场部", "customer": "客户",
          "year": "年份", "month": "月份", "channel": "渠道",
          "category": "品类", "ka_system": "KA", "store": "门店",
          "sku": "SKU", "segment": "客户类型", "pieces_per_box": "规格",
          "supply_intent": "供应链场景"}

MAX_TURNS_BACK = 6      # 最多回看 6 条历史提问，再远就不猜了


def is_followup(question: str) -> bool:
    q = question or ""
    return any(w in q for w in PRONOUNS)


def _parse(text: str) -> dict:
    """只做规则解析，不读库、不带页面上下文（历史里的实体是用户说过的话）。"""
    try:
        return intent.understand(text, None)
    except Exception:                       # pragma: no cover - 解析失败不影响主流程
        return {}


def build(question: str, history: list[dict] | None,
          remembered: dict | None = None) -> dict:
    """返回 ``{"fields": {...}, "note": str, "from_turn": int, "used": bool}``。

    只有在本句像追问时才承接，避免每句都继承上一轮的范围
    （用户换了话题却被旧条件悄悄限制，是更难发现的错误）。

    ``remembered`` 来自 ``agent/memory.py``：上一轮的结论焦点（例如缺口最大的客户），
    用来解释「这个客户 / 这家店」到底指谁——这层信息只存在于上一轮的结果里。
    """
    # 未承接时返回空 dict（而不是带一堆空键的对象），
    # 报告里用 `if report.get("followup")` 就能判断，不用逐个字段判空
    empty: dict = {}
    remembered = remembered or {}
    remembered_fields = remembered.get("fields") or {}
    supply_followup = bool(remembered_fields.get("supply_intent")) and any(
        word in (question or "") for word in
        ("需求增长", "增长", "缺口最大", "受到影响", "受影响", "在途", "延期"))
    if not is_followup(question) and not supply_followup:
        return empty
    users = [m for m in (history or []) if (m or {}).get("role") == "user"
             and str((m or {}).get("content") or "").strip()]
    fields: dict = dict(remembered.get("fields") or {})
    entity = dict(remembered.get("entity") or {})
    from_memory = bool(fields or entity)
    from_turn = 0

    # 指代词指向哪一类实体：说「这家店」不该解析成客户
    q = question or ""
    wants_store = any(w in q for w in ("这家店", "这个门店", "该门店", "这家", "门店"))
    wants_customer = any(w in q for w in ("这个客户", "该客户", "这家客户", "这个 Ka", "这个ka"))
    wants_category = any(w in q for w in ("这个品类", "该品类"))

    if entity:
        if (wants_store or not (wants_customer or wants_category)) and entity.get("store"):
            fields["store"] = entity["store"]
        if wants_customer and entity.get("customer"):
            fields["customer"] = entity["customer"]
        if wants_category and entity.get("category"):
            fields["category"] = entity["category"]
        if not (wants_store or wants_customer or wants_category) and entity.get("customer"):
            fields.setdefault("customer", entity["customer"])

    if not from_memory:
        for idx, msg in enumerate(reversed(users[-MAX_TURNS_BACK:]), 1):
            parsed = _parse(str(msg.get("content")))
            gained = False
            for key in FIELDS:
                if key in fields:
                    continue
                value = parsed.get(key)
                if value not in ("", None, [], 0):
                    fields[key] = value
                    gained = True
            if gained and not from_turn:
                from_turn = idx
            if all(k in fields for k in ("region", "customer", "month")):
                break

    # 是否真的「继承到了东西」。这一句自己带的条件不算继承——
    # 否则「那战区A呢」在没有任何前文时也会被标成「承接上一轮」，属于假承接。
    inherited = bool(fields)

    # 换了战区就必须丢掉跨战区的实体：用户在说「再看战区B」时，
    # 显然不是要继续看战区A的某个客户，否则会查出 0 行还以为是数据缺失。
    explicit_region = intent.parse_region(question)
    if explicit_region and explicit_region != fields.get("region"):
        for key in ("customer", "store", "ka_system", "department", "category"):
            fields.pop(key, None)
        entity = {}
        fields["region"] = explicit_region

    if not inherited or not fields:
        return empty
    parts = []
    for key in ("customer", "store", "ka_system", "category", "pieces_per_box",
                "channel", "region", "department", "year", "month"):
        if key not in fields:
            continue
        value = fields[key]
        if key == "month":
            value = "、".join(f"{m} 月" for m in value) if isinstance(value, list) else value
        parts.append(f"{LABELS[key]}={value}")
    note = "、".join(parts) + (
        "（承接上一轮结论）" if from_memory else f"（取自第 {from_turn} 轮前的提问）")
    return {"fields": fields, "note": note,
            "from_turn": from_turn, "used": True,
            "source": "memory" if from_memory else "history"}
