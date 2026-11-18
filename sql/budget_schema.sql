-- =====================================================================
-- 模拟销售预算 V1 · 表结构（MOCK_BUDGET）
--
-- 目标库：data/budget_v1.db（独立库，不写入 ETL 库）
-- 关联方式：查询时 ATTACH 'file:data/budget_v1.db?mode=ro' AS bud
--
-- 设计要点：
--   1. 只有 fact_budget_weekly 是写入点，年/季/月三张表由它整数下卷。
--   2. 最小时间原子是「周 × 月片段」，跨月周按自然日拆成两条记录。
--   3. 金额一律以「分」存 INTEGER，守恒断言用严格相等而非容差。
--   4. 所有事实表 CHECK 强制 data_source = 'MOCK_BUDGET'，物理上排除混入真实预算。
-- =====================================================================

PRAGMA foreign_keys = OFF;

-- ① 批次元数据 ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS budget_batch (
    batch_id                TEXT PRIMARY KEY,
    budget_version          TEXT    NOT NULL,
    year                    INTEGER NOT NULL,
    data_source             TEXT    NOT NULL DEFAULT 'MOCK_BUDGET'
                                    CHECK (data_source = 'MOCK_BUDGET'),
    seed                    INTEGER NOT NULL,
    generated_at            TEXT    NOT NULL,
    generator               TEXT    NOT NULL,
    generator_version       TEXT    NOT NULL,
    entity_count            INTEGER NOT NULL,
    segment_count           INTEGER NOT NULL,
    annual_budget_cents     INTEGER NOT NULL,
    ytd_budget_cents        INTEGER NOT NULL,
    ytd_actual_cents        INTEGER NOT NULL,
    ytd_attainment_pct      REAL    NOT NULL,
    annual_progress_pct     REAL    NOT NULL,
    band_distribution_json  TEXT    NOT NULL,
    tier_distribution_json  TEXT    NOT NULL,
    params_json             TEXT    NOT NULL,
    erp_db_md5              TEXT    NOT NULL,
    erp_row_count           INTEGER NOT NULL,
    erp_amount_cents        INTEGER NOT NULL,
    status                  TEXT    NOT NULL DEFAULT 'ACTIVE'
                                    CHECK (status IN ('ACTIVE','SUPERSEDED','DISCARDED')),
    notice                  TEXT    NOT NULL
);

-- ② 预算日历（2026 全年 365 天；1-10 月与 dim_date 逐日对齐）---------------
CREATE TABLE IF NOT EXISTS dim_budget_calendar (
    calendar_key        INTEGER PRIMARY KEY,
    full_date           TEXT    NOT NULL UNIQUE,
    year                INTEGER NOT NULL,
    quarter             INTEGER NOT NULL,
    month               INTEGER NOT NULL,
    week_of_year        INTEGER NOT NULL,
    week_start_date     TEXT    NOT NULL,
    week_end_date       TEXT    NOT NULL,
    day_of_week         INTEGER NOT NULL,
    in_observation      TEXT    NOT NULL CHECK (in_observation IN ('Y','N'))
);

-- ③ 周 × 月片段（63 行；跨月拆分的唯一权威定义）----------------------------
CREATE TABLE IF NOT EXISTS dim_budget_week_segment (
    segment_key         INTEGER PRIMARY KEY,
    year                INTEGER NOT NULL,
    week_of_year        INTEGER NOT NULL,
    month               INTEGER NOT NULL,
    quarter             INTEGER NOT NULL,
    week_start_date     TEXT    NOT NULL,
    week_end_date       TEXT    NOT NULL,
    seg_start_date      TEXT    NOT NULL,
    seg_end_date        TEXT    NOT NULL,
    seg_days            INTEGER NOT NULL CHECK (seg_days > 0),
    week_days           INTEGER NOT NULL CHECK (week_days > 0),
    in_observation      TEXT    NOT NULL CHECK (in_observation IN ('Y','N')),
    UNIQUE (year, week_of_year, month)
);

