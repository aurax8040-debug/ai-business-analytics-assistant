"""模拟销售预算 V1 查询层（MOCK_BUDGET，全只读）。

设计边界
--------
1. 预算库 ``data/budget_v1.db`` 与 ETL 库 ``bi_data_layer_v1.db`` 物理分离，
   两个库都以 ``mode=ro`` 打开，本模块不提供任何写入路径（模块内不出现 INSERT/UPDATE）。
2. 实际数一律来自 ERP 出货事实表 ``fact_erp_shipment``（Sell-in），
   绝不与 POS（Sell-out）金额相加，也不做跨表推断。
3. 双口径固定并存，接口里不合并成一个数：
   - **同期达成率** = 2026 年 1–10 月实际 ÷ 1–10 月预算（主指标）
   - **全年预算进度** = 2026 年 1–10 月实际 ÷ 全年 12 个月预算（辅助指标，10/12 期）
4. 11–12 月有预算、无实际：``actual_amount = null``、``attainment_pct = null``、
   ``occurred = false``，不补造 0 以外的任何数字，也不参与任何达成率计算。
5. 每个响应都带 ``meta.data_source = 'MOCK_BUDGET'`` 与不可关闭的提示语。
"""
from __future__ import annotations

import json
import sqlite3

from . import config
from .db import file_md5

BANDS = ("OVER", "ON", "UNDER", "SEVERE")
BAND_LABEL = {"OVER": "超额完成", "ON": "基本达成", "UNDER": "未达成", "SEVERE": "严重未达成"}
BAND_LIMIT = {"OVER": (105.0, None), "ON": (95.0, 105.0),
              "UNDER": (80.0, 95.0), "SEVERE": (55.0, 80.0)}

DIMS = {
    "region": ("战区", "region_name", "region_name_std", "bi_region_key"),
    "dept": ("市场部", "dept_short", "dept_short", "md_key"),
    "channel": ("渠道", "channel_type", "channel_type", "channel_key"),
    "tier": ("客户规模层", None, None, None),
}
DIM_NOTE = {
    "region": "P01 五大战区，不含战区F、战区G",
    "dept": "15 个市场部（ERP dept_short，与 dim_market_department 一一对应）",
    "channel": "ERP 渠道类型（商超/终端、批发、电商），与 POS 无关",
    "tier": "按同期实际金额分层：A ≥ ￥5,000 万、B ￥500–5,000 万、C < ￥500 万",
}

CUSTOMER_SORT = {
    "attainment_pct": "attainment_pct",
    "annual_progress_pct": "annual_progress_pct",
    "ytd_actual": "ytd_actual",
    "ytd_budget": "ytd_budget",
    "annual_budget": "annual_budget",
    "variance": "variance",
    "share_of_annual_budget_pct": "share_of_annual_budget_pct",
    "customer_name": "customer_name",
    "region_name": "region_name",
    "dept_short": "dept_short",
    "tier": "tier",
    "band": "band",
}

MAX_LIMIT = 200


class BudgetError(Exception):
    """预算查询参数错误（对外映射为 HTTP 400）。"""


# ---------------------------------------------------------------------------
# 连接与批次
# ---------------------------------------------------------------------------
def attach(conn: sqlite3.Connection) -> None:
    """把预算库以只读 URI 挂到现有 ETL 连接上（别名 bud）。"""
    if not config.BUDGET_DB.exists():
        raise BudgetError(
            f"预算库不存在：{config.BUDGET_DB}；请先执行 python3 tools/generate_budget.py --write")
    names = [r[0] for r in conn.execute("PRAGMA database_list")]
    if "bud" not in names:
        conn.execute("ATTACH DATABASE ? AS bud", (config.budget_uri(),))


def _batch(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM bud.budget_batch WHERE status='ACTIVE' "
        "ORDER BY generated_at DESC LIMIT 1").fetchone()
    if row is None:
        raise BudgetError("预算库中没有 ACTIVE 批次，请重新生成预算数据")
    return row


def _meta(conn: sqlite3.Connection, batch: sqlite3.Row, extra: dict | None = None) -> dict:
    """统一 meta：数据来源声明、双口径定义、批次可追溯信息。"""
    params = json.loads(batch["params_json"])
    erp_md5 = file_md5(config.ETL_DB)
    meta = {
        "data_source": config.BUDGET_DATA_SOURCE,
        "mock_budget": True,
        "notice": batch["notice"] or config.BUDGET_NOTICE,
        "batch": {
            "batch_id": batch["batch_id"],
            "budget_version": batch["budget_version"],
            "year": batch["year"],
            "seed": batch["seed"],
            "generated_at": batch["generated_at"],
            "generator": batch["generator"],
            "generator_version": batch["generator_version"],
            "status": batch["status"],
            "entity_count": batch["entity_count"],
            "segment_count": batch["segment_count"],
            "params": params,
        },
        "attainment_definition": {
            "primary": "同期达成率 = 2026 年 1–10 月 ERP 实际出货金额 ÷ 同期（1–10 月）预算",
            "secondary": "全年预算进度 = 2026 年 1–10 月 ERP 实际出货金额 ÷ 全年 12 个月预算（10/12 期）",
            "variance": "差异金额 = 同期实际 − 同期预算；负数为缺口",
            "band_limit": {b: {"low": BAND_LIMIT[b][0], "high": BAND_LIMIT[b][1],
                               "label": BAND_LABEL[b]} for b in BANDS},
            "note": "11–12 月有预算、无实际，标记未发生，不参与达成率；两种口径不合并、不相加",
        },
        "grain": "customer_key × bi_region_key × md_key（客户 × 战区 × 市场部）",
        "currency": "CNY",
        "data_protection": {
            "erp_db_md5_now": erp_md5,
            "erp_db_md5_at_generation": batch["erp_db_md5"],
            "erp_db_unchanged": erp_md5 == batch["erp_db_md5"],
            "budget_db": str(config.BUDGET_DB),
            "open_mode": "ETL 库与预算库均以 sqlite3 uri mode=ro 打开；预算库以只读 ATTACH 挂在 bud 别名下",
            "write_routes": [],
        },
        "scope_note": config.SCOPE_NOTE,
    }
    if extra:
        meta.update(extra)
    return meta


