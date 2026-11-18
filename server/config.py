"""全国经营驾驶舱 V1 — 全局配置。

数据保护：ETL V1 数据库只以 SQLite ``mode=ro`` 打开；
本模块不提供任何写入 ETL 库的能力，也不引用任何源 CSV 的写操作。

展示口径（V1.1 起）：全局固定 Y26（2026 自然年 1–10 月），
不提供跨年切换，也不计算跨年增长率。
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 唯一数据源：ETL V1 已验收的查询数据库（只读）
#
# 解析顺序：
#   1. 环境变量 ``ETL_DB_PATH`` 显式指定；
#   2. 工程内 ``data/bi_data_layer_v1.db``（公开版脱敏 Mock 数据）。
# 公开仓库不自动寻找父目录或历史工作区中的数据库，避免误读本机私有数据。
_ETL_DB_ENV = os.environ.get("ETL_DB_PATH", "").strip()
_ETL_DB_BUNDLED = ROOT / "data" / "bi_data_layer_v1.db"

ETL_DB = (Path(_ETL_DB_ENV).expanduser().resolve()
          if _ETL_DB_ENV else _ETL_DB_BUNDLED)

WEB_DIR = ROOT / "web"
SQL_DIR = ROOT / "sql"
LOG_DIR = ROOT / "logs"
REPORT_DIR = ROOT / "reports"

# 云端发布时由平台注入 PORT，并要求绑定 0.0.0.0；
# 本地直接 `python3 run.py` 仍是 127.0.0.1:8765，行为不变。
_ON_PLATFORM = bool(os.environ.get("PORT"))
HOST = os.environ.get("HOST") or ("0.0.0.0" if _ON_PLATFORM else "127.0.0.1")
PORT = int(os.environ.get("PORT") or 8765)

# ---- P01：第一版 BI 只展示五大战区 -------------------------------------
IN_SCOPE_REGIONS = ("战区A", "战区B", "战区C", "战区D", "战区E")
EXCLUDED_REGION_NOTE = "五大战区合计（不含战区F、战区G）"

# ---- P05 / V1.1：自然年，全局固定 Y26 ----------------------------------
# 数据库内 Y26 数据截止 2026-10-31；只读取 2026 自然年，不做跨年对比。
YEARS = {"Y26": 2026}
FIXED_YEAR = "Y26"
FIXED_YEAR_INT = 2026
COMPARABLE_FIRST_MONTH = 1
COMPARABLE_LAST_MONTH = 10
CALENDAR_Y26 = "2026-01-01 ~ 2026-10-31（当前数据截止）"
PERIOD_LABEL = "Y26 · 2026 年 1–10 月自然月（数据截止 2026-10-31）"
PERIOD_SHORT = "2026 年 1–10 月"

# ---- 综合经营分析 V1：六个分析维度 --------------------------------------
# 维度只决定「按什么拆解」，不改变任何指标口径：
#   战区 / 渠道 / 品类 / SKU 在 ERP 与 POS 两侧均可用；
#   KA 系统仅 POS（ERP 无 ka_system，映射 PENDING）；客户仅 ERP（POS 无客户字段）。
DIMENSIONS = (
    {"key": "region", "label": "战区", "sides": ("erp", "pos"),
     "member_label": "战区", "note": "五大战区固定骨架（P01：不含战区F、战区G）"},
    {"key": "channel", "label": "渠道", "sides": ("erp", "pos"),
     "member_label": "渠道", "note": "ERP 含商超/终端、批发、电商；POS 仅覆盖商超/终端"},
    {"key": "ka", "label": "KA 系统", "sides": ("pos",),
     "member_label": "KA 系统", "note": "仅 POS 终端销售；ERP KA 映射 PENDING，不猜测"},
    {"key": "customer", "label": "客户", "sides": ("erp",),
     "member_label": "客户", "note": "仅 ERP 出货客户；POS 事实表无客户字段"},
    {"key": "category", "label": "品类", "sides": ("erp", "pos"),
     "member_label": "品类", "note": "品类A / 品类B / 品类C"},
    {"key": "sku", "label": "SKU", "sides": ("erp", "pos"),
     "member_label": "SKU", "note": "按 sku_code 聚合，名称取 sku_name"},
)
DIMENSION_KEYS = tuple(d["key"] for d in DIMENSIONS)
DEFAULT_DIMENSION = "region"

# ---- 指标维度 -----------------------------------------------------------
METRICS = {
    "amount": {"erp": "sales_amount", "pos": "sales_amount", "label": "金额", "unit": "¥"},
    "boxes": {"erp": "sales_boxes", "pos": "sales_boxes", "label": "盒数", "unit": "盒"},
    "te": {"erp": "shipment_te", "pos": "sales_te", "label": "TE", "unit": "TE"},
}

# ETL V1 已交付并被本驾驶舱直接消费的 13 个查询视图
DELIVERED_VIEWS = (
    "v_erp_monthly", "v_pos_monthly", "v_pos_yoy", "v_pos_category_share",
    "v_pos_sku_rank", "v_pos_store_rank", "v_scope_coverage",
    "v_sfa_summary", "v_sfa_employee_coverage", "v_wholesale_monthly",
    "v_inventory_daily", "v_inventory_month_end", "v_inventory_zero_breakdown",
)

# 前端筛选投影视图（TEMP VIEW，定义在 sql/dashboard_views.sql，写入内存 temp schema）
FILTER_VIEWS = ("v_dash_erp", "v_dash_pos")

FILTER_APPLICABILITY_NOTES = (
    "战区 / 渠道 / 品类 / SKU：ERP 与 POS 两侧均生效（P01 五大战区内）。",
    "KA 系统：POS 侧按 fact_pos_sales.ka_system 生效；ERP 侧 ka_mapping_status 全为 PENDING、"
    "dim_ka.erp_customer_id 为空，无业务确认映射，故不猜测，仅提示「映射 PENDING」。",
    "客户：ERP 侧按 fact_erp_shipment.customer_id 生效；POS 事实表无客户字段，不跨系统推断。",
    "渠道：POS 侧仅覆盖「商超/终端」；选电商/批发时 POS 显示「无数据」而非 0。",
    "线上 / 线下：dim_channel.online_offline 与 POS channel_status 均为「未采集」，不提供该筛选、不推断。",
    "ERP 出货（Sell-in）与 POS 终端销售（Sell-out）分列展示，金额永不相加；批发利润独立核算。",
    "全局年份固定 2026 年，展示 1–10 月自然月；环比仅在相邻自然月之间计算，不提供跨年对比。",
)

SCOPE_NOTE = (
    "P01 第一版 BI：五大战区合计，不含战区F、战区G；POS 覆盖 84.63% 行 / 80.93% 金额 / 27 家门店，"
    "非完整全国数据。展示口径固定 Y26（2026-01-01 ~ 2026-10-31）自然月，不提供跨年对比。"
)


def db_uri() -> str:
    """只读 URI；``mode=ro`` 保证 SQLite 不产生任何写入。"""
    return f"file:{ETL_DB.as_posix()}?mode=ro"


# ---- 模拟销售预算 V1（MOCK_BUDGET）-------------------------------------
# 独立库，与 ETL 库物理分离：ERP 原始数据"未被修改"可以用库文件 MD5 硬证明。
# 查询时以只读 ATTACH 挂到 ETL 连接上（见 server/budget_queries.py），
# 两个库都在 mode=ro 下打开，任何写入尝试都会抛 readonly database。
BUDGET_DB = Path(os.environ.get("BUDGET_DB_PATH") or (ROOT / "data" / "budget_v1.db"))

BUDGET_DATA_SOURCE = "MOCK_BUDGET"
BUDGET_NOTICE = "本库为演示用模拟预算（MOCK_BUDGET），非真实业务预算；不得用于对外披露或内部考核。"


def budget_uri() -> str:
    return f"file:{BUDGET_DB.as_posix()}?mode=ro"


# ---- 供应链模块 V1（MOCK_SUPPLY_CHAIN）----------------------------------
# P1–P4 数据层 + P5 看板查询：独立库 data/supply_chain_v1.db，
# 固定种子 20261109（19 表，见 supply_chain/generate.py）。
# 查询时以只读 ATTACH 挂到 ETL 连接上（别名 sc，见 server/sc_queries.py）：
#   - 主库（ETL）提供 dim_sku / pieces_per_box（BOM 果子用量取数）；
#   - sc 库提供到货 / 质检 / 库存 / 包材 PO / BOM 全部事实。
# 业务口径（P2 修正）：星禾代理果萃，果子无采购（到货直接入库），
# 采购仅在包材；金额估算只用最近一次 PO 单价，果子无采购价不参与。
SC_DB = Path(os.environ.get("SUPPLY_CHAIN_DB_PATH") or (ROOT / "data" / "supply_chain_v1.db"))

SC_DATA_SOURCE = "MOCK_SUPPLY_CHAIN"
SC_NOTICE = ("本页为演示用模拟供应链数据（MOCK_SUPPLY_CHAIN），非真实业务数据；"
             "金额为最近 PO 单价估算，非账面成本。")

# 供应链库时间窗：2026-01 ~ 2026-10 自然月（月末快照），不提供跨年切换。
SC_FIRST_MONTH = "2026-01"
SC_LAST_MONTH = "2026-10"


def sc_uri() -> str:
    return f"file:{SC_DB.as_posix()}?mode=ro"


# ---- 模拟销售预算 TE 口径（MOCK_BUDGET_TE）------------------------------
# 业务口径：预算的考核指标是销售量 TE，不是金额。金额预算库（budget_v1.db）
# 与本库互不换算、互不覆盖；首页只读本库，且只与 ERP 的 shipment_te 比较。
BUDGET_TE_DB = Path(os.environ.get("BUDGET_TE_DB_PATH") or (ROOT / "data" / "budget_te_v1.db"))
BUDGET_TE_DATA_SOURCE = "MOCK_BUDGET_TE"
BUDGET_TE_NOTICE = ("本库为演示用模拟 TE 预算（MOCK_BUDGET_TE），预算单位是销售量 TE"
                    "（托盘当量），不是销售金额；非真实业务预算，不得用于对外披露或考核。")


def budget_te_uri() -> str:
    return f"file:{BUDGET_TE_DB.as_posix()}?mode=ro"


# ---- 机会引擎 · 机会一 SKU 渗透（AI_ANALYSIS_LAYER_V1）------------------
# 全部读 ETL 主库（不 ATTACH Mock 库）。阈值即口径，改阈值必须同步
# tests/test_26_gap.py 的黄金值。
GAP_YOY_MIN_PCT = 20.0          # 高增长下限：POS YoY ≥ +20%
GAP_Y26_AMOUNT_MIN = 1_000_000.0  # 规模门槛：Y26 销售额 ≥ ¥100 万（防小基数虚高）
GAP_WINDOW = (2026, (9, 10))    # 缺口窗口：2026-09/10 连续两月无 POS 记录
