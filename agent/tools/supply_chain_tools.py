"""Supply Chain V2 的四个只读 Agent 工具。

边界：本模块只做参数校验与标准信封适配。采购建议始终由
``server.sc_queries.forecast`` 计算，Agent 工具层不维护第二套净需求公式。
"""
from __future__ import annotations

from datetime import date, datetime

from server import config, sc_queries

from ._common import TOOL_VERSION, ToolError
from .registry import register


INVENTORY_TABLES = [
    "dim_fruit_raw", "fact_fruit_inventory_monthly", "dim_sc_material",
    "fact_packaging_inventory_monthly", "fact_packaging_po",
]
BOM_TABLES = ["dim_sku", "dim_fruit_raw", "dim_sc_material", "dim_sku_bom"]
DEMAND_TABLES = ["fact_production_forecast", "dim_sku_bom", "dim_sc_procurement_param"]
PURCHASE_TABLES = DEMAND_TABLES + ["fact_packaging_inventory_monthly", "fact_packaging_po"]


def _call(fn, conn, qs: dict) -> dict:
    """把供应链查询层的参数错误统一映射成 Agent 工具错误。"""
    try:
        return fn(conn, qs)
    except sc_queries.ScError as exc:
        raise ToolError("InvalidArgument", str(exc)) from exc


def _period(forecast_months: list[str]) -> dict:
    return {
        "year": config.FIXED_YEAR_INT,
        "months": [int(value.split("-")[1]) for value in forecast_months],
        "label": "、".join(forecast_months) + " 预测",
    }


def _caliber(*parts) -> str:
    values: list[str] = []
    for part in parts:
        if isinstance(part, dict):
            values.extend(str(value) for value in part.values() if value)
        elif part:
            values.append(str(part))
    return "；".join(values)


def _notes(meta: dict, *extra: str) -> list[str]:
    return [
        meta.get("notice") or "全部供应链数据均为固定种子 MOCK_SUPPLY_CHAIN 模拟数据。",
        *[item for item in extra if item],
        "只读结果：不会自动下采购单，也不会修改库存、BOM、预测或采购参数。",
    ]