# ---------------------------------------------------------------------------
# 实际数（ERP Sell-in，唯一来源）
# ---------------------------------------------------------------------------
_ACT_WHERE = ("year = :year AND month BETWEEN 1 AND 10 AND region_scope_status = 'IN_SCOPE'")


def _actual_scope(conn: sqlite3.Connection, year: int) -> dict:
    row = conn.execute(
        f"SELECT COUNT(*) AS rows, ROUND(SUM(sales_amount), 2) AS amount, "
        f"COUNT(DISTINCT customer_key || '|' || bi_region_key || '|' || dept_short) AS entities "
        f"FROM fact_erp_shipment WHERE {_ACT_WHERE}", {"year": year}).fetchone()
    return {"rows": row["rows"], "amount": row["amount"], "entities": row["entities"]}


def _actual_entity_month(conn: sqlite3.Connection, year: int) -> dict[tuple, int]:
    """(customer_key, region_key, md_key, month) → 实际金额（分）。

    ERP 事实表只到 dept_short，md_key 由 dim_market_department（source_system='ERP'）
    映射，先在 Python 侧建立唯一映射，避免 JOIN 放大行数导致金额翻倍。
    """
    md_map = {}
    for r in conn.execute(
            "SELECT md_name_std, md_key FROM dim_market_department WHERE source_system='ERP'"):
        md_map.setdefault(r["md_name_std"], r["md_key"])
    rows = conn.execute(
        "SELECT customer_key, bi_region_key, dept_short, month, SUM(sales_amount) AS amt "
        f"FROM fact_erp_shipment WHERE {_ACT_WHERE} GROUP BY 1,2,3,4", {"year": year}).fetchall()
    out: dict[tuple, int] = {}
    for r in rows:
        md_key = md_map.get(r["dept_short"])
        if md_key is None:
            continue          # 不存在映射时宁可不配对，也不猜
        key = (r["customer_key"], r["bi_region_key"], md_key, r["month"])
        out[key] = out.get(key, 0) + _cents(r["amt"])
    return out


def _cents(amount) -> int:
    return 0 if amount is None else int(round(float(amount) * 100))


def _as_cents(value) -> int | None:
    """已经是「分」的整数，只做类型归一，不再乘 100。"""
    return None if value is None else int(value)


def _yuan(cents) -> float:
    return None if cents is None else round(int(cents) / 100, 2)


def _rate(num, den):
    if den in (None, 0) or num is None:
        return None
    return round(float(num) / float(den) * 100, 4)


# ---------------------------------------------------------------------------
# ① /api/budget/meta —— 批次与口径声明
# ---------------------------------------------------------------------------
def meta(conn: sqlite3.Connection, qs: dict | None = None) -> dict:
    attach(conn)
    batch = _batch(conn)
    tables = {
        "budget_batch": conn.execute("SELECT COUNT(*) FROM bud.budget_batch").fetchone()[0],
        "dim_budget_calendar": conn.execute("SELECT COUNT(*) FROM bud.dim_budget_calendar").fetchone()[0],
        "dim_budget_week_segment": conn.execute("SELECT COUNT(*) FROM bud.dim_budget_week_segment").fetchone()[0],
        "fact_budget_weekly": conn.execute("SELECT COUNT(*) FROM bud.fact_budget_weekly").fetchone()[0],
        "fact_budget_monthly": conn.execute("SELECT COUNT(*) FROM bud.fact_budget_monthly").fetchone()[0],
        "fact_budget_quarterly": conn.execute("SELECT COUNT(*) FROM bud.fact_budget_quarterly").fetchone()[0],
        "fact_budget_yearly": conn.execute("SELECT COUNT(*) FROM bud.fact_budget_yearly").fetchone()[0],
        "budget_assumption": conn.execute("SELECT COUNT(*) FROM bud.budget_assumption").fetchone()[0],
    }
    data = {
        "batch": {
            "batch_id": batch["batch_id"],
            "budget_version": batch["budget_version"],
            "year": batch["year"],
            "seed": batch["seed"],
            "generated_at": batch["generated_at"],
            "generator": batch["generator"],
            "generator_version": batch["generator_version"],
            "status": batch["status"],
            "entity_count": batch["entity_count"],
            "segment_count": batch["segment_count"],
            "notice": batch["notice"],
        },
        "totals": {
            "annual_budget": _yuan(batch["annual_budget_cents"]),
            "ytd_budget": _yuan(batch["ytd_budget_cents"]),
            "ytd_actual": _yuan(batch["ytd_actual_cents"]),
            "ytd_attainment_pct": batch["ytd_attainment_pct"],
            "annual_progress_pct": batch["annual_progress_pct"],
            "remaining_budget": _yuan(batch["annual_budget_cents"] - batch["ytd_actual_cents"]),
        },
        "distribution": {
            "bands": json.loads(batch["band_distribution_json"]),
            "tiers": json.loads(batch["tier_distribution_json"]),
        },
        "tables": tables,
        "param_keys": sorted(json.loads(batch["params_json"]).keys()),
        "endpoints": sorted(BUDGET_ROUTES_DOC),
    }
    return {"ok": True, "data": data, "meta": _meta(conn, batch)}


