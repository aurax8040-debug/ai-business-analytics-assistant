-- 供应链模块 V1 DDL（P1–P4）
-- 独立库 data/supply_chain_v1.db，全部数据标记 MOCK_SUPPLY_CHAIN。
-- 金额一律整数分（*_cents）。时间窗 2026-01-01 ~ 2026-10-31，
-- 期初结转原料批次来自 2025-12 到货（有 PO 行，保证外键成立）。
-- 注意：ERP 库中 shipment_no 不唯一（一个发运单多 SKU 行），
-- link_erp_shipment 采用复合主键 (shipment_no, sku_code)。

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- 运行清单
CREATE TABLE sc_run (
    run_id        TEXT PRIMARY KEY,
    run_ts        TEXT NOT NULL,
    run_type      TEXT NOT NULL CHECK (run_type IN ('generate', 'quality')),
    seed          INTEGER NOT NULL,
    year          INTEGER NOT NULL,
    months        TEXT NOT NULL,
    total_checks  INTEGER NOT NULL DEFAULT 0,
    quality_passed INTEGER NOT NULL DEFAULT 0,
    row_summary   TEXT,
    note          TEXT,
    data_source   TEXT NOT NULL DEFAULT 'MOCK_SUPPLY_CHAIN'
);

-- ---------------------------------------------------------------- 维表
CREATE TABLE dim_sc_supplier (
    supplier_id     TEXT PRIMARY KEY,
    supplier_name   TEXT NOT NULL,
    supplier_type   TEXT NOT NULL CHECK (supplier_type = 'packaging'),
    contact_city    TEXT NOT NULL,
    rating          INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    data_source     TEXT NOT NULL DEFAULT 'MOCK_SUPPLY_CHAIN'
);

CREATE TABLE dim_sc_warehouse (
    warehouse_id    TEXT PRIMARY KEY,
    warehouse_name  TEXT NOT NULL,
    warehouse_type  TEXT NOT NULL CHECK (warehouse_type IN ('包材仓', '原料仓', '成品仓', '质检暂存')),
    data_source     TEXT NOT NULL DEFAULT 'MOCK_SUPPLY_CHAIN'
);

CREATE TABLE dim_sc_material (
    material_id     TEXT PRIMARY KEY,
    material_name   TEXT NOT NULL,
    material_category TEXT NOT NULL,
    specification   TEXT NOT NULL,
    unit            TEXT NOT NULL,
    warehouse_id    TEXT NOT NULL REFERENCES dim_sc_warehouse(warehouse_id),
    material_status TEXT NOT NULL DEFAULT 'active',
    data_source     TEXT NOT NULL DEFAULT 'MOCK_SUPPLY_CHAIN'
);

CREATE TABLE dim_fruit_raw (
    raw_material_id TEXT PRIMARY KEY,
    fruit_category  TEXT NOT NULL CHECK (fruit_category IN ('品类A', '品类C', '品类B')),
    specification   INTEGER NOT NULL,
    spec_label      TEXT NOT NULL,
    unit            TEXT NOT NULL DEFAULT '粒',
    grade_standard  TEXT NOT NULL,
    warehouse_id    TEXT NOT NULL REFERENCES dim_sc_warehouse(warehouse_id),
    data_source     TEXT NOT NULL DEFAULT 'MOCK_SUPPLY_CHAIN'
);

CREATE TABLE dim_sku_bom (
    bom_id          INTEGER PRIMARY KEY,
    sku_code        TEXT NOT NULL,
    item_type       TEXT NOT NULL CHECK (item_type IN ('fruit', 'packaging')),
    item_id         TEXT NOT NULL,
    qty_per_unit    REAL NOT NULL CHECK (qty_per_unit > 0),
    unit            TEXT NOT NULL,
    bom_version     TEXT NOT NULL DEFAULT 'V1',
    effective_from  TEXT NOT NULL DEFAULT '2026-01-01',
    bom_status      TEXT NOT NULL DEFAULT 'active',
    data_source     TEXT NOT NULL DEFAULT 'MOCK_SUPPLY_CHAIN',
    UNIQUE (sku_code, item_type, item_id, bom_version)
);

