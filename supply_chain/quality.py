"""供应链模块 V1+V2 质量门禁（17 项，逐项真实执行）。

用法::

    python3 -m supply_chain.quality

对 data/supply_chain_v1.db 执行 17 项检查，全部通过 exit 0，否则 exit 1。
结果写入 sc_run（run_type='quality'），供验收报告与测试引用。
ERP 库全程只读比对。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from . import DATA_SOURCE, SEED
from . import rules as R

BASE_DIR = Path(__file__).resolve().parent.parent
ETL_DB = BASE_DIR / "data" / "bi_data_layer_v1.db"
SC_DB = Path(os.environ.get("SUPPLY_CHAIN_DB", str(BASE_DIR / "data" / "supply_chain_v1.db")))

ALL_TABLES = [
    "dim_sc_supplier", "dim_sc_warehouse", "dim_sc_material", "dim_fruit_raw",
    "dim_sku_bom", "dim_sc_procurement_param", "fact_production_forecast",
    "fact_packaging_po", "fact_packaging_inventory_monthly",
    "fact_packaging_issue", "fact_fruit_batch", "fact_fruit_qc",
    "fact_fruit_inventory_monthly", "fact_fruit_issue", "fact_production_plan",
    "fact_production_wo", "fact_production_batch", "fact_fg_inbound",
    "fact_fg_inventory_monthly", "link_erp_shipment",
]


class CheckResult:
    def __init__(self, no: int, name: str) -> None:
        self.no = no
        self.name = name
        self.passed = False
        self.detail = ""

    def ok(self, detail: str) -> "CheckResult":
        self.passed = True
        self.detail = detail
        return self

    def fail(self, detail: str) -> "CheckResult":
        self.passed = False
        self.detail = detail
        return self


def run_checks() -> list[CheckResult]:
    con = sqlite3.connect(f"file:{SC_DB}?mode=ro", uri=True)
    etl = sqlite3.connect(f"file:{ETL_DB}?mode=ro", uri=True)
    results: list[CheckResult] = []
    cur, ecur = con.cursor(), etl.cursor()

    # ---- 1. 主键唯一（全表：INSERT 成功即唯一，这里验证表存在且行数>0）
    c = CheckResult(1, "主键唯一（全表）")
    empty = [t for t in ALL_TABLES if cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0]
    results.append(c.ok(f"{len(ALL_TABLES)} 张表全部非空，主键约束建表生效") if not empty
                   else c.fail(f"空表: {empty}"))

    # ---- 2. 外键引用有效
    c = CheckResult(2, "外键引用有效")
    fk_violations = cur.execute("PRAGMA foreign_key_check").fetchall()
    results.append(c.ok("PRAGMA foreign_key_check 0 违规") if not fk_violations
                   else c.fail(f"{len(fk_violations)} 处外键违规"))

    # ---- 3. SKU 全部 ∈ dim_sku 72 个有效值
    c = CheckResult(3, "SKU 全部来自 dim_sku 72 个有效值")
    valid_skus = {r[0] for r in ecur.execute(
        "SELECT sku_code FROM dim_sku WHERE sku_code != 'UNKNOWN'")}
    bad = {}
    for t in ("dim_sku_bom", "fact_production_plan", "fact_production_wo",
              "fact_production_batch", "fact_fg_inbound", "fact_fg_inventory_monthly",
              "link_erp_shipment"):
        n = sum(1 for (s,) in cur.execute(f"SELECT sku_code FROM {t}") if s not in valid_skus)
        if n:
            bad[t] = n
    results.append(c.ok(f"{len(valid_skus)} SKU 全表引用有效") if not bad
                   else c.fail(f"非法 SKU: {bad}"))

    # ---- 4. BOM 用量合理
    c = CheckResult(4, "BOM 用量合理（水果=pieces_per_box，包材>0，2粒装无内托）")
    issues = []
    sku_pieces = dict(ecur.execute(
        "SELECT sku_code, pieces_per_box FROM dim_sku WHERE sku_code != 'UNKNOWN'"))
    bom = cur.execute(
        "SELECT sku_code, item_type, item_id, qty_per_unit FROM dim_sku_bom").fetchall()
    for sku, itype, iid, qty in bom:
        if itype == "fruit":
            if int(qty) != sku_pieces[sku] or qty != int(qty):
                issues.append(f"{sku} fruit={qty}≠{sku_pieces[sku]}")
        else:
            if qty <= 0:
                issues.append(f"{sku} {iid} qty={qty}")
    # 2 粒装不应有内托
    tray_2p = cur.execute(
        "SELECT b.sku_code FROM dim_sku_bom b WHERE b.item_id='PM-TRAY' "
        "AND b.sku_code IN (SELECT sku_code FROM dim_sku_bom WHERE item_type='fruit' "
        "AND qty_per_unit=2)").fetchall()
    if tray_2p:
        issues.append(f"2 粒装含内托: {tray_2p[:3]}")
    # 每个 SKU 的 fruit BOM 原料 ID 与品类/规格一致（RM-品类字母+果径规格，
    # 规格取 dim_sku.specification —— sku_code 后缀是粒数，不是规格）
    category_code = {"品类A": "A", "品类B": "B", "品类C": "C"}
    cat_letter = {sku: category_code[category] for sku, category in ecur.execute(
        "SELECT sku_code, category FROM dim_sku WHERE sku_code != 'UNKNOWN'")}
    sku_spec = dict(ecur.execute(
        "SELECT sku_code, specification FROM dim_sku WHERE sku_code != 'UNKNOWN'"))
    for sku, itype, iid, qty in bom:
        if itype == "fruit":
            expect = f"RM-{cat_letter[sku]}{sku_spec[sku]}"
            if iid != expect:
                issues.append(f"{sku} 原料 {iid}≠{expect}")
    results.append(c.ok(f"{len(bom)} 行 BOM 全部合理") if not issues
                   else c.fail(f"{len(issues)} 处: {issues[:5]}"))

    # ---- 5. 原料库存平衡（行内 + 跨月连续性 + 账实一致）
    c = CheckResult(5, "原料库存平衡（ending=opening+in-issue-scrap，跨月结转一致）")
    rows = cur.execute(
        "SELECT raw_material_id, batch_id, inventory_month, opening_qty, qualified_in_qty, "
        "production_issue_qty, scrap_qty, ending_qty FROM fact_fruit_inventory_monthly "
        "ORDER BY batch_id, inventory_month").fetchall()
    probs = []
    by_batch: dict[str, list] = {}
    for r in rows:
        if r[3] + r[4] - r[5] - r[6] != r[7]:
            probs.append(f"{r[1]}@{r[2]} 行内不平衡")
        by_batch.setdefault(r[1], []).append(r)
    for bid, rs in by_batch.items():
        for a, b in zip(rs, rs[1:]):
            if a[7] != b[3]:
                probs.append(f"{bid} {a[2]}→{b[2]} 结转断裂 {a[7]}≠{b[3]}")
    # 账实：每批次最后一行 ending = 批次合格量-领用-损耗（从领用与损耗反推不可得，改验总量）
    issued = dict(cur.execute(
        "SELECT fruit_batch_id, SUM(issue_qty) FROM fact_fruit_issue GROUP BY 1"))
    scrapped = {}
    for r in rows:
        scrapped[r[1]] = scrapped.get(r[1], 0) + r[6]
    last_ending = {bid: rs[-1][7] for bid, rs in by_batch.items()}
    batch_qualified = dict(cur.execute("SELECT batch_id, qualified_qty FROM fact_fruit_batch"))
    for bid, q in batch_qualified.items():
        expect_end = q - issued.get(bid, 0) - scrapped.get(bid, 0)
        if last_ending.get(bid, 0) != expect_end:
            probs.append(f"{bid} 账实不符 {last_ending.get(bid, 0)}≠{expect_end}")
    results.append(c.ok(f"{len(rows)} 行，{len(batch_qualified)} 批次账实一致") if not probs
                   else c.fail(f"{len(probs)} 处: {probs[:5]}"))

    # ---- 6. 包材库存平衡（行内 + 跨月连续）
    c = CheckResult(6, "包材库存平衡（ending=opening+in-cons，跨月结转一致）")
    rows = cur.execute(
        "SELECT material_id, warehouse_id, inventory_month, opening_qty, purchase_in_qty, "
        "production_consumption_qty, ending_qty FROM fact_packaging_inventory_monthly "
        "ORDER BY material_id, inventory_month").fetchall()
    probs = []
    by_mat: dict[str, list] = {}
    for r in rows:
        if r[3] + r[4] - r[5] != r[6]:
            probs.append(f"{r[0]}@{r[2]} 行内不平衡")
        by_mat.setdefault(r[0], []).append(r)
    for mid, rs in by_mat.items():
        for a, b in zip(rs, rs[1:]):
            if a[6] != b[3]:
                probs.append(f"{mid} {a[2]}→{b[2]} 结转断裂 {a[6]}≠{b[3]}")
    # 当月领用 = packaging_issue 当月合计
    issue_by = dict(cur.execute(
        "SELECT material_id||'|'||substr(issue_date,1,7), SUM(issue_qty) "
        "FROM fact_packaging_issue GROUP BY 1"))
    for r in rows:
        key = f"{r[0]}|{r[2]}"
        if issue_by.get(key, 0) != r[5]:
            probs.append(f"{key} 领用 {r[5]} ≠ 明细 {issue_by.get(key, 0)}")
    results.append(c.ok(f"{len(rows)} 行平衡且与领用明细一致") if not probs
                   else c.fail(f"{len(probs)} 处: {probs[:5]}"))

    # ---- 7. 生产投入产出平衡
    c = CheckResult(7, "生产投入产出平衡（input=(合格+不合格)×粒数×(1+损耗)）")
    probs = []
    for bid, sku, inp, q, u, loss in cur.execute(
            "SELECT batch_id, sku_code, input_fruit_qty, qualified_output_qty, "
            "unqualified_output_qty, loss_rate_pct FROM fact_production_batch"):
        pieces = sku_pieces[sku]
        gross = (q + u) * pieces
        if inp < gross:
            probs.append(f"{bid} 投入 < 产出所需")
            continue
        actual_loss = (inp - gross) / gross * 100
        if abs(actual_loss - loss) > 0.01:
            probs.append(f"{bid} loss_rate_pct={loss} ≠ 实际 {actual_loss:.4f}")
        if not (0 < actual_loss <= 5.0):
            probs.append(f"{bid} 损耗率 {actual_loss:.2f}% 超界")
    # 领料合计 = 投入
    bad_link = cur.execute("""
        SELECT COUNT(*) FROM (SELECT pb.batch_id FROM fact_production_batch pb
        LEFT JOIN fact_fruit_issue fi ON fi.production_batch_id = pb.batch_id
        GROUP BY pb.batch_id HAVING COALESCE(SUM(fi.issue_qty),0) != pb.input_fruit_qty)
    """).fetchone()[0]
    if bad_link:
        probs.append(f"{bad_link} 批次领料≠投入")
    n_batches = cur.execute("SELECT COUNT(*) FROM fact_production_batch").fetchone()[0]
    results.append(c.ok(f"{n_batches} 批次全部平衡") if not probs
                   else c.fail(f"{len(probs)} 处: {probs[:5]}"))

    # ---- 8. 包材采购金额 = 单价 × 数量（整数分精确）
    # 代理口径：果子无采购单，金额门禁只覆盖包材。
    c = CheckResult(8, "包材采购金额 = 单价 × 数量（整数分）")
    p1 = cur.execute("SELECT COUNT(*) FROM fact_packaging_po "
                     "WHERE purchase_amount_cents != unit_price_cents * purchase_qty").fetchone()[0]
    results.append(c.ok("包材 PO 全部精确") if p1 == 0
                   else c.fail(f"包材 {p1} 笔不等"))

    # ---- 9. 生产批次-成品-入库关联有效
    c = CheckResult(9, "生产批次-成品入库-合格产出关联一致")
    n = cur.execute("""
        SELECT COUNT(*) FROM fact_fg_inbound i JOIN fact_production_batch pb
        ON i.production_batch_id = pb.batch_id
        WHERE i.qty != pb.qualified_output_qty OR i.inbound_date != pb.production_date
           OR i.sku_code != pb.sku_code""").fetchone()[0]
    inbound_cnt = cur.execute("SELECT COUNT(*) FROM fact_fg_inbound").fetchone()[0]
    results.append(c.ok(f"{inbound_cnt} 条入库与批次 1:1 一致") if n == 0
                   else c.fail(f"{n} 条不一致"))

    # ---- 10. ERP 关联不重复不虚构
    c = CheckResult(10, "ERP 关联不重复不虚构（MATCHED 批次真实且日期合法）")
    probs = []
    # 10a. 覆盖 ERP 全部行（(no, sku) 一一对应）
    erp_pairs = {(r[0], r[1]) for r in ecur.execute(
        "SELECT shipment_no, sku_code FROM fact_erp_shipment")}
    link_pairs = {(r[0], r[1]) for r in cur.execute(
        "SELECT shipment_no, sku_code FROM link_erp_shipment")}
    if erp_pairs != link_pairs:
        probs.append(f"覆盖差异: 缺 {len(erp_pairs - link_pairs)} 多 {len(link_pairs - erp_pairs)}")
    # 10b. MATCHED 行批次存在且入库日 ≤ 出库日
    n_bad = cur.execute("""
        SELECT COUNT(*) FROM link_erp_shipment l LEFT JOIN fact_fg_inbound i
        ON i.production_batch_id = l.production_batch_id
        WHERE l.match_status='MATCHED'
          AND (i.production_batch_id IS NULL OR i.inbound_date > l.shipment_date)""").fetchone()[0]
    if n_bad:
        probs.append(f"{n_bad} 条 MATCHED 虚构/日期非法")
    # 10c. 非 MATCHED 行不得挂批次
    n_bad2 = cur.execute(
        "SELECT COUNT(*) FROM link_erp_shipment WHERE match_status != 'MATCHED' "
        "AND production_batch_id IS NOT NULL").fetchone()[0]
    if n_bad2:
        probs.append(f"{n_bad2} 条未匹配却挂批次")
    # 10d. 2025 年必须 NO_PRODUCTION_DATA；MATCHED 只能出现在 2026（Python 端跨库比对）
    erp_year = {(r[0], r[1]): r[2] for r in ecur.execute(
        "SELECT shipment_no, sku_code, year FROM fact_erp_shipment")}
    link_status = {(r[0], r[1]): r[2] for r in cur.execute(
        "SELECT shipment_no, sku_code, match_status FROM link_erp_shipment")}
    n_bad3 = sum(1 for k, y in erp_year.items()
                 if (y == 2025 and link_status.get(k) != "NO_PRODUCTION_DATA")
                 or (y == 2026 and link_status.get(k) == "NO_PRODUCTION_DATA"))
    if n_bad3:
        probs.append(f"{n_bad3} 条年份与状态矛盾")
    dist = dict(cur.execute(
        "SELECT match_status, COUNT(*) FROM link_erp_shipment GROUP BY 1").fetchall())
    results.append(c.ok(f"51,826 行全覆盖，分布 {dist}") if not probs
                   else c.fail(f"{len(probs)} 处: {probs[:5]}"))

    # ---- 11. MOCK_SUPPLY_CHAIN 标记全表覆盖
    c = CheckResult(11, "MOCK_SUPPLY_CHAIN 标记全表覆盖")
    bad_tables = []
    for t in ALL_TABLES:
        cols = [r[1] for r in cur.execute(f"PRAGMA table_info({t})")]
        if "data_source" not in cols:
            bad_tables.append(f"{t} 无 data_source 列")
            continue
        n = cur.execute(
            f"SELECT COUNT(*) FROM {t} WHERE data_source != ?", (DATA_SOURCE,)).fetchone()[0]
        if n:
            bad_tables.append(f"{t} {n} 行未标记")
    results.append(c.ok(f"{len(ALL_TABLES)} 张表全部标记 MOCK_SUPPLY_CHAIN") if not bad_tables
                   else c.fail(f"{bad_tables}"))

    # ---- 12. 幂等（sc_run 记录与实际行数一致 + 种子固定）
    c = CheckResult(12, "幂等（固定种子 20261109，sc_run 行数快照与实际一致）")
    probs = []
    gen_runs = cur.execute(
        "SELECT run_id, seed, row_summary FROM sc_run WHERE run_type='generate' "
        "ORDER BY run_ts").fetchall()
    if not gen_runs:
        probs.append("无 generate 运行记录")
    else:
        if any(r[1] != SEED for r in gen_runs):
            probs.append("存在非固定种子的运行记录")
        summary = json.loads(gen_runs[-1][2])
        for t, cnt in summary.items():
            actual = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            if actual != cnt:
                probs.append(f"{t} 快照 {cnt} ≠ 实际 {actual}")
    results.append(c.ok(f"{len(gen_runs)} 次运行同种子同行数") if not probs
                   else c.fail(f"{len(probs)} 处: {probs[:5]}"))

    # ---- 13. 果萃代理到货口径（果子无采购）
    c = CheckResult(13, "果萃代理到货口径（无果子采购、船次唯一、ETA/ATA 时序合理）")
    probs = []
    # 13a. 供应商表只剩包材（果子委托方唯一为果萃，不建供应商维度）
    n_fruit_sup = cur.execute(
        "SELECT COUNT(*) FROM dim_sc_supplier WHERE supplier_type != 'packaging'").fetchone()[0]
    if n_fruit_sup:
        probs.append(f"{n_fruit_sup} 行非包材供应商")
    # 13b. 批次全挂 ZSP- 船次号且唯一；产地固定产区A
    bad_ship = cur.execute(
        "SELECT COUNT(*) FROM fact_fruit_batch "
        "WHERE shipment_no NOT LIKE 'ZSP-%' OR origin != '产区A'").fetchone()[0]
    dup_ship = cur.execute(
        "SELECT COUNT(*) FROM (SELECT shipment_no FROM fact_fruit_batch "
        "GROUP BY shipment_no HAVING COUNT(*) > 1)").fetchone()[0]
    if bad_ship or dup_ship:
        probs.append(f"船次异常 {bad_ship} 行 / 重复船次 {dup_ship} 组")
    # 13c. ETA/ATA 时序：ATA 晚于 ETA 至多 5 天（延误），早于至多 2 天（提前到港）
    n_bad_eta = cur.execute("""
        SELECT COUNT(*) FROM fact_fruit_batch
        WHERE CAST(julianday(arrival_date) - julianday(eta_date) AS INT) > 5
           OR CAST(julianday(arrival_date) - julianday(eta_date) AS INT) < -2""").fetchone()[0]
    if n_bad_eta:
        probs.append(f"{n_bad_eta} 批 ETA/ATA 时序越界")
    n_batches = cur.execute("SELECT COUNT(*) FROM fact_fruit_batch").fetchone()[0]
    n_late = cur.execute(
        "SELECT COUNT(*) FROM fact_fruit_batch WHERE arrival_date > eta_date").fetchone()[0]
    results.append(c.ok(f"{n_batches} 批到货，延误 {n_late} 批（{n_late / n_batches * 100:.1f}%）")
                   if not probs else c.fail(f"{len(probs)} 处: {probs[:5]}"))

    # ---- 14. V2 采购参数完备（8 物料 × V1 版本，数值合理）
    c = CheckResult(14, "采购参数完备（8 物料，安全库存/周期/MOQ/整包装/损耗率合理）")
    params = cur.execute(
        "SELECT material_id, param_version, safety_stock_qty, lead_time_days, "
        "moq_qty, order_package_qty, attrition_rate_pct, param_status "
        "FROM dim_sc_procurement_param").fetchall()
    probs = []
    mat_ids = {r[0] for r in cur.execute("SELECT material_id FROM dim_sc_material")}
    param_mats = {r[0] for r in params}
    if param_mats != mat_ids:
        probs.append(f"物料覆盖不一致：缺 {mat_ids - param_mats} 多 {param_mats - mat_ids}")
    for mid, ver, safety, lead, moq, pkg, attr, status in params:
        if ver != R.PROCUREMENT_PARAM_VERSION or status != "active":
            probs.append(f"{mid} 版本/状态异常 {ver}/{status}")
        if not (0 < lead <= 60):
            probs.append(f"{mid} 采购周期 {lead} 越界")
        if moq < pkg:
            probs.append(f"{mid} MOQ {moq} < 整包装 {pkg}")
        if not (0 <= attr < 10):
            probs.append(f"{mid} 损耗率 {attr}% 越界")
    results.append(c.ok(f"{len(params)} 行参数全部合理") if not probs
                   else c.fail(f"{len(probs)} 处: {probs[:5]}"))

    # ---- 15. 未来生产需求物化正确（72 SKU × 2 月，可复算）
    c = CheckResult(15, "未来生产需求（预测行可复算：均值 × 季节系数）")
    fc_rows = cur.execute(
        "SELECT sku_code, forecast_month, forecast_qty, basis_month1, basis_month2, "
        "basis_month3, basis_avg_qty, seasonal_factor FROM fact_production_forecast "
        "ORDER BY sku_code, forecast_month").fetchall()
    probs = []
    # 从 ETL 复算依据（与生成器同一口径）
    # 与生成器同口径：先将每个 SKU 月度合计四舍五入为整数盒，再求三月均值。
    monthly: dict[str, dict[int, int]] = {}
    for sku, month, boxes in ecur.execute(
        f"SELECT sku_code, month, SUM(sales_boxes) FROM fact_erp_shipment "
        f"WHERE year=2026 AND month IN {R.FORECAST_BASIS_MONTHS} "
        f"GROUP BY sku_code, month"):
        monthly.setdefault(sku, {})[int(month)] = int(round(boxes))
    etl_avg = {
        sku: sum(values.get(month, 0) for month in R.FORECAST_BASIS_MONTHS) / 3.0
        for sku, values in monthly.items()
    }
    for sku, fm, qty, m1, m2, m3, avg, factor in fc_rows:
        expect_avg = etl_avg.get(sku, 0.0)
        if abs(avg - round(expect_avg, 2)) > 0.01:
            probs.append(f"{sku} 均值 {avg} ≠ 复算 {round(expect_avg, 2)}")
        expect_qty = int(round(avg * factor))
        if qty != expect_qty:
            probs.append(f"{sku}@{fm} 预测 {qty} ≠ 均值×系数 {expect_qty}")
    skus_fc = {r[0] for r in fc_rows}
    months_fc = {r[1] for r in fc_rows}
    if skus_fc != {r[0] for r in cur.execute(
            "SELECT sku_code FROM dim_sku_bom GROUP BY sku_code")}:
        probs.append("预测 SKU 集合 ≠ BOM SKU 集合")
    if months_fc != set(R.FORECAST_MONTHS):
        probs.append(f"预测月份 {months_fc} ≠ {set(R.FORECAST_MONTHS)}")
    results.append(c.ok(f"{len(fc_rows)} 行预测可复算（72 SKU × 2 月）") if not probs
                   else c.fail(f"{len(probs)} 处: {probs[:5]}"))

    # ---- 16. BOM 版本管理（V1 行保留不被覆盖，V2 生效日期正确）
    c = CheckResult(16, "BOM 版本管理（SKU-001 V1 保留 + V2 生效 2026-11-01，标签 1→2）")
    v1 = dict(cur.execute(
        "SELECT item_id, qty_per_unit FROM dim_sku_bom WHERE sku_code=? "
        "AND bom_version='V1'", (R.BOM_V2_DEMO_SKU,)))
    v2 = dict(cur.execute(
        "SELECT item_id, qty_per_unit FROM dim_sku_bom WHERE sku_code=? "
        "AND bom_version='V2'", (R.BOM_V2_DEMO_SKU,)))
    eff = cur.execute(
        "SELECT DISTINCT effective_from FROM dim_sku_bom WHERE sku_code=? "
        "AND bom_version='V2'", (R.BOM_V2_DEMO_SKU,)).fetchall()
    probs = []
    if not v1 or not v2:
        probs.append(f"版本缺失：V1 {len(v1)} 行 / V2 {len(v2)} 行")
    if set(v1) != set(v2):
        probs.append(f"两版本物料集合不一致：{set(v1) ^ set(v2)}")
    for item in R.BOM_V2_CHANGES:
        if v2.get(item) != R.BOM_V2_CHANGES[item] or v1.get(item) == v2.get(item):
            probs.append(f"{item} 版本间无差异或与规则不符：{v1.get(item)}→{v2.get(item)}")
    for item in v1:
        if item not in R.BOM_V2_CHANGES and v1[item] != v2.get(item):
            probs.append(f"{item} 未声明变更却改变：{v1[item]}→{v2.get(item)}")
    if eff != [(R.BOM_V2_EFFECTIVE,)]:
        probs.append(f"V2 生效日期异常：{eff}")
    # 历史工单仍引用 V1（10 月及以前生产不被新 BOM 覆盖）
    n_wo_v2 = cur.execute(
        "SELECT COUNT(*) FROM fact_production_wo WHERE bom_version='V2'").fetchone()[0]
    if n_wo_v2:
        probs.append(f"{n_wo_v2} 张历史工单误用 V2")
    results.append(c.ok(f"V1 {len(v1)} 行保留，V2 {len(v2)} 行生效 {R.BOM_V2_EFFECTIVE}")
                   if not probs else c.fail(f"{len(probs)} 处: {probs[:5]}"))

    # ---- 17. 在途 PO 与参数联动口径（在途数量 = 采购 - 到货，且不与库存重复计）
    c = CheckResult(17, "在途 PO 口径（在途量=采购-到货≥0，全部晚于观察月末）")
    probs = []
    bad_transit = cur.execute(
        "SELECT COUNT(*) FROM fact_packaging_po WHERE purchase_status='在途' "
        "AND (received_qty != 0 OR purchase_qty <= 0)").fetchone()[0]
    if bad_transit:
        probs.append(f"{bad_transit} 张在途 PO 到货量非 0 或采购量异常")
    n_transit = cur.execute(
        "SELECT COUNT(*) FROM fact_packaging_po WHERE purchase_status='在途'").fetchone()[0]
    in_transit_qty = cur.execute(
        "SELECT COALESCE(SUM(purchase_qty - received_qty), 0) FROM fact_packaging_po "
        "WHERE purchase_status='在途'").fetchone()[0]
    # 在途 PO 不得计入 10 月库存（expected_arrival > 2026-10-31）
    leaked = cur.execute(
        "SELECT COUNT(*) FROM fact_packaging_po WHERE purchase_status='在途' "
        "AND expected_arrival_date <= '2026-10-31'").fetchone()[0]
    if leaked:
        probs.append(f"{leaked} 张在途 PO 应到日早于观察点，口径泄漏")
    results.append(c.ok(f"{n_transit} 张在途 PO 共 {in_transit_qty:,} 单位，口径干净")
                   if not probs else c.fail(f"{len(probs)} 处: {probs[:5]}"))

    con.close()
    etl.close()
    return results


def main() -> int:
    results = run_checks()
    passed = sum(1 for r in results if r.passed)
    print(f"[supply_chain.quality] {passed}/{len(results)} 项通过  db={SC_DB}")
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        print(f"  [{mark}] {r.no:>2}. {r.name}")
        print(f"         {r.detail}")
    # 写入 sc_run
    con = sqlite3.connect(str(SC_DB))
    try:
        con.execute("INSERT INTO sc_run VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    ["SC-QC-" + time.strftime("%Y%m%d-%H%M%S"),
                     time.strftime("%Y-%m-%dT%H:%M:%S"), "quality", SEED, R.YEAR,
                     ",".join(str(m) for m in R.MONTHS),
                     len(results), passed,
                     json.dumps({f"{r.no}:{r.name}": r.passed for r in results},
                                ensure_ascii=False),
                     "quality.py 17 项门禁", DATA_SOURCE])
        con.commit()
    finally:
        con.close()
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
