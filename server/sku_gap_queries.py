"""机会引擎 · 机会一「高增长 SKU 渗透」查询层（全只读，ETL 主库）。

设计边界（M2 · SKU Gap Agent 六步流水线）
----------------------------------------
1. 数据全部来自 ETL V1 主库（fact_pos_sales / fact_sfa_visit / dim_sku /
   metric_pos_yoy），不 ATTACH 任何 Mock 库——这是 AI 分析层的进攻线，
   与供应链 Mock 层物理分开。
2. 六步全是**确定性规则**，本模块不含 LLM 调用：
   ① 高增长 SKU：YoY ≥ +20% 且 Y26 销售额 ≥ ¥100 万（排除小基数虚高）
   ② 基准池：POS 对齐 27 家在售门店的分位数（P25/P50/P75）
   ③ 缺口门店：2026-09、10 连续两个月无该 SKU POS 记录
   ④ 三态分类（联合 SFA）：有陈列采集且记录已上架但无销售 → 疑似缺货；
      无陈列采集 → 待核实；「确认未上架」当前数据不可判定（如实声明）
   ⑤ 适配性：以在售门店分位数做保守/中性/乐观三档，不用均值（防极端店拉偏）
   ⑥ 测算：缺口门店数 × 分位数单店月均箱数
3. 口径红线：
   - POS 只覆盖 27 家对齐门店（全部KA客户C），其余 394 家无 POS 数据源，
     不按零销售计——「无 POS 记录 ≠ 未上架」是本场景的核心判别逻辑；
   - 门店 city / store_level 均未采集，对照基准从「同城市 × 同等级」
     降级为「范围内在售门店分位数」，页面如实标注；
   - SFA 拜访是 2026-09-28 ~ 10-03 一周快照，「近 30 天有无拜访」
     以此快照为准；
   - 研判文案为**规则模板生成**（非 LLM 输出），一律标「待验证」。
4. 每个响应都带 meta.caliber 口径声明，前端原样渲染、不重算。
"""
from __future__ import annotations

import sqlite3
import statistics

from . import config
from .db import file_md5

GAP_DATA_SOURCE = "AI_ANALYSIS_LAYER_V1"

# 三态标签（对外固定文案，前端/测试都依赖）
STATE_SUSPECT_OOS = "疑似缺货"
STATE_TO_VERIFY = "待核实"

# 拜访记录的「已上架」口径值（源数据统一为该文案）
SHELF_ALL_ON = "果萃SKU已全部上架"

CALIBER = [
    "高增长 = POS YoY ≥ +20% 且 Y26 销售额 ≥ ¥100 万（metric_pos_yoy，POS 口径）",
    "缺口门店 = POS 对齐 27 家中，2026-09 与 10 连续两月无该 SKU 记录",
    "无 POS 记录 ≠ 未上架：联合 SFA 拜访判三态（疑似缺货 / 待核实）",
    "基准 = 在售门店单店月均箱数分位数 P25/P50/P75；城市与门店等级未采集，"
    "不按「同城市×同等级」细分",
    "低动销 = 已有 POS 销售且单店月均箱数低于同 SKU 责任范围 P25；不等同于未铺货",
    "增量测算 = 缺口门店数 × 分位数单店月均；金额按该 SKU 2026 年均价估算",
    "「确认未上架」当前数据不可判定（拜访统一记录已全部上架），页面如实标注",
]

DATA_FACTS = [
    "POS 对齐门店 27 家（全部KA客户C，region_scope_status=IN_SCOPE）；"
    "其余 394 家无 POS 数据源，不参与本场景、不按零销售计",
    "SFA 拜访为 2026-09-28 ~ 2026-10-03 一周快照（696 次拜访，覆盖 27 家）",
    "门店 city / store_level 在 dim_store 中统一「未采集」，不从门店名推断",
]

NOT_PROVIDED = [
    {"metric": "确认未上架（三态之一）",
     "reason": "拜访统一记录「果萃SKU已全部上架」，无逐 SKU 上架明细，不可判定"},
    {"metric": "同城市 × 同等级对照基准",
     "reason": "city / store_level 未采集，降级为范围内在售门店分位数"},
    {"metric": "销量弹性 / 调价情景",
     "reason": "属机会二（价格优化）范围，本页不做"},
]


class GapError(Exception):
    """机会查询参数错误（对外映射为 HTTP 400）。"""