def query_material_inventory(conn, month: str) -> dict:
    """查询月末水果原料、包材库存及包材在途采购。"""
    packaging = _call(sc_queries.packaging, conn, {"month": month})
    bom_summary = _call(sc_queries.bom, conn, {"month": month})
    pack_data, pack_meta = packaging["data"], packaging["meta"]
    fruit_stock = bom_summary["data"].get("fruit_stock_by_rm") or {}

    # 与采购预测共用同一在途口径：仅状态=在途，数量=采购量−到货量。
    transit = sc_queries._in_transit_qty(conn)

    fruit_meta = {row[0]: row for row in conn.execute(
        "SELECT raw_material_id, fruit_category, specification, spec_label, unit, warehouse_id "
        "FROM sc.dim_fruit_raw ORDER BY raw_material_id")}
    fruit_rows = []
    for raw_id in sorted(fruit_meta):
        row = fruit_meta[raw_id]
        fruit_rows.append({
            "raw_material_id": raw_id, "fruit_category": row[1], "specification": row[2],
            "spec_label": row[3], "unit": row[4], "warehouse_id": row[5],
            "ending_qty": int(fruit_stock.get(raw_id, 0)), "in_transit_qty": None,
            "note": "星禾代理果萃：水果原料无采购 PO，在途采购不适用",
        })

    packaging_rows = [
        {**row, "in_transit_qty": int(transit.get(row["material_id"], 0))}
        for row in pack_data.get("materials") or []
    ]
    return {
        "ok": True, "tool": "query_material_inventory", "version": TOOL_VERSION,
        "args": {"month": month},
        "period": {"year": int(month[:4]), "months": [int(month[5:])],
                   "label": f"{month} 月末快照"},
        "scope": {"business": "水果原料与包材库存", "observation_month": month},
        "data_source": pack_meta.get("data_source") or "MOCK_SUPPLY_CHAIN",
        "mock_supply_chain": bool(pack_meta.get("mock_supply_chain", True)),
        "caliber": _caliber(
            "库存为自然月月末快照（inventory_month），非实时可用库存",
            "水果与包材单位不同，不跨层相加",
            "包材在途 = purchase_status='在途' 的采购量 − 到货量",
            pack_meta.get("caliber")),
        "metrics": {
            "fruit_materials": len(fruit_rows), "packaging_materials": len(packaging_rows),
            "fruit_ending_pieces": sum(row["ending_qty"] for row in fruit_rows),
            "packaging_ending_units": sum(row["ending_qty"] for row in packaging_rows),
            "packaging_in_transit_units": sum(row["in_transit_qty"] for row in packaging_rows),
        },
        "rows": {"fruit_inventory": fruit_rows, "packaging_inventory": packaging_rows},
        "tables": INVENTORY_TABLES,
        "notes": _notes(pack_meta, pack_meta.get("business_caliber") or ""),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def query_sku_bom(conn, sku: str, effective_date: str) -> dict:
    """按生效日查询一个 SKU 的水果原料与包材 BOM。"""
    try:
        date.fromisoformat(effective_date)
    except (TypeError, ValueError) as exc:
        raise ToolError("InvalidArgument", "effective_date 必须是 YYYY-MM-DD") from exc

    sc_queries.attach(conn)
    sku_code = sku.strip().upper()
    if sku_code == "ALL":
        sku_codes = [row[0] for row in conn.execute(
            "SELECT sku_code FROM dim_sku WHERE sku_code != 'UNKNOWN' ORDER BY sku_code")]
    else:
        sku_row = conn.execute(
            "SELECT sku_code FROM dim_sku WHERE sku_code = ? AND sku_code != 'UNKNOWN'",
            (sku_code,)).fetchone()
        if sku_row is None:
            raise ToolError("InvalidArgument", f"sku 不存在：{sku_code}")
        sku_codes = [sku_code]

    rows = []
    for current_sku in sku_codes:
        for item_type in ("fruit", "packaging"):
            for item_id, qty, unit, version in sc_queries._bom_rows_effective(
                    conn, current_sku, effective_date, item_type):
                table, id_col, name_col = (
                    ("dim_fruit_raw", "raw_material_id", "spec_label") if item_type == "fruit"
                    else ("dim_sc_material", "material_id", "material_name"))
                item_row = conn.execute(
                    f"SELECT {name_col} FROM sc.{table} WHERE {id_col} = ?", (item_id,)).fetchone()
                rows.append({
                    "sku_code": current_sku, "item_type": item_type, "item_id": item_id,
                    "item_name": item_row[0] if item_row else item_id,
                    "qty_per_unit": float(qty), "unit": unit, "bom_version": version,
                    "effective_date": effective_date,
                })
    if not rows:
        raise ToolError("InvalidArgument", f"{sku_code} 在 {effective_date} 没有生效 BOM")

    versions = sorted({row["bom_version"] for row in rows})
    return {
        "ok": True, "tool": "query_sku_bom", "version": TOOL_VERSION,
        "args": {"sku": sku_code, "effective_date": effective_date},
        "period": {"year": int(effective_date[:4]), "months": [int(effective_date[5:7])],
                   "label": f"{effective_date} 生效口径"},
        "scope": {"business": "SKU BOM", "sku": sku_code,
                  "effective_date": effective_date},
        "data_source": "MOCK_SUPPLY_CHAIN", "mock_supply_chain": True,
        "caliber": "BOM 按 effective_from ≤ 生效日的最新 active 版本逐物料取数；历史版本不覆盖",
        "metrics": {
            "bom_items": len(rows),
            "skus": len(sku_codes),
            "fruit_items": sum(row["item_type"] == "fruit" for row in rows),
            "packaging_items": sum(row["item_type"] == "packaging" for row in rows),
            "bom_versions": versions,
        },
        "rows": {"bom_items": rows}, "tables": BOM_TABLES,
        "notes": _notes({}, "水果用量单位为粒；包材按各自计量单位，不跨物料相加"),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def forecast_material_demand(conn, month: str, window: int) -> dict:
    """返回未来水果原料与包材需求，不在此工具生成采购建议。"""
    if window not in (1, 2):
        raise ToolError("InvalidArgument", "window 只能是 1 或 2")
    raw = _call(sc_queries.forecast, conn, {"month": month, "window": str(window)})
    data, meta = raw["data"], raw["meta"]
    forecast_months = list(data.get("forecast_months") or [])

    material_meta = {row[0]: (row[1], row[2]) for row in conn.execute(
        "SELECT material_id, material_name, unit FROM sc.dim_sc_material")}
    packaging_rows = []
    for row in data.get("items") or []:
        name, unit = material_meta.get(row["material_id"], (row.get("material_name"), "件"))
        packaging_rows.append({
            "material_id": row["material_id"], "material_name": name,
            "material_type": "packaging", "unit": unit,
            "applicable_skus": row.get("applicable_skus"),
            "demand_by_month": row.get("forecast_demand") or {},
            "demand_total": row.get("demand_total"),
            "attrition_rate_pct": row.get("attrition_rate_pct"), "attrition_applied": True,
        })

    fruit_meta = {row[0]: (row[1], row[2]) for row in conn.execute(
        "SELECT raw_material_id, spec_label, unit FROM sc.dim_fruit_raw")}
    fruit_demand: dict[str, dict[str, int]] = {}
    fruit_skus: dict[str, set[str]] = {}
    placeholders = ",".join("?" for _ in forecast_months)
    forecast_rows = conn.execute(
        "SELECT f.sku_code, s.sku_name, s.category, s.pieces_per_box, s.specification, "
        "f.forecast_month, f.forecast_qty "
        "FROM sc.fact_production_forecast f JOIN dim_sku s ON s.sku_code=f.sku_code "
        f"WHERE f.forecast_month IN ({placeholders}) ORDER BY f.sku_code, f.forecast_month",
        forecast_months).fetchall()
    sku_forecast = []
    for sku_code, sku_name, category, pieces, specification, forecast_month, forecast_qty in forecast_rows:
        sku_forecast.append({
            "sku_code": sku_code, "sku_name": sku_name, "category": category,
            "pieces_per_box": int(pieces), "specification": specification,
            "forecast_month": forecast_month, "forecast_boxes": int(forecast_qty),
        })
        for item_id, qty_per_unit, _unit, _version in sc_queries._bom_rows_effective(
                conn, sku_code, forecast_month + "-01", "fruit"):
            by_month = fruit_demand.setdefault(item_id, {fm: 0 for fm in forecast_months})
            by_month[forecast_month] += int(round(forecast_qty * qty_per_unit))
            fruit_skus.setdefault(item_id, set()).add(sku_code)
    fruit_rows = []
    for item_id in sorted(fruit_demand):
        name, unit = fruit_meta.get(item_id, (item_id, "粒"))
        by_month = fruit_demand[item_id]
        fruit_rows.append({
            "material_id": item_id, "material_name": name, "material_type": "fruit",
            "unit": unit, "applicable_skus": len(fruit_skus.get(item_id, set())),
            "demand_by_month": by_month, "demand_total": sum(by_month.values()),
            "attrition_rate_pct": None, "attrition_applied": False,
            "note": "水果原料仅按预测生产盒数 × 生效 BOM 粒数展开；无采购损耗参数，不编造",
        })

    return {
        "ok": True, "tool": "forecast_material_demand", "version": TOOL_VERSION,
        "args": {"month": month, "window": window}, "period": _period(forecast_months),
        "scope": {"business": "未来物料需求", "observation_month": data.get("obs_month"),
                  "forecast_months": forecast_months},
        "data_source": meta.get("data_source") or "MOCK_SUPPLY_CHAIN",
        "mock_supply_chain": bool(meta.get("mock_supply_chain", True)),
        "caliber": _caliber(
            data.get("formula", {}).get("demand"),
            "未来 SKU 生产量来自 fact_production_forecast，按 ERP Sell-in 推导，非 POS Sell-out",
            "水果不套用包材采购损耗参数；只展开生效 BOM 粒数"),
        "metrics": {"forecast_boxes": data.get("forecast_boxes") or {},
                    "fruit_materials": len(fruit_rows),
                    "packaging_materials": len(packaging_rows),
                    "materials": len(fruit_rows) + len(packaging_rows)},
        "rows": {"sku_forecast": sku_forecast, "fruit_demand": fruit_rows,
                 "packaging_demand": packaging_rows},
        "tables": DEMAND_TABLES,
        "notes": _notes(meta, data.get("forecast_basis") or ""),
        "forecast_basis": data.get("forecast_basis") or "",
        "formula": {"packaging_demand": data.get("formula", {}).get("demand"),
                    "fruit_demand": "预测生产盒数 × 预测月生效的水果 BOM 粒数"},
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def calculate_purchase_suggestion(conn, month: str, window: int) -> dict:
    """调用供应链 V2 确定性采购预测，不在 Agent 层重算建议数量。"""
    if window not in (1, 2):
        raise ToolError("InvalidArgument", "window 只能是 1 或 2")
    raw = _call(sc_queries.forecast, conn, {"month": month, "window": str(window)})
    data, meta = raw["data"], raw["meta"]
    items = data.get("items") or []
    active = [row for row in items if row.get("param_status") == "active"]
    formulas = data.get("formula") or {}
    return {
        "ok": True, "tool": "calculate_purchase_suggestion", "version": TOOL_VERSION,
        "args": {"month": month, "window": window},
        "period": _period(data.get("forecast_months") or []),
        "scope": {"business": "包材采购预测", "observation_month": data.get("obs_month"),
                  "forecast_months": data.get("forecast_months") or []},
        "data_source": meta.get("data_source") or "MOCK_SUPPLY_CHAIN",
        "mock_supply_chain": bool(meta.get("mock_supply_chain", True)),
        "caliber": _caliber(formulas, meta.get("caliber")),
        "metrics": {
            "materials": len(items),
            "materials_to_order": sum((row.get("suggested_order_qty") or 0) > 0 for row in active),
            "high_risk_materials": sum(row.get("risk_level") == "high" for row in active),
            "forecast_boxes": data.get("forecast_boxes") or {},
        },
        "rows": {"procurement_items": items}, "tables": PURCHASE_TABLES,
        "notes": _notes(meta, data.get("forecast_basis") or ""),
        "formula": formulas, "forecast_basis": data.get("forecast_basis") or "",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


register(
    "query_material_inventory", "查询指定月末的水果原料与包材库存，以及包材在途采购；只读。",
    {"month": {"type": "string", "description": "观察月 YYYY-MM，当前范围 2026-01~2026-10"}},
    ["month"], query_material_inventory,
    source="MOCK_SUPPLY_CHAIN / Supply Chain V2（只读）",
    returns="水果原料和包材月末库存、包材在途数量、数据来源与口径")

register(
    "query_sku_bom", "按生效日期查询 SKU 对应的水果原料和包材 BOM 版本；只读。",
    {"sku": {"type": "string", "description": "SKU 编码，如 SKU-001"},
     "effective_date": {"type": "string", "description": "BOM 生效观察日 YYYY-MM-DD"}},
    ["sku", "effective_date"], query_sku_bom,
    source="MOCK_SUPPLY_CHAIN / dim_sku_bom（只读）",
    returns="SKU、物料类型、物料 ID、单盒用量、单位、BOM 版本及生效日")

register(
    "forecast_material_demand", "查询未来一至两个月的水果原料和包材需求；不生成采购建议。",
    {"month": {"type": "string", "description": "观察月，当前固定为 2026-10"},
     "window": {"type": "integer", "description": "预测窗口，1 或 2 个月"}},
    ["month", "window"], forecast_material_demand,
    source="MOCK_SUPPLY_CHAIN / production forecast + effective BOM（只读）",
    returns="按水果原料和包材拆分的逐月需求、需求合计、预测依据与口径")

register(
    "calculate_purchase_suggestion",
    "调用供应链 V2 确定性公式计算包材净需求、MOQ/整包装取整与采购建议；只读。",
    {"month": {"type": "string", "description": "观察月，当前固定为 2026-10"},
     "window": {"type": "integer", "description": "预测窗口，1 或 2 个月"}},
    ["month", "window"], calculate_purchase_suggestion,
    source="MOCK_SUPPLY_CHAIN / Supply Chain V2 forecast（只读）",
    returns="包材需求、库存、在途、安全库存、净需求、建议采购量、风险及完整计算依据")
