-- ===========================================================================
-- 模拟销售预算（TE 口径）V1 · 表结构
--
-- 与 data/budget_v1.db（金额预算，单位「分」）**物理分离**：
--   业务口径已明确：预算考核指标是销售量 TE，不是销售金额。
--   金额预算库保持原样、不被覆盖、不被本页引用；本库只存 TE 数量预算。
--
-- 计量单位：TE（Tray Equivalent，托盘当量）。存整数「毫 TE」（1 TE = 1000），
--           避免浮点误差，使「年 = 4 季 = 12 月」是精确相等而不是近似相等。
-- 数据来源：模拟数据，data_source 恒为 MOCK_BUDGET_TE，CHECK 从物理上拒绝其他值。
-- ===========================================================================

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- 批次元数据
CREATE TABLE IF NOT EXISTS te_budget_batch (
    batch_id                TEXT PRIMARY KEY,
    budget_version          TEXT    NOT NULL,
    year                    INTEGER NOT NULL,
    unit                    TEXT    NOT NULL DEFAULT 'TE',
    unit_scale              INTEGER NOT NULL DEFAULT 1000,
    data_source             TEXT    NOT NULL CHECK (data_source = 'MOCK_BUDGET_TE'),
    seed                    INTEGER NOT NULL,
    generated_at            TEXT    NOT NULL,
    generator               TEXT    NOT NULL,
    generator_version       TEXT    NOT NULL,
    entity_count            INTEGER NOT NULL,
    month_row_count         INTEGER NOT NULL,
    annual_budget_te        INTEGER NOT NULL,   -- 毫 TE
    ytd_budget_te           INTEGER NOT NULL,   -- 1–10 月
    ytd_actual_te           INTEGER NOT NULL,   -- ERP 实际
    ytd_attainment_pct      REAL,
    annual_progress_pct     REAL,
    band_distribution_json  TEXT    NOT NULL,
    tier_distribution_json  TEXT    NOT NULL,
    params_json             TEXT    NOT NULL,
    erp_db_md5              TEXT    NOT NULL,
    erp_row_count           INTEGER NOT NULL,
    erp_te_milli            INTEGER NOT NULL,
    amount_budget_db_md5    TEXT    NOT NULL,   -- 金额预算库指纹，证明它没被改过
    status                  TEXT    NOT NULL CHECK (status IN ('ACTIVE', 'SUPERSEDED')),
    notice                  TEXT    NOT NULL
);

-- ---------------------------------------------------------------- 年月维表
CREATE TABLE IF NOT EXISTS dim_te_budget_month (
    year            INTEGER NOT NULL,
    month           INTEGER NOT NULL CHECK (month BETWEEN 1 AND 12),
    quarter         INTEGER NOT NULL CHECK (quarter BETWEEN 1 AND 4),
    days_in_month   INTEGER NOT NULL,
    in_observation  TEXT    NOT NULL CHECK (in_observation IN ('Y', 'N')),
    PRIMARY KEY (year, month)
);

-- ---------------------------------------------------------------- 原子事实：客户×战区×市场部×月
CREATE TABLE IF NOT EXISTS fact_te_budget_monthly (
    budget_key          INTEGER PRIMARY KEY,
    batch_id            TEXT    NOT NULL,
    budget_version      TEXT    NOT NULL,
    year                INTEGER NOT NULL,
    month               INTEGER NOT NULL,
    quarter             INTEGER NOT NULL,
    customer_key        INTEGER NOT NULL,
    customer_id         TEXT    NOT NULL,
    customer_name       TEXT    NOT NULL,
    bi_region_key       INTEGER NOT NULL,
    region_name         TEXT    NOT NULL,
    md_key              INTEGER NOT NULL,
    dept_short          TEXT    NOT NULL,
    channel_key         INTEGER,
    channel_type        TEXT,
    customer_type       TEXT,
    budget_te           INTEGER NOT NULL CHECK (budget_te >= 0),   -- 毫 TE
    in_observation      TEXT    NOT NULL,
    data_source         TEXT    NOT NULL CHECK (data_source = 'MOCK_BUDGET_TE'),
    seed                INTEGER NOT NULL,
    generated_at        TEXT    NOT NULL,
    UNIQUE (batch_id, customer_key, bi_region_key, md_key, month)
);

