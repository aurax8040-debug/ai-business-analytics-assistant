"""供应链看板 P5 查询层（MOCK_SUPPLY_CHAIN，全只读）。

设计边界（docs/supply_chain_p5_design.md）
----------------------------------------
1. 供应链库 ``data/supply_chain_v1.db`` 以 ``mode=ro`` ATTACH 到 ETL 连接（别名 sc）；
   主库（ETL）只读提供 ``dim_sku``（72 个真实 SKU + pieces_per_box）。
   本模块不提供任何写入路径（模块内不出现 INSERT/UPDATE）。
2. 业务口径（P2 修正）：星禾代理果萃，果子无采购——到货直接入库只看数量；
   采购仅在包材（8 种配件，5 家供应商）。金额一律整数分。
3. 口径红线：
   - 包材库存金额 = 期末 × 该物料**最近一次 PO 单价**，是估算值，非账面成本；
     果子无采购价，绝不参与金额估算；
   - 可支撑天数 = 期末 ÷ (当月出库 ÷ 30)，月度近似，非财务周转天数；
   - 到货准班率只统计已到批次（arrival ≤ eta），在途未到期不计分母；
   - 可支撑盒数只用月末快照，不扣在途 PO 与已占用量（「当前可用上限」）。
4. 每个响应都带 ``meta.data_source = 'MOCK_SUPPLY_CHAIN'`` 与口径声明（caliber），
   前端原样渲染、不重算。
"""
from __future__ import annotations

import sqlite3

from . import config
from .db import file_md5

# 主材物料（BOM 4 项主材：果子 / 包装盒 / 内托 / 网套）
MAT_BOX = "PM-BOX"
MAT_TRAY = "PM-TRAY"
MAT_NET = "PM-NET"
# 兜底单价（分）：BOM 或库存缺失时的展示兜底，永远不会用于金额估算合计
FALLBACK_PRICE_CENTS = {MAT_BOX: 180, MAT_TRAY: 26, MAT_NET: 6}

CATEGORY_KEYS = {"品类A": "品类A", "品类B": "品类B", "品类C": "品类C"}

MAX_PICK = 4  # 多选对比上限（与前端一致，超出仅提示不静默替换）

BOTTLENECK_LABEL = {
    "fruit": "果子",
    MAT_BOX: "包装盒",
    MAT_TRAY: "内托",
    MAT_NET: "网套",
}

NOT_PROVIDED = [
    {"metric": "果子采购成本 / 果萃结算价",
     "reason": "星禾是代理，无采购环节；结算价属财务域，无字段不建模"},
    {"metric": "库存周转天数（财务口径）",
     "reason": "无日均出库成本与日流水；只给「可支撑天数」月度近似"},
    {"metric": "库存预测", "reason": "无预测模型与季节性依据，做了就是编数"},
    {"metric": "库存利用率", "reason": "无库容字段"},
    {"metric": "库存策略 / 安全库存", "reason": "无策略配置"},
    {"metric": "呆滞库存率（单一百分比）",
     "reason": "仅原料可按批次算；包材/成品无批次，不混算"},
    {"metric": "成品 / 包材库龄", "reason": "库存表无批次字段"},
]


class ScError(Exception):
    """供应链查询参数错误（对外映射为 HTTP 400）。"""


# ---------------------------------------------------------------------------
# 连接与公共件
# ---------------------------------------------------------------------------
def attach(conn: sqlite3.Connection) -> None:
    """把供应链库以只读 URI 挂到现有 ETL 连接上（别名 sc）。"""
    if not config.SC_DB.exists():
        raise ScError(
            f"供应链库不存在：{config.SC_DB}；请先执行 python3 -m supply_chain.generate")
    # PRAGMA database_list = (seq, name, file)；别名在第 2 列。
    # 取 seq 会导致同一连接第二次调用供应链接口时重复 ATTACH。
    names = [r[1] for r in conn.execute("PRAGMA database_list")]
    if "sc" not in names:
        conn.execute("ATTACH DATABASE ? AS sc", (config.sc_uri(),))


def _parse_month(qs: dict) -> str:
    month = (qs.get("month") or config.SC_LAST_MONTH).strip()
    if month == "":
        month = config.SC_LAST_MONTH
    ok = (len(month) == 7 and month[4] == "-" and month[:4].isdigit() and month[5:].isdigit()
          and config.SC_FIRST_MONTH <= month <= config.SC_LAST_MONTH)
    if not ok:
        raise ScError(
            f"month 非法：{month!r}；合法区间 {config.SC_FIRST_MONTH}~{config.SC_LAST_MONTH}"
            "（自然月月末快照，不提供跨年切换）")
    return month


def _prev_month(month: str) -> str | None:
    y, m = int(month[:4]), int(month[5:])
    m -= 1
    if m == 0:
        y, m = y - 1, 12
    prev = f"{y:04d}-{m:02d}"
    return prev if prev >= config.SC_FIRST_MONTH else None


def month_end(month: str) -> str:
    """自然月末（YYYY-MM-DD）。"""
    import datetime as _dt
    y, m = int(month[:4]), int(month[5:])
    if m == 12:
        n_y, n_m = y + 1, 1
    else:
        n_y, n_m = y, m + 1
    return (_dt.date(n_y, n_m, 1) - _dt.timedelta(days=1)).isoformat()


def _meta(conn: sqlite3.Connection, month: str, extra: dict | None = None) -> dict:
    meta = {
        "data_source": config.SC_DATA_SOURCE,
        "mock_supply_chain": True,
        "notice": config.SC_NOTICE,
        "month": month,
        "month_end": month_end(month),
        "snapshot": "月末快照（inventory_month），非实时库存",
        "database_md5": file_md5(config.SC_DB),
        "business_caliber": (
            "星禾代理果萃：果子无采购（到货直接入库，只看数量）；采购仅在包材"),
        "not_provided": NOT_PROVIDED,
    }
    if extra:
        meta["caliber"] = extra
    return meta


def _ok(data, meta) -> dict:
    return {"ok": True, "data": data, "meta": meta}


def _latest_price_cents(conn: sqlite3.Connection) -> dict[str, int]:
    """每种包材最近一次 PO 单价（分）。ROW_NUMBER 口径与设计稿 §2 指标 4 一致。"""
    rows = conn.execute(f"""
        SELECT material_id, unit_price_cents FROM (
            SELECT material_id, unit_price_cents,
                   ROW_NUMBER() OVER (PARTITION BY material_id
                                      ORDER BY order_date DESC, po_id DESC) rn
            FROM sc.fact_packaging_po)
        WHERE rn = 1
    """).fetchall()
    return {r[0]: int(r[1]) for r in rows}


def _pm_ending(conn: sqlite3.Connection, month: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT material_id, SUM(ending_qty) FROM sc.fact_packaging_inventory_monthly "
        "WHERE inventory_month = ? GROUP BY 1", (month,)).fetchall()
    return {r[0]: int(r[1] or 0) for r in rows}


def _fruit_ending(conn: sqlite3.Connection, month: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT raw_material_id, SUM(ending_qty) FROM sc.fact_fruit_inventory_monthly "
        "WHERE inventory_month = ? GROUP BY 1", (month,)).fetchall()
    return {r[0]: int(r[1] or 0) for r in rows}


def _bom_rows_effective(conn: sqlite3.Connection, sku_code: str, obs_date: str,
                        item_type: str = "packaging") -> list[tuple]:
    """取观察日（含）生效的 BOM 行，同物料取 effective_from 最新版本。

    V2 起 dim_sku_bom 可含多版本（如 SKU-001 标签 V1→V2，2026-11-01 生效）：
    观察日 ≤ 2026-10-31 时 V1 仍是生效版本 → 历史查询结果与 V1 完全一致（兼容）。
    """
    return conn.execute(
        """
        SELECT b.item_id, b.qty_per_unit, b.unit, b.bom_version
        FROM sc.dim_sku_bom b
        WHERE b.sku_code = ? AND b.item_type = ? AND b.bom_status = 'active'
          AND b.effective_from <= ?
          AND NOT EXISTS (
              SELECT 1 FROM sc.dim_sku_bom b2
              WHERE b2.sku_code = b.sku_code AND b2.item_type = b.item_type
                AND b2.item_id = b.item_id AND b2.bom_status = 'active'
                AND b2.effective_from <= ? AND b2.effective_from > b.effective_from)
        ORDER BY b.item_id
        """,
        (sku_code, item_type, obs_date, obs_date)).fetchall()


