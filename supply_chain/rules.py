"""供应链 Mock 业务规则（明确标注的假设，不是真实果萃/星禾数据）。

业务口径（与代理模式对齐）：
- 果子：星禾是果萃（Orchardia）中国代理，**不存在果子采购**——果萃产区A发船、
  到港后直接入库，供应链只关注「到货时间（ETA/ATA）与数量（分品类×果径规格）」；
  代理结算价属于财务域，本模块不建模、不展示。
- 包材：采购只发生在包材配件（彩盒/内托/网套/标签/外箱/胶带/果垫/封箱贴），
  按 BOM 组装 2/4/8 粒装等 SKU，单价为整数分的采购成本价。

所有系数集中在这里：改规则只动这一个文件，生成与质量校验共用同一份常量，
避免「生成用一套数、校验用另一套数」的分叉。
"""
from __future__ import annotations

# ---------------------------------------------------------------- 期间
# 与 ERP 2026 年出库窗口对齐：2026-01-01 ~ 2026-10-31。
MONTHS = list(range(1, 11))          # 1..10
YEAR = 2026
# 期初结转来源：2025-12 末到港的果萃批次（有到货行，保证外键成立），
# 2026-01 的库存行 opening_qty 即取其合格量。
OPENING_ARRIVAL_MONTH = (2025, 12)

# 生产节奏：当月生产批次落在这个日期区间（避开月初 1-7 日——
# 原料到货+质检要 2 天，月初头几天的出库允许 NO_EARLIER_BATCH，是真实边界）。
PROD_DAY_RANGE = (8, 26)
# 原料到货日：月初 1-6 日，保证当月生产日之前已完成质检。
FRUIT_ARRIVAL_DAY_RANGE = (1, 7)
FRUIT_QC_LEAD_DAYS = (1, 2)          # 到货后 1-2 天出质检结果

# ---------------------------------------------------------------- 生产
PROD_COVER_FACTOR = (1.03, 1.06)     # 月计划 = 需求 × 1.03~1.06（备成品安全库存）
BATCH_SPLIT_THRESHOLD = (20_000, 80_000)   # <2万盒 1 批，2-8万 2 批，>8万 3 批
UNQUALIFIED_RATE = (0.008, 0.02)     # 分选不合格产出占比
LOSS_RATE = (0.015, 0.04)            # 加工损耗率（剔除果、修整损耗）

# ---------------------------------------------------------------- 原料
# ---------------------------------------------------------------- 果萃到货（代理，无采购）
ORCHARDIA_ORIGIN = "产区A"              # 货源唯一：果萃产区A（代理模式）
ORCHARDIA_SHIPMENT_PREFIX = "ZSP"        # 船次号前缀（柜号级）
ORCHARDIA_ETA_LEAD_DAYS = (10, 25)      # 船期计划提前量：ETA 在 ATA 前 10~25 天排定
ORCHARDIA_LATE_RATE = 0.12              # 12% 的批次延误（ATA 晚于 ETA）
ORCHARDIA_LATE_DAYS = (1, 5)            # 延误幅度 1~5 天
ORCHARDIA_EARLY_DAYS = (0, 2)           # 准时批次允许提前 0~2 天到港

FRUIT_ARRIVAL_BUFFER = (1.02, 1.08)  # 当月到货合格量 = 当月需求 × 1.02~1.08
OPENING_CARRY_RATIO = (0.15, 0.30)   # 期初结转 = 1 月需求 × 15%~30%
REJECT_RATE = (0.01, 0.12)           # 批次不合格率（批次间必须有差异）
SCRAP_RATE = (0.002, 0.008)          # 月末在库原料的存储损耗
QUALITY_GRADE_RULE = (97.0, 92.0)    # 合格率 ≥97% 优，≥92% 良，其余 合格
QC_SAMPLE_RATE = 0.02                # 抽检 2%
DEFECT_KINDS = ("次果", "伤果", "软果", "小果")

# ---------------------------------------------------------------- 包材
PACK_ARRIVAL_BUFFER = (1.03, 1.12)   # 当月采购入库 = 当月领用 × 1.03~1.12
PACK_OPENING_RATIO = (0.4, 0.7)      # 期初库存 = 1 月领用 × 40%~70%
PARTIAL_DELIVERY_RATE = 0.10         # 10% 的 PO 部分到货
IN_TRANSIT_PO_PER_MATERIAL = 2       # 每种物料 2 笔 10 月下单、11 月到货的在途 PO