-- ---------------------------------------------------------------- P1 包材
CREATE TABLE fact_packaging_po (
    po_id                TEXT PRIMARY KEY,
    supplier_id          TEXT NOT NULL REFERENCES dim_sc_supplier(supplier_id),
    material_id          TEXT NOT NULL REFERENCES dim_sc_material(material_id),
    order_date           TEXT NOT NULL,
    expected_arrival_date TEXT NOT NULL,
    actual_arrival_date  TEXT,
    purchase_qty         INTEGER NOT NULL CHECK (purchase_qty > 0),
    received_qty         INTEGER NOT NULL CHECK (received_qty >= 0),
    unit_price_cents     INTEGER NOT NULL CHECK (unit_price_cents > 0),
    purchase_amount_cents INTEGER NOT NULL CHECK (purchase_amount_cents >= 0),
    purchase_status      TEXT NOT NULL CHECK (purchase_status IN ('完成', '部分到货', '在途')),
    data_source          TEXT NOT NULL DEFAULT 'MOCK_SUPPLY_CHAIN'
);

CREATE TABLE fact_packaging_inventory_monthly (
    material_id                TEXT NOT NULL REFERENCES dim_sc_material(material_id),
    warehouse_id               TEXT NOT NULL REFERENCES dim_sc_warehouse(warehouse_id),
    inventory_month            TEXT NOT NULL,
    opening_qty                INTEGER NOT NULL CHECK (opening_qty >= 0),
    purchase_in_qty            INTEGER NOT NULL CHECK (purchase_in_qty >= 0),
    production_consumption_qty INTEGER NOT NULL CHECK (production_consumption_qty >= 0),
    ending_qty                 INTEGER NOT NULL CHECK (ending_qty >= 0),
    data_source                TEXT NOT NULL DEFAULT 'MOCK_SUPPLY_CHAIN',
    PRIMARY KEY (material_id, warehouse_id, inventory_month),
    CHECK (ending_qty = opening_qty + purchase_in_qty - production_consumption_qty)
);

-- ---------------------------------------------------------------- P2 果萃到货（代理，无采购）
-- 星禾代理果萃：果子没有采购单，产区A发船到港后直接入库。
-- 供应链只关注到货时间（ETA 计划到港 / ATA 实际到港）与数量（分品类×果径规格）。
-- 代理结算价属财务域，不建模。

CREATE TABLE fact_fruit_batch (
    batch_id         TEXT PRIMARY KEY,
    raw_material_id  TEXT NOT NULL REFERENCES dim_fruit_raw(raw_material_id),
    shipment_no      TEXT NOT NULL,       -- 果萃船次号（柜号），ZSP- 前缀
    origin           TEXT NOT NULL DEFAULT '产区A',
    eta_date         TEXT NOT NULL,       -- 计划到港日
    arrival_date     TEXT NOT NULL,       -- 实际到港日（即入库时点）
    received_qty     INTEGER NOT NULL CHECK (received_qty > 0),
    qualified_qty    INTEGER NOT NULL CHECK (qualified_qty >= 0),
    rejected_qty     INTEGER NOT NULL CHECK (rejected_qty >= 0),
    quality_grade    TEXT NOT NULL CHECK (quality_grade IN ('优', '良', '合格')),
    qc_status        TEXT NOT NULL DEFAULT 'PASS',
    data_source      TEXT NOT NULL DEFAULT 'MOCK_SUPPLY_CHAIN',
    CHECK (received_qty = qualified_qty + rejected_qty)
);

CREATE TABLE fact_fruit_qc (
    qc_id                    TEXT PRIMARY KEY,
    batch_id                 TEXT NOT NULL REFERENCES fact_fruit_batch(batch_id),
    inspection_date          TEXT NOT NULL,
    sample_qty               INTEGER NOT NULL CHECK (sample_qty > 0),
    defect_substandard_qty   INTEGER NOT NULL CHECK (defect_substandard_qty >= 0),
    defect_bruise_qty        INTEGER NOT NULL CHECK (defect_bruise_qty >= 0),
    defect_soft_qty          INTEGER NOT NULL CHECK (defect_soft_qty >= 0),
    defect_small_qty         INTEGER NOT NULL CHECK (defect_small_qty >= 0),
    qualified_rate_pct       REAL NOT NULL CHECK (qualified_rate_pct BETWEEN 0 AND 100),
    inspector                TEXT NOT NULL,
    data_source              TEXT NOT NULL DEFAULT 'MOCK_SUPPLY_CHAIN'
);

