"""供应链模块 V1（Mock）。

独立于 agent/server：只读引用 ETL 库（SKU 与 2026 出库量做锚点），
产出的所有数据写入独立库 ``data/supply_chain_v1.db``，
全部标记 ``MOCK_SUPPLY_CHAIN``。ERP/POS/SFA/批发/预算一律不碰。
"""
__version__ = "supply-chain-v1"
DATA_SOURCE = "MOCK_SUPPLY_CHAIN"
SEED = 20261109