# ---------------------------------------------------------------- 包材采购单价（整数分）
# 包材是唯一采购对象：库存金额口径 = 最近采购单价 × 期末数量（估算值，须标注）。
PACK_PRICE_CENTS = {                 # 分/单位
    "PM-BOX": 180,     # 彩盒
    "PM-TRAY": 25,     # 内托
    "PM-NET": 6,       # 网套
    "PM-LABEL": 3,     # 标签
    "PM-CARTON": 320,  # 外箱
    "PM-TAPE": 450,    # 胶带（卷）
    "PM-PAD": 12,      # 果垫
    "PM-SEAL": 8,      # 封箱贴
}
PACK_PO_PRICE_JITTER = 0.10          # ±10%

# ---------------------------------------------------------------- BOM 规则
# 每盒用量。外箱/胶带/果垫/封箱贴是「每 N 盒 1 个」的分数用量，
# 领用时按批产量四舍五入取整，尾差由质量校验的容差吸收。
CARTON_PER_BOX_SINGLE = 36           # 单层箱（is_single_layer=Y）36 盒/外箱
CARTON_PER_BOX_DOUBLE = 40           # 双层 40 盒/外箱
TAPE_PER_BOX = 1 / 400               # 1 卷胶带 / 400 盒
PAD_PER_BOX = 1 / 20                 # 果垫 1 张 / 20 盒
SEAL_PER_BOX = 1 / 40                # 封箱贴 1 张 / 40 盒（每外箱 1 张）

MATERIALS = [
    # material_id, name, category, specification, unit, warehouse_id
    ("PM-BOX",    "彩印礼盒",   "彩盒",   "按 SKU 分版", "个", "WH-PM-01"),
    ("PM-TRAY",   "塑料内托",   "内托",   "按粒数分型", "个", "WH-PM-01"),
    ("PM-NET",    "发泡网套",   "网套",   "通用",       "个", "WH-PM-01"),
    ("PM-LABEL",  "追溯标签",   "标签",   "通用",       "张", "WH-PM-01"),
    ("PM-CARTON", "五层瓦楞外箱", "外箱", "按装箱率分型", "个", "WH-PM-02"),
    ("PM-TAPE",   "打包胶带",   "胶带",   "通用",       "卷", "WH-PM-02"),
    ("PM-PAD",    "缓冲果垫",   "果垫",   "通用",       "张", "WH-PM-02"),
    ("PM-SEAL",   "封箱贴",     "封箱贴", "通用",       "张", "WH-PM-02"),
]

SUPPLIERS = [
    # supplier_id, name, type, city, rating —— 采购只有包材供应商；
    # 果子是果萃代理到货，委托方唯一（ORCHARDIA），不建供应商维度。
    ("SUP-PM-01", "包材供应商A", "packaging", "城市A", 4),
    ("SUP-PM-02", "包材供应商B", "packaging", "城市B", 5),
    ("SUP-PM-03", "包材供应商C", "packaging", "城市C", 3),
    ("SUP-PM-04", "包材供应商D", "packaging", "城市D", 4),
    ("SUP-PM-05", "包材供应商E", "packaging", "城市E", 3),
]

WAREHOUSES = [
    ("WH-PM-01", "包材一号仓（彩印/塑托/网套/标签）", "包材仓"),
    ("WH-PM-02", "包材二号仓（外箱/胶带/果垫/封箱贴）", "包材仓"),
    ("WH-FR-01", "原料一号仓（品类A）", "原料仓"),
    ("WH-FR-02", "原料二号仓（品类C/品类B）", "原料仓"),
    ("WH-FG-01", "成品一号仓", "成品仓"),
    ("WH-FG-02", "成品二号仓", "成品仓"),
    ("WH-QC-01", "质检暂存区", "质检暂存"),
]

FRUIT_ORIGIN_TEXT = ORCHARDIA_ORIGIN   # 到货批次产地字段固定为产区A（代理口径）