# ---------------------------------------------------------------------------
# 公共件
# ---------------------------------------------------------------------------
def _meta(conn: sqlite3.Connection, extra: dict | None = None) -> dict:
    meta = {
        "data_source": GAP_DATA_SOURCE,
        "database_md5": file_md5(config.ETL_DB),
        "thresholds": {
            "yoy_min_pct": config.GAP_YOY_MIN_PCT,
            "y26_amount_min_yuan": config.GAP_Y26_AMOUNT_MIN,
            "window": [f"{config.GAP_WINDOW[0]}-{m:02d}" for m in config.GAP_WINDOW[1]],
            "benchmark": "P50（中位数，另有 P25/P75 两档）",
        },
        "business_caliber": "BI 算数（本模块全部确定性规则）、AI 研判（模板初稿）、人定夺",
        "not_provided": NOT_PROVIDED,
        "caliber": CALIBER,
        "data_facts": DATA_FACTS,
    }
    if extra:
        meta.update(extra)
    return meta


def _ok(data, meta) -> dict:
    return {"ok": True, "data": data, "meta": meta}


def _window_sql() -> str:
    months = ",".join(str(m) for m in config.GAP_WINDOW[1])
    return f"year = {config.GAP_WINDOW[0]} AND month IN ({months})"


def _stores(conn: sqlite3.Connection, region: str = "") -> list[sqlite3.Row]:
    where = " AND region_name_std = ?" if region else ""
    params = (region,) if region else ()
    return conn.execute(f"""
        SELECT DISTINCT store_key, store_id, store_name, region_name_std
        FROM fact_pos_sales WHERE region_scope_status = 'IN_SCOPE'
        {where} ORDER BY store_name""", params).fetchall()


def _visits(conn: sqlite3.Connection, store_key: int) -> sqlite3.Row:
    return conn.execute("""
        SELECT COUNT(*) n,
               SUM(CASE WHEN display_form IS NOT NULL AND display_form != ''
                        THEN 1 ELSE 0 END) disp,
               MIN(visit_date) first_visit, MAX(visit_date) last_visit
        FROM fact_sfa_visit WHERE store_key = ?""", (store_key,)).fetchone()


# ---------------------------------------------------------------------------
# 核心计算：单 SKU 的缺口、三态、分位数、测算
# ---------------------------------------------------------------------------
def _sku_gap(conn: sqlite3.Connection, sku: str,
             stores: list[sqlite3.Row] | None = None) -> dict:
    stores = stores if stores is not None else _stores(conn)
    sold_rows = conn.execute(f"""
        SELECT store_key, SUM(sales_boxes) boxes, SUM(sales_amount) amt
        FROM fact_pos_sales
        WHERE region_scope_status = 'IN_SCOPE' AND sku_code = ? AND {_window_sql()}
        GROUP BY store_key""", (sku,)).fetchall()
    store_keys = {s["store_key"] for s in stores}
    sold = {r["store_key"]: r for r in sold_rows if r["store_key"] in store_keys}
    sold_rows = list(sold.values())
    gap = [s for s in stores if s["store_key"] not in sold]

    # ④ 三态：有陈列采集（且拜访记录已上架）→ 疑似缺货；否则待核实
    tri = {STATE_SUSPECT_OOS: [], STATE_TO_VERIFY: []}
    detail = []
    for s in gap:
        v = _visits(conn, s["store_key"])
        state = (STATE_SUSPECT_OOS
                 if (v["disp"] or 0) > 0 else STATE_TO_VERIFY)
        tri[state].append(s)
        detail.append({
            "store_key": s["store_key"], "store_id": s["store_id"],
            "store_name": s["store_name"], "region": s["region_name_std"],
            "state": state,
            "visits": int(v["n"] or 0),
            "display_collected": int(v["disp"] or 0),
            "first_visit": v["first_visit"], "last_visit": v["last_visit"],
        })

    # ②/⑤ 基准分位数：在售门店单店月均箱数（窗口 2 个月）
    per_store_month = [float(r["boxes"]) / len(config.GAP_WINDOW[1])
                       for r in sold_rows if (r["boxes"] or 0) > 0]
    per_store_month.sort()
    if len(per_store_month) >= 2:
        q = statistics.quantiles(per_store_month, n=4, method="inclusive")
        p25, p50, p75 = q[0], q[1], q[2]
    else:
        p25 = p50 = p75 = None

    # 已经有销售记录的门店不能重复推荐铺货；低于同 SKU P25 时转入低动销清单。
    # 这里复用上面的同一责任范围、同一时间窗和分位数，不另造口径。
    store_by_key = {s["store_key"]: s for s in stores}
    low_velocity_detail = []
    if p25 is not None:
        for r in sold_rows:
            monthly_boxes = float(r["boxes"] or 0) / len(config.GAP_WINDOW[1])
            if 0 < monthly_boxes < p25:
                s = store_by_key.get(r["store_key"])
                if s is None:
                    continue
                low_velocity_detail.append({
                    "store_key": s["store_key"], "store_id": s["store_id"],
                    "store_name": s["store_name"], "region": s["region_name_std"],
                    "monthly_boxes": round(monthly_boxes, 2),
                    "p25_boxes": round(float(p25), 2),
                    "status": "已售但低动销",
                })
        low_velocity_detail.sort(key=lambda x: (x["monthly_boxes"], x["store_name"]))

    price = conn.execute("""
        SELECT AVG(unit_price) FROM fact_pos_sales
        WHERE sku_code = ? AND year = ?""", (sku, config.GAP_WINDOW[0])).fetchone()[0]

    out = {
        "sku": sku,
        "total_stores": len(stores),
        "sold_stores": len(sold),
        "gap_stores": len(gap),
        "tri": {k: len(v) for k, v in tri.items()},
        "gap_detail": detail,
        "low_velocity_stores": len(low_velocity_detail),
        "low_velocity_detail": low_velocity_detail,
        "benchmark_stores": len(per_store_month),
        "p25": p25, "p50": p50, "p75": p75,
        "avg_price_yuan": round(float(price), 2) if price is not None else None,
    }
    # ⑥ 测算：缺口 × 分位数（有缺口且有分位数才算）
    if gap and p50 is not None:
        n = len(gap)
        out["scenarios"] = {
            "p25": {"boxes": round(n * p25, 1),
                    "amount_wan": round(n * p25 * (price or 0) / 10000, 2)},
            "p50": {"boxes": round(n * p50, 1),
                    "amount_wan": round(n * p50 * (price or 0) / 10000, 2)},
            "p75": {"boxes": round(n * p75, 1),
                    "amount_wan": round(n * p75 * (price or 0) / 10000, 2)},
        }
    else:
        out["scenarios"] = None
    return out


