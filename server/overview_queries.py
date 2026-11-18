"""集团总览（``/api/overview``）：把四个业务模块的当期指标汇到同一行卡片。

**这个模块只做聚合展示，不做任何新的业务计算。**

口径红线（与 P01 / P05 / P07 及已验收看板完全一致）：

1. 每个模块只读自己的视图 / 事实表：ERP → v_erp_shipment，POS → v_pos_sales，
   SFA → fact_sfa_visit，批发 → fact_wholesale_purchase / sales / inventory_daily。
   模块之间**没有一次 JOIN**，四类金额也**从不相加**（Sell-in、Sell-out、出库、库存
   是四条不同的链路，相加没有业务含义）。
2. SFA 与批发不在这里重写口径，而是直接调用各自已验收的查询模块
   （``sfa_visit_queries.analysis`` / ``wholesale_queries.analysis``），
   只把这两个看板真正支持的筛选（月份、战区）传进去。
3. 每张卡都带 ``scope`` 声明：该指标真正响应的筛选维度。KA / 客户 / 品类 / SKU 属于
   ERP 与 POS 的维度，SFA 与批发没有这些维度，**不猜测映射**，也不假装跟着变。
4. 环比统一为「较上月」，且只在有相邻两个月真实数据时给出；算不出来就返回 null，
   由页面显示 ``—``，不补零、不预测。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from . import board_filters, config, filters, queries, sfa_visit_queries, wholesale_queries

BOARD = "overview"

# 各模块 KPI 真正响应的筛选维度（写在卡片上，避免「看起来都联动」的误解）
SCOPE = {
    "erp": "月份 · 战区 · 渠道 · 客户 · 品类 · SKU",
    "pos": "月份 · 战区 · KA · 品类 · SKU",
    "sfa": "月份 · 战区",
    "wholesale": "月份 · 战区",
}

NOTES = (
    "KPI 行按模块分别核算：ERP 出货、POS 销售、SFA 拜访、批发进销存各读各的视图，四类金额永不相加。",
    "SFA 与批发只响应月份与战区；KA / 客户 / 品类 / SKU 仅作用于 ERP 与 POS（不猜测跨系统映射）。",
    "环比为「较上月」，仅在相邻两个月都有真实数据时给出，不补零、不预测。",
    "拜访目标当前含示例数据（DEMO），完成率仅用于演示展示逻辑，不代表真实目标达成。",
)

VIEWS = ["v_erp_shipment", "v_pos_sales", "fact_sfa_visit", "fact_wholesale_purchase",
         "fact_wholesale_sales", "fact_wholesale_inventory_daily"]


# ------------------------------------------------------------------ 小工具
def _mom(cur: Any, prev: Any) -> float | None:
    """环比百分比；分母为 0 或缺失时返回 None（不回填 0）。"""
    try:
        cur_f, prev_f = float(cur), float(prev)
    except (TypeError, ValueError):
        return None
    if prev_f == 0:
        return None
    return (cur_f - prev_f) * 100.0 / prev_f


def _rate(visited: Any, target: Any) -> float | None:
    try:
        v, t = float(visited), float(target)
    except (TypeError, ValueError):
        return None
    return None if t == 0 else v * 100.0 / t


def _sub_qs(f: filters.Filters) -> dict[str, str]:
    """构造给看板查询模块的筛选子集：只传它们支持的 months / regions。"""
    qs: dict[str, str] = {}
    if f.months:
        qs["months"] = ",".join(str(m) for m in f.months)
    if tuple(f.regions) != tuple(config.IN_SCOPE_REGIONS):
        qs["regions"] = ",".join(f.regions)
    return qs


def _card(key: str, label: str, value: Any, unit: str, *, fmt: str,
          delta: dict | None, note: str, scope: str, module: str, tone: str) -> dict:
    return {
        "key": key, "label": label, "value": value, "unit": unit, "fmt": fmt,
        "delta": delta, "note": note, "scope": scope, "module": module, "tone": tone,
    }


def _trend_series(months: list[int], rows: list[dict], field: str) -> list:
    by_month = {int(r["month"]): r.get(field) for r in rows if r.get("month") is not None}
    return [by_month.get(m) for m in months]


# ------------------------------------------------------------------ 主入口
def overview(conn: sqlite3.Connection, f: filters.Filters) -> dict:
    months = list(f.months) if f.months else list(range(1, config.COMPARABLE_LAST_MONTH + 1))

    # ---- 1) ERP / POS：直接复用驾驶舱已验收的 KPI 与趋势（同一套 WHERE、同一套视图）
    k = queries.kpi(conn, f)["data"]
    t = queries.trend(conn, f)["data"]
    erp, pos = k["erp"], k["pos"]

    # ---- 2) SFA / 批发：复用各自看板的查询模块，只传 months / regions
    sub = _sub_qs(f)
    sfa_payload = sfa_visit_queries.analysis(conn, board_filters.parse_board(sub, "sfa_visit"))
    sfa = sfa_payload["data"]
    wh = wholesale_queries.analysis(conn, board_filters.parse_board(sub, "wholesale"))["data"]
    sfa_kpi, wh_kpi = sfa["kpi"], wh["kpi"]

    # SFA 底表只有 2026-09-28 起的真实拜访流水。若所选区间一条拜访记录都没有，
    # 「0 家」会被读成「一家都没拜访」，与「这段没采集」是两件事——按 P13 的原则，
    # 此时卡片返回空值并说明数据起止，不写 0、不补零。
    sfa_win = (sfa_payload.get("meta") or {}).get("coverage_window") or {}
    sfa_empty = sfa_win.get("first_date") is None
    sfa_data_from = ""
    if sfa_empty:
        full_win = sfa_visit_queries.analysis(
            conn, board_filters.parse_board({}, "sfa_visit"))["meta"]["coverage_window"]
        sfa_data_from = full_win.get("first_date") or ""
    sfa_empty_note = (f"所选区间无拜访记录（SFA 数据自 {sfa_data_from} 起）"
                      if sfa_data_from else "所选区间的拜访数据未采集")

    # ---- 3) 环比：只用相邻两个月的真实值
    # SFA 的两张卡是「窗口内去重门店数」与「窗口完成率」，与单月环比不是同一粒度，
    # 因此**不给环比角标**（宁可不给，也不把两种粒度混在一张卡上）。
    sfa_trend = sfa.get("trend") or []
    sfa_last = sfa_trend[-1] if sfa_trend else None

    wh_trend = wh.get("trend") or []
    wh_last = wh_trend[-1] if wh_trend else None
    wh_prev = wh_trend[-2] if len(wh_trend) > 1 else None
    wh_sales_mom = (_mom(wh_last.get("sales_amount"), wh_prev.get("sales_amount"))
                    if wh_last and wh_prev else None)

    inv_months = (wh.get("inventory") or {}).get("months") or []
    inv_last = inv_months[-1] if inv_months else None
    inv_prev = inv_months[-2] if len(inv_months) > 1 else None
    inv_mom = (_mom(inv_last.get("inventory_amount"), inv_prev.get("inventory_amount"))
               if inv_last and inv_prev else None)

    def delta(pct, label="较上月", pp=False):
        return None if pct is None else {"pct": round(pct, 2), "label": label, "pp": pp}

    # ---- 4) 六张卡：一眼看全集团四条业务线
    kpi = [
        _card("erp_amount", "ERP 出货金额", erp.get("sales_amount"), "元", fmt="money",
              delta=delta((erp.get("mom") or {}).get("amount")),
              note=f"Sell-in · 客户 {erp.get('rows_count', 0):,} 行明细汇总",
              scope=SCOPE["erp"], module="erp", tone="brand"),
        _card("pos_amount", "POS 终端销售金额", pos.get("sales_amount"), "元", fmt="money",
              delta=delta((pos.get("mom") or {}).get("amount")),
              note=f"Sell-out · 终端 {pos.get('rows_count', 0):,} 行明细汇总",
              scope=SCOPE["pos"], module="pos", tone="green"),
        _card("sfa_stores", "拜访门店数",
              None if sfa_empty else sfa_kpi.get("actual_store_total"), "家", fmt="int",
              delta=None,
              note=(sfa_empty_note if sfa_empty else
                    f"覆盖 {sfa_kpi.get('covered_stores')} / {sfa_kpi.get('universe_stores')} 家 · "
                    f"{sfa_kpi.get('visit_count')} 次拜访"),
              scope=SCOPE["sfa"], module="sfa", tone="violet"),
        _card("sfa_rate", "拜访达成率",
              None if sfa_empty else sfa_kpi.get("completion_rate"), "%", fmt="pct",
              delta=None,
              note=(sfa_empty_note if sfa_empty else
                    ("目标含示例数据（DEMO）" if sfa_kpi.get("target_is_demo")
                     else f"目标 {sfa_kpi.get('target_store_total')} 家")),
              scope=SCOPE["sfa"], module="sfa", tone="violet"),
        _card("wh_out", "批发出库金额", wh_kpi.get("sales_amount"), "元", fmt="money",
              delta=delta(wh_sales_mom),
              note=f"出库单 {wh_kpi.get('sales_docs', 0):,} 张 · 毛利额 "
                   f"{(wh_kpi.get('gross_profit') or 0) / 1e4:,.0f} 万元",
              scope=SCOPE["wholesale"], module="wholesale", tone="amber"),
        _card("wh_inv", "期末库存金额", wh_kpi.get("inventory_amount"), "元", fmt="money",
              delta=delta(inv_mom),
              note=f"时点值 · {wh_kpi.get('anchor_date') or '—'} · 不与出库相加",
              scope=SCOPE["wholesale"], module="wholesale", tone="slate"),
    ]

    # ---- 5) 趋势：四条线各用自己的单位，不共用一根纵轴（量级差 3 个数量级）
    trend = {
        "months": months,
        "series": {
            "erp": {"label": "ERP 出货金额", "unit": "元", "kind": "money",
                    "values": [x.get("value") for x in t["erp"]]},
            "pos": {"label": "POS 终端销售金额", "unit": "元", "kind": "money",
                    "values": [x.get("value") for x in t["pos"]]},
            "wholesale": {"label": "批发出库金额", "unit": "元", "kind": "money",
                          "values": _trend_series(months, wh_trend, "sales_amount")},
            "sfa": {"label": "SFA 拜访门店数", "unit": "家", "kind": "int",
                    "values": _trend_series(months, sfa_trend, "visited_store_count")},
        },
    }

    # ---- 6) 两个模块概览块
    sfa_block = {
        "employee_total": sfa_kpi.get("employee_total"),
        "target_store_total": sfa_kpi.get("target_store_total"),
        "actual_store_total": sfa_kpi.get("actual_store_total"),
        "completion_rate": sfa_kpi.get("completion_rate"),
        "coverage_rate": sfa_kpi.get("coverage_rate"),
        "covered_stores": sfa_kpi.get("covered_stores"),
        "universe_stores": sfa_kpi.get("universe_stores"),
        "visit_count": sfa_kpi.get("visit_count"),
        "repeat_visit_count": sfa_kpi.get("repeat_visit_count"),
        "target_is_demo": bool(sfa_kpi.get("target_is_demo")),
        "months_active": sfa_kpi.get("months_active"),
        "latest": sfa_last,
        # 区间内一条拜访记录都没有时为 True：前端据此显示「未采集」而不是 0
        "no_window_data": bool(sfa_empty),
        "data_from": sfa_data_from or sfa_win.get("first_date"),
    }
    wholesale_block = {
        "purchase_amount": wh_kpi.get("purchase_amount"),
        "sales_amount": wh_kpi.get("sales_amount"),
        "inventory_amount": wh_kpi.get("inventory_amount"),
        "gross_profit": wh_kpi.get("gross_profit"),
        "gross_margin": wh_kpi.get("gross_margin"),
        "inventory_te": wh_kpi.get("inventory_te"),
        "anchor_date": wh_kpi.get("anchor_date"),
        "inventory_series": [
            {"label": x.get("label"), "value": x.get("inventory_amount")}
            for x in inv_months
        ],
    }

    data = {
        "kpi": kpi,
        "trend": trend,
        "sfa": sfa_block,
        "wholesale": wholesale_block,
        "scope": SCOPE,
        "notes": list(NOTES),
    }
    return {
        "ok": True,
        "data": data,
        "meta": {
            "board": BOARD,
            "filters": f.as_dict(),
            "region_label": f.region_label,
            "period": config.PERIOD_LABEL,
            "year": config.FIXED_YEAR,
            "year_int": config.FIXED_YEAR_INT,
            "scope_note": config.SCOPE_NOTE,
            "source_views": VIEWS,
            "no_cross_table_join": True,
            "board_filters_passed": sub,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "notes": list(NOTES),
        },
    }