def _packaging_cost_cents(conn: sqlite3.Connection, sku_code: str,
                          prices: dict[str, int],
                          obs_date: str | None = None) -> int | None:
    """单盒包材成本（分，估算）= Σ(该 SKU BOM 包材用量 × 最近 PO 单价)。

    BOM 版本按 obs_date（默认 2026-12-31，即最新生效版本）取。
    """
    if obs_date is None:
        obs_date = "9999-12-31"
    rows = [(r[0], r[1]) for r in
            _bom_rows_effective(conn, sku_code, obs_date)]
    total = 0
    for item_id, qty in rows:
        price = prices.get(item_id)
        if price is None:
            return None
        total += round(price * qty)
    return total


# ---------------------------------------------------------------------------
# 指标 1–6：GET /api/sc/summary?month=
# ---------------------------------------------------------------------------
def summary(conn: sqlite3.Connection, qs: dict) -> dict:
    month = _parse_month(qs)
    attach(conn)

    def _fg(m: str) -> int:
        v = conn.execute("SELECT COALESCE(SUM(ending_qty),0) FROM sc.fact_fg_inventory_monthly "
                         "WHERE inventory_month = ?", (m,)).fetchone()[0]
        return int(v)

    def _fr(m: str) -> int:
        v = conn.execute("SELECT COALESCE(SUM(ending_qty),0) FROM sc.fact_fruit_inventory_monthly "
                         "WHERE inventory_month = ?", (m,)).fetchone()[0]
        return int(v)

    def _pm(m: str) -> int:
        v = conn.execute(
            "SELECT COALESCE(SUM(ending_qty),0) FROM sc.fact_packaging_inventory_monthly "
            "WHERE inventory_month = ?", (m,)).fetchone()[0]
        return int(v)

    fg_end, fr_end, pm_end = _fg(month), _fr(month), _pm(month)
    batches_in_stock = int(conn.execute(
        "SELECT COUNT(*) FROM sc.fact_fruit_inventory_monthly "
        "WHERE inventory_month = ? AND ending_qty > 0", (month,)).fetchone()[0])

    # 包材金额（估算）：期末 × 最近 PO 单价，仅包材，果子无采购价不参与
    prices = _latest_price_cents(conn)
    pm_stock = _pm_ending(conn, month)
    amount_cents = sum(qty * prices[mid] for mid, qty in pm_stock.items()
                       if mid in prices)

    # 可支撑天数：期末 ÷（当月出库 ÷ 30），三层
    fg_out = int(conn.execute(
        "SELECT COALESCE(SUM(erp_out_qty),0) FROM sc.fact_fg_inventory_monthly "
        "WHERE inventory_month = ?", (month,)).fetchone()[0])
    # 可支撑天数的消耗速度取领用量（production_issue_qty）：报废是异常损耗，
    # 不是持续消耗速度，不摊入分母（与设计稿 §7 黄金值 20.1 天一致）
    fr_out = int(conn.execute(
        "SELECT COALESCE(SUM(production_issue_qty),0) "
        "FROM sc.fact_fruit_inventory_monthly WHERE inventory_month = ?",
        (month,)).fetchone()[0])
    pm_out = int(conn.execute(
        "SELECT COALESCE(SUM(production_consumption_qty),0) "
        "FROM sc.fact_packaging_inventory_monthly WHERE inventory_month = ?",
        (month,)).fetchone()[0])

    def _days(ending: int, out: int) -> float | None:
        if out <= 0:
            return None
        return round(ending / (out / 30.0), 1)

    # 果萃到货准班率：已到批次（arrival ≤ month 月末）中 arrival ≤ eta 的占比
    end = month_end(month)
    arrived_total, arrived_ontime = conn.execute(
        "SELECT COUNT(*), SUM(arrival_date <= eta_date) FROM sc.fact_fruit_batch "
        "WHERE arrival_date <= ?", (end,)).fetchone()
    on_time_rate = (round(arrived_ontime / arrived_total * 100, 1)
                    if arrived_total else None)

    prev = _prev_month(month)
    fg_prev, fr_prev, pm_prev = (_fg(prev), _fr(prev), _pm(prev)) if prev else (None,) * 3

    def _mom(cur: int, old) -> float | None:
        if old is None or old == 0:
            return None
        return round((cur - old) / old * 100, 1)

    data = {
        "fg_ending_boxes": fg_end, "fg_mom_pct": _mom(fg_end, fg_prev),
        "fruit_ending_pieces": fr_end, "fruit_mom_pct": _mom(fr_end, fr_prev),
        "fruit_batches_in_stock": batches_in_stock,
        "pm_ending_units": pm_end, "pm_mom_pct": _mom(pm_end, pm_prev),
        "pm_amount_cents_est": amount_cents,
        "pm_amount_est": round(amount_cents / 100.0, 2),
        "days_of_cover": {
            "fg": _days(fg_end, fg_out),
            "fruit": _days(fr_end, fr_out),
            "packaging": _days(pm_end, pm_out),
        },
        "arrival": {
            "arrived_batches": int(arrived_total),
            "delayed_batches": int(arrived_total - arrived_ontime),
            "on_time_rate_pct": on_time_rate,
        },
        "prev_month": prev,
    }
    caliber = {
        "pm_amount": "包材期末 × 最近一次 PO 单价（估算，非账面成本）；果子无采购价，不含",
        "days_of_cover": "期末 ÷ (当月出库 ÷ 30)：月度近似，非财务周转天数；"
                         "原料消耗速度取领用量（报废不摊入分母）",
        "on_time_rate": "已到批次（arrival ≤ 月末）中 arrival ≤ eta 的占比；在途未到期不计分母",
        "mom": "相邻自然月期末环比；2026-01 无上期返回 null",
    }
    return _ok(data, _meta(conn, month, caliber))


# ---------------------------------------------------------------------------
# 指标 7：GET /api/sc/trend —— 三层月度 in / out / ending（2026-01~10）
# ---------------------------------------------------------------------------
def trend(conn: sqlite3.Connection, qs: dict) -> dict:
    month = _parse_month(qs)
    attach(conn)

    def _rows(table: str, in_col: str, out_cols: tuple[str, ...]) -> dict[str, dict]:
        out_expr = " + ".join(f"COALESCE({c},0)" for c in out_cols)
        rs = conn.execute(
            f"SELECT inventory_month, SUM({in_col}), SUM({out_expr}), SUM(ending_qty) "
            f"FROM sc.{table} WHERE inventory_month <= ? GROUP BY 1 ORDER BY 1",
            (month,)).fetchall()
        return {r[0]: {"in": int(r[1] or 0), "out": int(r[2] or 0),
                       "ending": int(r[3] or 0)} for r in rs}

    fg = _rows("fact_fg_inventory_monthly", "production_in_qty", ("erp_out_qty",))
    fr = _rows("fact_fruit_inventory_monthly", "qualified_in_qty",
               ("production_issue_qty", "scrap_qty"))
    pm = _rows("fact_packaging_inventory_monthly", "purchase_in_qty",
               ("production_consumption_qty",))
    months = sorted(set(fg) | set(fr) | set(pm))

    data = {
        "months": months,
        "fg": [fg.get(m) for m in months],
        "fruit": [fr.get(m) for m in months],
        "packaging": [pm.get(m) for m in months],
        "units": {"fg": "盒", "fruit": "粒", "packaging": "件"},
    }
    caliber = {
        "grain": "自然月；in = 入库/到货合格入库，out = 出库/领用/报废",
        "fruit_out": "原料出库 = production_issue_qty + scrap_qty",
    }
    return _ok(data, _meta(conn, month, caliber))