CREATE TABLE fact_fruit_inventory_monthly (
    raw_material_id   TEXT NOT NULL REFERENCES dim_fruit_raw(raw_material_id),
    batch_id          TEXT NOT NULL REFERENCES fact_fruit_batch(batch_id),
    inventory_month   TEXT NOT NULL,
    opening_qty       INTEGER NOT NULL CHECK (opening_qty >= 0),
    qualified_in_qty  INTEGER NOT NULL CHECK (qualified_in_qty >= 0),
    production_issue_qty INTEGER NOT NULL CHECK (production_issue_qty >= 0),
    scrap_qty         INTEGER NOT NULL CHECK (scrap_qty >= 0),
    ending_qty        INTEGER NOT NULL CHECK (ending_qty >= 0),
    data_source       TEXT NOT NULL DEFAULT 'MOCK_SUPPLY_CHAIN',
    PRIMARY KEY (raw_material_id, batch_id, inventory_month),
    CHECK (ending_qty = opening_qty + qualified_in_qty - production_issue_qty - scrap_qty)
);

-- ---------------------------------------------------------------- P3 生产
CREATE TABLE fact_production_plan (
    plan_id      TEXT PRIMARY KEY,
    plan_month   TEXT NOT NULL,
    sku_code     TEXT NOT NULL,
    planned_qty  INTEGER NOT NULL CHECK (planned_qty > 0),
    data_source  TEXT NOT NULL DEFAULT 'MOCK_SUPPLY_CHAIN'
);

CREATE TABLE fact_production_wo (
    wo_id        TEXT PRIMARY KEY,
    plan_id      TEXT NOT NULL REFERENCES fact_production_plan(plan_id),
    sku_code     TEXT NOT NULL,
    bom_version  TEXT NOT NULL DEFAULT 'V1',
    planned_qty  INTEGER NOT NULL CHECK (planned_qty > 0),
    status       TEXT NOT NULL DEFAULT '完成',
    data_source  TEXT NOT NULL DEFAULT 'MOCK_SUPPLY_CHAIN'
);

CREATE TABLE fact_production_batch (
    batch_id               TEXT PRIMARY KEY,
    wo_id                  TEXT NOT NULL REFERENCES fact_production_wo(wo_id),
    sku_code               TEXT NOT NULL,
    production_date        TEXT NOT NULL,
    input_fruit_qty        INTEGER NOT NULL CHECK (input_fruit_qty > 0),
    qualified_output_qty   INTEGER NOT NULL CHECK (qualified_output_qty > 0),
    unqualified_output_qty INTEGER NOT NULL CHECK (unqualified_output_qty >= 0),
    loss_rate_pct          REAL NOT NULL CHECK (loss_rate_pct >= 0),
    status                 TEXT NOT NULL DEFAULT '完成',
    data_source            TEXT NOT NULL DEFAULT 'MOCK_SUPPLY_CHAIN'
);

CREATE TABLE fact_fruit_issue (
    issue_id            TEXT PRIMARY KEY,
    production_batch_id TEXT NOT NULL REFERENCES fact_production_batch(batch_id),
    fruit_batch_id      TEXT NOT NULL REFERENCES fact_fruit_batch(batch_id),
    issue_qty           INTEGER NOT NULL CHECK (issue_qty > 0),
    issue_date          TEXT NOT NULL,
    data_source         TEXT NOT NULL DEFAULT 'MOCK_SUPPLY_CHAIN'
);

CREATE TABLE fact_packaging_issue (
    issue_id            TEXT PRIMARY KEY,
    production_batch_id TEXT NOT NULL REFERENCES fact_production_batch(batch_id),
    material_id         TEXT NOT NULL REFERENCES dim_sc_material(material_id),
    issue_qty           INTEGER NOT NULL CHECK (issue_qty > 0),
    issue_date          TEXT NOT NULL,
    data_source         TEXT NOT NULL DEFAULT 'MOCK_SUPPLY_CHAIN'
);

CREATE TABLE fact_fg_inbound (
    inbound_id          TEXT PRIMARY KEY,
    production_batch_id TEXT NOT NULL REFERENCES fact_production_batch(batch_id),
    sku_code            TEXT NOT NULL,
    warehouse_id        TEXT NOT NULL REFERENCES dim_sc_warehouse(warehouse_id),
    qty                 INTEGER NOT NULL CHECK (qty > 0),
    inbound_date        TEXT NOT NULL,
    data_source         TEXT NOT NULL DEFAULT 'MOCK_SUPPLY_CHAIN'
);