def bom_items(sku: dict) -> list[dict]:
    """按 Mock BOM 规则返回 SKU 的物料清单（item_type, item_id, qty, unit）。

    规则表见 docs/supply_chain_model.md §5。2 粒装无内托；
    外箱装箱率按单层/双层区分。水果用量严格 = pieces_per_box。
    """
    pieces = int(sku["pieces_per_box"])
    single = sku["is_single_layer"] == "Y"
    items = [
        {"item_type": "fruit", "item_id": sku["raw_material_id"],
         "qty_per_unit": float(pieces), "unit": "粒"},
        {"item_type": "packaging", "item_id": "PM-BOX", "qty_per_unit": 1.0, "unit": "个"},
        {"item_type": "packaging", "item_id": "PM-NET",
         "qty_per_unit": float(pieces), "unit": "个"},
        {"item_type": "packaging", "item_id": "PM-LABEL", "qty_per_unit": 1.0, "unit": "张"},
        {"item_type": "packaging", "item_id": "PM-CARTON",
         "qty_per_unit": 1.0 / (CARTON_PER_BOX_SINGLE if single else CARTON_PER_BOX_DOUBLE),
         "unit": "个"},
        {"item_type": "packaging", "item_id": "PM-TAPE",
         "qty_per_unit": TAPE_PER_BOX, "unit": "卷"},
        {"item_type": "packaging", "item_id": "PM-PAD",
         "qty_per_unit": PAD_PER_BOX, "unit": "张"},
        {"item_type": "packaging", "item_id": "PM-SEAL",
         "qty_per_unit": SEAL_PER_BOX, "unit": "张"},
    ]
    if pieces > 2:
        items.insert(2, {"item_type": "packaging", "item_id": "PM-TRAY",
                         "qty_per_unit": 1.0, "unit": "个"})
    return items


# ================================================================ V2 增量
# 以下常量属于供应链数据链路 V2（采购参数 / 预测 / BOM 版本演示）。
# 采购参数为 MOCK 设定：安全库存按 10 月领用量动态折算（半月用量），
# 其余为按物料品类设定的合理假设，页面上必须标注「参数为 MOCK 设定」。

# ---------------------------------------------------------------- 采购预测窗口
# 观察点 = 数据窗最后一个月末（2026-10-31）；预测 2026-11 与 2026-12。
# 数据窗内 11 月无任何已发生领用/入库记录，因此 11 月「剩余期间 = 全月」，
# 计算依据写进 fact_production_forecast.forecast_note，不重复计算已发生需求。
FORECAST_MONTHS = ("2026-11", "2026-12")
FORECAST_BASIS_MONTHS = (8, 9, 10)      # 最近 3 个完整月份（Y26）
SEASONAL_REF_YEAR = 2025                # Y25 同期季节性
FORECAST_NOTE = ("预测口径 = ERP Sell-in 出库（非 POS Sell-out）；"
                 "近 3 月均值 × Y25 同期季节系数；数据窗止 2026-10-31，"
                 "11 月无已发生需求，剩余期间 = 全月")

# ---------------------------------------------------------------- 包材采购参数（MOCK 设定）
# safety_ratio: 安全库存 = 10 月领用量 × 该比例（半月用量）
# lead_days: 采购周期（印刷品 > 塑业 > 通用件）
# moq / package: 最小起订量与采购整包装数量（单位 = 物料单位）
# attrition_pct: 包材损耗率（%），用于需求 = 用量 × (1+损耗率)
PROCUREMENT_PARAM_RULES = {
    # material_id: (safety_ratio, lead_days, moq, package, attrition_pct)
    "PM-BOX":    (0.50, 14, 20_000, 1_000, 0.5),
    "PM-TRAY":   (0.50, 10, 50_000, 5_000, 0.3),
    "PM-NET":    (0.50,  7, 100_000, 10_000, 1.0),
    "PM-LABEL":  (0.50, 10, 100_000, 10_000, 0.5),
    "PM-CARTON": (0.50, 14,  5_000,   500, 0.4),
    "PM-TAPE":   (0.50,  7,    200,    50, 1.0),
    "PM-PAD":    (0.50,  7, 20_000, 2_000, 1.0),
    "PM-SEAL":   (0.50,  7, 40_000, 4_000, 1.0),
}
PROCUREMENT_PARAM_VERSION = "V1"
PROCUREMENT_PARAM_EFFECTIVE = "2026-01-01"

# ---------------------------------------------------------------- BOM 版本演示
# SKU-001 追溯标签 1 张 → 2 张（双语出口标），V2 于 2026-11-01 生效。
# 用途：证明 BOM 版本管理（历史生产记录仍引用 V1，不被覆盖）。
# 领用/预测计算按「观察/预测月取生效版本」选择 V1 或 V2。
BOM_V2_DEMO_SKU = "SKU-001"
BOM_V2_EFFECTIVE = "2026-11-01"
BOM_V2_CHANGES = {"PM-LABEL": 2.0}      # 其余用量与 V1 相同