-- ④ 原子事实：客户-组织 × 周 × 月片段 --------------------------------------
CREATE TABLE IF NOT EXISTS fact_budget_weekly (
    budget_key          INTEGER PRIMARY KEY,
    batch_id            TEXT    NOT NULL,
    budget_version      TEXT    NOT NULL,
    year                INTEGER NOT NULL,
    week_of_year        INTEGER NOT NULL,
    month               INTEGER NOT NULL,
    quarter             INTEGER NOT NULL,
    seg_start_date      TEXT    NOT NULL,
    seg_end_date        TEXT    NOT NULL,
    seg_days            INTEGER NOT NULL,
    customer_key        INTEGER NOT NULL,
    customer_id         TEXT    NOT NULL,
    customer_name       TEXT    NOT NULL,
    bi_region_key       INTEGER NOT NULL,
    region_name         TEXT    NOT NULL,
    md_key              INTEGER NOT NULL,
    dept_short          TEXT    NOT NULL,
    channel_key         INTEGER NOT NULL,
    channel_type        TEXT    NOT NULL,
    customer_type       TEXT    NOT NULL,
    budget_amount_cents INTEGER NOT NULL CHECK (budget_amount_cents > 0),
    currency            TEXT    NOT NULL DEFAULT 'CNY',
    data_source         TEXT    NOT NULL DEFAULT 'MOCK_BUDGET'
                                CHECK (data_source = 'MOCK_BUDGET'),
    seed                INTEGER NOT NULL,
    generated_at        TEXT    NOT NULL,
    UNIQUE (batch_id, customer_key, bi_region_key, md_key, year, week_of_year, month)
);

-- ⑤ 月度派生表 ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fact_budget_monthly (
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
    channel_key         INTEGER NOT NULL,
    channel_type        TEXT    NOT NULL,
    customer_type       TEXT    NOT NULL,
    segment_count       INTEGER NOT NULL,
    in_observation      TEXT    NOT NULL CHECK (in_observation IN ('Y','N')),
    budget_amount_cents INTEGER NOT NULL CHECK (budget_amount_cents > 0),
    currency            TEXT    NOT NULL DEFAULT 'CNY',
    data_source         TEXT    NOT NULL DEFAULT 'MOCK_BUDGET'
                                CHECK (data_source = 'MOCK_BUDGET'),
    seed                INTEGER NOT NULL,
    generated_at        TEXT    NOT NULL,
    UNIQUE (batch_id, customer_key, bi_region_key, md_key, year, month)
);

-- ⑥ 季度派生表 ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fact_budget_quarterly (
    budget_key          INTEGER PRIMARY KEY,
    batch_id            TEXT    NOT NULL,
    budget_version      TEXT    NOT NULL,
    year                INTEGER NOT NULL,
    quarter             INTEGER NOT NULL,
    customer_key        INTEGER NOT NULL,
    customer_id         TEXT    NOT NULL,
    customer_name       TEXT    NOT NULL,
    bi_region_key       INTEGER NOT NULL,
    region_name         TEXT    NOT NULL,
    md_key              INTEGER NOT NULL,
    dept_short          TEXT    NOT NULL,
    channel_key         INTEGER NOT NULL,
    channel_type        TEXT    NOT NULL,
    customer_type       TEXT    NOT NULL,
    month_count         INTEGER NOT NULL,
    budget_amount_cents INTEGER NOT NULL CHECK (budget_amount_cents > 0),
    currency            TEXT    NOT NULL DEFAULT 'CNY',
    data_source         TEXT    NOT NULL DEFAULT 'MOCK_BUDGET'
                                CHECK (data_source = 'MOCK_BUDGET'),
    seed                INTEGER NOT NULL,
    generated_at        TEXT    NOT NULL,
    UNIQUE (batch_id, customer_key, bi_region_key, md_key, year, quarter)
);