# ---------------------------------------------------------------------------
# 指标 8：GET /api/sc/arrival —— 到货节奏 + 准班率 + 延误明细
# ---------------------------------------------------------------------------
def arrival(conn: sqlite3.Connection, qs: dict) -> dict:
    month = _parse_month(qs)
    attach(conn)
    end = month_end(month)

    monthly = conn.execute("""
        SELECT strftime('%Y-%m', arrival_date) m, COUNT(*),
               SUM(received_qty), SUM(qualified_qty)
        FROM sc.fact_fruit_batch
        WHERE arrival_date <= ?
        GROUP BY 1 ORDER BY 1
    """, (end,)).fetchall()

    delayed = conn.execute("""
        SELECT b.batch_id, b.raw_material_id, b.shipment_no,
               b.eta_date, b.arrival_date,
               CAST(julianday(b.arrival_date) - julianday(b.eta_date) AS INT) delay_days
        FROM sc.fact_fruit_batch b
        WHERE b.arrival_date <= ? AND b.arrival_date > b.eta_date
        ORDER BY delay_days DESC, b.arrival_date
    """, (end,)).fetchall()

    total = sum(r[1] for r in monthly)
    late = len(delayed)
    data = {
        "monthly": [
            {"month": r[0], "batches": int(r[1]), "received": int(r[2] or 0),
             "qualified": int(r[3] or 0)} for r in monthly],
        "on_time_rate_pct": round((total - late) / total * 100, 1) if total else None,
        "arrived_batches": total, "delayed_batches": late,
        "max_delay_days": int(delayed[0][5]) if delayed else 0,
        "delayed_top": [
            {"batch_id": r[0], "raw_material_id": r[1], "shipment_no": r[2],
             "eta_date": r[3], "arrival_date": r[4], "delay_days": int(r[5])}
            for r in delayed[:10]],
    }
    caliber = {"delay": "delay_days = 实际到港 − 计划到港；仅统计已到批次",
               "top": "延误明细按延误天数降序取前 10"}
    return _ok(data, _meta(conn, month, caliber))


# ---------------------------------------------------------------------------
# 指标 9：GET /api/sc/aging —— 原料批次库龄分桶 + 最老批次
# ---------------------------------------------------------------------------
def aging(conn: sqlite3.Connection, qs: dict) -> dict:
    month = _parse_month(qs)
    attach(conn)
    end = month_end(month)

    rows = conn.execute("""
        SELECT f.batch_id, f.raw_material_id, f.ending_qty, b.arrival_date,
               CAST(julianday(?) - julianday(b.arrival_date) AS INT) age_days
        FROM sc.fact_fruit_inventory_monthly f
        JOIN sc.fact_fruit_batch b ON b.batch_id = f.batch_id
        WHERE f.inventory_month = ? AND f.ending_qty > 0
    """, (end, month)).fetchall()

    buckets = {"<30": [0, 0], "30-60": [0, 0], "60-90": [0, 0], ">90": [0, 0]}
    for r in rows:
        d = int(r[4])
        key = ("<30" if d < 30 else "30-60" if d < 60 else "60-90" if d < 90 else ">90")
        buckets[key][0] += 1
        buckets[key][1] += int(r[2])

    oldest = None
    if rows:
        top = sorted(rows, key=lambda r: (-r[4], r[0]))[0]
        oldest = {"batch_id": top[0], "raw_material_id": top[1],
                  "ending_qty": int(top[2]), "arrival_date": top[3],
                  "age_days": int(top[4])}

    data = {
        "buckets": [{"bucket": k, "batches": v[0], "pieces": v[1]}
                    for k, v in buckets.items()],
        "batches_total": sum(v[0] for v in buckets.values()),
        "pieces_total": sum(v[1] for v in buckets.values()),
        "oldest": oldest,
    }
    caliber = {"age": "库龄 = 月末 − 实际到港日；仅原料有批次，包材/成品不混算",
               "empty_bucket": "空桶如实显示 0，不画假数据"}
    return _ok(data, _meta(conn, month, caliber))


# ---------------------------------------------------------------------------
# 仓库分布：GET /api/sc/warehouses?month=
# ---------------------------------------------------------------------------
def warehouses(conn: sqlite3.Connection, qs: dict) -> dict:
    month = _parse_month(qs)
    attach(conn)
    wh = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT warehouse_id, warehouse_name, warehouse_type FROM sc.dim_sc_warehouse")}

    # 按层（原料/包材/成品）分别取仓库期末分布；单位不同层不同，不跨层相加
    layers = {}
    for table, label, unit, sql in (
        ("fruit", "原料", "粒", """
            SELECT r.warehouse_id, SUM(f.ending_qty) FROM sc.fact_fruit_inventory_monthly f
            JOIN sc.dim_fruit_raw r ON r.raw_material_id = f.raw_material_id
            WHERE f.inventory_month = ? GROUP BY 1"""),
        ("packaging", "包材", "件", """
            SELECT warehouse_id, SUM(ending_qty) FROM sc.fact_packaging_inventory_monthly
            WHERE inventory_month = ? GROUP BY 1"""),
        ("fg", "成品", "盒", """
            SELECT warehouse_id, SUM(ending_qty) FROM sc.fact_fg_inventory_monthly
            WHERE inventory_month = ? GROUP BY 1"""),
    ):
        layers[table] = [
            {"warehouse_id": r[0], "warehouse_name": wh.get(r[0], (r[0], ""))[0],
             "layer": label, "unit": unit, "ending_qty": int(r[1] or 0)}
            for r in conn.execute(sql, (month,))]

    rows = [row for key in ("fruit", "packaging", "fg") for row in layers[key]]
    data = {"rows": rows, "by_layer": layers,
            "warehouses_total": len({r["warehouse_id"] for r in rows})}
    caliber = {"grain": "仓库 × 层（原料/包材/成品）月末期末；单位不同层不同（粒/件/盒）",
               "qc_area": "质检暂存区不单独出库存行（已计入对应批次口径）"}
    return _ok(data, _meta(conn, month, caliber))