# ---------------------------------------------------------------------------
# ② /api/budget/summary —— 全国 / 战区 / 市场部 / 渠道 / 规模层
# ---------------------------------------------------------------------------
def summary(conn: sqlite3.Connection, qs: dict) -> dict:
    attach(conn)
    batch = _batch(conn)
    batch_id = batch["batch_id"]
    year = batch["year"]
    dim = (qs.get("dim") or "region").strip()
    if dim not in DIMS:
        raise BudgetError(f"不支持的 dim 取值：{dim}；可选 {sorted(DIMS)}")

    annual = conn.execute(
        "SELECT SUM(budget_amount_cents) AS c FROM bud.fact_budget_yearly WHERE batch_id=?",
        (batch_id,)).fetchone()["c"]
    ytd = conn.execute(
        "SELECT SUM(budget_amount_cents) AS c FROM bud.fact_budget_monthly "
        "WHERE batch_id=? AND in_observation='Y'", (batch_id,)).fetchone()["c"]
    rem = conn.execute(
        "SELECT SUM(budget_amount_cents) AS c FROM bud.fact_budget_monthly "
        "WHERE batch_id=? AND in_observation='N'", (batch_id,)).fetchone()["c"]
    act = _actual_scope(conn, year)

    national = {
        "label": "全国（五大战区合计）",
        "entity_count": batch["entity_count"],
        "annual_budget": _yuan(annual),
        "ytd_budget": _yuan(ytd),
        "ytd_actual": act["amount"],
        "variance": round(act["amount"] - _yuan(ytd), 2),
        "attainment_pct": _rate(_cents(act["amount"]), ytd),
        "annual_progress_pct": _rate(_cents(act["amount"]), annual),
        "unoccurred_budget": _yuan(rem),
        "unoccurred_months": [11, 12],
    }

    # 预算侧按维度汇总（1–10 月同期 + 全年）
    label, bud_col, act_col, key_col = DIMS[dim]
    if dim == "tier":
        rows = _tier_breakdown(conn, batch_id, year)
    else:
        rows = _dim_breakdown(conn, batch_id, year, bud_col, act_col, key_col)

    members = []
    for r in rows:
        # 这两列在 SQL 里已经是「分」，不能再走 _cents（否则放大 100 倍）
        ytd_b = _as_cents(r["ytd_budget"])
        annual_b = _as_cents(r["annual_budget"])
        ytd_a = r["ytd_actual"]
        members.append({
            "key": r["k"],
            "label": r["label"],
            "entity_count": r["entity_count"],
            "annual_budget": _yuan(annual_b),
            "ytd_budget": _yuan(ytd_b),
            "ytd_actual": None if ytd_a is None else round(ytd_a, 2),
            "variance": None if ytd_a is None or ytd_b is None else round(ytd_a - _yuan(ytd_b), 2),
            "attainment_pct": None if ytd_a is None else _rate(_cents(ytd_a), ytd_b),
            "annual_progress_pct": None if ytd_a is None else _rate(_cents(ytd_a), annual_b),
            "share_of_annual_budget_pct": _rate(annual_b, annual),
            "budget_only": ytd_a is None,
        })
    members.sort(key=lambda m: -(m["ytd_budget"] or 0))

    band_rows = conn.execute(
        "SELECT target_band AS band, COUNT(*) AS n, SUM(ytd_actual_cents) AS act, "
        "SUM(ytd_budget_cents) AS bud FROM bud.budget_assumption WHERE batch_id=? "
        "GROUP BY 1", (batch_id,)).fetchall()
    by_band = {r["band"]: r for r in band_rows}
    bands = []
    for b in BANDS:
        r = by_band.get(b)
        n = r["n"] if r else 0
        bands.append({
            "band": b, "label": BAND_LABEL[b],
            "low": BAND_LIMIT[b][0], "high": BAND_LIMIT[b][1],
            "entity_count": n,
            "entity_share_pct": _rate(n, batch["entity_count"]),
            "ytd_actual": _yuan(r["act"]) if r else None,
            "ytd_budget": _yuan(r["bud"]) if r else None,
            "attainment_pct": _rate(r["act"], r["bud"]) if r else None,
        })

    data = {
        "national": national,
        "breakdown": {"dim": dim, "label": label, "note": DIM_NOTE[dim],
                      "member_count": len(members), "members": members},
        "bands": bands,
    }
    return {"ok": True, "data": data,
            "meta": _meta(conn, batch, {"dim": dim})}


