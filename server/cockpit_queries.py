"""AI 经营驾驶舱 · 查询层。

设计边界
--------
1. **只用 ERP Sell-in**：本页所有指标来自 ``fact_erp_shipment``，POS Sell-out 一行都不读，
   两边金额/数量永不相加。POS 有自己的看板，不在这里混算。
2. **预算按 TE 考核**：达成率分母来自 ``budget_te_v1.db``（MOCK_BUDGET_TE，单位毫 TE），
   与金额预算库（``budget_v1.db``，单位分）完全不换算、不混用。
   TE 换算在 ETL 完成：``shipment_te = sales_boxes × pieces_per_box / specification``。
3. **实测 + 模型分开标注**：1–10 月是实际；11–12 月是模型外推，字段名带 ``forecast``，
   并且每条预测都带 ``model`` 说明，不把预测当实际。
4. **口径只有一个**：期间、战区、KA 三个筛选在同一份取数上切片，
   KPI / 战区 / 趋势 / KA / 利润 各块互相能对上（合计恒等）。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from . import config

YEAR = 2026
OBS_LAST_MONTH = 10
REGIONS = ("战区A", "战区B", "战区C", "战区D", "战区E")
GRAINS = ("month", "quarter")

# 客户分组（互斥，合计 = 当前范围）
#   KA  ：ERP customer_type = NKA —— KA客户A / KA客户B / KA客户C / KA客户D / KA客户E / KA客户F / KA客户G
#   区域商超：channel_type = 商超/终端 但不是 NKA（物美、永旺、华润万家等地方连锁）
#   批发 ：channel_type = 批发
# 判据只用 ERP 字段，不按客户名称猜（「品类A贸易」这类名字不能当依据）。
CUSTOMER_GROUPS = (
    {"key": "ka", "label": "KA 客户", "basis": "customer_type = NKA"},
    {"key": "rt", "label": "区域商超", "basis": "channel_type = 商超/终端 且非 NKA"},
    {"key": "ws", "label": "批发", "basis": "channel_type = 批发"},
)
GROUP_KEYS = tuple(g["key"] for g in CUSTOMER_GROUPS)


def customer_group(customer_type: str | None, channel_type: str | None) -> str:
    """ERP 客户 → 分组 key；未知组合归 other，不并入任何一组。"""
    ctype = (channel_type or "").strip()
    if (customer_type or "").strip().upper() == "NKA":
        return "ka"
    if ctype == "批发":
        return "ws"
    if ctype == "商超/终端":
        return "rt"
    return "other"

# 预测模型参数：全部写进响应的 forecast.model，前端与报告直接引用
FORECAST_MODEL = {
    "name": "季节性外推 v1（2025 同月基数 × 同比规模系数 × 近三月动能系数）",
    "horizon": "2026 年 11–12 月（未发生月份）",
    "formula": "预测 = 该组合 2025 年同月 TE × 规模系数 × 动能系数",
    "scale": "规模系数 = 该组合 2026 年 1–10 月 TE ÷ 2025 年 1–10 月 TE，截断到 [0.70, 1.40]",
    "momentum": "动能系数 = 2026 年 8–10 月占 1–10 月比重 ÷ 2025 年同期比重，截断到 [0.90, 1.10]",
    "fallback": "该组合 2025 年 1–10 月无出货时，用 2026 年 8–10 月月均，不外推增长",
    "boundary": "只预测 11–12 月；1–10 月一律用实际，不做拟合回填",
}


class CockpitError(Exception):
    """驾驶舱参数错误（对外映射 HTTP 400）。"""


# --------------------------------------------------------------------------- 基础
def _round(v, digits=2):
    return None if v is None else round(float(v), digits)


def _pct(num, den, digits=4):
    if not den:
        return None
    return round(num / den * 100, digits)


def parse_filters(qs: dict) -> dict:
    raw_month = (qs.get("month") or "").strip()
    if raw_month in ("", "all", "0"):
        month = 0
    else:
        try:
            month = int(raw_month)
        except ValueError:
            raise CockpitError(f"month 必须是 1–{OBS_LAST_MONTH} 或 all，收到 {raw_month!r}")
        if not 1 <= month <= OBS_LAST_MONTH:
            raise CockpitError(f"month 超出可观测区间 1–{OBS_LAST_MONTH}：{month}")
    region = (qs.get("region") or "").strip()
    if region and region not in REGIONS:
        raise CockpitError(f"未知战区 {region!r}，可选：{'/'.join(REGIONS)}")
    ka = (qs.get("ka") or "").strip()
    grain = (qs.get("grain") or "month").strip()
    if grain not in GRAINS:
        raise CockpitError(f"grain 只能是 month / quarter，收到 {grain!r}")
    return {"month": month, "region": region, "ka": ka, "grain": grain}


def attach(conn: sqlite3.Connection) -> dict:
    """把 TE 预算库以只读 URI 挂到 ETL 连接上（别名 bte），返回批次信息。"""
    if not config.BUDGET_TE_DB.exists():
        raise CockpitError(
            f"TE 预算库不存在：{config.BUDGET_TE_DB}；请先执行 python3 tools/generate_te_budget.py --write")
    attached = {r[1] for r in conn.execute("PRAGMA database_list")}
    if "bte" not in attached:          # 同一连接上可能被调用多次（如洞察逐战区归因）
        conn.execute("ATTACH DATABASE ? AS bte", (config.budget_te_uri(),))
    row = conn.execute(
        "SELECT batch_id, budget_version, seed, generated_at, annual_budget_te, ytd_budget_te,"
        " ytd_actual_te, ytd_attainment_pct, data_source, unit, unit_scale, notice"
        " FROM bte.te_budget_batch WHERE status='ACTIVE' ORDER BY generated_at DESC LIMIT 1").fetchone()
    if row is None:
        raise CockpitError("TE 预算库里没有 ACTIVE 批次，请重新生成预算")
    return dict(row)


# --------------------------------------------------------------------------- 取数
ERP_SQL = """
SELECT customer_key, customer_id, customer_short AS customer_name,
       bi_region_key, region_name_std AS region_name,
       dept_short, customer_type, channel_type, month,
       SUM(shipment_te) AS te, SUM(sales_amount) AS amt, SUM(gross_profit) AS gp