-- ⑦ 年度派生表 ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fact_budget_yearly (
    budget_key          INTEGER PRIMARY KEY,
    batch_id            TEXT    NOT NULL,
    budget_version      TEXT    NOT NULL,
    year                INTEGER NOT NULL,
    customer_key        INTEGER NOT NULL,
    customer_id         TEXT    NOT NULL,
    customer_name       TEXT    NOT NULL,
    bi_region_key       INTEGER NOT NULL,
    region_name         TEXT    NOT NULL,
    md_key              INTEGER NOT NULL,
    dept_short          TEXT    NOT NULL,
    channel_key         INTEGER NOT NULL,
    channel_type        TEXT    NOT NULL,
    customer_type       TEXT    NOT NULL,
    budget_amount_cents INTEGER NOT NULL CHECK (budget_amount_cents > 0),
    currency            TEXT    NOT NULL DEFAULT 'CNY',
    data_source         TEXT    NOT NULL DEFAULT 'MOCK_BUDGET'
                                CHECK (data_source = 'MOCK_BUDGET'),
    seed                INTEGER NOT NULL,
    generated_at        TEXT    NOT NULL,
    UNIQUE (batch_id, customer_key, bi_region_key, md_key, year)
);

-- ⑧ 生成假设（每个组合一行，可审计、可讲解）-------------------------------
CREATE TABLE IF NOT EXISTS budget_assumption (
    batch_id            TEXT    NOT NULL,
    budget_version      TEXT    NOT NULL,
    customer_key        INTEGER NOT NULL,
    customer_id         TEXT    NOT NULL,
    customer_name       TEXT    NOT NULL,
    bi_region_key       INTEGER NOT NULL,
    region_name         TEXT    NOT NULL,
    md_key              INTEGER NOT NULL,
    dept_short          TEXT    NOT NULL,
    channel_key         INTEGER NOT NULL,
    channel_type        TEXT    NOT NULL,
    tier                TEXT    NOT NULL CHECK (tier IN ('A','B','C')),
    target_band         TEXT    NOT NULL
                                CHECK (target_band IN ('OVER','ON','UNDER','SEVERE')),
    band_low_pct        REAL    NOT NULL,
    band_high_pct       REAL    NOT NULL,
    target_attainment   REAL    NOT NULL,
    actual_months       INTEGER NOT NULL,
    baseline_note       TEXT    NOT NULL,
    ytd_actual_cents    INTEGER NOT NULL,
    ytd_budget_cents    INTEGER NOT NULL,
    attainment_pct      REAL    NOT NULL,
    annual_budget_cents INTEGER NOT NULL,
    annualized_actual_cents INTEGER NOT NULL,
    implied_growth_pct  REAL    NOT NULL,
    monthly_profile_json TEXT   NOT NULL,
    data_source         TEXT    NOT NULL DEFAULT 'MOCK_BUDGET'
                                CHECK (data_source = 'MOCK_BUDGET'),
    seed                INTEGER NOT NULL,
    generated_at        TEXT    NOT NULL,
    PRIMARY KEY (batch_id, customer_key, bi_region_key, md_key)
);

-- 索引 --------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS ix_bud_wk_batch   ON fact_budget_weekly   (batch_id, year);
CREATE INDEX IF NOT EXISTS ix_bud_wk_cust    ON fact_budget_weekly   (batch_id, customer_key, bi_region_key);
CREATE INDEX IF NOT EXISTS ix_bud_wk_period  ON fact_budget_weekly   (batch_id, year, week_of_year);
CREATE INDEX IF NOT EXISTS ix_bud_mo_period  ON fact_budget_monthly  (batch_id, year, month);
CREATE INDEX IF NOT EXISTS ix_bud_mo_cust    ON fact_budget_monthly  (batch_id, customer_key, bi_region_key);
CREATE INDEX IF NOT EXISTS ix_bud_mo_region  ON fact_budget_monthly  (batch_id, year, month, bi_region_key);
CREATE INDEX IF NOT EXISTS ix_bud_qt_period  ON fact_budget_quarterly(batch_id, year, quarter);
CREATE INDEX IF NOT EXISTS ix_bud_yr_cust    ON fact_budget_yearly   (batch_id, customer_key);
CREATE INDEX IF NOT EXISTS ix_bud_as_band    ON budget_assumption    (batch_id, target_band);