def _dim_breakdown(conn, batch_id, year, bud_col, act_col, key_col):
    """预算侧与 ERP 侧各自按同一维度聚合，再按维度键对齐。

    两侧都不做业务推断：预算里没有的维度成员不会凭空出现，
    ERP 里没有的维度成员预算侧也不会补 0。
    """
    bud = conn.execute(
        f"SELECT {bud_col} AS k, SUM(CASE WHEN in_observation='Y' "
        f"  THEN budget_amount_cents ELSE 0 END) AS ytd_budget, "
        f"  SUM(budget_amount_cents) AS annual_budget, "
        f"  COUNT(DISTINCT customer_key || '|' || bi_region_key || '|' || md_key) AS entity_count "
        f"FROM bud.fact_budget_monthly WHERE batch_id=? GROUP BY 1", (batch_id,)).fetchall()
    act = conn.execute(
        f"SELECT {act_col} AS k, ROUND(SUM(sales_amount), 2) AS ytd_actual "
        f"FROM fact_erp_shipment WHERE {_ACT_WHERE} GROUP BY 1", {"year": year}).fetchall()
    act_map = {r["k"]: r["ytd_actual"] for r in act}
    bud_keys = {r["k"] for r in bud}
    out = [{"k": r["k"], "label": r["k"], "entity_count": r["entity_count"],
            "ytd_budget": r["ytd_budget"], "annual_budget": r["annual_budget"],
            "ytd_actual": act_map.get(r["k"])} for r in bud]
    # ERP 有、预算无的成员：显式列出并标 actual_only，不补 0 预算、不算达成率
    for k, v in act_map.items():
        if k not in bud_keys:
            out.append({"k": k, "label": k, "entity_count": 0, "ytd_budget": None,
                        "annual_budget": None, "ytd_actual": v})
    return out


def _tier_breakdown(conn, batch_id, year):
    rows = conn.execute(
        "SELECT tier, COUNT(*) AS entity_count, SUM(ytd_budget_cents) AS ytd_budget, "
        "SUM(annual_budget_cents) AS annual_budget, SUM(ytd_actual_cents) AS ytd_actual "
        "FROM bud.budget_assumption WHERE batch_id=? GROUP BY 1", (batch_id,)).fetchall()
    label = {"A": "A 层 ≥ ￥5,000 万", "B": "B 层 ￥500–5,000 万", "C": "C 层 < ￥500 万"}
    return [{"k": r["tier"], "label": label.get(r["tier"], r["tier"]),
             "entity_count": r["entity_count"], "ytd_budget": r["ytd_budget"],
             "annual_budget": r["annual_budget"],
             "ytd_actual": _yuan(r["ytd_actual"])} for r in rows]


# ---------------------------------------------------------------------------
# ③ /api/budget/customer —— 80 个客户组织组合
# ---------------------------------------------------------------------------
def customer(conn: sqlite3.Connection, qs: dict) -> dict:
    attach(conn)
    batch = _batch(conn)
    batch_id = batch["batch_id"]
    year = batch["year"]

    band = (qs.get("band") or "").strip().upper()
    if band and band not in BANDS:
        raise BudgetError(f"band 只能是 {list(BANDS)} 之一或留空")
    tier = (qs.get("tier") or "").strip().upper()
    if tier and tier not in ("A", "B", "C"):
        raise BudgetError("tier 只能是 A / B / C 之一或留空")
    region = (qs.get("region") or "").strip()
    dept = (qs.get("dept") or "").strip()
    keyword = (qs.get("q") or "").strip()

    sort_key = (qs.get("sort") or "ytd_actual").strip()
    if sort_key not in CUSTOMER_SORT:
        raise BudgetError(f"sort 只能是 {sorted(CUSTOMER_SORT)} 之一")
    order = (qs.get("order") or "desc").strip().lower()
    if order not in ("asc", "desc"):
        raise BudgetError("order 只能是 asc / desc")
    limit = _int_in_range(qs.get("limit"), 1, MAX_LIMIT, 80)
    offset = _int_in_range(qs.get("offset"), 0, 100000, 0)

    act = _actual_entity_month(conn, year)
    act_tot: dict[tuple, int] = {}
    for (ck, rk, mk, _m), v in act.items():
        act_tot[(ck, rk, mk)] = act_tot.get((ck, rk, mk), 0) + v

    rows = conn.execute(
        "SELECT a.customer_key, a.customer_id, a.customer_name, a.bi_region_key, a.region_name, "
        "       a.md_key, a.dept_short, a.channel_type, a.tier, a.target_band, a.target_attainment, "
        "       a.band_low_pct, a.band_high_pct, a.actual_months, a.baseline_note, "
        "       a.ytd_actual_cents, a.ytd_budget_cents, a.attainment_pct, a.annual_budget_cents, "
        "       a.implied_growth_pct, "
        "       (SELECT SUM(budget_amount_cents) FROM bud.fact_budget_monthly m "
        "         WHERE m.batch_id=a.batch_id AND m.customer_key=a.customer_key "
        "           AND m.bi_region_key=a.bi_region_key AND m.md_key=a.md_key "
        "           AND m.in_observation='N') AS unoccurred_cents "
        "FROM bud.budget_assumption a WHERE a.batch_id=?", (batch_id,)).fetchall()

    annual_total = conn.execute(
        "SELECT SUM(budget_amount_cents) AS c FROM bud.fact_budget_yearly WHERE batch_id=?",
        (batch_id,)).fetchone()["c"]

    items = []
    for r in rows:
        if band and r["target_band"] != band:
            continue
        if tier and r["tier"] != tier:
            continue
        if region and r["region_name"] != region:
            continue
        if dept and r["dept_short"] != dept:
            continue
        if keyword and keyword not in (r["customer_name"] or "") and keyword not in (r["customer_id"] or ""):
            continue
        actual_cents = act_tot.get((r["customer_key"], r["bi_region_key"], r["md_key"]))
        actual_yuan = _yuan(actual_cents)
        ytd_budget = _yuan(r["ytd_budget_cents"])
        items.append({
            "customer_id": r["customer_id"],
            "customer_name": r["customer_name"],
            "region_name": r["region_name"],
            "dept_short": r["dept_short"],
            "channel_type": r["channel_type"],
            "tier": r["tier"],
            "band": r["target_band"],
            "band_label": BAND_LABEL[r["target_band"]],
            "target_attainment_pct": round(r["target_attainment"] * 100, 4),
            "actual_months": r["actual_months"],
            "annual_budget": _yuan(r["annual_budget_cents"]),
            "ytd_budget": ytd_budget,
            # 实际数只认 ERP 同一粒度汇总；ERP 无记录时给 null，不填 0
            "ytd_actual": actual_yuan,
            "variance": None if actual_yuan is None else round(actual_yuan - ytd_budget, 2),
            "attainment_pct": None if actual_yuan is None else _rate(actual_cents, r["ytd_budget_cents"]),
            "annual_progress_pct": (None if actual_yuan is None
                                    else _rate(actual_cents, r["annual_budget_cents"])),
            "unoccurred_budget": _yuan(r["unoccurred_cents"]),
            "share_of_annual_budget_pct": _rate(r["annual_budget_cents"], annual_total),
            "implied_growth_pct": r["implied_growth_pct"],
            "baseline_note": r["baseline_note"],
        })

    missing = [i["customer_name"] + "/" + i["region_name"] + "/" + i["dept_short"]
               for i in items if i["ytd_actual"] is None]
    # 空值恒排末尾：既不把「无实际」的组合顶到榜首，也不让它污染 TOP10 观感
    field = CUSTOMER_SORT[sort_key]
    has_val = [i for i in items if i[field] is not None]
    no_val = [i for i in items if i[field] is None]
    has_val.sort(key=lambda x: x[field], reverse=(order == "desc"))
    items = has_val + no_val
    total = len(items)
    page = items[offset:offset + limit]

    data = {
        "rows": page,
        "count": len(page),
        "total": total,
        "pagination": {"limit": limit, "offset": offset, "has_more": offset + len(page) < total},
        "filters": {"band": band or None, "tier": tier or None,
                    "region": region or None, "dept": dept or None, "q": keyword or None},
        "sort": {"field": sort_key, "order": order,
                 "whitelist": sorted(CUSTOMER_SORT)},
        "actual_missing": missing,
        "actual_note": ("每个组合的同期实际来自 ERP 同一粒度的独立汇总；"
                        "actual_missing 里的组合在 ERP 1–10 月没有该粒度的出货记录，"
                        "按「POS 无记录 ≠ 零销售」同一原则，不填 0、不参与达成率。"),
    }
    return {"ok": True, "data": data, "meta": _meta(conn, batch, {"grain": "customer_key×bi_region_key×md_key"})}