FROM fact_erp_shipment
WHERE year = ? AND region_scope_status = 'IN_SCOPE'
GROUP BY customer_key, customer_id, customer_short, bi_region_key, region_name_std,
         dept_short, customer_type, channel_type, month
"""


def load(conn: sqlite3.Connection, batch_id: str) -> dict:
    rows26 = [dict(r) for r in conn.execute(ERP_SQL, (YEAR,))]
    rows25 = [dict(r) for r in conn.execute(ERP_SQL, (YEAR - 1,))]
    budgets = [dict(r) for r in conn.execute(
        "SELECT customer_id, bi_region_key, dept_short, month, budget_te, in_observation"
        " FROM bte.fact_te_budget_monthly WHERE batch_id = ?", (batch_id,))]
    if not rows26:
        raise CockpitError("ERP 事实表没有 2026 年数据")
    return {"rows26": rows26, "rows25": rows25, "budgets": budgets}


def _key(r: dict) -> tuple:
    return (r["customer_key"], r["bi_region_key"], r["dept_short"])


def _plus(bucket: dict, key: str, value) -> None:
    bucket[key] = bucket.get(key, 0.0) + (value or 0.0)


# --------------------------------------------------------------------------- 预测
def forecast_entities(data: dict) -> dict:
    """逐组合预测 2026 年 11–12 月 TE。返回 {key: {"m11":x,"m12":y,"meta":{...}}}"""
    te26 = {}
    for r in data["rows26"]:
        te26.setdefault(_key(r), {})[r["month"]] = r["te"] or 0.0
    te25 = {}
    for r in data["rows25"]:
        te25.setdefault(_key(r), {})[r["month"]] = r["te"] or 0.0

    out = {}
    for key, m26 in te26.items():
        m25 = te25.get(key, {})
        a26 = sum(m26.get(m, 0.0) for m in range(1, OBS_LAST_MONTH + 1))
        a25 = sum(m25.get(m, 0.0) for m in range(1, OBS_LAST_MONTH + 1))
        meta = {"scale": None, "momentum": None, "method": "seasonal"}
        if a25 > 0:
            scale = min(max(a26 / a25, 0.70), 1.40)
            s26 = sum(m26.get(m, 0.0) for m in (8, 9, 10)) / a26 if a26 else 0
            s25 = sum(m25.get(m, 0.0) for m in (8, 9, 10)) / a25 if a25 else 0
            momentum = min(max((s26 / s25) if s25 else 1.0, 0.90), 1.10)
            m11 = (m25.get(11, 0.0)) * scale * momentum
            m12 = (m25.get(12, 0.0)) * scale * momentum
            meta.update({"scale": round(scale, 4), "momentum": round(momentum, 4)})
        else:
            avg = sum(m26.get(m, 0.0) for m in (8, 9, 10)) / 3
            m11 = m12 = avg
            meta.update({"method": "recent_average", "scale": 1.0, "momentum": 1.0})
        out[key] = {"m11": m11, "m12": m12, "meta": meta}
    return out


# --------------------------------------------------------------------------- 主查询
def build(conn: sqlite3.Connection, qs: dict) -> dict:
    f = parse_filters(qs)
    batch = attach(conn)
    data = load(conn, batch["batch_id"])

    month = f["month"]
    months = list(range(1, OBS_LAST_MONTH + 1)) if month == 0 else [month]

    known_ids = {r["customer_id"] for r in data["rows26"]}
    if f["ka"] and f["ka"] not in GROUP_KEYS and f["ka"] not in known_ids:
        raise CockpitError(f"未知客户或渠道组 {f['ka']!r}；传 ERP 客户代码，或 {'/'.join(GROUP_KEYS)}")

    def keep(r: dict) -> bool:
        if f["region"] and r["region_name"] != f["region"]:
            return False
        if f["ka"]:
            if f["ka"] in GROUP_KEYS:
                if customer_group(r["customer_type"], r["channel_type"]) != f["ka"]:
                    return False
            elif r["customer_id"] != f["ka"]:
                return False
        return True

    ent = {}
    for r in data["rows26"]:
        if not keep(r):
            continue
        e = ent.setdefault(_key(r), {
            "customer_key": r["customer_key"], "customer_id": r["customer_id"],
            "customer_name": r["customer_name"], "region_name": r["region_name"],
            "bi_region_key": r["bi_region_key"],
            "dept_short": r["dept_short"], "customer_type": r["customer_type"],
            "channel_type": r["channel_type"],
            "group": customer_group(r["customer_type"], r["channel_type"]),
            "te": 0.0, "amt": 0.0, "gp": 0.0, "budget": 0.0,
            "by_month": {}, "budget_by_month": {},
        })
        if r["month"] in months:
            e["te"] += r["te"] or 0.0
            e["amt"] += r["amt"] or 0.0
            e["gp"] += r["gp"] or 0.0
        e["by_month"][r["month"]] = {"te": r["te"] or 0.0, "amt": r["amt"] or 0.0, "gp": r["gp"] or 0.0}

    key_by_cid = {}
    for k, e in ent.items():
        key_by_cid[(e["customer_id"], k[1], k[2])] = k
    for b in data["budgets"]:
        k = key_by_cid.get((b["customer_id"], b["bi_region_key"], b["dept_short"]))
        if k is None:
            continue
        if b["month"] in months:
            ent[k]["budget"] += b["budget_te"] / 1000.0
        ent[k]["budget_by_month"][b["month"]] = b["budget_te"] / 1000.0

    fc = forecast_entities(data)

    # ---------------- KPI 与合计
    te = sum(e["te"] for e in ent.values())
    amt = sum(e["amt"] for e in ent.values())
    gp = sum(e["gp"] for e in ent.values())
    budget = sum(e["budget"] for e in ent.values())
    fc_tail = sum(fc.get(k, {}).get("m11", 0) + fc.get(k, {}).get("m12", 0) for k in ent)
    te_ytd = sum(v["te"] for e in ent.values() for m, v in e["by_month"].items() if m <= OBS_LAST_MONTH)
    amt_ytd = sum(v["amt"] for e in ent.values() for m, v in e["by_month"].items() if m <= OBS_LAST_MONTH)
    gp_ytd = sum(v["gp"] for e in ent.values() for m, v in e["by_month"].items() if m <= OBS_LAST_MONTH)
    budget_annual = sum(v for e in ent.values() for m, v in e["budget_by_month"].items())
    budget_ytd = sum(v for e in ent.values() for m, v in e["budget_by_month"].items() if m <= OBS_LAST_MONTH)
    te_forecast_year = te_ytd + fc_tail

    # 环比：期间内最后一个月 vs 上一个月（上月无数据时不显示）
    def month_sum(field: str, m: int) -> float:
        return sum(e["by_month"].get(m, {}).get(field, 0.0) for e in ent.values())

    last_m = months[-1]
    prev_m = last_m - 1
    mom = {}
    if prev_m >= 1:
        for field in ("te", "amt", "gp"):
            cur, prev = month_sum(field, last_m), month_sum(field, prev_m)
            mom[field] = _pct(cur - prev, prev) if prev else None
        cur_gm = _pct(month_sum("gp", last_m), month_sum("amt", last_m))
        prev_gm = _pct(month_sum("gp", prev_m), month_sum("amt", prev_m))
        mom["gm_pp"] = round(cur_gm - prev_gm, 2) if (cur_gm is not None and prev_gm is not None) else None
    else:
        mom = {"te": None, "amt": None, "gp": None, "gm_pp": None}

    gm = _pct(gp, amt)
    attainment = _pct(te, budget)
    kpi = [
        {"key": "te_attainment", "label": "TE 预算达成率", "value": _round(attainment, 2),
         "unit": "%", "digits": 2, "primary": True,
         "delta": {"value": _round((attainment - 100) if attainment is not None else None, 2),
                   "unit": "pp", "label": "较预算", "direction": "higher_better"},
         "sub": f"实际 {te / 10000:,.1f} 万 TE ÷ 预算 {budget / 10000:,.1f} 万 TE",
         "caliber": "ERP 出货 TE ÷ TE 预算（同期口径）"},
        {"key": "te_actual", "label": "实际出货量（TE）", "value": _round(te, 2), "unit": "TE", "digits": 0,
         "delta": {"value": mom["te"], "unit": "%", "label": "环比", "direction": "higher_better"},
         "sub": f"ERP Sell-in · {_period_label(month)}",
         "caliber": "fact_erp_shipment.shipment_te"},
        {"key": "te_forecast", "label": "销售预测（TE）", "value": _round(te_forecast_year, 2),
         "unit": "TE", "digits": 0,
         "delta": {"value": _pct(te_forecast_year - budget_annual, budget_annual), "unit": "%",
                   "label": "较全年预算", "direction": "higher_better"},
         "sub": f"全年 12 个月 · 已实现 {_round(te_ytd / 10000, 1)} 万 + 11–12 月预测 {_round(fc_tail / 10000, 1)} 万",
         "caliber": "季节性外推 v1（见 forecast.model），非实际"},
        {"key": "erp_amount", "label": "ERP 出货金额", "value": _round(amt, 2), "unit": "元", "digits": 0,
         "delta": {"value": mom["amt"], "unit": "%", "label": "环比", "direction": "higher_better"},
         "sub": f"同期金额口径 · {_period_label(month)}",
         "caliber": "fact_erp_shipment.sales_amount 合计，未做含税/未税换算"},
        {"key": "gross_profit", "label": "毛利额", "value": _round(gp, 2), "unit": "元", "digits": 0,
         "delta": {"value": mom["gp"], "unit": "%", "label": "环比", "direction": "higher_better"},
         "sub": "ERP 出货金额 − 成本金额",
         "caliber": "fact_erp_shipment.gross_profit 合计"},
        {"key": "gross_margin", "label": "毛利率", "value": _round(gm, 2), "unit": "%", "digits": 2,
         "delta": {"value": mom["gm_pp"], "unit": "pp", "label": "环比", "direction": "higher_better"},
         "sub": f"毛利额 {gp / 10000:,.1f} 万 ÷ 金额 {amt / 10000:,.1f} 万",
         "caliber": "Σ毛利 ÷ Σ金额（不加权平均单笔毛利率）"},
    ]

    # ---------------- 战区
    regions = []
    for name in REGIONS:
        items = [e for e in ent.values() if e["region_name"] == name]
        if not items and f["region"] and f["region"] != name:
            continue
        r_te = sum(e["te"] for e in items)
        r_bud = sum(e["budget"] for e in items)
        r_amt = sum(e["amt"] for e in items)
        r_gp = sum(e["gp"] for e in items)
        r_fc = sum(fc.get(k, {}).get("m11", 0) + fc.get(k, {}).get("m12", 0)
                   for k in items_key(items, fc))
        r_ty = sum(v["te"] for e in items for m, v in e["by_month"].items() if m <= OBS_LAST_MONTH)
        regions.append({
            "name": name, "entity_count": len(items),
            "te": _round(r_te, 2), "budget_te": _round(r_bud, 2),
            "attainment_pct": _pct(r_te, r_bud),
            "amount": _round(r_amt, 2), "gross_profit": _round(r_gp, 2),
            "gross_margin_pct": _pct(r_gp, r_amt),
            "forecast_te": _round(r_ty + r_fc, 2),
            "forecast_tail_te": _round(r_fc, 2),
            "share_of_te_pct": _pct(r_te, te),
            "share_of_budget_pct": _pct(r_bud, budget),
            "gap_te": _round(r_te - r_bud, 2),
            "no_data": not items,
        })

    # ---------------- 预算 vs 实际 vs 预测（横向对比）
    compare = [{
        "label": "全公司", "level": "national",
        "budget_te": _round(budget, 2), "actual_te": _round(te, 2),
        "attainment_pct": _pct(te, budget), "forecast_te": _round(te_forecast_year, 2),
        "forecast_year_pct": _pct(te_forecast_year, budget_annual),
        "gap_te": _round(te - budget, 2),
    }]
    for r in regions:
        compare.append({
            "label": r["name"], "level": "region",
            "budget_te": r["budget_te"], "actual_te": r["te"],
            "attainment_pct": r["attainment_pct"], "forecast_te": r["forecast_te"],
            "forecast_year_pct": _pct(r["forecast_te"], _annual_budget(items_of(ent, r["name"]))),
            "gap_te": r["gap_te"],
        })

    # ---------------- 趋势
    trend_months = []
    for m in range(1, 13):
        a_te = month_sum("te", m) if m <= OBS_LAST_MONTH else 0.0
        a_amt = month_sum("amt", m) if m <= OBS_LAST_MONTH else 0.0
        b_te = sum(e["budget_by_month"].get(m, 0.0) for e in ent.values())
        f_te = 0.0 if m <= OBS_LAST_MONTH else sum(fc.get(k, {}).get(f"m{m}", 0.0) for k in ent)
        trend_months.append({
            "period": m, "label": f"{m} 月",
            "actual_te": _round(a_te, 2) if m <= OBS_LAST_MONTH else None,
            "forecast_te": _round(f_te, 2) if m > OBS_LAST_MONTH else None,
            "te": _round(a_te if m <= OBS_LAST_MONTH else f_te, 2),
            "budget_te": _round(b_te, 2),
            "amount": _round(a_amt, 2) if m <= OBS_LAST_MONTH else None,
            "occurred": m <= OBS_LAST_MONTH,
            "selected": (month == 0 and m == OBS_LAST_MONTH) or (month == m),
        })

    quarters = []
    for q in range(1, 5):
        qm = [m for m in range(3 * q - 2, 3 * q + 1)]
        q_bud = sum(t["budget_te"] or 0 for t in trend_months if t["period"] in qm)
        obs = [t for t in trend_months if t["period"] in qm and t["occurred"]]
        fcst = [t for t in trend_months if t["period"] in qm and not t["occurred"]]
        q_act = sum(t["actual_te"] or 0 for t in obs)
        q_fc = sum(t["forecast_te"] or 0 for t in fcst)
        quarters.append({
            "period": q, "label": f"Q{q}",
            "actual_te": _round(q_act, 2), "forecast_te": _round(q_fc, 2),
            "budget_te": _round(q_bud, 2),
            "budget_observed_te": _round(sum(t["budget_te"] or 0 for t in obs), 2),
            "attainment_pct": _pct(q_act, sum(t["budget_te"] or 0 for t in obs)),
            "partial": len(obs) < 3,
        })

    # ---------------- 渠道分组（ERP 客户级）：商超 KA / 电商 / 批发
    # 三组按 channel_type 互斥拆分，合计 = 当前范围；KA 榜只取商超，批发单独成列，
    # 电商只在结构条里汇总（不列客户），避免「批发客户被当成 KA」。
    cust_rows = {}
    for e in ent.values():
        cid = e["customer_id"]
        row = cust_rows.setdefault(cid, {
            "customer_id": cid, "name": e["customer_name"],
            "customer_type": e["customer_type"], "channel_type": e["channel_type"],
            "group": e["group"], "regions": set(), "by_region": {},
            "te": 0.0, "amount": 0.0, "gp": 0.0, "budget": 0.0, "tail_te": 0.0,
        })
        row["regions"].add(e["region_name"])
        row["by_region"][e["region_name"]] = row["by_region"].get(e["region_name"], 0.0) + (e["te"] or 0.0)
        row["te"] += e["te"]
        row["amount"] += e["amt"]
        row["gp"] += e["gp"]
        row["budget"] += e["budget"]
        row["tail_te"] += fc.get(_key(e), {}).get("m11", 0) + fc.get(_key(e), {}).get("m12", 0)

    for r in cust_rows.values():
        r["regions"] = sorted(r["regions"])
        r["region_count"] = len(r["regions"])
        # 单客户下钻时用：该客户在各战区的 TE 结构（占比以该客户自身为分母）
        raw_te = r["te"]
        r["by_region"] = [{"name": n, "te": _round(v, 2), "share_pct": _pct(v, raw_te)}
                          for n, v in sorted(r["by_region"].items(), key=lambda kv: -kv[1])]
        r["share_pct"] = _pct(r["te"], te)
        r["share_amount_pct"] = _pct(r["amount"], amt)
        r["gross_margin_pct"] = _pct(r["gp"], r["amount"])
        r["attainment_pct"] = _pct(r["te"], r["budget"])
        r["forecast_te"] = _round(r["te"] + r["tail_te"], 2)
        r["te"] = _round(r["te"], 2)
        r["amount"] = _round(r["amount"], 2)
        r["gp"] = _round(r["gp"], 2)
        r["budget"] = _round(r["budget"], 2)

    channel_groups = []
    for g in CUSTOMER_GROUPS:
        items = sorted((r for r in cust_rows.values() if r["group"] == g["key"]),
                       key=lambda r: -(r["te"] or 0))
        g_te = sum(r["te"] or 0 for r in items)
        g_amt = sum(r["amount"] or 0 for r in items)
        g_gp = sum(r["gp"] or 0 for r in items)
        g_bud = sum(r["budget"] or 0 for r in items)
        channel_groups.append({
            "key": g["key"], "label": g["label"], "basis": g["basis"],
            "customers": len(items),
            "te": _round(g_te, 2), "amount": _round(g_amt, 2), "gross_profit": _round(g_gp, 2),
            "gross_margin_pct": _pct(g_gp, g_amt), "attainment_pct": _pct(g_te, g_bud),
            "share_pct": _pct(g_te, te),
            "items": items,
        })
    ka_list = next(x["items"] for x in channel_groups if x["key"] == "ka")
    ws_group = next(x for x in channel_groups if x["key"] == "ws")
    top3_share = round(sum(x["share_pct"] or 0 for x in ka_list[:3]), 2) if ka_list else None

    # ---------------- 利润
    profit_regions = [{
        "name": r["name"], "gross_profit": r["gross_profit"], "amount": r["amount"],
        "gross_margin_pct": r["gross_margin_pct"],
        "share_of_gp_pct": _pct(r["gross_profit"], gp),
        "gap_pp": (round(r["gross_margin_pct"] - gm, 2)
                   if r["gross_margin_pct"] is not None and gm is not None else None),
    } for r in regions if not r["no_data"]]

    alerts = _alerts(regions, ka_list, trend_months, te_forecast_year, budget_annual,
                     attainment, gm, month)

    if f["ka"] in GROUP_KEYS:
        ka_label = next(g["label"] for g in CUSTOMER_GROUPS if g["key"] == f["ka"])
    elif f["ka"]:
        ka_label = next((r["name"] for r in cust_rows.values() if r["customer_id"] == f["ka"]), f["ka"])
    else:
        ka_label = "全部客户"

    payload = {
        "ok": True,
        "meta": {
            "year": YEAR,
            "period_label": _period_label(month),
            "month": month,
            "region": f["region"] or "全部",
            "ka": f["ka"] or "全部",
            "ka_label": ka_label,
            "grain": f["grain"],
            "entity_count": len(ent),
            "data_from": "fact_erp_shipment（ERP Sell-in，只读）",
            "budget_source": config.BUDGET_TE_DATA_SOURCE,
            "budget_batch": {k: batch[k] for k in ("batch_id", "budget_version", "seed", "generated_at")},
            "budget_notice": batch["notice"],
            "pos_excluded": True,
            "excluded_note": "本页不含 POS Sell-out；POS 与 ERP 各自核算，金额与数量永不相加。",
            "forecast": FORECAST_MODEL,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        },
        "data": {
            "kpi": kpi,
            "totals": {
                "te": _round(te, 2), "budget_te": _round(budget, 2),
                "attainment_pct": _pct(te, budget),
                "amount": _round(amt, 2), "gross_profit": _round(gp, 2),
                "gross_margin_pct": gm,
                "te_ytd": _round(te_ytd, 2), "budget_annual_te": _round(budget_annual, 2),
                "budget_ytd_te": _round(budget_ytd, 2),
                "te_forecast_year": _round(te_forecast_year, 2),
                "forecast_tail_te": _round(fc_tail, 2),
                "annual_progress_pct": _pct(te_ytd, budget_annual),
                "te_share_top3_pct": top3_share,
                "erp_amount_ytd": _round(amt_ytd, 2), "gross_profit_ytd": _round(gp_ytd, 2),
            },
            "region": {"items": regions, "max_te": max((r["te"] or 0) for r in regions) if regions else 0},
            "compare": compare,
            "trend": {"months": trend_months, "quarters": quarters},
            "ka": {"items": ka_list[:10], "total": len(ka_list), "top3_share_pct": top3_share,
                   "label": "KA 客户", "basis": "customer_type = NKA",
                   "caliber": "KA = ERP customer_type NKA（7 家）；批发与区域商超单列，不混入 KA 榜"},
            "wholesale": {"items": ws_group["items"], "total": ws_group["customers"],
                          "te": ws_group["te"], "share_pct": ws_group["share_pct"],
                          "gross_margin_pct": ws_group["gross_margin_pct"],
                          "attainment_pct": ws_group["attainment_pct"], "label": "批发"},
            "channel": {"groups": channel_groups, "denominator": "占当前范围 TE",
                        "note": "KA（NKA）/ 区域商超 / 批发互斥分组，合计 = 当前范围"},
            "profit": {"national": {"gross_profit": _round(gp, 2), "amount": _round(amt, 2),
                                    "gross_margin_pct": gm},
                       "regions": profit_regions},
            "alerts": alerts,
        },
    }
    return payload


def items_key(items: list[dict], fc: dict) -> list[tuple]:
    return [(e["customer_key"], e["bi_region_key"], e["dept_short"]) for e in items]


def items_of(ent: dict, region: str) -> list[dict]:
    return [e for e in ent.values() if e["region_name"] == region]


def _annual_budget(items: list[dict]) -> float:
    return sum(e["budget_by_month"].get(m, 0.0) for e in items for m in range(1, 13))


def _period_label(month: int) -> str:
    return "2026 年 1–10 月累计" if month == 0 else f"2026 年 {month} 月"


# --------------------------------------------------------------------------- 异常规则
def _alerts(regions, ka_list, months, forecast_te, budget_annual, attainment, gm, month) -> list[dict]:
    out = []

    for r in regions:
        if r["no_data"] or r["attainment_pct"] is None:
            continue
        if r["attainment_pct"] < 90:
            out.append({
                "level": "high", "type": "attainment",
                "title": f"{r['name']} TE 达成率 {r['attainment_pct']:.1f}%",
                "evidence": (f"实际 {r['te'] / 10000:,.1f} 万 TE / 预算 {r['budget_te'] / 10000:,.1f} 万 TE，"
                             f"缺口 {abs(r['gap_te']) / 10000:,.1f} 万 TE"),
                "metric": r["attainment_pct"], "dimension": {"region": r["name"]},
            })
        elif r["attainment_pct"] < 95:
            out.append({
                "level": "mid", "type": "attainment",
                "title": f"{r['name']} TE 达成率 {r['attainment_pct']:.1f}%，低于 95%",
                "evidence": (f"缺口 {abs(r['gap_te']) / 10000:,.1f} 万 TE；"
                             f"该战区占全国预算 {r['share_of_budget_pct']:.1f}%"),
                "metric": r["attainment_pct"], "dimension": {"region": r["name"]},
            })

    for r in ka_list[:10]:
        if r["attainment_pct"] is not None and r["attainment_pct"] < 85 and (r["share_pct"] or 0) >= 2:
            out.append({
                "level": "high", "type": "ka",
                "title": f"{r['name']} TE 达成率 {r['attainment_pct']:.1f}%",
                "evidence": (f"实际 {r['te'] / 10000:,.1f} 万 TE / 预算 {r['budget'] / 10000:,.1f} 万 TE，"
                             f"占全国 TE {r['share_pct']:.1f}%"),
                "metric": r["attainment_pct"], "dimension": {"ka": r["customer_id"]},
            })

    if gm is not None:
        for r in regions:
            if r["no_data"] or r["gross_margin_pct"] is None:
                continue
            gap = r["gross_margin_pct"] - gm
            if abs(gap) >= 0.8:
                out.append({
                    "level": "mid", "type": "margin",
                    "title": f"{r['name']} 毛利率 {r['gross_margin_pct']:.1f}%，偏离全国 {gap:+.1f} 个百分点",
                    "evidence": (f"毛利额 {r['gross_profit'] / 10000:,.1f} 万 / 金额 "
                                 f"{r['amount'] / 10000:,.1f} 万；全国 {gm:.1f}%"),
                    "metric": r["gross_margin_pct"], "dimension": {"region": r["name"]},
                })

    obs = [m for m in months if m["occurred"]]
    if len(obs) >= 2:
        last, prev = obs[-1], obs[-2]
        if prev["actual_te"]:
            chg = (last["actual_te"] - prev["actual_te"]) / prev["actual_te"] * 100
            if abs(chg) >= 12:
                out.append({
                    "level": "mid" if chg < 0 else "low", "type": "trend",
                    "title": f"{last['period']} 月 TE 环比 {chg:+.1f}%（{prev['period']} 月 {prev['actual_te'] / 10000:,.1f} 万 → {last['actual_te'] / 10000:,.1f} 万）",
                    "evidence": "相邻自然月环比，不做跨年比较",
                    "metric": round(chg, 2), "dimension": {},
                })

    if budget_annual and forecast_te < budget_annual * 0.99:
        gap = budget_annual - forecast_te
        out.append({
            "level": "high", "type": "forecast",
            "title": f"全年预测低于预算 {gap / 10000:,.1f} 万 TE",
            "evidence": (f"预测 {forecast_te / 10000:,.1f} 万 TE（已实现 + 11–12 月外推）/ "
                         f"全年预算 {budget_annual / 10000:,.1f} 万 TE"),
            "metric": _pct(forecast_te, budget_annual), "dimension": {},
        })

    order = {"high": 0, "mid": 1, "low": 2}
    out.sort(key=lambda a: (order[a["level"]], a["type"]))
    return out[:6]


# --------------------------------------------------------------------------- 筛选选项
def options(conn: sqlite3.Connection) -> dict:
    rows = list(conn.execute(
        "SELECT DISTINCT customer_id, customer_short, customer_type, channel_type FROM fact_erp_shipment"
        " WHERE year=? AND region_scope_status='IN_SCOPE' ORDER BY customer_short", (YEAR,)))
    te_rows = {r[0]: r[1] for r in conn.execute(
        "SELECT customer_id, SUM(shipment_te) FROM fact_erp_shipment"
        " WHERE year=? AND region_scope_status='IN_SCOPE' GROUP BY 1", (YEAR,))}
    kas = [{"customer_id": r[0], "label": r[1], "type": r[2],
            "channel_type": r[3], "group": customer_group(r[2], r[3]),
            "te": round(te_rows.get(r[0], 0), 2)}
           for r in rows]
    kas.sort(key=lambda x: -x["te"])
    groups = []
    for g in CUSTOMER_GROUPS:
        members = [x for x in kas if x["group"] == g["key"]]
        groups.append({**g, "customers": len(members),
                       "te": round(sum(x["te"] for x in members), 2)})
    return {
        "ok": True,
        "data": {
            "years": [{"value": YEAR, "label": "2026（Y26）", "selectable": False,
                       "note": "数据库仅覆盖 2026 自然年，跨年对比不在本版范围"}],
            "months": [{"value": 0, "label": "全部（1–10 月累计）"}] +
                      [{"value": m, "label": f"{m} 月"} for m in range(1, OBS_LAST_MONTH + 1)],
            "regions": [{"value": r, "label": r} for r in REGIONS],
            "kas": kas,
            "groups": groups,
            "defaults": {"year": YEAR, "month": 0, "region": "", "ka": ""},
        },
        "meta": {"generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                 "ka_note": "KA = ERP customer_type NKA（7 家）；批发与区域商超单列。客户按 ERP 客户代码归集"},
    }