# ---------------------------------------------------------------------------
# 指标 10/11 数据源 + 包材采购区：GET /api/sc/packaging?month=
# ---------------------------------------------------------------------------
def packaging(conn: sqlite3.Connection, qs: dict) -> dict:
    month = _parse_month(qs)
    attach(conn)

    mat = {r[0]: r for r in conn.execute(
        "SELECT material_id, material_name, specification, unit, warehouse_id "
        "FROM sc.dim_sc_material WHERE material_status = 'active'")}
    prices = _latest_price_cents(conn)
    stock = _pm_ending(conn, month)

    materials = []
    for mid in sorted(mat):
        r = mat[mid]
        qty = stock.get(mid, 0)
        price = prices.get(mid)
        amount = qty * price if price is not None else None
        materials.append({
            "material_id": mid, "material_name": r[1], "specification": r[2],
            "unit": r[3], "warehouse_id": r[4],
            "latest_price": (round(price / 100.0, 2) if price is not None else None),
            "ending_qty": qty,
            "amount_cents_est": amount,
            "amount_est": (round(amount / 100.0, 2) if amount is not None else None),
        })
    total_amount = sum(m["amount_cents_est"] or 0 for m in materials)

    # 供应商准时率（已到 = 完成/部分到货；在途未到期不计分母）
    suppliers = {r[0]: r[1] for r in conn.execute(
        "SELECT supplier_id, supplier_name FROM sc.dim_sc_supplier")}
    po_stats = conn.execute("""
        SELECT supplier_id,
               SUM(purchase_status IN ('完成','部分到货')) arrived,
               SUM(purchase_status IN ('完成','部分到货')
                   AND actual_arrival_date <= expected_arrival_date) on_time,
               SUM(purchase_status = '在途') in_transit
        FROM sc.fact_packaging_po GROUP BY 1 ORDER BY 1
    """).fetchall()
    supplier_rows = [
        {"supplier_id": r[0], "supplier_name": suppliers.get(r[0], r[0]),
         "arrived_pos": int(r[1] or 0), "on_time_pos": int(r[2] or 0),
         "in_transit_pos": int(r[3] or 0),
         "on_time_rate_pct": (round(int(r[2] or 0) / int(r[1] or 0) * 100, 1)
                              if r[1] else None)}
        for r in po_stats]

    in_transit = conn.execute("""
        SELECT po_id, supplier_id, material_id, order_date, expected_arrival_date,
               purchase_qty, unit_price_cents
        FROM sc.fact_packaging_po
        WHERE purchase_status = '在途' ORDER BY expected_arrival_date, po_id
    """).fetchall()

    data = {
        "materials": materials,
        "total": {"ending_units": sum(m["ending_qty"] for m in materials),
                  "amount_cents_est": total_amount,
                  "amount_est": round(total_amount / 100.0, 2)},
        "suppliers": supplier_rows,
        "in_transit_pos": [
            {"po_id": r[0], "supplier_id": r[1], "material_id": r[2],
             "order_date": r[3], "expected_arrival_date": r[4],
             "purchase_qty": int(r[5]), "unit_price": round(r[6] / 100.0, 2)}
            for r in in_transit],
    }
    caliber = {
        "latest_price": "最近一次 PO 单价（ROW_NUMBER BY order_date DESC）；估算口径，非账面成本",
        "supplier_on_time": "已到 PO 中 actual ≤ expected 的占比；在途 16 单不计分母（在途未到期 ≠ 延迟）",
    }
    return _ok(data, _meta(conn, month, caliber))


