"""ChatBI 统一业务语义层：只做解析、定义和路由提示，不取数。"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

CATALOG_PATH = Path(__file__).with_name("semantic_catalog.json")


@lru_cache(maxsize=1)
def catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def _contains(text: str, term: str) -> bool:
    return term.lower().replace(" ", "") in text.lower().replace(" ", "")


def _metric(code: str, definition: dict) -> dict:
    return {
        "code": code,
        "name": definition["name"],
        "definition": definition["definition"],
        "unit": definition["unit"],
        "fields": list(definition.get("fields") or []),
        "tool": definition["tool"],
        "dimensions": list(definition.get("dimensions") or []),
    }

def resolve(question: str, context: dict | None = None) -> dict:
    """把自然语言映射到统一指标/维度和公开工具提示。

    返回值不会执行工具。歧义口径保持空路由并要求澄清，避免默认把“销量”
    当成 ERP 或 POS 中的任意一种。
    """
    cfg = catalog()
    q = (question or "").strip()
    low = q.lower()
    ctx = context or {}
    matched: list[dict] = []
    matched_terms: list[dict] = []
    for code, meta in cfg["metrics"].items():
        terms = list(meta.get("synonyms") or [])
        hits = [term for term in terms if _contains(low, term)]
        if hits:
            matched.append(_metric(code, meta))
            matched_terms.extend({"term": term, "metric": code} for term in hits)

    clarification = ""
    generic_sales = _contains(low, "销量")
    qualified_sales = any(m["code"] in ("erp_te", "pos_sales_te") for m in matched)
    if generic_sales and not qualified_sales:
        module = str(ctx.get("module") or ctx.get("page_id") or "").lower()
        if module in ("pos", "sfa_pos"):
            matched.append(_metric("pos_sales_te", cfg["metrics"]["pos_sales_te"]))
            matched_terms.append({"term": "销量", "metric": "pos_sales_te",
                                  "resolved_by": "page_context"})
        elif module in ("erp", "dashboard", "budget"):
            matched.append(_metric("erp_te", cfg["metrics"]["erp_te"]))
            matched_terms.append({"term": "销量", "metric": "erp_te",
                                  "resolved_by": "page_context"})
        else:
            clarification = cfg["ambiguities"]["销量"]["clarification"]

    dimensions: list[dict] = []
    for code, meta in cfg["dimensions"].items():
        hits = [term for term in meta.get("synonyms") or [] if _contains(low, term)]
        if hits:
            dimensions.append({"code": code, "name": meta["name"], "matched": hits})

    route_tools: list[str] = []
    if not clarification:
        for item in matched:
            if item["tool"] not in route_tools:
                route_tools.append(item["tool"])

    return {
        "version": cfg["version"],
        "normalized_question": q,
        "metrics": matched,
        "dimensions": dimensions,
        "matched_terms": matched_terms,
        "route_tools": route_tools,
        "needs_clarification": bool(clarification),
        "clarification": clarification,
    }