CREATE INDEX IF NOT EXISTS ix_te_budget_month ON fact_te_budget_monthly (batch_id, month);
CREATE INDEX IF NOT EXISTS ix_te_budget_region ON fact_te_budget_monthly (batch_id, bi_region_key);
CREATE INDEX IF NOT EXISTS ix_te_budget_customer ON fact_te_budget_monthly (batch_id, customer_id);
CREATE INDEX IF NOT EXISTS ix_te_budget_entity
    ON fact_te_budget_monthly (batch_id, customer_key, bi_region_key, md_key);

-- ---------------------------------------------------------------- 生成假设（逐实体可追溯）
CREATE TABLE IF NOT EXISTS te_budget_assumption (
    batch_id            TEXT    NOT NULL,
    budget_version      TEXT    NOT NULL,
    customer_key        INTEGER NOT NULL,
    customer_id         TEXT    NOT NULL,
    customer_name       TEXT    NOT NULL,
    bi_region_key       INTEGER NOT NULL,
    region_name         TEXT    NOT NULL,
    md_key              INTEGER NOT NULL,
    dept_short          TEXT    NOT NULL,
    channel_type        TEXT,
    customer_type       TEXT,
    tier                TEXT    NOT NULL CHECK (tier IN ('A', 'B', 'C')),
    target_band         TEXT    NOT NULL CHECK (target_band IN ('OVER', 'ON', 'UNDER', 'SEVERE')),
    band_low_pct        REAL    NOT NULL,
    band_high_pct       REAL,
    target_attainment   REAL    NOT NULL,
    actual_months       INTEGER NOT NULL,
    ytd_actual_te       INTEGER NOT NULL,
    ytd_budget_te       INTEGER NOT NULL,
    attainment_pct      REAL,
    annual_budget_te    INTEGER NOT NULL,
    profile_json        TEXT    NOT NULL,     -- 12 个月权重，可复算
    baseline_note       TEXT    NOT NULL,
    data_source         TEXT    NOT NULL CHECK (data_source = 'MOCK_BUDGET_TE'),
    seed                INTEGER NOT NULL,
    generated_at        TEXT    NOT NULL,
    PRIMARY KEY (batch_id, customer_key, bi_region_key, md_key)
);

-- ---------------------------------------------------------------- 下卷视图（只读）
-- 季度 / 年度不单独落表：TE 是数量，SUM 天然守恒，视图即口径，
-- 也就不存在「同一预算在多个粒度各存一份、彼此对不上」的可能。
CREATE VIEW IF NOT EXISTS v_te_budget_quarterly AS
SELECT batch_id, budget_version, year, quarter,
       customer_key, customer_id, customer_name, bi_region_key, region_name,
       md_key, dept_short, channel_type, customer_type,
       SUM(budget_te) AS budget_te,
       MIN(in_observation) AS in_observation,
       data_source, seed
FROM fact_te_budget_monthly
GROUP BY batch_id, budget_version, year, quarter, customer_key, customer_id,
         customer_name, bi_region_key, region_name, md_key, dept_short,
         channel_type, customer_type, data_source, seed;

CREATE VIEW IF NOT EXISTS v_te_budget_yearly AS
SELECT batch_id, budget_version, year,
       customer_key, customer_id, customer_name, bi_region_key, region_name,
       md_key, dept_short, channel_type, customer_type,
       SUM(budget_te) AS budget_te,
       SUM(CASE WHEN in_observation = 'Y' THEN budget_te ELSE 0 END) AS ytd_budget_te,
       data_source, seed
FROM fact_te_budget_monthly
GROUP BY batch_id, budget_version, year, customer_key, customer_id, customer_name,
         bi_region_key, region_name, md_key, dept_short, channel_type, customer_type,
         data_source, seed;