# ---------------------------------------------------------------------------
# 指标 10：GET /api/sc/bom?sku= —— 单 SKU 明细 / 14 种粒装规格汇总
# ---------------------------------------------------------------------------
def bom(conn: sqlite3.Connection, qs: dict) -> dict:
    month = _parse_month(qs)
    attach(conn)
    sku = (qs.get("sku") or "").strip().upper()
    prices = _latest_price_cents(conn)

    if sku:
        row = conn.execute(
            "SELECT sku_code, sku_name, category, pieces_per_box, specification "
            "FROM dim_sku WHERE sku_code = ?", (sku,)).fetchone()
        if row is None or row[0] == "UNKNOWN":
            raise ScError(f"sku 不存在：{sku}（可用 SKU 见 /api/sc/capacity）")
        fruit_id = "RM-" + sku.split("-")[0]
        fruit_row = conn.execute(
            "SELECT raw_material_id, fruit_category, specification, spec_label "
            "FROM sc.dim_fruit_raw WHERE raw_material_id = ?", (fruit_id,)).fetchone()

        items = []
        main = [("fruit", fruit_id, row[3], "粒")]
        for item_type, item_id, qty, unit in main:
            items.append({"item_type": "fruit", "item_id": item_id,
                          "item_name": (fruit_row[3] if fruit_row else item_id),
                          "qty_per_unit": qty, "unit": unit, "role": "main"})
        main_mats = [(MAT_BOX, 1), (MAT_NET, row[3])]
        if row[3] != 2:                      # 2 粒装不使用内托：整项不出现，不是用量 0
            main_mats.insert(1, (MAT_TRAY, 1))
        for mid, qty in main_mats:
            items.append({"item_type": "packaging", "item_id": mid,
                          "item_name": mat_name(conn, mid),
                          "qty_per_unit": qty, "unit": mat_unit(conn, mid),
                          "role": "main",
                          "latest_price": prices.get(mid)})

        # 辅材按 BOM 实际行取（外箱/标签/果垫/封箱贴/胶带按装箱率摊入）；
        # 版本按观察月末取生效版（SKU-001 标签 2026-11 起 V2=2 张，10 月及以前仍 V1）
        obs = month_end(month)
        aux = [(r[0], r[1], r[2], r[3]) for r in _bom_rows_effective(conn, sku, obs)
               if r[0] not in (MAT_BOX, MAT_TRAY, MAT_NET)]
        for r in aux:
            items.append({"item_type": "packaging", "item_id": r[0],
                          "item_name": mat_name(conn, r[0]),
                          "qty_per_unit": float(r[1]), "unit": r[2], "role": "aux",
                          "latest_price": prices.get(r[0]),
                          "bom_version": r[3]})
        bom_versions = {r[0]: r[3] for r in aux}
        # 主材版本（展示用；2 粒装无内托则无该行）
        for r in _bom_rows_effective(conn, sku, obs):
            if r[0] in (MAT_BOX, MAT_TRAY, MAT_NET):
                bom_versions[r[0]] = r[3]

        cost = _packaging_cost_cents(conn, sku, prices, obs)
        data = {
            "sku_code": row[0], "sku_name": row[1], "category": row[2],
            "pieces_per_box": row[3], "specification": row[4],
            "fruit_raw_id": fruit_id,
            "fruit_category": (fruit_row[1] if fruit_row else row[2]),
            "items": items,
            "bom_versions": bom_versions,
            "packaging_cost_cents_est": cost,
            "packaging_cost_est": (round(cost / 100.0, 2) if cost is not None else None),
        }
        caliber = {
            "fruit_qty": "果子用量 = 粒数（pieces_per_box），按品类果径固定量",
            "tray": "2 粒装不使用内托（qty = 0）",
            "aux": "辅材按 BOM 装箱率摊入；单盒成本为最近 PO 单价估算",
            "bom_version": "BOM 按观察月末生效版本取（同 SKU 多版本时取 effective_from 最新且 ≤ 观察日）",
        }
        return _ok(data, _meta(conn, month, caliber))

    # 不传 sku：14 种粒装规格汇总（含包材可支撑盒数）
    pm = _pm_ending(conn, month)
    spec_rows = conn.execute("""
        SELECT pieces_per_box, COUNT(*), MIN(specification), MAX(specification)
        FROM dim_sku WHERE sku_code != 'UNKNOWN' GROUP BY 1 ORDER BY 1
    """).fetchall()
    fruit_stock = _fruit_ending(conn, month)

    specs = []
    for r in spec_rows:
        pieces, sku_cnt = int(r[0]), int(r[1])
        caps = []  # (名称, 可支撑盒数)：各主材库存 ÷ 单盒用量（盒1 / 内托1 / 网套=粒数）
        caps.append(("包装盒", pm.get(MAT_BOX, 0) // 1))
        if pieces != 2:
            caps.append(("内托", pm.get(MAT_TRAY, 0) // 1))
        caps.append(("网套", pm.get(MAT_NET, 0) // pieces))
        pm_cap_val = min(c for _, c in caps)
        pm_cap = next(name for name, c in caps if c == pm_cap_val)
        # 单盒包材成本（主材 + 辅材）：取该粒装任一代表 SKU 的 BOM 估算
        rep = conn.execute(
            "SELECT sku_code FROM dim_sku WHERE pieces_per_box = ? "
            "AND sku_code != 'UNKNOWN' ORDER BY sku_code LIMIT 1", (pieces,)).fetchone()
        cost = (_packaging_cost_cents(conn, rep[0], prices, month_end(month))
                if rep else None)
        specs.append({
            "pieces_per_box": pieces, "sku_count": sku_cnt,
            "box_qty": 1, "tray_qty": (0 if pieces == 2 else 1), "net_qty": pieces,
            "packaging_cap_boxes": pm_cap_val, "packaging_bottleneck": pm_cap,
            "packaging_cost_cents_est": cost,
            "packaging_cost_est": (round(cost / 100.0, 2) if cost is not None else None),
        })
    data = {"specs": specs, "fruit_stock_by_rm": fruit_stock}
    caliber = {
        "packaging_cap": "仅包材口径：MIN(主材库存 ÷ 单盒用量)；含果子的 SKU 级口径见 /api/sc/capacity",
        "grain": "14 种粒装 × 72 SKU；同粒装不同品类共用包材结构",
    }
    return _ok(data, _meta(conn, month, caliber))


def mat_name(conn: sqlite3.Connection, mid: str) -> str:
    r = conn.execute("SELECT material_name FROM sc.dim_sc_material WHERE material_id = ?",
                     (mid,)).fetchone()
    return r[0] if r else mid


def mat_unit(conn: sqlite3.Connection, mid: str) -> str:
    r = conn.execute("SELECT unit FROM sc.dim_sc_material WHERE material_id = ?",
                     (mid,)).fetchone()
    return r[0] if r else "件"


# ---------------------------------------------------------------------------
# 指标 11：GET /api/sc/capacity —— 72 SKU 全量可支撑 + 多选对比
# ---------------------------------------------------------------------------
def capacity(conn: sqlite3.Connection, qs: dict) -> dict:
    month = _parse_month(qs)
    attach(conn)

    category = (qs.get("category") or "").strip()
    if category and category not in CATEGORY_KEYS:
        raise ScError(f"category 非法：{category!r}；可选：品类A / 品类B / 品类C，留空为全部")
    q = (qs.get("q") or "").strip().lower()
    skus_param = (qs.get("skus") or "").strip().upper()

    prices = _latest_price_cents(conn)
    pm = _pm_ending(conn, month)
    fruit = _fruit_ending(conn, month)

    rows = conn.execute("""
        SELECT sku_code, sku_name, category, pieces_per_box
        FROM dim_sku WHERE sku_code != 'UNKNOWN' ORDER BY sku_code
    """).fetchall()

    items = []
    for sku_code, sku_name, cat, pieces in rows:
        fruit_id = "RM-" + sku_code.split("-")[0]
        fruit_qty = int(fruit.get(fruit_id, 0))
        # 4 项主材的可支撑（floor(库存 ÷ 单盒用量)）
        cands = [(BOTTLENECK_LABEL["fruit"], fruit_qty // pieces, "fruit", fruit_qty, pieces)]
        cands.append((BOTTLENECK_LABEL[MAT_BOX], pm.get(MAT_BOX, 0) // 1,
                      MAT_BOX, pm.get(MAT_BOX, 0), 1))
        if pieces != 2:
            cands.append((BOTTLENECK_LABEL[MAT_TRAY], pm.get(MAT_TRAY, 0) // 1,
                          MAT_TRAY, pm.get(MAT_TRAY, 0), 1))
        cands.append((BOTTLENECK_LABEL[MAT_NET], pm.get(MAT_NET, 0) // pieces,
                      MAT_NET, pm.get(MAT_NET, 0), pieces))
        best = min(cands, key=lambda c: c[1])
        cost = _packaging_cost_cents(conn, sku_code, prices, month_end(month))
        items.append({
            "sku_code": sku_code, "sku_name": sku_name, "category": cat,
            "pieces_per_box": pieces, "fruit_raw_id": fruit_id,
            "fruit_available": fruit_qty,
            "cap_boxes": int(best[1]), "bottleneck": best[0],
            "bottleneck_type": best[2],
            "packaging_cost_cents_est": cost,
            "packaging_cost_est": (round(cost / 100.0, 2) if cost is not None else None),
        })

    if skus_param:
        wanted = [s for s in (s.strip() for s in skus_param.split(",")) if s]
        wanted = [s for s in wanted if s]
        truncated = len(wanted) > MAX_PICK
        wanted = wanted[:MAX_PICK]
        by_code = {it["sku_code"]: it for it in items}
        picked = [by_code[s] for s in wanted if s in by_code]
        picked.sort(key=lambda it: it["cap_boxes"])
        data = {"items": picked, "total": len(items), "picked": len(picked),
                "truncated": truncated, "max_pick": MAX_PICK}
    else:
        view = items
        if category:
            view = [it for it in view if it["category"] == category]
        if q:
            view = [it for it in view
                    if q in it["sku_code"].lower() or q in it["sku_name"].lower()]
        view.sort(key=lambda it: it["cap_boxes"])
        data = {"items": view, "total": len(items), "filtered": len(view),
                "truncated": False, "max_pick": MAX_PICK}

    fruit_ids = {it["fruit_raw_id"] for it in items}
    data["fruit_available"] = {fid: int(fruit.get(fid, 0)) for fid in sorted(fruit_ids)}
    data["packaging_available"] = {k: int(pm.get(k, 0)) for k in (MAT_BOX, MAT_TRAY, MAT_NET)}
    caliber = {
        "cap": "可支撑盒数 = MIN(各主材期末库存 ÷ 单盒用量)，floor 取整",
        "scope": "仅用本月末快照；不扣在途 PO 与已占用量，不含排产计划（当前可用上限）",
        "sort": "默认按可支撑盒数升序（最受限在前）；前端可切换降序",
        "bottleneck": "瓶颈项即上限约束；当前数据下 72 个 SKU 瓶颈均为果子",
        "pick": f"skus= 逗号分隔最多 {MAX_PICK} 个，超出截断并返回 truncated:true",
    }
    return _ok(data, _meta(conn, month, caliber))


# ---------------------------------------------------------------------------
# 到货批次明细：GET /api/sc/batches?month=&page=
# ---------------------------------------------------------------------------
def batches(conn: sqlite3.Connection, qs: dict) -> dict:
    month = _parse_month(qs)
    attach(conn)
    end = month_end(month)
    try:
        page = max(1, int((qs.get("page") or "1").strip()))
    except ValueError:
        raise ScError("page 必须是正整数")
    page_size = 20

    total = int(conn.execute(
        "SELECT COUNT(*) FROM sc.fact_fruit_inventory_monthly "
        "WHERE inventory_month = ? AND ending_qty > 0", (month,)).fetchone()[0])
    offset = (page - 1) * page_size
    rows = conn.execute("""
        SELECT f.batch_id, f.raw_material_id, b.shipment_no, b.eta_date, b.arrival_date,
               f.ending_qty, r.warehouse_id,
               CAST(julianday(?) - julianday(b.arrival_date) AS INT) age_days
        FROM sc.fact_fruit_inventory_monthly f
        JOIN sc.fact_fruit_batch b ON b.batch_id = f.batch_id
        JOIN sc.dim_fruit_raw r ON r.raw_material_id = f.raw_material_id
        WHERE f.inventory_month = ? AND f.ending_qty > 0
        ORDER BY f.ending_qty DESC, f.batch_id
        LIMIT ? OFFSET ?
    """, (end, month, page_size, offset)).fetchall()

    rm_label = {r[0]: f"{r[1]} {r[2]}" for r in conn.execute(
        "SELECT raw_material_id, fruit_category, specification FROM sc.dim_fruit_raw")}
    data = {
        "page": page, "page_size": page_size, "total": total,
        "pages": (total + page_size - 1) // page_size,
        "rows": [
            {"batch_id": r[0], "raw_material_id": r[1],
             "raw_label": rm_label.get(r[1], r[1]), "shipment_no": r[2],
             "eta_date": r[3], "arrival_date": r[4], "ending_qty": int(r[5]),
             "warehouse_id": r[6], "age_days": int(r[7])}
            for r in rows],
    }
    caliber = {"grain": "月末在库批次（ending > 0），按期末余量降序；行级下钻预留 P6",
               "age": "库龄 = 月末 − 实际到港日"}
    return _ok(data, _meta(conn, month, caliber))


# ===========================================================================
# V2 数据链路扩展：生产加工 / 批次追溯 / 库存流水 / 包材采购预测
# 全部只读；核心计算在后端完成并输出可追溯的计算依据。
# ===========================================================================
import datetime as _dt
import math

FORECAST_NOTE_KEY = "forecast"
FORECAST_MONTHS = ("2026-11", "2026-12")   # 与 supply_chain/rules.py 保持一致


def _procurement_params(conn: sqlite3.Connection) -> dict[str, dict]:
    """生效采购参数（V1 active）。缺参数的物料如实返回，不虚构。"""
    rows = conn.execute(
        "SELECT material_id, param_version, safety_stock_qty, lead_time_days, "
        "moq_qty, order_package_qty, attrition_rate_pct, effective_from "
        "FROM sc.dim_sc_procurement_param WHERE param_status = 'active'").fetchall()
    return {r[0]: {"param_version": r[1], "safety_stock_qty": int(r[2]),
                   "lead_time_days": int(r[3]), "moq_qty": int(r[4]),
                   "order_package_qty": int(r[5]), "attrition_rate_pct": float(r[6]),
                   "effective_from": r[7]} for r in rows}


def _in_transit_qty(conn: sqlite3.Connection) -> dict[str, int]:
    """在途数量 = Σ(采购量 − 到货量)，状态 = 在途（未到货不计库存）。"""
    rows = conn.execute(
        "SELECT material_id, SUM(purchase_qty - received_qty) "
        "FROM sc.fact_packaging_po WHERE purchase_status = '在途' "
        "GROUP BY material_id").fetchall()
    return {r[0]: int(r[1]) for r in rows}


# ---------------------------------------------------------------------------
# 生产加工与成品入库：GET /api/sc/production?month=
# ---------------------------------------------------------------------------
def production(conn: sqlite3.Connection, qs: dict) -> dict:
    month = _parse_month(qs)
    attach(conn)
    end = month_end(month)

    # 概览（当月）
    ov = conn.execute("""
        SELECT COUNT(*), COALESCE(SUM(p.input_fruit_qty), 0),
               COALESCE(SUM(p.qualified_output_qty), 0),
               COALESCE(SUM(p.unqualified_output_qty), 0),
               COALESCE(AVG(p.loss_rate_pct), 0)
        FROM sc.fact_production_batch p
        WHERE substr(p.production_date, 1, 7) = ?
    """, (month,)).fetchone()

    # 月度趋势（计划 / 合格产出 / 投入）
    trend = conn.execute("""
        SELECT substr(p.production_date, 1, 7) m, COUNT(*),
               COALESCE(SUM(pl.planned_qty), 0), COALESCE(SUM(p.qualified_output_qty), 0),
               COALESCE(SUM(p.input_fruit_qty), 0), COALESCE(AVG(p.loss_rate_pct), 0)
        FROM sc.fact_production_batch p
        LEFT JOIN sc.fact_production_wo w ON w.wo_id = p.wo_id
        LEFT JOIN sc.fact_production_plan pl ON pl.plan_id = w.plan_id
        GROUP BY 1 ORDER BY 1
    """).fetchall()

    # 当月批次明细（含领用与入库关联；默认 10 行，limit ≤ 50）
    try:
        limit = int((qs.get("limit") or "10").strip())
    except ValueError:
        raise ScError("limit 必须是正整数（1~50）")
    if not (1 <= limit <= 50):
        raise ScError("limit 必须是正整数（1~50）")
    limit = min(50, limit)

    batches = conn.execute("""
        SELECT p.batch_id, p.sku_code, p.production_date, p.input_fruit_qty,
               p.qualified_output_qty, p.unqualified_output_qty, p.loss_rate_pct,
               w.bom_version
        FROM sc.fact_production_batch p
        JOIN sc.fact_production_wo w ON w.wo_id = p.wo_id
        WHERE substr(p.production_date, 1, 7) = ?
        ORDER BY p.production_date, p.batch_id LIMIT ?
    """, (month, limit)).fetchall()
    issue_fruit = dict(conn.execute(
        "SELECT production_batch_id, COUNT(*) FROM sc.fact_fruit_issue GROUP BY 1"))
    issue_fruit_qty = dict(conn.execute(
        "SELECT production_batch_id, SUM(issue_qty) FROM sc.fact_fruit_issue GROUP BY 1"))
    issue_pack = dict(conn.execute(
        "SELECT production_batch_id, COUNT(*) FROM sc.fact_packaging_issue GROUP BY 1"))
    inbound = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT production_batch_id, qty, inbound_date FROM sc.fact_fg_inbound")}

    rows = []
    for bid, sku, pdate, inp, qual, unq, loss, bver in batches:
        inb = inbound.get(bid)
        rows.append({
            "batch_id": bid, "sku_code": sku, "production_date": pdate,
            "input_fruit_qty": int(inp), "qualified_output_qty": int(qual),
            "unqualified_output_qty": int(unq), "loss_rate_pct": round(float(loss), 2),
            "bom_version": bver,
            "fruit_issue_batches": int(issue_fruit.get(bid, 0)),
            "fruit_issue_qty": int(issue_fruit_qty.get(bid, 0)),
            "packaging_issue_lines": int(issue_pack.get(bid, 0)),
            "fg_inbound_qty": (int(inb[0]) if inb else 0),
            "fg_inbound_date": (inb[1] if inb else None),
        })

    data = {
        "overview": {
            "batches": int(ov[0]), "input_fruit_pieces": int(ov[1]),
            "qualified_boxes": int(ov[2]), "unqualified_boxes": int(ov[3]),
            "avg_loss_rate_pct": round(float(ov[4]), 2),
        },
        "trend": [
            {"month": r[0], "batches": int(r[1]), "planned_boxes": int(r[2]),
             "qualified_boxes": int(r[3]), "input_fruit_pieces": int(r[4]),
             "avg_loss_rate_pct": round(float(r[5]), 2)}
            for r in trend],
        "batches": rows, "limit": limit,
        "chain": "原料批次 --领用--> 生产批次 --合格产出--> 成品入库 --出库--> ERP",
    }
    caliber = {
        "balance": "投入 =（合格+不合格）× 粒数 ×（1+损耗率），质检第 7 项锁定",
        "deduct": "原料/包材按实际领用扣减（月库存守恒约束锁定）；成品按合格产出入库",
        "bom_version": "生产工单记录 BOM 版本；2026-10 及以前均为 V1（SKU-001 的 V2 于 2026-11-01 生效）",
    }
    return _ok(data, _meta(conn, month, caliber))


# ---------------------------------------------------------------------------
# 批次反向追溯：GET /api/sc/trace?shipment_no=&sku_code=  或  ?batch_id=
# ---------------------------------------------------------------------------
def trace(conn: sqlite3.Connection, qs: dict) -> dict:
    attach(conn)
    month = config.SC_LAST_MONTH

    shipment_no = (qs.get("shipment_no") or "").strip()
    sku_code = (qs.get("sku_code") or "").strip().upper()
    batch_id = (qs.get("batch_id") or "").strip()

    if not batch_id and not (shipment_no and sku_code):
        raise ScError("需要 batch_id= 或 shipment_no=+sku_code=（二选一）")
    if shipment_no and not sku_code:
        raise ScError("按 ERP 行追溯必须同时给 shipment_no 与 sku_code（发运单号不唯一）")

    # ---- 反向：ERP 出库行 → link → 生产批次
    if shipment_no:
        erp = conn.execute(
            "SELECT full_date, sales_boxes FROM fact_erp_shipment "
            "WHERE shipment_no = ? AND sku_code = ? ORDER BY full_date LIMIT 1",
            (shipment_no, sku_code)).fetchone()
        if erp is None:
            raise ScError(f"ERP 出库行不存在：{shipment_no} × {sku_code}（权威源 fact_erp_shipment 只读比对）")
        link = conn.execute(
            "SELECT production_batch_id, match_status, match_basis "
            "FROM sc.link_erp_shipment WHERE shipment_no = ? AND sku_code = ?",
            (shipment_no, sku_code)).fetchone()
        if link is None:
            raise ScError("ERP 行未在关联表中（数据不一致，请报告）")
        data = {
            "direction": "erp_to_batch", "shipment_no": shipment_no,
            "sku_code": sku_code, "shipment_date": erp[0],
            "sales_boxes": float(erp[1]),
            "match_status": link[1], "match_basis": link[2],
        }
        if link[1] != "MATCHED" or not link[0]:
            # 未关联：如实返回，绝不虚构追溯链
            data["production_batch"] = None
            data["note"] = ("该出库行无法追溯到生产批次（状态见 match_status / match_basis），"
                            "不构造虚假关联")
            return _ok(data, _meta(conn, month, {"trace": "未关联行如实标注，不虚构"}))
        batch_id = link[0]
        data["production_batch_id"] = batch_id
    else:
        data = {"direction": "batch_to_erp", "production_batch_id": batch_id}

    # ---- 生产批次详情 + 双向领用 + 入库 + 关联出库
    pb = conn.execute("""
        SELECT p.batch_id, p.sku_code, p.production_date, p.input_fruit_qty,
               p.qualified_output_qty, p.unqualified_output_qty, p.loss_rate_pct,
               w.wo_id, w.bom_version, w.planned_qty, pl.plan_id, pl.plan_month
        FROM sc.fact_production_batch p
        JOIN sc.fact_production_wo w ON w.wo_id = p.wo_id
        LEFT JOIN sc.fact_production_plan pl ON pl.plan_id = w.plan_id
        WHERE p.batch_id = ?
    """, (batch_id,)).fetchone()
    if pb is None:
        raise ScError(f"生产批次不存在：{batch_id}")
    fruit_issues = conn.execute("""
        SELECT fi.fruit_batch_id, fb.raw_material_id, fi.issue_qty, fi.issue_date,
               fb.shipment_no, fb.eta_date, fb.arrival_date, fb.quality_grade
        FROM sc.fact_fruit_issue fi
        JOIN sc.fact_fruit_batch fb ON fb.batch_id = fi.fruit_batch_id
        WHERE fi.production_batch_id = ? ORDER BY fi.issue_date, fi.fruit_batch_id
    """, (batch_id,)).fetchall()
    pack_issues = conn.execute("""
        SELECT pi.material_id, m.material_name, pi.issue_qty, pi.issue_date
        FROM sc.fact_packaging_issue pi
        JOIN sc.dim_sc_material m ON m.material_id = pi.material_id
        WHERE pi.production_batch_id = ? ORDER BY pi.material_id
    """, (batch_id,)).fetchall()
    inb = conn.execute(
        "SELECT inbound_id, warehouse_id, qty, inbound_date FROM sc.fact_fg_inbound "
        "WHERE production_batch_id = ?", (batch_id,)).fetchall()
    # 该批次关联的 ERP 出库行（正向视角）
    out_rows = conn.execute("""
        SELECT l.shipment_no, l.sku_code, l.shipment_date, e.sales_boxes
        FROM sc.link_erp_shipment l
        LEFT JOIN fact_erp_shipment e
          ON e.shipment_no = l.shipment_no AND e.sku_code = l.sku_code
          AND e.full_date = l.shipment_date
        WHERE l.production_batch_id = ?
        ORDER BY l.shipment_date LIMIT 20
    """, (batch_id,)).fetchall()

    data["production_batch"] = {
        "batch_id": pb[0], "sku_code": pb[1], "production_date": pb[2],
        "input_fruit_qty": int(pb[3]), "qualified_output_qty": int(pb[4]),
        "unqualified_output_qty": int(pb[5]), "loss_rate_pct": round(float(pb[6]), 2),
        "wo_id": pb[7], "bom_version": pb[8], "planned_qty": int(pb[9]),
        "plan_id": pb[10], "plan_month": pb[11],
    }
    data["fruit_issues"] = [
        {"fruit_batch_id": r[0], "raw_material_id": r[1], "issue_qty": int(r[2]),
         "issue_date": r[3], "zspri_shipment_no": r[4], "eta_date": r[5],
         "arrival_date": r[6], "quality_grade": r[7]}
        for r in fruit_issues]
    data["packaging_issues"] = [
        {"material_id": r[0], "material_name": r[1], "issue_qty": int(r[2]),
         "issue_date": r[3]} for r in pack_issues]
    data["fg_inbound"] = [
        {"inbound_id": r[0], "warehouse_id": r[1], "qty": int(r[2]),
         "inbound_date": r[3]} for r in inb]
    data["erp_shipments"] = [
        {"shipment_no": r[0], "sku_code": r[1], "shipment_date": r[2],
         "sales_boxes": (float(r[3]) if r[3] is not None else None)}
        for r in out_rows]
    data["note"] = "追溯链闭合：原料批次 → 生产批次 → 成品入库 → ERP 出库（仅 MATCHED 行）"
    caliber = {
        "link": "MATCHED = 同 SKU 入库日 ≤ 出库日的最近生产批次；其余状态如实标注",
        "readonly": "ERP 为权威数据源（只读比对）；关联表不回写 ERP",
    }
    return _ok(data, _meta(conn, month, caliber))


# ---------------------------------------------------------------------------
# 包材库存流水：GET /api/sc/ledger?material_id=&month=
# ---------------------------------------------------------------------------
def ledger(conn: sqlite3.Connection, qs: dict) -> dict:
    month = _parse_month(qs)
    attach(conn)
    mid = (qs.get("material_id") or "").strip().upper()
    if not mid:
        raise ScError("material_id 必填（如 PM-BOX；清单见 /api/sc/packaging）")
    mat = conn.execute(
        "SELECT material_id, material_name, unit FROM sc.dim_sc_material "
        "WHERE material_id = ?", (mid,)).fetchone()
    if mat is None:
        raise ScError(f"material_id 不存在：{mid}")

    # 月度汇总（1~10 月，含平衡四栏）
    monthly = conn.execute("""
        SELECT inventory_month, opening_qty, purchase_in_qty,
               production_consumption_qty, ending_qty
        FROM sc.fact_packaging_inventory_monthly
        WHERE material_id = ? ORDER BY inventory_month
    """, (mid,)).fetchall()

    # 当月流水：PO 到货入库（received>0）与生产领用，按日合并
    events = []
    for po_id, arr, rec, status, price in conn.execute(
        "SELECT po_id, actual_arrival_date, received_qty, purchase_status, "
        "unit_price_cents FROM sc.fact_packaging_po "
        "WHERE material_id = ? AND received_qty > 0 AND actual_arrival_date LIKE ?",
            (mid, month + "%")):
        events.append({
            "date": arr, "type": "采购入库", "ref": po_id, "qty": int(rec),
            "unit": mat[2], "note": (f"单价 {price / 100:.2f} 元（部分到货）"
                                     if status == "部分到货" else "PO 到货")})
    for iid, bid, qty, idate in conn.execute(
        "SELECT issue_id, production_batch_id, issue_qty, issue_date "
        "FROM sc.fact_packaging_issue WHERE material_id = ? AND issue_date LIKE ?",
            (mid, month + "%")):
        events.append({
            "date": idate, "type": "生产领用", "ref": bid, "qty": int(qty),
            "unit": mat[2], "note": f"领用单 {iid}（生产批次扣减）"})
    events.sort(key=lambda e: (e["date"], e["type"]))

    cur_month = next((r for r in monthly if r[0] == month), None)
    data = {
        "material_id": mid, "material_name": mat[1], "unit": mat[2],
        "month": month,
        "monthly": [
            {"month": r[0], "opening_qty": int(r[1]), "purchase_in_qty": int(r[2]),
             "consumption_qty": int(r[3]), "ending_qty": int(r[4])}
            for r in monthly],
        "month_summary": (
            {"opening_qty": int(cur_month[1]), "purchase_in_qty": int(cur_month[2]),
             "consumption_qty": int(cur_month[3]), "ending_qty": int(cur_month[4])}
            if cur_month else None),
        "events": events,
        "events_count": len(events),
    }
    caliber = {
        "balance": "流水由 PO 到货与生产领用两源派生（不落重复存储表），"
                   "月度汇总满足 ending = opening + 采购入 − 领用（库内 CHECK 锁定）",
        "in_transit": "在途 PO 未到货不计入流水与库存（见 /api/sc/forecast 的在途口径）",
    }
    return _ok(data, _meta(conn, month, caliber))


# ---------------------------------------------------------------------------
# 包材采购预测（V2 核心能力）：GET /api/sc/forecast?month=2026-10&window=2
# ---------------------------------------------------------------------------
def forecast(conn: sqlite3.Connection, qs: dict) -> dict:
    month = _parse_month(qs)
    if month != config.SC_LAST_MONTH:
        raise ScError(
            f"采购预测的观察点固定为数据窗最后一个月（{config.SC_LAST_MONTH} 月末），"
            f"预测 2026-11 / 2026-12；当前 month={month} 不支持")
    attach(conn)
    obs_date = month_end(month)
    try:
        window = min(2, max(1, int((qs.get("window") or "2").strip())))
    except ValueError:
        raise ScError("window 必须是整数（1 或 2，默认 2）")
    f_months = FORECAST_MONTHS[:window]

    params = _procurement_params(conn)
    pm_ending = _pm_ending(conn, month)
    in_transit = _in_transit_qty(conn)
    prices = _latest_price_cents(conn)
    materials = {r[0]: r[1] for r in conn.execute(
        "SELECT material_id, material_name FROM sc.dim_sc_material")}

    # 预测需求（SKU × 月 → 物料 × 月），BOM 按预测月首日生效版本取
    fc_rows = conn.execute(
        "SELECT sku_code, forecast_month, forecast_qty, basis_avg_qty, "
        "seasonal_factor, seasonal_basis, forecast_note "
        "FROM sc.fact_production_forecast ORDER BY sku_code, forecast_month"
    ).fetchall()
    demand: dict[tuple[str, str], float] = {}
    fc_skus: dict[str, set] = {}
    fc_note = ""
    total_boxes = {fm: 0 for fm in f_months}
    for sku, fm, qty, avg, factor, sbasis, note in fc_rows:
        if fm not in f_months:
            continue
        fc_note = note
        total_boxes[fm] += int(qty)
        for item_id, qty_per_unit, _unit, _ver in _bom_rows_effective(
                conn, sku, fm + "-01"):
            key = (item_id, fm)
            demand[key] = demand.get(key, 0.0) + qty * qty_per_unit
            fc_skus.setdefault(item_id, set()).add(sku)

    items = []
    for mid in sorted(materials):
        mat_name_ = materials[mid]
        p = params.get(mid)
        ending = int(pm_ending.get(mid, 0))
        transit = int(in_transit.get(mid, 0))
        # 缺参数：如实输出「参数待配置」，不编造数字
        if p is None:
            items.append({
                "material_id": mid, "material_name": mat_name_,
                "param_status": "pending_config",
                "note": "采购参数未配置（安全库存/周期/MOQ/损耗率缺失），不给建议数量",
            })
            continue
        attr = p["attrition_rate_pct"] / 100.0
        demand_total = 0.0
        demand_by_month = {}
        for fm in f_months:
            raw_qty = demand.get((mid, fm), 0.0)
            with_attr = raw_qty * (1 + attr)
            demand_by_month[fm] = {
                "gross_demand": int(round(raw_qty)),
                "with_attrition": int(round(with_attr)),
            }
            demand_total += with_attr
        demand_total_r = int(round(demand_total))
        net = demand_total_r + p["safety_stock_qty"] - ending - transit
        raw_suggest = max(0, net)
        # MOQ / 采购整包装调整（向上取整到整包装；不足 MOQ 抬到 MOQ 的整包装倍数）
        if raw_suggest > 0:
            pkg = p["order_package_qty"]
            suggest = math.ceil(raw_suggest / pkg) * pkg
            if suggest < p["moq_qty"]:
                suggest = math.ceil(p["moq_qty"] / pkg) * pkg
        else:
            suggest = 0
        # 建议下单日 = 首个有需求的预测月首日 − 采购周期
        first_need_month = next(
            (fm for fm in f_months if demand_by_month.get(fm, {}).get("gross_demand", 0) > 0),
            None)
        if first_need_month:
            need_first_day = _dt.date.fromisoformat(first_need_month + "-01")
            best_order = need_first_day - _dt.timedelta(days=p["lead_time_days"])
            best_order_str = best_order.isoformat()
            late = best_order <= _dt.date.fromisoformat(obs_date)
            order_advice = ("立即下单（最优下单日已过，采购周期开始吃首月需求）"
                            if late else f"{best_order_str} 前下单")
        else:
            best_order_str = None
            order_advice = "窗口内无需求，无需下单"
            late = False
        if raw_suggest <= 0:
            risk = "low"
            risk_note = "净需求 ≤ 0：库存 + 在途可覆盖预测需求与安全库存"
        elif late:
            risk = "high"
            risk_note = (f"最优下单日 {best_order_str} 已早于观察点，"
                         f"{p['lead_time_days']} 天采购周期无法足量覆盖首月需求")
        else:
            risk = "medium"
            risk_note = f"净需求为正，采购周期 {p['lead_time_days']} 天内下单可覆盖"
        est_amount_cents = (suggest * prices.get(mid, 0)) if suggest > 0 else 0
        items.append({
            "material_id": mid, "material_name": mat_name_,
            "param_status": "active", "param_version": p["param_version"],
            "applicable_skus": len(fc_skus.get(mid, set())),
            "forecast_demand": demand_by_month,
            "demand_total": demand_total_r,
            "attrition_rate_pct": p["attrition_rate_pct"],
            "safety_stock_qty": p["safety_stock_qty"],
            "available_qty": ending, "in_transit_qty": transit,
            "net_requirement": net,
            "suggested_order_qty": suggest,
            "suggested_order_amount_cents_est": int(est_amount_cents),
            "moq_qty": p["moq_qty"], "order_package_qty": p["order_package_qty"],
            "lead_time_days": p["lead_time_days"],
            "best_order_date": best_order_str, "order_advice": order_advice,
            "risk_level": risk, "risk_note": risk_note,
            "basis": (
                f"净需求 = 需求 {demand_total_r:,}（含损耗 {p['attrition_rate_pct']}%）"
                f" + 安全库存 {p['safety_stock_qty']:,} − 可用库存 {ending:,}"
                f" − 在途 {transit:,} = {net:,}；建议 {suggest:,}"
                f"（按整包装 {p['order_package_qty']:,} 向上取整"
                + (f"，不足 MOQ {p['moq_qty']:,} 抬至 MOQ" if 0 < suggest < p["moq_qty"] else "")
                + "）"),
        })

    data = {
        "obs_month": month, "obs_date": obs_date,
        "forecast_months": list(f_months),
        "forecast_boxes": {fm: total_boxes[fm] for fm in f_months},
        "items": items,
        "formula": {
            "demand": "预计包材需求量 = Σ(预计生产盒数 × 单盒 BOM 用量 ×(1+损耗率))，"
                      "BOM 按预测月首日生效版本（SKU-001 标签自 2026-11-01 起为 V2 = 2 张）",
            "suggest": "建议采购量 = max(0, 需求 + 安全库存 − 可用库存 − 在途)，"
                       "再按整包装向上取整并满足 MOQ",
            "order": "建议下单日 = 首个需求月 1 号 − 采购周期；早于观察点则提示立即下单",
        },
        "forecast_basis": fc_note or "近 3 月 ERP 出库均值 × Y25 同期季节系数",
    }
    caliber = {
        "mock_param": "采购参数（安全库存/周期/MOQ/整包装/损耗率）为 MOCK 设定，"
                      "非真实供应商合同",
        "demand_source": "未来生产需求来自 fact_production_forecast（ERP Sell-in 口径推导，"
                         "非 POS Sell-out）；数据窗止 2026-10-31，11 月无已发生需求",
        "in_transit": "在途 = 在途 PO 的（采购量 − 到货量），未计入月末库存",
        "bom_version": "预测月 BOM 取生效版本（effective_from ≤ 预测月首日的最新版）",
    }
    return _ok(data, _meta(conn, month, caliber))
