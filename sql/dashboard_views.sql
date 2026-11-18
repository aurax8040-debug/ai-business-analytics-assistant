-- =====================================================================
-- 全国经营驾驶舱 V1 · 筛选投影视图层
--
-- 运行方式：以 sqlite3 ``mode=ro`` 打开 ETL V1 数据库后，通过
--           ``CREATE TEMP VIEW`` 在连接私有的 temp schema 中建立。
--           TEMP VIEW 只存在于内存，不写入、不修改 ETL 数据库文件。
--
-- 设计边界：
--   1. 每个视图只引用「一张」事实表 + 维度表，禁止不同事实表明细 JOIN。
--   2. 只做投影与 P01 范围过滤，不重新计算任何业务指标：
--      sales_amount / sales_boxes / shipment_te / sales_te / gross_profit
--      均为 ETL V1 已物化的行级度量，汇总与公式由指标字典冻结。
--   3. 所有筛选条件由后端参数化 SQL 施加在这些视图与 13 个交付视图上。
-- =====================================================================

-- ERP 出货（星禾 Sell-in）筛选投影 ---------------------------------------
CREATE TEMP VIEW IF NOT EXISTS v_dash_erp AS
SELECT
    f.year                 AS year,
    f.month                AS month,
    f.region_name_std      AS region_name_std,
    f.sku_code             AS sku_code,
    f.category             AS category,
    f.channel_type         AS channel_type,
    f.customer_id          AS customer_id,
    COALESCE(NULLIF(c.customer_name, ''), NULLIF(f.erp_customer_name, ''),
             f.customer_id)                       AS customer_name,
    COALESCE(s.sku_name, f.sku_name)             AS sku_name,
    COALESCE(s.pieces_per_box, f.pieces_per_box) AS pieces_per_box,
    COALESCE(s.specification, f.specification)   AS specification,
    f.sales_boxes          AS sales_boxes,
    f.sales_amount         AS sales_amount,
    f.shipment_te          AS shipment_te,
    f.cost_amount          AS cost_amount,
    f.gross_profit         AS gross_profit,
    f.shipment_no          AS shipment_no,
    f.full_date            AS full_date
FROM fact_erp_shipment f
LEFT JOIN dim_customer c ON c.customer_key = f.customer_key
LEFT JOIN dim_sku      s ON s.sku_key      = f.sku_key
WHERE f.region_scope_status = 'IN_SCOPE';

-- POS 终端销售（Sell-out）筛选投影 ---------------------------------------
CREATE TEMP VIEW IF NOT EXISTS v_dash_pos AS
SELECT
    f.year                 AS year,
    f.month                AS month,
    f.region_name_std      AS region_name_std,
    f.sku_code             AS sku_code,
    f.category             AS category,
    f.channel_type         AS channel_type,
    f.channel_status       AS channel_status,
    f.ka_system            AS ka_system,
    f.store_id             AS store_id,
    f.store_name           AS store_name,
    f.pos_link_status      AS pos_link_status,
    COALESCE(s.sku_name, f.sku_name)             AS sku_name,
    COALESCE(s.pieces_per_box, f.pieces_per_box) AS pieces_per_box,
    COALESCE(s.specification, f.specification)   AS specification,
    f.sales_boxes          AS sales_boxes,
    f.sales_amount         AS sales_amount,
    f.sales_te             AS sales_te,
    f.pos_record_id        AS pos_record_id,
    f.full_date            AS full_date
FROM fact_pos_sales f
LEFT JOIN dim_sku s ON s.sku_key = f.sku_key
WHERE f.region_scope_status = 'IN_SCOPE';