CREATE TABLE fact_fg_inventory_monthly (
    sku_code         TEXT NOT NULL,
    warehouse_id     TEXT NOT NULL REFERENCES dim_sc_warehouse(warehouse_id),
    inventory_month  TEXT NOT NULL,
    opening_qty      INTEGER NOT NULL CHECK (opening_qty >= 0),
    production_in_qty INTEGER NOT NULL CHECK (production_in_qty >= 0),
    erp_out_qty      INTEGER NOT NULL CHECK (erp_out_qty >= 0),
    ending_qty       INTEGER NOT NULL CHECK (ending_qty >= 0),
    data_source      TEXT NOT NULL DEFAULT 'MOCK_SUPPLY_CHAIN',
    PRIMARY KEY (sku_code, warehouse_id, inventory_month),
    CHECK (ending_qty = opening_qty + production_in_qty - erp_out_qty)
);

-- ---------------------------------------------------------------- P4 ERP 关联
CREATE TABLE link_erp_shipment (
    shipment_no         TEXT NOT NULL,
    sku_code            TEXT NOT NULL,
    shipment_date       TEXT NOT NULL,
    production_batch_id TEXT,
    match_status        TEXT NOT NULL CHECK (match_status IN
                        ('MATCHED', 'NO_PRODUCTION_DATA', 'NO_EARLIER_BATCH')),
    match_basis         TEXT NOT NULL,
    link_date           TEXT NOT NULL,
    data_source         TEXT NOT NULL DEFAULT 'MOCK_SUPPLY_CHAIN',
    PRIMARY KEY (shipment_no, sku_code)
);

CREATE INDEX idx_bom_sku       ON dim_sku_bom(sku_code);
CREATE INDEX idx_ppo_material  ON fact_packaging_po(material_id);
CREATE INDEX idx_fbatch_raw    ON fact_fruit_batch(raw_material_id);
CREATE INDEX idx_pbatch_sku    ON fact_production_batch(sku_code, production_date);
CREATE INDEX idx_fi_pbatch     ON fact_fruit_issue(production_batch_id);
CREATE INDEX idx_pi_pbatch     ON fact_packaging_issue(production_batch_id);
CREATE INDEX idx_fg_sku_date   ON fact_fg_inbound(sku_code, inbound_date);
CREATE INDEX idx_link_status   ON link_erp_shipment(match_status);

-- ---------------------------------------------------------------- V2 采购参数
-- 包材采购参数（版本化）：安全库存 / 采购周期 / 最小起订量 / 采购整包装 / 损耗率。
-- 全部为 MOCK 设定（安全库存按 10 月领用量 × 比例动态折算），页面须标注。
CREATE TABLE dim_sc_procurement_param (
    material_id       TEXT NOT NULL REFERENCES dim_sc_material(material_id),
    param_version     TEXT NOT NULL,
    safety_stock_qty  INTEGER NOT NULL CHECK (safety_stock_qty >= 0),
    lead_time_days    INTEGER NOT NULL CHECK (lead_time_days > 0),
    moq_qty           INTEGER NOT NULL CHECK (moq_qty > 0),
    order_package_qty INTEGER NOT NULL CHECK (order_package_qty > 0),
    attrition_rate_pct REAL NOT NULL CHECK (attrition_rate_pct >= 0),
    effective_from    TEXT NOT NULL,
    param_status      TEXT NOT NULL DEFAULT 'active'
                      CHECK (param_status IN ('active', 'superseded', 'pending_config')),
    data_source       TEXT NOT NULL DEFAULT 'MOCK_SUPPLY_CHAIN',
    PRIMARY KEY (material_id, param_version)
);

-- ---------------------------------------------------------------- V2 未来生产需求
-- SKU × 预测月的预计生产量（物化，可追溯）：
-- forecast_qty = 近 3 个完整月 ERP 出库均值 × Y25 同期季节系数（四舍五入）。
-- basis_* 字段保留完整计算依据；预测口径 = ERP Sell-in（非 POS Sell-out）。
CREATE TABLE fact_production_forecast (
    sku_code       TEXT NOT NULL,
    forecast_month TEXT NOT NULL,
    forecast_qty   INTEGER NOT NULL CHECK (forecast_qty >= 0),
    basis_month1   TEXT NOT NULL,
    basis_month2   TEXT NOT NULL,
    basis_month3   TEXT NOT NULL,
    basis_avg_qty  REAL NOT NULL CHECK (basis_avg_qty >= 0),
    seasonal_factor REAL NOT NULL CHECK (seasonal_factor > 0),
    seasonal_basis TEXT NOT NULL,
    forecast_note  TEXT NOT NULL,
    data_source    TEXT NOT NULL DEFAULT 'MOCK_SUPPLY_CHAIN',
    PRIMARY KEY (sku_code, forecast_month)
);
CREATE INDEX idx_forecast_month ON fact_production_forecast(forecast_month);