def _high_growth(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT dimension_value sku, y25_amount, y26_amount, yoy_pct
        FROM metric_pos_yoy
        WHERE dimension = 'SKU' AND yoy_pct >= ? AND y26_amount >= ?
        ORDER BY yoy_pct DESC""",
        (config.GAP_YOY_MIN_PCT, config.GAP_Y26_AMOUNT_MIN)).fetchall()


def _sku_info(conn: sqlite3.Connection, sku: str) -> sqlite3.Row | None:
    return conn.execute("""
        SELECT sku_code, sku_name, category, pieces_per_box, specification
        FROM dim_sku WHERE sku_code = ?""", (sku,)).fetchone()


# ---------------------------------------------------------------------------
# 端点 1：meta
# ---------------------------------------------------------------------------
def meta(conn: sqlite3.Connection, qs: dict) -> dict:
    n_hg = len(_high_growth(conn))
    extra = {
        "page": "机会引擎 · 机会一（高增长 SKU 渗透）",
        "high_growth_sku_count": n_hg,
        "pos_aligned_stores": len(_stores(conn)),
    }
    return _ok({"engine": "sku_gap_v1", "opportunity": "sku_penetration"},
               _meta(conn, extra))


# ---------------------------------------------------------------------------
# 端点 2：高增长 SKU 概览
# ---------------------------------------------------------------------------
def skus(conn: sqlite3.Connection, qs: dict) -> dict:
    rows = _high_growth(conn)
    stores = _stores(conn)
    items = []
    for r in rows:
        sku = r["sku"]
        info = _sku_info(conn, sku)
        g = _sku_gap(conn, sku, stores)
        p50 = g["p50"]
        item = {
            "sku_code": sku,
            "sku_name": info["sku_name"] if info else sku,
            "category": info["category"] if info else None,
            "pieces_per_box": info["pieces_per_box"] if info else None,
            "y25_amount_wan": round(r["y25_amount"] / 10000, 1),
            "y26_amount_wan": round(r["y26_amount"] / 10000, 1),
            "yoy_pct": round(r["yoy_pct"], 1),
            "sold_stores": g["sold_stores"],
            "gap_stores": g["gap_stores"],
            "suspect_oos": g["tri"][STATE_SUSPECT_OOS],
            "to_verify": g["tri"][STATE_TO_VERIFY],
            "benchmark_stores": g["benchmark_stores"],
            "p50_boxes": round(p50, 1) if p50 is not None else None,
            "inc_p50_boxes": (g["scenarios"]["p50"]["boxes"]
                              if g["scenarios"] else None),
            "inc_p50_amount_wan": (g["scenarios"]["p50"]["amount_wan"]
                                   if g["scenarios"] else None),
        }
        items.append(item)
    data = {
        "total": len(items),
        "items": items,
    }
    return _ok(data, _meta(conn, {"page": "高增长 SKU 概览"}))


def opportunities(conn: sqlite3.Connection, region: str = "", top_n: int = 10) -> dict:
    """供 Agent 调用的范围化机会清单，复用本模块同一套六步规则。"""
    stores = _stores(conn, region)
    items = []
    for row in _high_growth(conn):
        sku = row["sku"]
        info = _sku_info(conn, sku)
        gap = _sku_gap(conn, sku, stores)
        if gap["gap_stores"] <= 0 and gap["low_velocity_stores"] <= 0:
            continue
        p50 = gap["p50"]
        scenario = (gap.get("scenarios") or {}).get("p50") or {}
        items.append({
            "sku_code": sku,
            "sku_name": info["sku_name"] if info else sku,
            "category": info["category"] if info else None,
            "pieces_per_box": info["pieces_per_box"] if info else None,
            "y25_amount_wan": round(row["y25_amount"] / 10000, 1),
            "y26_amount_wan": round(row["y26_amount"] / 10000, 1),
            "yoy_pct": round(row["yoy_pct"], 1),
            "responsible_stores": gap["total_stores"],
            "sold_stores": gap["sold_stores"],
            "gap_stores": gap["gap_stores"],
            "suspect_oos": gap["tri"][STATE_SUSPECT_OOS],
            "to_verify": gap["tri"][STATE_TO_VERIFY],
            "benchmark_stores": gap["benchmark_stores"],
            # 保留分位数计算精度；报告层可格式化，但 Checker 必须能由公开依据精确复算。
            "p25_boxes": gap["p25"],
            "p50_boxes": p50,
            "p75_boxes": gap["p75"],
            "inc_p50_boxes": scenario.get("boxes"),
            "inc_p50_amount_wan": scenario.get("amount_wan"),
            "gap_detail": gap["gap_detail"],
            "low_velocity_stores": gap["low_velocity_stores"],
            "low_velocity_detail": gap["low_velocity_detail"],
        })
    items.sort(key=lambda x: (-(x.get("inc_p50_amount_wan") or 0),
                              -x["yoy_pct"], x["sku_code"]))
    return {
        "items": items[:max(1, min(int(top_n or 10), 50))],
        "total_high_growth": len(_high_growth(conn)),
        "responsible_stores": len(stores),
        "region": region or "全国 POS 对齐门店",
        "meta": _meta(conn, {"page": "Agent 高增长 SKU 铺货机会"}),
    }


# ---------------------------------------------------------------------------
# 端点 3：单 SKU 完整机会卡片
# ---------------------------------------------------------------------------
def card(conn: sqlite3.Connection, qs: dict) -> dict:
    sku = (qs.get("sku") or "").strip()
    if sku == "":
        raise GapError("缺少参数 sku，用法：/api/gap/card?sku=G25-25")
    info = _sku_info(conn, sku)
    if info is None:
        raise GapError(f"SKU 不存在：{sku}（dim_sku 无此编码）")
    yoy = conn.execute("""
        SELECT y25_amount, y26_amount, yoy_pct FROM metric_pos_yoy
        WHERE dimension = 'SKU' AND dimension_value = ?""", (sku,)).fetchone()
    if yoy is None or (yoy["y26_amount"] or 0) <= 0:
        raise GapError(f"SKU {sku} 无 POS 销售记录，无法生成机会卡片")

    stores = _stores(conn)
    g = _sku_gap(conn, sku, stores)
    high_growth = yoy["yoy_pct"] >= config.GAP_YOY_MIN_PCT and \
        yoy["y26_amount"] >= config.GAP_Y26_AMOUNT_MIN

    # 增速证据：2026 年逐月箱数与金额
    trend = [dict(zip(("month", "boxes", "amount_wan"),
                      (m, round(b, 1), round(a / 10000, 2))))
             for m, b, a in conn.execute(f"""
                 SELECT month, SUM(sales_boxes), SUM(sales_amount)
                 FROM fact_pos_sales
                 WHERE region_scope_status = 'IN_SCOPE' AND sku_code = ?
                   AND year = ?
                 GROUP BY month ORDER BY month""",
                 (sku, config.GAP_WINDOW[0]))]

    # 在售门店明细（基准池证据）
    sold_detail = [dict(zip(("store_name", "region", "boxes_month"),
                            (r[0], r[1], round(float(r[2]) / len(config.GAP_WINDOW[1]), 1))))
                   for r in conn.execute(f"""
                       SELECT s.store_name, s.region_name_std, SUM(p.sales_boxes)
                       FROM fact_pos_sales p JOIN dim_store s ON s.store_key = p.store_key
                       WHERE p.region_scope_status = 'IN_SCOPE' AND p.sku_code = ?
                         AND p.{_window_sql()}
                       GROUP BY 1, 2 ORDER BY 3 DESC""", (sku,))]

    # SFA 核实任务（待核实门店 → 拜访任务清单）
    sfa_tasks = [{
        "task_id": f"GAP-{sku}-{d['store_id']}",
        "store_name": d["store_name"], "region": d["region"],
        "last_visit": d["last_visit"],
        "task": f"现场核实 {sku}（{info['sku_name']}）是否上架 / 缺货 / 未扫码，"
                "回填陈列与库存照片",
    } for d in g["gap_detail"] if d["state"] == STATE_TO_VERIFY]

    # 疑似缺货门店 → 排查方向（不生成任务，给链路穿透线索）
    oos_clues = [{
        "store_name": d["store_name"], "region": d["region"],
        "last_visit": d["last_visit"],
        "clue": "拜访记录已上架但连续两月无销售：先查门店库存与扫码记录，"
                "排除「上架未扫码」，再查批发档口库存",
    } for d in g["gap_detail"] if d["state"] == STATE_SUSPECT_OOS]

    # 研判初稿（规则模板生成，非 LLM；一律标待验证）
    if g["gap_stores"] == 0:
        draft = (f"{sku} 在 27 家对齐门店全部有销售记录，本期无铺货缺口，"
                 "建议保持现有覆盖并关注月度动销。")
    else:
        draft = (f"{sku}（{info['sku_name']}）YoY +{yoy['yoy_pct']:.1f}%，"
                 f"但 27 家对齐门店中 {g['gap_stores']} 家连续两月无销售："
                 f"① {g['tri'][STATE_TO_VERIFY]} 家待核实——已生成 SFA 拜访任务，"
                 "确认是否未上架；"
                 f"② {g['tri'][STATE_SUSPECT_OOS]} 家疑似缺货——先查库存与扫码，"
                 "排除「上架未扫码」；"
                 f"③ 测算口径为在售门店分位数（{g['benchmark_stores']} 家在售），"
                 + (f"中性 P50 增量 {g['scenarios']['p50']['boxes']:.0f} 盒/月"
                    f"（≈¥{g['scenarios']['p50']['amount_wan']:.1f} 万/月），"
                    "按门店逐一确认后执行。"
                    if g["scenarios"] else "在售门店不足两家，无法给出分位数测算。"))
    recommendation = {
        "draft": draft,
        "status": "待验证",
        "generated_by": "规则模板（sku_gap_v1），非 LLM 输出；"
                        "数字可溯源 fact_pos_sales / fact_sfa_visit",
    }

    data = {
        "sku_code": sku, "sku_name": info["sku_name"],
        "category": info["category"], "pieces_per_box": info["pieces_per_box"],
        "high_growth": bool(high_growth),
        "growth": {
            "y25_amount_wan": round(yoy["y25_amount"] / 10000, 1),
            "y26_amount_wan": round(yoy["y26_amount"] / 10000, 1),
            "yoy_pct": round(yoy["yoy_pct"], 1),
            "trend_2026": trend,
        },
        "coverage": {
            "total_stores": g["total_stores"],
            "sold_stores": g["sold_stores"],
            "gap_stores": g["gap_stores"],
            "suspect_oos": g["tri"][STATE_SUSPECT_OOS],
            "to_verify": g["tri"][STATE_TO_VERIFY],
        },
        "gap_detail": g["gap_detail"],
        "benchmark": {
            "stores": g["benchmark_stores"],
            "p25": g["p25"], "p50": g["p50"], "p75": g["p75"],
            "sold_detail": sold_detail,
            "note": "在售门店单店月均箱数分位数；城市/等级未采集，不细分",
        },
        "scenarios": g["scenarios"],
        "sfa_tasks": sfa_tasks,
        "oos_clues": oos_clues,
        "recommendation": recommendation,
    }
    return _ok(data, _meta(conn, {"page": f"机会卡片 · {sku}"}))