# ---------------------------------------------------------------------------
# ④ /api/budget/period —— 月 / 季 / 年 / 周
# ---------------------------------------------------------------------------
def period(conn: sqlite3.Connection, qs: dict) -> dict:
    attach(conn)
    batch = _batch(conn)
    batch_id = batch["batch_id"]
    year = batch["year"]
    gran = (qs.get("granularity") or "month").strip().lower()
    if gran not in ("month", "quarter", "year", "week"):
        raise BudgetError("granularity 只能是 month / quarter / year / week")

    bud_tbl = {"month": "fact_budget_monthly", "quarter": "fact_budget_quarterly",
               "year": "fact_budget_yearly", "week": "fact_budget_weekly"}[gran]
    col = {"month": "month", "quarter": "quarter", "year": "1", "week": "week_of_year"}[gran]

    # 全期预算（含 11–12 月）
    bud_all_map = {int(r["p"]): r["bud"] for r in conn.execute(
        f"SELECT {col} AS p, SUM(budget_amount_cents) AS bud FROM bud.{bud_tbl} "
        f"WHERE batch_id=? GROUP BY 1 ORDER BY 1", (batch_id,))}

    # 同期（可观测部分）预算：达成率的分母只能是这段，否则 Q4 会拿 10 月实际比 3 个月预算
    if gran == "week":
        obs_sql = (
            "SELECT w.week_of_year AS p, SUM(w.budget_amount_cents) AS bud "
            "FROM bud.fact_budget_weekly w "
            "JOIN bud.dim_budget_week_segment s "
            "  ON s.year = w.year AND s.week_of_year = w.week_of_year AND s.month = w.month "
            "WHERE w.batch_id=? AND s.in_observation='Y' GROUP BY 1")
    elif gran == "quarter":
        obs_sql = ("SELECT quarter AS p, SUM(budget_amount_cents) AS bud FROM bud.fact_budget_monthly "
                   "WHERE batch_id=? AND in_observation='Y' GROUP BY 1")
    elif gran == "month":
        obs_sql = ("SELECT month AS p, SUM(budget_amount_cents) AS bud FROM bud.fact_budget_monthly "
                   "WHERE batch_id=? AND in_observation='Y' GROUP BY 1")
    else:
        obs_sql = ("SELECT 1 AS p, SUM(budget_amount_cents) AS bud FROM bud.fact_budget_monthly "
                   "WHERE batch_id=? AND in_observation='Y'")
    obs_map = {int(r["p"]): r["bud"] for r in conn.execute(obs_sql, (batch_id,))}

    if gran == "week":
        # 周的实际按 ERP ship_week（与预算同一周定义：周一起点、含 1/1 的周为第 1 周）
        act = conn.execute(
            "SELECT CAST(ship_week AS INTEGER) AS p, ROUND(SUM(sales_amount), 2) AS amt "
            f"FROM fact_erp_shipment WHERE {_ACT_WHERE} GROUP BY 1 ORDER BY 1",
            {"year": year}).fetchall()
    elif gran == "month":
        act = conn.execute(
            "SELECT month AS p, ROUND(SUM(sales_amount), 2) AS amt "
            f"FROM fact_erp_shipment WHERE {_ACT_WHERE} GROUP BY 1 ORDER BY 1",
            {"year": year}).fetchall()
    elif gran == "quarter":
        act = conn.execute(
            "SELECT quarter AS p, ROUND(SUM(sales_amount), 2) AS amt "
            f"FROM fact_erp_shipment WHERE {_ACT_WHERE} GROUP BY 1 ORDER BY 1",
            {"year": year}).fetchall()
    else:
        act = conn.execute(
            f"SELECT 1 AS p, ROUND(SUM(sales_amount), 2) AS amt "
            f"FROM fact_erp_shipment WHERE {_ACT_WHERE}", {"year": year}).fetchall()
    act_map = {int(r["p"]): r["amt"] for r in act}

    annual = sum(bud_all_map.values())
    rows = []
    for p in sorted(bud_all_map):
        full_b = bud_all_map[p]
        obs_b = obs_map.get(p, 0)
        a = act_map.get(p)
        occurred = obs_b > 0 and a is not None
        rows.append({
            "period": p,
            "period_label": _period_label(gran, p, year),
            "occurred": occurred,
            "partial": occurred and obs_b != full_b,
            "budget": _yuan(full_b),
            "observed_budget": _yuan(obs_b),
            "actual": a if occurred else None,
            "variance": round(a - _yuan(obs_b), 2) if occurred else None,
            "attainment_pct": _rate(_cents(a), obs_b) if occurred else None,
            "annual_progress_pct": _rate(_cents(a), annual) if occurred else None,
            "note": (("实际只覆盖可观测部分（同期预算 {:,.2f}），全期预算含未发生月份，"
                      "达成率以同期预算为分母").format(_yuan(obs_b))
                     if occurred and obs_b != full_b
                     else (None if occurred
                           else "预算已下达，实际未发生（2026 年 11–12 月不在可观测区间）")),
        })

    # 累计口径，方便画达成曲线（只累计已发生期间，分母同样只用同期预算）
    cum_b = cum_a = 0.0
    for r in rows:
        if r["occurred"]:
            cum_b += r["observed_budget"] or 0
            cum_a += r["actual"] or 0
        r["cum_budget"] = round(cum_b, 2)
        r["cum_actual"] = round(cum_a, 2)
        r["cum_attainment_pct"] = _rate(_cents(cum_a), _cents(cum_b))

    data = {
        "granularity": gran,
        "granularity_label": {"month": "月", "quarter": "季度", "year": "年度", "week": "周"}[gran],
        "rows": rows,
        "totals": {"budget": _yuan(annual), "observed_budget": round(cum_b, 2),
                   "actual": round(cum_a, 2),
                   "attainment_pct": _rate(_cents(cum_a), _cents(cum_b)),
                   "annual_progress_pct": _rate(_cents(cum_a), annual)},
        "period_count": len(rows),
        "notes": ("周口径与 ERP 一致：周一起点，含 1 月 1 日的周记为第 1 周；"
                  "跨月周在月度表里已按自然日拆成两条周片段，周视图则为该周全部片段之和，"
                  "所以在周粒度看是完整一周，在月粒度看不会重复计入。"
                  if gran == "week" else
                  "同期实际只覆盖 1–10 月；11–12 月预算存在但未发生，actual 与 attainment_pct 均为 null。"
                  "季度/年度行的 attainment_pct 以 observed_budget（同期预算）为分母，"
                  "budget 为全期预算，两者差 11–12 月部分，partial 标记此情况。"),
    }
    return {"ok": True, "data": data, "meta": _meta(conn, batch, {"granularity": gran})}


