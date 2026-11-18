"""页面注册表（P8）：后端唯一的「有哪些 BI 页面、每页能采哪些字段」。

为什么要在后端也放一份：
- 前端 `web/page-context.js` 需要**同步可用**，不能等一个 fetch 才渲染入口，所以它自带一份；
- 后端需要回答「这个 page_id 合法吗、它属于哪个模块」，做请求校验与报告标注；
- 两份清单会漂移，所以 `tests/test_20_page_context.py` 会逐项比对
  ``/api/agent/pages`` 与前端注册表，任一新增/删除未同步都会失败。

字段名与 `AgentContext` 保持一致；``fields`` 只列该页面**真实存在**的控件。
"""
from __future__ import annotations

PAGES: tuple[dict, ...] = (
    {
        "page_id": "cockpit", "page_name": "AI 经营驾驶舱", "module": "cockpit",
        "path": "/",
        "fields": ("year", "month", "region", "customer"),
    },
    {
        "page_id": "analysis", "page_name": "战区销售达成跟进", "module": "analysis",
        "path": "/analysis.html",
        "fields": ("year", "month", "region", "department", "category", "segment"),
    },
    {
        "page_id": "pos", "page_name": "POS 分析", "module": "pos",
        "path": "/pos.html",
        "fields": ("month", "region", "ka_system", "category", "sku", "store"),
    },
    {
        "page_id": "sfa_visit", "page_name": "SFA 拜访分析", "module": "sfa",
        "path": "/sfa-visit.html",
        "fields": ("month", "region", "department", "employee"),
    },
    {
        "page_id": "sfa_profile", "page_name": "SFA 门店画像", "module": "sfa",
        "path": "/sfa-profile.html",
        "fields": ("month", "region", "department", "employee", "ka_system", "sku"),
    },
    {
        "page_id": "wholesale", "page_name": "批发经营分析", "module": "wholesale",
        "path": "/wholesale.html",
        "fields": ("month", "region", "customer", "stall", "sku"),
    },
    {
        "page_id": "sc", "page_name": "供应链看板", "module": "sc",
        "path": "/sc.html",
        "fields": ("month",),
    },
)

BY_ID = {p["page_id"]: p for p in PAGES}

# 模块 → 该模块工具真正认识的维度。用于报告里区分「页面有、但这个模块算不了」。
MODULE_DIMENSIONS = {
    "cockpit": ("region", "customer"),
    "dashboard": ("region", "channel", "ka_system", "customer", "category", "sku"),
    "analysis": ("region", "department", "category", "segment"),
    "pos": ("region", "ka_system", "category", "sku", "store"),
    "sfa": ("region", "department", "ka_system", "store"),
    "wholesale": ("region", "customer", "stall", "sku"),
    # 供应链模块走 /api/sc/*，业务维度（战区/客户/SK）不在其口径内
    "sc": (),
}


def catalog() -> list[dict]:
    """给前端的只读清单（tuple 转 list，便于 JSON 序列化）。"""
    return [{**p, "fields": list(p["fields"])} for p in PAGES]


def name_of(page_id: str) -> str:
    return (BY_ID.get(page_id) or {}).get("page_name", "")


def module_of(page_id: str) -> str:
    return (BY_ID.get(page_id) or {}).get("module", "")
