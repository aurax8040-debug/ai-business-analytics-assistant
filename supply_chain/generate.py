"""供应链 Mock 数据生成器（P1–P4）。

用法::

    python3 -m supply_chain.generate

- 固定种子（supply_chain.SEED），幂等 = 每次整库重建 data/supply_chain_v1.db
- 只读引用 ETL 库（dim_sku + 2026 年 ERP 月度出库量做锚点），不写 ERP/POS/SFA/预算
- 业务口径：果子为果萃代理到货（无采购单，船次 ETA/ATA + 数量）；
  采购只发生在包材配件；金额一律整数分，且只有包材有金额
- 全部新增数据标记 MOCK_SUPPLY_CHAIN
- 平衡律由 schema 的 CHECK 约束兜底，生成逻辑自身保证逐行成立：
  原料库存 ending = opening + qualified_in - issue - scrap（批次内跨月结转）
  包材库存 ending = opening + purchase_in - consumption
  成品库存 ending = opening + production_in - erp_out
  批次到货 received = qualified + rejected
- 不使用内置 hash()（进程随机盐），仓库分配等映射全部用稳定散列，保证幂等。
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import time
from bisect import bisect_right
from datetime import date, timedelta
from pathlib import Path

from . import DATA_SOURCE, SEED
from . import rules as R

BASE_DIR = Path(__file__).resolve().parent.parent
ETL_DB = BASE_DIR / "data" / "bi_data_layer_v1.db"
# 输出路径可被环境变量 SUPPLY_CHAIN_DB 覆盖（默认落项目 data/ 目录）
SC_DB = Path(os.environ.get("SUPPLY_CHAIN_DB", str(BASE_DIR / "data" / "supply_chain_v1.db")))
SCHEMA = Path(__file__).resolve().parent / "schema.sql"

SPEC_LIST = [18, 22, 25, 27, 30, 33, 36, 39]
CAT_LETTER = {"品类A": "A", "品类B": "B", "品类C": "C"}
CAT_BY_LETTER = {"A": "品类A", "B": "品类B", "C": "品类C"}
FRUIT_WH = {"品类A": "WH-FR-01", "品类C": "WH-FR-02", "品类B": "WH-FR-02"}
INSPECTORS = ["质检员-王敏", "质检员-李强", "质检员-赵蕾", "质检员-陈涛"]


def stable_hash(text: str) -> int:
    return sum(ord(c) for c in text)


def month_str(year: int, month: int) -> str:
    return f"{year}-{month:02d}"


class Seq:
    """稳定编号器：前缀 + yymm + 序号（同前缀同月递增）。"""

    def __init__(self) -> None:
        self._counters: dict[tuple[str, str], int] = {}

    def next(self, prefix: str, yymm: str) -> str:
        key = (prefix, yymm)
        self._counters[key] = self._counters.get(key, 0) + 1
        width = 5 if prefix in ("PB", "FI", "PI", "FGI") else 4
        return f"{prefix}-{yymm}-{self._counters[key]:0{width}d}"


def load_anchors() -> tuple[dict[str, dict], dict[tuple[str, int], int]]:
    """只读打开 ETL 库：72 个有效 SKU + 2026 年月度出库量（盒）。"""
    con = sqlite3.connect(f"file:{ETL_DB}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        skus: dict[str, dict] = {}
        for sku_code, category, spec, pieces, single in cur.execute(
            "SELECT sku_code, category, specification, pieces_per_box, is_single_layer "
            "FROM dim_sku WHERE sku_code != 'UNKNOWN' ORDER BY sku_code"
        ):
            letter = CAT_LETTER[category]
            skus[sku_code] = {
                "sku_code": sku_code,
                "category": category,
                "specification": int(spec),
                "pieces_per_box": int(pieces),
                "is_single_layer": single,
                "raw_material_id": f"RM-{letter}{int(spec)}",
                "fg_wh": "WH-FG-01" if stable_hash(sku_code) % 2 == 0 else "WH-FG-02",
            }
        demand: dict[tuple[str, int], int] = {}
        for sku_code, month, boxes in cur.execute(
            "SELECT sku_code, month, SUM(sales_boxes) FROM fact_erp_shipment "
            "WHERE year = 2026 GROUP BY sku_code, month"
        ):
            demand[(sku_code, int(month))] = int(round(boxes))
        return skus, demand
    finally:
        con.close()


class Generator:
    def __init__(self) -> None:
        self.rng = random.Random(SEED)
        self.seq = Seq()
        self.skus, self.demand = load_anchors()
        self.con: sqlite3.Connection | None = None  # run() 中创建（先清旧库再连接）
        # 内存中间结构（编号按固定顺序分配，保证幂等）
        self.prod_batches: list[dict] = []
        self.pack_consumption: dict[tuple[str, int], int] = {}  # (material, month) -> qty
        self.raw_demand: dict[tuple[str, int], int] = {}        # (raw_id, month) -> 粒
        self.fruit_batches: list[dict] = []
        self.fruit_batch_rows: list[list] = []
        self.fruit_qc_rows: list[list] = []
        self.fruit_issue_rows: list[list] = []
        self.fruit_inv_rows: list[list] = []
        self.pack_po_rows: list[list] = []
        self.pack_issue_rows: list[list] = []
        self.pack_inv_rows: list[list] = []
        self.fg_inbound_rows: list[list] = []
        self.fg_inv_rows: list[list] = []
        self.plan_rows: list[list] = []
        self.wo_rows: list[list] = []
        self.pb_rows: list[list] = []

    # ------------------------------------------------------------ 维表
    def gen_dimensions(self) -> None:
        cur = self.con.cursor()
        cur.executemany(
            "INSERT INTO dim_sc_supplier VALUES (?,?,?,?,?,?)",
            [[sid, name, typ, city, rating, DATA_SOURCE]
             for sid, name, typ, city, rating in R.SUPPLIERS],
        )
        cur.executemany(
            "INSERT INTO dim_sc_warehouse VALUES (?,?,?,?)",
            [[wid, name, typ, DATA_SOURCE] for wid, name, typ in R.WAREHOUSES],
        )
        cur.executemany(
            "INSERT INTO dim_sc_material VALUES (?,?,?,?,?,?,?,?)",
            [[mid, name, cat, spec, unit, wh, "active", DATA_SOURCE]
             for mid, name, cat, spec, unit, wh in R.MATERIALS],
        )
        raw_rows = []
        for cat, letter in CAT_LETTER.items():
            for spec in SPEC_LIST:
                raw_rows.append([
                    f"RM-{letter}{spec}", cat, spec, f"{spec}#",
                    "粒", "单果 ≥ 70g，可溶性固形物 ≥ 14%，无霉变/虫伤",
                    FRUIT_WH[cat], DATA_SOURCE,
                ])
        cur.executemany("INSERT INTO dim_fruit_raw VALUES (?,?,?,?,?,?,?,?)", raw_rows)

    def gen_bom(self) -> None:
        rows = []
        bom_id = 0
        for sku_code in sorted(self.skus):
            for item in R.bom_items(self.skus[sku_code]):
                bom_id += 1
                rows.append([bom_id, sku_code, item["item_type"], item["item_id"],
                             item["qty_per_unit"], item["unit"], "V1",
                             "2026-01-01", "active", DATA_SOURCE])
        # V2 版本演示：公开版 SKU-001 标签 1 → 2 张，2026-11-01 生效。
        # V1 行保留不删：历史生产记录（wo.bom_version='V1'）仍可追溯到原用量。
        for item in R.bom_items(self.skus[R.BOM_V2_DEMO_SKU]):
            bom_id += 1
            qty = R.BOM_V2_CHANGES.get(item["item_id"], item["qty_per_unit"])
            rows.append([bom_id, R.BOM_V2_DEMO_SKU, item["item_type"], item["item_id"],
                         qty, item["unit"], "V2", R.BOM_V2_EFFECTIVE,
                         "active", DATA_SOURCE])
        self.con.executemany("INSERT INTO dim_sku_bom VALUES (?,?,?,?,?,?,?,?,?,?)", rows)

    # ------------------------------------------------------------ V2 采购参数
    def gen_procurement_params(self) -> None:
        """8 种包材的采购参数（MOCK 设定）。

        安全库存动态折算 = 10 月领用量 × safety_ratio（约半月用量），
        其余参数（采购周期 / MOQ / 整包装 / 损耗率）按 rules.py 品类设定。
        """
        rows = []
        for mid in sorted(R.PROCUREMENT_PARAM_RULES):
            ratio, lead, moq, package, attr = R.PROCUREMENT_PARAM_RULES[mid]
            consumption_10m = self.pack_consumption.get((mid, 10), 0)
            safety = int(round(consumption_10m * ratio))
            rows.append([mid, R.PROCUREMENT_PARAM_VERSION, safety, lead, moq, package,
                         attr, R.PROCUREMENT_PARAM_EFFECTIVE, "active", DATA_SOURCE])
        self.con.executemany(
            "INSERT INTO dim_sc_procurement_param VALUES (?,?,?,?,?,?,?,?,?,?)", rows)

    # ------------------------------------------------------------ V2 未来生产需求
    def gen_forecast(self) -> dict:
        """SKU × 预测月（2026-11/12）的预计生产量，物化入库。

        forecast_qty = 近 3 个完整月（Y26 M8-10）ERP 出库均值 × Y25 同期季节系数。
        - Y25 同期系数 = Y25 该月 SKU 盒数 ÷ Y25 M8-10 均值；
          Y25 近 3 月无出库（分母 0）时系数取 1.0 并如实标注。
        - 近 3 月无出库（均值为 0）的 SKU：forecast = 0，如实保留行。
        预测口径 = ERP Sell-in（非 POS Sell-out），写入 forecast_note。
        """
        con = sqlite3.connect(f"file:{ETL_DB}?mode=ro", uri=True)
        try:
            cur = con.cursor()
            # Y26 近 3 月按 SKU 盒数
            y26: dict[tuple[str, int], int] = {}
            for sku, m, boxes in cur.execute(
                "SELECT sku_code, month, SUM(sales_boxes) FROM fact_erp_shipment "
                "WHERE year = 2026 GROUP BY sku_code, month"):
                y26[(sku, int(m))] = int(round(boxes))
            # Y25 同期（M8-10 基准 + 预测月）按 SKU 盒数
            y25: dict[tuple[str, int], int] = {}
            for sku, m, boxes in cur.execute(
                "SELECT sku_code, month, SUM(sales_boxes) FROM fact_erp_shipment "
                "WHERE year = ? GROUP BY sku_code, month", (R.SEASONAL_REF_YEAR,)):
                y25[(sku, int(m))] = int(round(boxes))
        finally:
            con.close()

        b1, b2, b3 = R.FORECAST_BASIS_MONTHS
        rows = []
        stats = {"sku_total": 0, "sku_with_demand": 0,
                 "factor_fallback": 0, "factor_range": None}
        for sku_code in sorted(self.skus):
            stats["sku_total"] += 1
            basis_vals = [y26.get((sku_code, m), 0) for m in (b1, b2, b3)]
            avg = sum(basis_vals) / 3.0
            if avg > 0:
                stats["sku_with_demand"] += 1
            for fm in R.FORECAST_MONTHS:
                m_num = int(fm.split("-")[1])
                y25_base_vals = [y25.get((sku_code, m), 0) for m in (b1, b2, b3)]
                y25_base = sum(y25_base_vals) / 3.0
                y25_same = y25.get((sku_code, m_num), 0)
                if y25_base > 0:
                    factor = y25_same / y25_base
                    seasonal_basis = (f"Y25 {m_num} 月 {y25_same:,} 盒 ÷ "
                                      f"Y25 {b1}-{b3} 月均值 {y25_base:,.0f} 盒")
                else:
                    factor = 1.0
                    stats["factor_fallback"] += 1
                    seasonal_basis = f"Y25 {b1}-{b3} 月无该 SKU 出库，系数取 1.0（无同期基准）"
                # 系数先舍入到 4 位再参与计算：库内存的数字与复算完全自洽
                factor_r = round(factor, 4)
                avg_r = round(avg, 2)
                # 预测量只使用随记录一同落库的舍入后输入，确保外部可精确复算。
                qty = int(round(avg_r * factor_r)) if avg_r > 0 else 0
                factors = []
                rows.append([sku_code, fm, qty,
                             f"2026-{b1:02d}", f"2026-{b2:02d}", f"2026-{b3:02d}",
                             avg_r, factor_r, seasonal_basis,
                             R.FORECAST_NOTE, DATA_SOURCE])
                if avg > 0:
                    factors.append(factor_r)
            if factors:
                lo, hi = min(factors), max(factors)
                cur_range = stats["factor_range"]
                stats["factor_range"] = (min(lo, cur_range[0]), max(hi, cur_range[1])) \
                    if cur_range else (lo, hi)
        self.con.executemany(
            "INSERT INTO fact_production_forecast VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        return stats

    # ------------------------------------------------------------ P3 生产
    def gen_production(self) -> None:
        rng = self.rng
        for month in R.MONTHS:
            yymm = f"26{month:02d}"
            for sku_code in sorted(self.skus):
                dem = self.demand.get((sku_code, month), 0)
                if dem <= 0:
                    continue
                sku = self.skus[sku_code]
                plan_qty = int(round(dem * rng.uniform(*R.PROD_COVER_FACTOR)))
                plan_id = f"PLAN-{yymm}-{sku_code}"
                self.plan_rows.append([plan_id, month_str(R.YEAR, month), sku_code,
                                       plan_qty, DATA_SOURCE])
                n_batches = 1 if plan_qty < R.BATCH_SPLIT_THRESHOLD[0] \
                    else 2 if plan_qty < R.BATCH_SPLIT_THRESHOLD[1] else 3
                base = plan_qty // n_batches
                for i in range(n_batches):
                    batch_plan = base + (plan_qty - base * n_batches if i == 0 else 0)
                    wo_id = f"WO-{yymm}-{sku_code}-{i + 1:02d}"
                    self.wo_rows.append([wo_id, plan_id, sku_code, "V1", batch_plan,
                                         "完成", DATA_SOURCE])
                    unq_rate = rng.uniform(*R.UNQUALIFIED_RATE)
                    unqualified = max(0, int(round(batch_plan * unq_rate)))
                    qualified = batch_plan - unqualified
                    if qualified <= 0:
                        qualified, unqualified = batch_plan, 0
                    loss_rate = rng.uniform(*R.LOSS_RATE)
                    gross = (qualified + unqualified) * sku["pieces_per_box"]
                    input_qty = int(round(gross * (1 + loss_rate)))
                    if input_qty <= gross:
                        input_qty = gross + 1
                    loss_pct = round((input_qty - gross) / gross * 100, 4)
                    prod_date = date(R.YEAR, month, rng.randint(*R.PROD_DAY_RANGE))
                    batch_id = self.seq.next("PB", yymm)
                    self.prod_batches.append({
                        "batch_id": batch_id, "sku_code": sku_code, "month": month,
                        "prod_date": prod_date, "input_qty": input_qty,
                        "qualified": qualified, "unqualified": unqualified,
                        "raw_id": sku["raw_material_id"], "fg_wh": sku["fg_wh"],
                        "pieces": sku["pieces_per_box"],
                    })
                    self.pb_rows.append([batch_id, wo_id, sku_code, prod_date.isoformat(),
                                         input_qty, qualified, unqualified, loss_pct,
                                         "完成", DATA_SOURCE])
        self.prod_batches.sort(key=lambda b: (b["month"], b["prod_date"], b["batch_id"]))

    # ------------------------------------------------------------ P2 果萃到货 / 批次 / QC
    def gen_fruit_supply(self) -> None:
        """聚合原料月需求 → 期初结转批次 + 每月到货批次（船次 + QC）。

        代理口径：果萃产区A发船，无采购单。每个批次挂一个船次号（ZSP-），
        ETA 按「计划提前 10~25 天」排定；12% 批次延误 1~5 天（ATA > ETA）。
        到货量按「合格量目标」折算 received：reject 先抽定，
        received = 目标合格量 + rejected，保证 qualified ≥ 当月领用需求。
        """
        rng = self.rng
        for pb in self.prod_batches:
            key = (pb["raw_id"], pb["month"])
            self.raw_demand[key] = self.raw_demand.get(key, 0) + pb["input_qty"]

        for raw_id in sorted({k[0] for k in self.raw_demand}):
            first_month_demand = self.raw_demand.get((raw_id, 1), 0)
            if first_month_demand > 0:
                carry_target = int(round(first_month_demand * rng.uniform(*R.OPENING_CARRY_RATIO)))
                if carry_target > 0:
                    self._add_fruit_batch(raw_id,
                                          date(2025, 12, rng.randint(25, 31)), carry_target,
                                          is_opening=True)
            for month in R.MONTHS:
                need = self.raw_demand.get((raw_id, month), 0)
                if need <= 0:
                    continue
                qualified_target = int(round(need * rng.uniform(*R.FRUIT_ARRIVAL_BUFFER)))
                n_batches = 1 if qualified_target < 1_000_000 \
                    else 2 if qualified_target < 5_000_000 else 3
                base = qualified_target // n_batches
                for i in range(n_batches):
                    target = base + (qualified_target - base * n_batches if i == 0 else 0)
                    arrival = date(R.YEAR, month, rng.randint(*R.FRUIT_ARRIVAL_DAY_RANGE))
                    self._add_fruit_batch(raw_id, arrival, target,
                                          is_opening=False)

    def _add_fruit_batch(self, raw_id: str, arrival: date, qualified_target: int,
                         is_opening: bool) -> None:
        rng = self.rng
        yymm = f"{arrival.year % 100}{arrival.month:02d}"
        shipment_no = self.seq.next(R.ORCHARDIA_SHIPMENT_PREFIX, yymm)
        reject_rate = rng.uniform(*R.REJECT_RATE)
        rejected = max(1, int(round(qualified_target * reject_rate / (1 - reject_rate))))
        received = qualified_target + rejected
        qualified = received - rejected
        rate_pct = round(qualified / received * 100, 2)
        grade = "优" if rate_pct >= R.QUALITY_GRADE_RULE[0] \
            else "良" if rate_pct >= R.QUALITY_GRADE_RULE[1] else "合格"
        # ETA：准时批次提前 0~2 天到港；延误批次（12%）实际晚 1~5 天
        if rng.random() < R.ORCHARDIA_LATE_RATE:
            eta = arrival - timedelta(days=rng.randint(*R.ORCHARDIA_LATE_DAYS))
        else:
            eta = arrival + timedelta(days=rng.randint(*R.ORCHARDIA_EARLY_DAYS))
        batch_id = self.seq.next("FB", yymm)
        self.fruit_batches.append({
            "batch_id": batch_id, "raw_id": raw_id, "shipment_no": shipment_no,
            "arrival": arrival, "received": received, "qualified": qualified,
            "rejected": rejected, "grade": grade, "is_opening": is_opening,
            "remaining": qualified,
        })
        self.fruit_batch_rows.append([
            batch_id, raw_id, shipment_no, R.ORCHARDIA_ORIGIN,
            eta.isoformat(), arrival.isoformat(),
            received, qualified, rejected, grade, "PASS", DATA_SOURCE])
        insp_date = arrival + timedelta(days=rng.randint(*R.FRUIT_QC_LEAD_DAYS))
        sample = max(30, int(round(received * R.QC_SAMPLE_RATE)))
        total_defects = int(round(sample * rejected / received))
        weights = [rng.random() for _ in R.DEFECT_KINDS]
        parts = self._split(total_defects, weights)
        self.fruit_qc_rows.append([
            f"QC-{batch_id[3:]}", batch_id, insp_date.isoformat(), sample,
            parts[0], parts[1], parts[2], parts[3], rate_pct,
            INSPECTORS[rng.randrange(len(INSPECTORS))], DATA_SOURCE])

    @staticmethod
    def _split(total: int, weights: list[float]) -> list[int]:
        if total <= 0 or sum(weights) == 0:
            return [0] * len(weights)
        s = sum(weights)
        raw = [int(round(total * w / s)) for w in weights]
        raw[raw.index(max(raw))] += total - sum(raw)
        return raw

    # ------------------------------------------------------------ 原料领用（FIFO）+ 原料库存
    def gen_fruit_issue_and_inventory(self) -> None:
        """FIFO 领料 + 批次×月库存（opening 取该批次最近前一行 ending，跨月结转）。"""
        rng = self.rng
        # FIFO 队列只装「已到货」批次：期初（2025-12 到货）先入队，
        # 当月到货批次在到货月按到货日排序后入队（避免未来批次提前扣损耗）。
        queues: dict[str, list[dict]] = {}
        for fb in self.fruit_batches:
            if fb["arrival"].year < R.YEAR:
                queues.setdefault(fb["raw_id"], []).append(fb)
        for q in queues.values():
            q.sort(key=lambda b: (b["arrival"], b["batch_id"]))
        # ledger[batch_id] = {month: [opening, in, issue, scrap, ending]}
        ledger: dict[str, dict[int, list[int]]] = {}

        def open_slot(fb: dict, month: int) -> list[int]:
            slots = ledger.setdefault(fb["batch_id"], {})
            if month not in slots:
                if fb["is_opening"] and month == 1:
                    opening = fb["qualified"]
                else:
                    # 结转：取最近前一个月的实际 ending（opening+in-issue-scrap）
                    prev_month = max((m for m in slots if m < month), default=None)
                    if prev_month is not None:
                        pm = slots[prev_month]
                        opening = pm[0] + pm[1] - pm[2] - pm[3]
                    else:
                        opening = 0
                slots[month] = [opening, 0, 0, 0, 0]
            return slots[month]

        arrivals_by_month: dict[int, list[dict]] = {}
        for fb in self.fruit_batches:
            m = fb["arrival"].month if fb["arrival"].year == R.YEAR else 0
            arrivals_by_month.setdefault(m, []).append(fb)

        for month in R.MONTHS:
            arrivals_this_month = arrivals_by_month.get(month, [])
            for fb in arrivals_this_month:
                open_slot(fb, month)[1] += fb["qualified"]
            arrivals_by_raw: dict[str, list[dict]] = {}
            for fb in arrivals_this_month:
                arrivals_by_raw.setdefault(fb["raw_id"], []).append(fb)
            for raw_id, fbs in arrivals_by_raw.items():
                fbs.sort(key=lambda b: (b["arrival"], b["batch_id"]))
                queues.setdefault(raw_id, []).extend(fbs)
            for pb in [b for b in self.prod_batches if b["month"] == month]:
                queue = queues[pb["raw_id"]]
                need = pb["input_qty"]
                while need > 0:
                    if not queue:
                        raise RuntimeError(
                            f"原料供给不足：{pb['raw_id']} {month} 月缺 {need} 粒 "
                            f"（生产批次 {pb['batch_id']}）——需放宽到货 buffer")
                    head = queue[0]
                    take = min(head["remaining"], need)
                    issue_id = self.seq.next("FI", f"26{month:02d}")
                    self.fruit_issue_rows.append([
                        issue_id, pb["batch_id"], head["batch_id"], take,
                        pb["prod_date"].isoformat(), DATA_SOURCE])
                    head["remaining"] -= take
                    open_slot(head, month)[2] += take
                    need -= take
                    if head["remaining"] == 0:
                        queue.pop(0)
            for q in queues.values():
                for fb in q:
                    if fb["remaining"] > 0:
                        scrap = int(round(fb["remaining"] * rng.uniform(*R.SCRAP_RATE)))
                        if scrap > 0:
                            fb["remaining"] -= scrap
                            open_slot(fb, month)[3] += scrap
        # 账实一致性自检 + 展开为行
        for fb in self.fruit_batches:
            slots = ledger.get(fb["batch_id"], {})
            last_month = max(slots) if slots else None
            for month in sorted(slots):
                opening, _in, issue, scrap, _ = slots[month]
                ending = opening + _in - issue - scrap
                if month == last_month and ending != fb["remaining"]:
                    raise RuntimeError(
                        f"批次 {fb['batch_id']} 账实不符：ledger ending={ending}, "
                        f"remaining={fb['remaining']}")
                if opening == 0 and _in == 0 and issue == 0 and scrap == 0 and ending == 0:
                    continue
                self.fruit_inv_rows.append([
                    fb["raw_id"], fb["batch_id"], month_str(R.YEAR, month),
                    opening, _in, issue, scrap, ending, DATA_SOURCE])

    # ------------------------------------------------------------ P1 包材 PO / 领用 / 库存
    def gen_packaging(self) -> None:
        rng = self.rng
        for pb in self.prod_batches:
            sku = self.skus[pb["sku_code"]]
            yymm = f"26{pb['month']:02d}"
            for item in R.bom_items(sku):
                if item["item_type"] != "packaging":
                    continue
                qty = max(1, int(round(pb["qualified"] * item["qty_per_unit"])))
                issue_id = self.seq.next("PI", yymm)
                self.pack_issue_rows.append([
                    issue_id, pb["batch_id"], item["item_id"], qty,
                    pb["prod_date"].isoformat(), DATA_SOURCE])
                key = (item["item_id"], pb["month"])
                self.pack_consumption[key] = self.pack_consumption.get(key, 0) + qty
        pack_suppliers = [s for s in R.SUPPLIERS if s[2] == "packaging"]
        mat_wh = {mid: wh for mid, _n, _c, _s, _u, wh in R.MATERIALS}
        for mid, _name, _cat, _spec, _unit, _wh in R.MATERIALS:
            carry = 0
            for month in R.MONTHS:
                yymm = f"26{month:02d}"
                need = self.pack_consumption.get((mid, month), 0)
                if month == 1 and need > 0:
                    carry = int(round(need * rng.uniform(*R.PACK_OPENING_RATIO)))
                if need <= 0 and carry <= 0:
                    continue
                target_in = 0
                if need > 0:
                    target_in = max(int(round(need * rng.uniform(*R.PACK_ARRIVAL_BUFFER))),
                                    need - carry)
                month_in = 0
                n_po = rng.randint(2, 4) if target_in > 0 else 0
                if n_po > 0:
                    base = target_in // n_po
                    for i in range(n_po):
                        purchase = base + (target_in - base * n_po if i == 0 else 0)
                        supplier = pack_suppliers[
                            (stable_hash(mid) + i + month) % len(pack_suppliers)]
                        order_date = date(R.YEAR, month, rng.randint(1, 20))
                        arrival = order_date + timedelta(days=rng.randint(3, 12))
                        if arrival.month != month:
                            arrival = date(R.YEAR, month, 28)
                        received, status = purchase, "完成"
                        if rng.random() < R.PARTIAL_DELIVERY_RATE:
                            received = max(1, int(round(purchase * rng.uniform(0.6, 0.9))))
                            status = "部分到货"
                        price = int(round(R.PACK_PRICE_CENTS[mid] *
                                          (1 + rng.uniform(-R.PACK_PO_PRICE_JITTER,
                                                           R.PACK_PO_PRICE_JITTER))))
                        po_id = self.seq.next("PPO", yymm)
                        self.pack_po_rows.append([
                            po_id, supplier[0], mid, order_date.isoformat(),
                            arrival.isoformat(), arrival.isoformat(), purchase, received,
                            price, price * purchase, status, DATA_SOURCE])
                        month_in += received
                # 平衡保护：部分到货造成缺口 → 当月该物料 PO 全部补齐到货
                if carry + month_in < need:
                    for row in self.pack_po_rows:
                        if row[2] == mid and row[0].startswith(f"PPO-{yymm}") \
                                and row[7] < row[6]:
                            row[7] = row[6]
                            row[10] = "完成"
                    month_in = sum(r[7] for r in self.pack_po_rows
                                   if r[2] == mid and r[0].startswith(f"PPO-{yymm}"))
                ending = carry + month_in - need
                self.pack_inv_rows.append([
                    mid, mat_wh[mid], month_str(R.YEAR, month), carry, month_in,
                    need, ending, DATA_SOURCE])
                carry = ending
            # 在途 PO：10 月下单、11 月到货
            for _ in range(R.IN_TRANSIT_PO_PER_MATERIAL):
                order_date = date(R.YEAR, 10, rng.randint(5, 25))
                purchase = max(100, int(round(
                    self.pack_consumption.get((mid, 10), 1000) / rng.randint(6, 12))))
                supplier = pack_suppliers[(stable_hash(mid) + 10) % len(pack_suppliers)]
                price = int(round(R.PACK_PRICE_CENTS[mid] *
                                  (1 + rng.uniform(-R.PACK_PO_PRICE_JITTER,
                                                   R.PACK_PO_PRICE_JITTER))))
                po_id = self.seq.next("PPO", "2610")
                self.pack_po_rows.append([
                    po_id, supplier[0], mid, order_date.isoformat(),
                    "2026-11-05", None, purchase, 0, price, price * purchase,
                    "在途", DATA_SOURCE])

    # ------------------------------------------------------------ 成品入库 / 库存
    def gen_fg(self) -> None:
        rng = self.rng
        for pb in self.prod_batches:
            inbound_id = self.seq.next("FGI", f"26{pb['month']:02d}")
            self.fg_inbound_rows.append([
                inbound_id, pb["batch_id"], pb["sku_code"], pb["fg_wh"],
                pb["qualified"], pb["prod_date"].isoformat(), DATA_SOURCE])
        prod_by_sku_month: dict[tuple[str, int], int] = {}
        for pb in self.prod_batches:
            key = (pb["sku_code"], pb["month"])
            prod_by_sku_month[key] = prod_by_sku_month.get(key, 0) + pb["qualified"]
        for sku_code in sorted(self.skus):
            wh = self.skus[sku_code]["fg_wh"]
            carry = 0
            for month in R.MONTHS:
                prod_in = prod_by_sku_month.get((sku_code, month), 0)
                out = self.demand.get((sku_code, month), 0)
                if month == 1 and out > 0:
                    carry = int(round(out * rng.uniform(0.2, 0.4)))
                if carry == 0 and prod_in == 0 and out == 0:
                    continue
                ending = carry + prod_in - out
                if ending < 0:
                    raise RuntimeError(
                        f"成品库存失衡：{sku_code} {month} 月缺 {-ending} 盒")
                self.fg_inv_rows.append([
                    sku_code, wh, month_str(R.YEAR, month), carry, prod_in, out,
                    ending, DATA_SOURCE])
                carry = ending

    # ------------------------------------------------------------ P4 ERP 关联
    def gen_link(self) -> dict:
        link_date = date.today().isoformat()
        con = sqlite3.connect(f"file:{ETL_DB}?mode=ro", uri=True)
        try:
            shipments = con.execute(
                "SELECT shipment_no, sku_code, full_date, year FROM fact_erp_shipment "
                "ORDER BY full_date, shipment_no, sku_code").fetchall()
        finally:
            con.close()
        by_sku: dict[str, tuple[list[str], list[str]]] = {}
        for row in self.fg_inbound_rows:
            _iid, pb_id, sku_code, _wh, _qty, inbound_date, _ds = row
            dates, batches = by_sku.setdefault(sku_code, ([], []))
            dates.append(inbound_date)
            batches.append(pb_id)
        rows = []
        matched = no_prod = no_earlier = 0
        for shipment_no, sku_code, full_date, year in shipments:
            if year == 2026:
                dates, batches = by_sku.get(sku_code, ([], []))
                idx = bisect_right(dates, full_date)
                if idx > 0:
                    rows.append([shipment_no, sku_code, full_date, batches[idx - 1],
                                 "MATCHED", "同 SKU 入库日 ≤ 出库日的最近生产批次",
                                 link_date, DATA_SOURCE])
                    matched += 1
                else:
                    rows.append([shipment_no, sku_code, full_date, None,
                                 "NO_EARLIER_BATCH", "出库日早于该 SKU 所有生产批次入库日",
                                 link_date, DATA_SOURCE])
                    no_earlier += 1
            else:
                rows.append([shipment_no, sku_code, full_date, None,
                             "NO_PRODUCTION_DATA", "2025 年历史出库，本轮仅生成 2026 年生产数据",
                             link_date, DATA_SOURCE])
                no_prod += 1
        self.con.executemany(
            "INSERT INTO link_erp_shipment VALUES (?,?,?,?,?,?,?,?)", rows)
        return {"MATCHED": matched, "NO_PRODUCTION_DATA": no_prod,
                "NO_EARLIER_BATCH": no_earlier}

    # ------------------------------------------------------------ 主流程
    def run(self) -> dict:
        # 幂等重建前抢救历史 sc_run 记录（生成/质检运行清单不随重建丢失）
        prev_runs: list[list] = []
        if SC_DB.exists():
            try:
                old = sqlite3.connect(f"file:{SC_DB}?mode=ro", uri=True)
                prev_runs = old.execute(
                    "SELECT run_id, run_ts, run_type, seed, year, months, "
                    "total_checks, quality_passed, row_summary, note, data_source "
                    "FROM sc_run ORDER BY run_ts").fetchall()
                old.close()
            except sqlite3.Error:
                prev_runs = []
        # 临时路径生成 + 成功后原子替换：避免「connect 后 unlink 再写」的
        # journal/inode 脱钩（disk I/O error），也保证幂等重跑安全。
        tmp_db = Path(str(SC_DB) + ".tmp")
        for suffix in ("", "-journal", "-wal", "-shm"):
            p = Path(str(tmp_db) + suffix)
            if p.exists():
                p.unlink()
        self.con = sqlite3.connect(str(tmp_db))
        self.con.execute("PRAGMA foreign_keys = ON")
        self.con.executescript(SCHEMA.read_text(encoding="utf-8"))
        self.gen_dimensions()
        self.gen_bom()
        self.gen_production()
        self.gen_fruit_supply()
        self.gen_fruit_issue_and_inventory()
        self.gen_packaging()
        self.gen_fg()
        # V2：采购参数须在 gen_packaging 之后（安全库存按 10 月领用量折算）
        self.gen_procurement_params()
        forecast_stats = self.gen_forecast()
        cur = self.con.cursor()
        cur.executemany("INSERT INTO fact_production_plan VALUES (?,?,?,?,?)", self.plan_rows)
        cur.executemany("INSERT INTO fact_production_wo VALUES (?,?,?,?,?,?,?)", self.wo_rows)
        cur.executemany("INSERT INTO fact_production_batch VALUES (?,?,?,?,?,?,?,?,?,?)", self.pb_rows)
        cur.executemany("INSERT INTO fact_fruit_batch VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", self.fruit_batch_rows)
        cur.executemany("INSERT INTO fact_fruit_qc VALUES (?,?,?,?,?,?,?,?,?,?,?)", self.fruit_qc_rows)
        cur.executemany("INSERT INTO fact_fruit_inventory_monthly VALUES (?,?,?,?,?,?,?,?,?)", self.fruit_inv_rows)
        cur.executemany("INSERT INTO fact_fruit_issue VALUES (?,?,?,?,?,?)", self.fruit_issue_rows)
        cur.executemany("INSERT INTO fact_packaging_po VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", self.pack_po_rows)
        cur.executemany("INSERT INTO fact_packaging_issue VALUES (?,?,?,?,?,?)", self.pack_issue_rows)
        cur.executemany("INSERT INTO fact_packaging_inventory_monthly VALUES (?,?,?,?,?,?,?,?)", self.pack_inv_rows)
        cur.executemany("INSERT INTO fact_fg_inbound VALUES (?,?,?,?,?,?,?)", self.fg_inbound_rows)
        cur.executemany("INSERT INTO fact_fg_inventory_monthly VALUES (?,?,?,?,?,?,?,?)", self.fg_inv_rows)
        link_stats = self.gen_link()

        tables = ["dim_sc_supplier", "dim_sc_warehouse", "dim_sc_material", "dim_fruit_raw",
                  "dim_sku_bom", "dim_sc_procurement_param", "fact_production_forecast",
                  "fact_packaging_po", "fact_packaging_inventory_monthly",
                  "fact_packaging_issue", "fact_fruit_batch", "fact_fruit_qc",
                  "fact_fruit_inventory_monthly", "fact_fruit_issue", "fact_production_plan",
                  "fact_production_wo", "fact_production_batch", "fact_fg_inbound",
                  "fact_fg_inventory_monthly", "link_erp_shipment"]
        summary = {t: cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
        if prev_runs:
            cur.executemany("INSERT INTO sc_run VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            [list(r) for r in prev_runs])
        run_id = "SC-RUN-" + time.strftime("%Y%m%d-%H%M%S")
        cur.execute("INSERT INTO sc_run VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [run_id, time.strftime("%Y-%m-%dT%H:%M:%S"), "generate", SEED, R.YEAR,
                     ",".join(str(m) for m in R.MONTHS), 0, 0,
                     json.dumps(summary, ensure_ascii=False),
                     "P1-P4 + V2（采购参数/预测/BOM 版本）生成完成，待 quality 门禁",
                     DATA_SOURCE])
        self.con.commit()
        self.con.close()
        self.con = None
        # 原子替换正式库（旧库连同 journal 一并清掉）
        for suffix in ("-journal", "-wal", "-shm"):
            p = Path(str(SC_DB) + suffix)
            if p.exists():
                p.unlink()
        os.replace(str(tmp_db), str(SC_DB))
        return {"run_id": run_id, "rows": summary, "link": link_stats,
                "forecast": forecast_stats}


def main() -> None:
    gen = Generator()
    result = gen.run()
    print(f"[supply_chain.generate] run_id={result['run_id']} seed={SEED}")
    for table, count in result["rows"].items():
        print(f"  {table:<34} {count:>8,}")
    print("  link_erp_shipment 匹配分布:", result["link"])
    fc = result["forecast"]
    print(f"  预测：{fc['sku_with_demand']}/{fc['sku_total']} 个 SKU 有近 3 月出库，"
          f"系数兜底 {fc['factor_fallback']} 行，"
          f"系数范围 {fc['factor_range']}")
    print(f"  db = {SC_DB}")


if __name__ == "__main__":
    main()