def _period_label(gran, p, year):
    if gran == "month":
        return f"{year}-{p:02d}"
    if gran == "quarter":
        return f"{year} Q{p}"
    if gran == "week":
        return f"{year} W{p:02d}"
    return f"{year} 年"


# ---------------------------------------------------------------------------
# ⑤ /api/budget/assumption —— 生成假设（可追溯）
# ---------------------------------------------------------------------------
def assumption(conn: sqlite3.Connection, qs: dict) -> dict:
    attach(conn)
    batch = _batch(conn)
    batch_id = batch["batch_id"]
    band = (qs.get("band") or "").strip().upper()
    if band and band not in BANDS:
        raise BudgetError(f"band 只能是 {list(BANDS)} 之一或留空")
    tier = (qs.get("tier") or "").strip().upper()
    if tier and tier not in ("A", "B", "C"):
        raise BudgetError("tier 只能是 A / B / C 之一或留空")

    where = ["a.batch_id = ?"]
    args = [batch_id]
    if band:
        where.append("a.target_band = ?")
        args.append(band)
    if tier:
        where.append("a.tier = ?")
        args.append(tier)
    rows = conn.execute(
        "SELECT a.* FROM bud.budget_assumption a WHERE " + " AND ".join(where) +
        " ORDER BY a.target_band, a.ytd_actual_cents DESC", args).fetchall()

    items = []
    for r in rows:
        items.append({
            "customer_id": r["customer_id"], "customer_name": r["customer_name"],
            "region_name": r["region_name"], "dept_short": r["dept_short"],
            "channel_type": r["channel_type"],
            "tier": r["tier"], "band": r["target_band"], "band_label": BAND_LABEL[r["target_band"]],
            "band_range_pct": [r["band_low_pct"], r["band_high_pct"]],
            "target_attainment_pct": round(r["target_attainment"] * 100, 4),
            "actual_months": r["actual_months"],
            "baseline_note": r["baseline_note"],
            "baseline_actual": _yuan(r["ytd_actual_cents"]),
            "ytd_budget": _yuan(r["ytd_budget_cents"]),
            "attainment_pct": r["attainment_pct"],
            "annual_budget": _yuan(r["annual_budget_cents"]),
            "annualized_actual": _yuan(r["annualized_actual_cents"]),
            "implied_growth_pct": r["implied_growth_pct"],
            "monthly_profile": json.loads(r["monthly_profile_json"]),
            "data_source": r["data_source"],
            "seed": r["seed"],
        })

    data = {
        "rows": items,
        "count": len(items),
        "how_to_read": {
            "baseline": "基期 = 2026 年 1–10 月 ERP 实际出货（该组合自身）；"
                        "缺月组合用 0.6×自身月度结构 + 0.4×2025 年同客户节奏平滑，缺失月不补 0 预算",
            "target": "target_attainment = 该组合年预算 ÷ 基期实际，档内均匀随机取值，种子固定",
            "implied_growth": "implied_growth_pct = (年预算 ÷ 基期年化实际 − 1) × 100，"
                              "用于说明「这个预算相当于要求客户增长多少」",
            "trace": "每条记录带 batch_id / seed / 生成时间，可整批重放；"
                     "参数快照见 /api/budget/meta 的 batch.params",
        },
    }
    return {"ok": True, "data": data, "meta": _meta(conn, batch, {"filters": {"band": band or None, "tier": tier or None}})}


# ---------------------------------------------------------------------------
# ⑥ /api/budget/consistency —— 四粒度守恒与完整性自查
# ---------------------------------------------------------------------------
def consistency(conn: sqlite3.Connection, qs: dict | None = None) -> dict:
    attach(conn)
    batch = _batch(conn)
    batch_id = batch["batch_id"]
    checks = []

    def add(code, name, ok, detail):
        checks.append({"code": code, "name": name, "ok": bool(ok), "detail": detail})

    def triples(table):
        rows = conn.execute(
            f"SELECT customer_key, bi_region_key, md_key, SUM(budget_amount_cents) AS c "
            f"FROM bud.{table} WHERE batch_id=? GROUP BY 1,2,3", (batch_id,)).fetchall()
        return {(r["customer_key"], r["bi_region_key"], r["md_key"]): r["c"] for r in rows}

    y = triples("fact_budget_yearly")
    q = triples("fact_budget_quarterly")
    m = triples("fact_budget_monthly")
    w = triples("fact_budget_weekly")
    keys = set(y) | set(q) | set(m) | set(w)

    def max_diff(a, b):
        return max((abs(a.get(k, 0) - b.get(k, 0)) for k in keys), default=0)

    d1, d2, d3 = max_diff(y, q), max_diff(q, m), max_diff(m, w)
    add("B1", "年度 = 4 个季度之和", d1 == 0, f"{len(keys)} 组合逐条比对，最大差异 {d1} 分")
    add("B2", "季度 = 对应 3 个月之和", d2 == 0, f"{len(keys)} 组合逐条比对，最大差异 {d2} 分")
    add("B3", "月 = 该月全部周片段之和", d3 == 0, f"{len(keys)} 组合逐条比对，最大差异 {d3} 分")
    add("B4", "年度 = 全部 63 个周片段之和", max_diff(y, w) == 0,
        f"最大差异 {max_diff(y, w)} 分（周是唯一写入点，其余三层为整数下卷）")

    tot = {name: conn.execute(
        f"SELECT SUM(budget_amount_cents) AS c FROM bud.{t} WHERE batch_id=?", (batch_id,)).fetchone()["c"]
        for name, t in (("year", "fact_budget_yearly"), ("quarter", "fact_budget_quarterly"),
                        ("month", "fact_budget_monthly"), ("week", "fact_budget_weekly"))}
    add("B5", "四层全局合计一致且等于批次总额",
        len(set(tot.values())) == 1 and tot["year"] == batch["annual_budget_cents"],
        "年 {:,} / 季 {:,} / 月 {:,} / 周 {:,} 分".format(
            tot["year"], tot["quarter"], tot["month"], tot["week"]))

    dup = conn.execute(
        "SELECT COUNT(*) AS n FROM (SELECT customer_key, bi_region_key, md_key, week_of_year, month, "
        "COUNT(*) AS c FROM bud.fact_budget_weekly WHERE batch_id=? "
        "GROUP BY 1,2,3,4,5 HAVING c > 1)", (batch_id,)).fetchone()["n"]
    add("B6", "周片段主键无重复", dup == 0, f"重复键 {dup} 个")

    seg = conn.execute(
        "SELECT COUNT(*) AS segs, COUNT(DISTINCT week_of_year) AS weeks, "
        "COUNT(*) - COUNT(DISTINCT week_of_year) AS splits FROM bud.dim_budget_week_segment").fetchone()
    add("B7", "周结构 53 周 / 63 个周月片段", seg["weeks"] == 53 and seg["segs"] == 63,
        f"{seg['weeks']} 周、{seg['segs']} 个片段，其中 {seg['splits']} 个跨月拆分")

    cal = conn.execute(
        "SELECT COUNT(*) AS n, SUM(in_observation='Y') AS obs FROM bud.dim_budget_calendar").fetchone()
    add("B8", "日历覆盖 365 天，1–10 月可观测", cal["n"] == 365 and cal["obs"] == 304,
        f"{cal['n']} 天，其中可观测 {cal['obs']} 天（2026-01-01 ~ 2026-10-31）")

    mois = conn.execute(
        "SELECT COUNT(DISTINCT month) AS n, SUM(CASE WHEN in_observation='N' THEN 1 ELSE 0 END) AS unocc "
        "FROM bud.fact_budget_monthly WHERE batch_id=?", (batch_id,)).fetchone()
    add("B9", "12 个月均有预算，11–12 月标记未发生", mois["n"] == 12 and mois["unocc"] == 160,
        f"覆盖 {mois['n']} 个月；未发生月份记录 {mois['unocc']} 条（80 组合 × 2 个月）")

    src = conn.execute(
        "SELECT (SELECT COUNT(*) FROM bud.fact_budget_weekly WHERE data_source<>'MOCK_BUDGET') + "
        "       (SELECT COUNT(*) FROM bud.fact_budget_monthly WHERE data_source<>'MOCK_BUDGET') + "
        "       (SELECT COUNT(*) FROM bud.fact_budget_quarterly WHERE data_source<>'MOCK_BUDGET') + "
        "       (SELECT COUNT(*) FROM bud.fact_budget_yearly WHERE data_source<>'MOCK_BUDGET') + "
        "       (SELECT COUNT(*) FROM bud.budget_assumption WHERE data_source<>'MOCK_BUDGET') AS n").fetchone()["n"]
    add("B10", "全部事实行 data_source = MOCK_BUDGET", src == 0, f"非 MOCK_BUDGET 行 {src} 条")

    zero = conn.execute(
        "SELECT COUNT(*) AS n FROM bud.fact_budget_weekly WHERE budget_amount_cents <= 0").fetchone()["n"]
    add("B11", "无零/负预算记录", zero == 0, f"违规 {zero} 条")

    erp_md5 = file_md5(config.ETL_DB)
    add("B12", "ERP 库与生成时 MD5 一致（原始数据未改变）", erp_md5 == batch["erp_db_md5"],
        f"当前 {erp_md5[:12]}…，生成时 {batch['erp_db_md5'][:12]}…")

    bands = {r["target_band"]: r["n"] for r in conn.execute(
        "SELECT target_band, COUNT(*) AS n FROM bud.budget_assumption WHERE batch_id=? GROUP BY 1",
        (batch_id,))}
    want = {"OVER": 20, "ON": 24, "UNDER": 28, "SEVERE": 8}
    add("B13", "档位分布 20/24/28/8（25%/30%/35%/10%）",
        all(bands.get(k) == v for k, v in want.items()),
        f"实际 {[bands.get(b, 0) for b in BANDS]}，目标 {[want[b] for b in BANDS]}")

    active = conn.execute(
        "SELECT COUNT(*) AS n FROM bud.budget_batch WHERE status='ACTIVE'").fetchone()["n"]
    add("B14", "有且只有一个 ACTIVE 批次", active == 1, f"ACTIVE 批次 {active} 个")

    ent = conn.execute(
        "SELECT COUNT(DISTINCT customer_key || '|' || bi_region_key || '|' || md_key) AS n "
        "FROM bud.fact_budget_yearly WHERE batch_id=?", (batch_id,)).fetchone()["n"]
    ent_erp = conn.execute(
        f"SELECT COUNT(DISTINCT customer_key || '|' || bi_region_key || '|' || dept_short) AS n "
        f"FROM fact_erp_shipment WHERE {_ACT_WHERE}", {"year": batch["year"]}).fetchone()["n"]
    add("B15", "核算粒度与 ERP 出货组合数一致", ent == ent_erp == batch["entity_count"],
        f"预算 {ent} 个组合 / ERP {ent_erp} 个组合 / 批次记录 {batch['entity_count']} 个")

    data = {
        "checks": checks,
        "passed": sum(1 for c in checks if c["ok"]),
        "total": len(checks),
        "all_passed": all(c["ok"] for c in checks),
        "totals_cents": tot,
        "definition": "本接口对已入库数据实时重算，不复用生成脚本的校验结果；"
                      "关注的是「库里这 6,463 行是否自洽」，不是「脚本是否跑通」。",
    }
    return {"ok": True, "data": data, "meta": _meta(conn, batch)}


# ---------------------------------------------------------------------------
# 参数工具
# ---------------------------------------------------------------------------
def _int_in_range(raw, low, high, default):
    if raw in (None, ""):
        return default
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        raise BudgetError(f"分页参数必须是整数：{raw!r}")
    if v < low or v > high:
        raise BudgetError(f"分页参数超出范围 {low}–{high}：{v}")
    return v


BUDGET_ROUTES_DOC = {
    "/api/budget/meta": "批次、种子、生成参数、数据源声明、双口径定义",
    "/api/budget/summary": "全国总览 + 按 dim(region|dept|channel|tier) 拆分的达成情况",
    "/api/budget/customer": "客户×战区×市场部组合级达成（可排序/分页/筛选）",
    "/api/budget/period": "按 granularity(month|quarter|year|week) 的预算与达成",
    "/api/budget/assumption": "每个组合的生成假设与基期说明（可追溯）",
    "/api/budget/consistency": "四粒度守恒、主键、分布、ERP 未改动的只读自查",
}
