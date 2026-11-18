"""客户经理月度拜访目标配置（可写配置库，与 ETL 库物理隔离）。

设计边界
--------
- ETL V1 数据库永远只以 ``mode=ro`` 打开；本模块**不写** ETL 库、不读源 CSV。
- 目标数据落在独立的小型 SQLite：``bi_dashboard_v1/data/app_config.db``，
  即使被误删也只会让目标回到「未配置」，不会影响任何已验收数据。
- **不自造真实目标**：真实业务目标只能靠录入 / CSV 导入得到；未配置的客户经理
  在页面上显示「未配置」，既不按 0 计、也不按平均值补、也不参与完成率分子分母。
- **示例目标（DEMO）**：库里一条真实目标都没有时，可用「一键生成示例目标」得到一组
  **显式标记为 DEMO** 的目标，只为了让完成率相关的图表 / 排名看得清楚。
  这类数据在数据库（``data_source='DEMO'``）、接口（``target_source`` / ``demo_rows``）、
  页面（角标 + 提示条 + 列标记）三处同时标注，可一键清除，
  并且**永远不会覆盖**手动录入（MANUAL）或导入（IMPORT）的目标。

字段（业务要求 1:1 对应）
------------------------
客户经理 ID / 客户经理姓名 / 年份 / 月份 / 月度目标门店数 / 配置时间 / 配置状态
"""
from __future__ import annotations

import csv
import hashlib
import io
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from . import config

DATA_DIR = config.ROOT / "data"
TARGET_DB = DATA_DIR / "app_config.db"

# 配置状态：ACTIVE 参与完成率；DISABLED 保留历史但不参与
STATUSES = ("ACTIVE", "DISABLED")

# 数据来源：MANUAL 手工录入 / IMPORT CSV 导入 / DEMO 示例（可一键清除）
SOURCE_MANUAL = "MANUAL"
SOURCE_IMPORT = "IMPORT"
SOURCE_DEMO = "DEMO"
SOURCES = (SOURCE_MANUAL, SOURCE_IMPORT, SOURCE_DEMO)

MAX_IMPORT_ROWS = 2000
MAX_TARGET = 100000          # 单月目标上限，防明显口误（如把金额填进来）

# 示例目标的生成系数区间：0.80–1.30。用哈希保证同一「人+年+月」每次生成结果一致，
# 否则每点一次按钮完成率就变一次，无法复现也就无法验收。
DEMO_FACTOR_MIN = 0.80
DEMO_FACTOR_SPAN = 0.50
DEMO_NOTE = ("示例目标（DEMO）：按该客户经理当月实际完成门店数 × 0.80–1.30 的确定性系数生成，"
             "仅用于演示完成率展示，非真实业务目标，可一键清除")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sfa_monthly_target (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id       TEXT    NOT NULL,
    employee_name     TEXT,
    year              INTEGER NOT NULL,
    month             INTEGER NOT NULL,
    target_store_count INTEGER NOT NULL,
    config_status     TEXT    NOT NULL DEFAULT 'ACTIVE',
    configured_at     TEXT    NOT NULL,
    note              TEXT,
    data_source       TEXT    NOT NULL DEFAULT 'MANUAL',
    UNIQUE (employee_id, year, month)
);
CREATE INDEX IF NOT EXISTS idx_target_ym ON sfa_monthly_target (year, month);
"""

CSV_HEADERS = {
    "employee_id": ("客户经理id", "客户经理编号", "employee_id", "manager_virtual_id", "经理id"),
    "employee_name": ("客户经理姓名", "客户经理", "姓名", "employee_name", "name"),
    "year": ("年份", "年", "year"),
    "month": ("月份", "月", "month"),
    "target_store_count": ("月度目标门店数", "目标门店数", "月度目标", "目标", "target_store_count", "target"),
    "config_status": ("配置状态", "状态", "config_status", "status"),
    "note": ("备注", "note", "remark"),
}


class TargetError(ValueError):
    """目标配置非法入参。"""


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(TARGET_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """老库补列：``data_source`` 是后加的，历史行一律视为 MANUAL（真实录入）。"""
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(sfa_monthly_target)")}
    if "data_source" not in columns:
        conn.execute("ALTER TABLE sfa_monthly_target "
                     "ADD COLUMN data_source TEXT NOT NULL DEFAULT 'MANUAL'")
        conn.commit()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _clean_id(value) -> str:
    text = str(value or "").strip()
    if not text:
        raise TargetError("客户经理 ID 不能为空")
    if len(text) > 40:
        raise TargetError("客户经理 ID 最长 40 个字符")
    return text


def _clean_year(value) -> int:
    try:
        year = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise TargetError(f"年份必须为整数，收到 {value!r}") from exc
    if year != config.FIXED_YEAR_INT:
        raise TargetError(
            f"本期驾驶舱只展示 Y26（{config.FIXED_YEAR_INT} 年），不接受 {year} 年的目标")
    return year


def _clean_month(value) -> int:
    try:
        month = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise TargetError(f"月份必须为整数，收到 {value!r}") from exc
    if month < config.COMPARABLE_FIRST_MONTH or month > config.COMPARABLE_LAST_MONTH:
        raise TargetError(
            f"月份必须在 {config.COMPARABLE_FIRST_MONTH}–{config.COMPARABLE_LAST_MONTH} 之间")
    return month


def _clean_target(value) -> int:
    try:
        target = int(float(str(value).strip()))
    except (TypeError, ValueError) as exc:
        raise TargetError(f"月度目标门店数必须为整数，收到 {value!r}") from exc
    if target <= 0:
        # 0 会在下游被当作「未配置」，写进库只会让人误以为配置成功，
        # 所以在这一层就拒绝：没有目标就留空，不要让库里出现 0。
        raise TargetError("月度目标门店数必须为正整数；没有目标请留空，不要填 0")
    if target > MAX_TARGET:
        raise TargetError(f"月度目标门店数 {target} 超出合理上限 {MAX_TARGET}，请确认单位是否填错")
    return target


def _clean_status(value) -> str:
    status = str(value or "ACTIVE").strip().upper()
    if status not in STATUSES:
        raise TargetError(f"配置状态只能是 {' / '.join(STATUSES)}，收到 {value!r}")
    return status


# ------------------------------------------------------------------ 员工花名册
def employee_registry(etl_conn: sqlite3.Connection) -> dict[str, dict]:
    """从 ETL 库只读拉取客户经理花名册（含战区与市场部），用于校验 ID 是否真实存在。"""
    rows = etl_conn.execute(
        "SELECT manager_virtual_id AS employee_id, store_manager_name AS employee_name, "
        "war_zone, market_department, managed_store_count "
        "FROM dim_employee WHERE employee_key > 0"
    ).fetchall()
    return {r["employee_id"]: dict(r) for r in rows}


# ------------------------------------------------------------------ 查询
def list_targets(etl_conn: sqlite3.Connection, year: int | None = None,
                 month: int | None = None, employee_id: str | None = None) -> dict:
    """返回目标列表 + 未配置的客户经理清单（两侧都来自真实数据）。"""
    registry = employee_registry(etl_conn)
    conn = _connect()
    try:
        clauses, params = [], []
        if year:
            clauses.append("year = ?")
            params.append(_clean_year(year))
        if month:
            clauses.append("month = ?")
            params.append(_clean_month(month))
        if employee_id:
            clauses.append("employee_id = ?")
            params.append(_clean_id(employee_id))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM sfa_monthly_target{where} "
            "ORDER BY year DESC, month DESC, employee_id", params).fetchall()]
    finally:
        conn.close()

    for row in rows:
        info = registry.get(row["employee_id"])
        row["in_registry"] = info is not None
        row["war_zone"] = info["war_zone"] if info else None
        row["market_department"] = info["market_department"] if info else None
        row["employee_name"] = row["employee_name"] or (info["employee_name"] if info else None)
        row["is_demo"] = row.get("data_source") == SOURCE_DEMO

    configured = {(r["employee_id"], r["year"], r["month"]) for r in rows
                  if r["config_status"] == "ACTIVE"}
    demo_rows = [r for r in rows if r["is_demo"]]
    # 未配置：花名册里每个「年份×月份」组合都应有目标；这里只报当前（或指定）月份的缺口
    missing: list[dict] = []
    check_months = [month] if month else list(range(
        config.COMPARABLE_FIRST_MONTH, config.COMPARABLE_LAST_MONTH + 1))
    check_year = _clean_year(year) if year else config.FIXED_YEAR_INT
    for m in check_months:
        for eid, info in registry.items():
            if (eid, check_year, m) in configured:
                continue
            missing.append({
                "employee_id": eid, "employee_name": info["employee_name"],
                "war_zone": info["war_zone"], "market_department": info["market_department"],
                "year": check_year, "month": m,
            })

    return {
        "rows": rows,
        "missing": missing,
        "summary": {
            "configured_rows": len([r for r in rows if r["config_status"] == "ACTIVE"]),
            "disabled_rows": len([r for r in rows if r["config_status"] != "ACTIVE"]),
            "demo_rows": len(demo_rows),
            "manual_rows": len([r for r in rows if not r["is_demo"]]),
            "registry_employees": len(registry),
            "missing_count": len(missing),
            "unknown_employee_rows": len([r for r in rows if not r["in_registry"]]),
        },
        "demo": {
            "rows": len(demo_rows),
            "months": sorted({int(r["month"]) for r in demo_rows}),
            "employees": len({r["employee_id"] for r in demo_rows}),
            "formula": ("示例目标 = max(1, round(该客户经理当月实际完成门店数 × 系数))，"
                        f"系数由「客户经理ID|年份|月份」哈希得出，范围 "
                        f"{DEMO_FACTOR_MIN:.2f}–{DEMO_FACTOR_MIN + DEMO_FACTOR_SPAN:.2f}"),
            "note": DEMO_NOTE if demo_rows else None,
        },
        "registry": sorted(registry.values(), key=lambda x: x["employee_id"]),
        "note": ("目标为空白状态时页面显示「未配置」，不按 0 参与完成率；"
                 "系统不会静默补默认值。示例目标（DEMO）在库、接口、页面三处均有标注，可一键清除。"),
    }


# ------------------------------------------------------------------ 写入
def upsert_targets(etl_conn: sqlite3.Connection, items: list[dict],
                   data_source: str = SOURCE_MANUAL) -> dict:
    """批量新增 / 修改目标（按 客户经理ID + 年 + 月 唯一键覆盖）。

    手工录入 / CSV 导入写入的目标来源为 MANUAL / IMPORT，会把手上的 DEMO 行覆盖成真实来源
    （即：一旦人工录了真实目标，同一「人+月」的示例目标自动失效、不会被清除逻辑误删）。
    """
    if data_source not in SOURCES:
        raise TargetError(f"data_source 只能是 {' / '.join(SOURCES)}，收到 {data_source!r}")
    if not items:
        raise TargetError("至少提供 1 条目标记录")
    if len(items) > MAX_IMPORT_ROWS:
        raise TargetError(f"单次最多提交 {MAX_IMPORT_ROWS} 条")
    registry = employee_registry(etl_conn)
    prepared, errors = [], []
    for idx, raw in enumerate(items, 1):
        try:
            eid = _clean_id(raw.get("employee_id"))
            item = {
                "employee_id": eid,
                "employee_name": (str(raw.get("employee_name") or "").strip() or
                                  (registry[eid]["employee_name"] if eid in registry else None)),
                "year": _clean_year(raw.get("year")),
                "month": _clean_month(raw.get("month")),
                "target_store_count": _clean_target(raw.get("target_store_count")),
                "config_status": _clean_status(raw.get("config_status")),
                "configured_at": _now(),
                "note": (str(raw.get("note") or "").strip() or None),
            }
            item["_warn"] = None if eid in registry else f"{eid} 不在 SFA 花名册内，已保存但不参与完成率"
            prepared.append(item)
        except TargetError as exc:
            errors.append({"row": idx, "error": str(exc), "input": raw})

    if not prepared:
        return {"saved": 0, "created": 0, "updated": 0, "errors": errors, "warnings": []}

    conn = _connect()
    created = updated = 0
    try:
        with conn:
            for item in prepared:
                exists = conn.execute(
                    "SELECT id FROM sfa_monthly_target WHERE employee_id=? AND year=? AND month=?",
                    (item["employee_id"], item["year"], item["month"])).fetchone()
                if exists:
                    conn.execute(
                        "UPDATE sfa_monthly_target SET employee_name=?, target_store_count=?, "
                        "config_status=?, configured_at=?, note=?, data_source=? WHERE id=?",
                        (item["employee_name"], item["target_store_count"],
                         item["config_status"], item["configured_at"], item["note"],
                         data_source, exists["id"]))
                    updated += 1
                else:
                    conn.execute(
                        "INSERT INTO sfa_monthly_target (employee_id, employee_name, year, month, "
                        "target_store_count, config_status, configured_at, note, data_source) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (item["employee_id"], item["employee_name"], item["year"], item["month"],
                         item["target_store_count"], item["config_status"],
                         item["configured_at"], item["note"], data_source))
                    created += 1
    finally:
        conn.close()

    warnings = [w for w in (p.get("_warn") for p in prepared) if w]
    return {"saved": created + updated, "created": created, "updated": updated,
            "errors": errors, "warnings": warnings, "data_source": data_source,
            "configured_at": _now()}


def delete_target(employee_id: str, year: int, month: int) -> dict:
    """删除一条目标（用于撤回误配；不做物理伤害，只是回到「未配置」）。"""
    eid, y, m = _clean_id(employee_id), _clean_year(year), _clean_month(month)
    conn = _connect()
    try:
        with conn:
            cur = conn.execute(
                "DELETE FROM sfa_monthly_target WHERE employee_id=? AND year=? AND month=?",
                (eid, y, m))
        return {"deleted": cur.rowcount}
    finally:
        conn.close()


# ------------------------------------------------------------------ CSV 导入
def _pick(row: dict, keys: tuple[str, ...]) -> str | None:
    lowered = {str(k).strip().lower(): v for k, v in row.items() if k is not None}
    for key in keys:
        if key in lowered:
            return lowered[key]
    return None


def import_csv(etl_conn: sqlite3.Connection, text: str) -> dict:
    """解析 CSV 文本并 upsert。

    表头允许中文或英文（见 ``CSV_HEADERS``）；缺列直接报错，不做猜测。
    """
    if not text or not text.strip():
        raise TargetError("CSV 内容为空")
    sample = text.lstrip("\ufeff")
    try:
        dialect = csv.Sniffer().sniff(sample.splitlines()[0], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(sample), dialect=dialect)
    if not reader.fieldnames:
        raise TargetError("CSV 缺少表头行")
    header_map = {name: key for key, aliases in CSV_HEADERS.items()
                  for name in (reader.fieldnames or [])
                  if str(name).strip().lower() in aliases}
    required = {"employee_id", "year", "month", "target_store_count"}
    missing = required - set(header_map.values())
    if missing:
        raise TargetError(
            "CSV 缺少必需列：" + "、".join(sorted(missing))
            + "；表头示例：客户经理ID,客户经理姓名,年份,月份,月度目标门店数")

    items, errors = [], []
    for idx, row in enumerate(reader, 2):
        if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
            continue
        if len(items) >= MAX_IMPORT_ROWS:
            errors.append({"row": idx, "error": f"超过单次上限 {MAX_IMPORT_ROWS} 行，其余未导入"})
            break
        payload = {key: _pick(row, aliases) for key, aliases in CSV_HEADERS.items()}
        if payload.get("target_store_count") in (None, ""):
            errors.append({"row": idx, "error": "月度目标门店数为空，已跳过"})
            continue
        items.append(payload)

    result = upsert_targets(etl_conn, items, data_source=SOURCE_IMPORT) if items else {
        "saved": 0, "created": 0, "updated": 0, "errors": [], "warnings": [],
        "data_source": SOURCE_IMPORT}
    result["errors"] = errors + result["errors"]
    result["parsed_rows"] = len(items)
    return result


def targets_for_period(year: int, month: int) -> dict[str, int]:
    """取某年某月的「客户经理 → 目标门店数」映射（只含 ACTIVE）。

    供拜访分析查询层直接调用；文件不存在 / 表为空时返回空字典（= 全部未配置）。
    """
    try:
        conn = _connect()
    except (sqlite3.Error, OSError):
        return {}
    try:
        rows = conn.execute(
            "SELECT employee_id, target_store_count FROM sfa_monthly_target "
            "WHERE year=? AND month=? AND config_status='ACTIVE'", (year, month)).fetchall()
        return {r["employee_id"]: int(r["target_store_count"]) for r in rows}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


# ------------------------------------------------------------------ 示例目标（DEMO）
def _demo_factor(employee_id: str, year: int, month: int) -> float:
    """确定性系数：同一「人+年+月」永远得到同一个值，保证可复现、可验收。"""
    digest = hashlib.blake2s(f"{employee_id}|{year}|{month}".encode("utf-8"),
                             digest_size=4).digest()
    unit = int.from_bytes(digest, "big") / float(2 ** 32)
    return DEMO_FACTOR_MIN + DEMO_FACTOR_SPAN * unit


def _actual_store_counts(etl_conn: sqlite3.Connection, year: int) -> list[dict]:
    """按「客户经理 × 自然月」取实际完成门店数。

    口径与看板完全一致（复用 ``board_filters.where_sfa_visit``：已拜访 + 门店非空 +
    日期非空 + Y26 + 五大战区），否则示例目标会基于一套数字、完成率基于另一套。
    """
    from . import board_filters          # 局部导入：避免 server 模块间的循环引用
    clause, params = board_filters.where_sfa_visit(board_filters.BoardFilters())
    sql = ("SELECT v.manager_virtual_id AS employee_id, "
           "CAST(substr(v.visit_date, 6, 2) AS INTEGER) AS month, "
           "COUNT(DISTINCT v.store_id) AS actual_stores "
           "FROM fact_sfa_visit v WHERE " + clause + " GROUP BY 1, 2 ORDER BY 2, 1")
    return [dict(r) for r in etl_conn.execute(sql, params).fetchall()]


def seed_demo_targets(etl_conn: sqlite3.Connection, year: int | None = None) -> dict:
    """为「有实际拜访的 客户经理×月份」生成一组示例目标。

    规则（页面与文档逐字一致）
    ------------------------
    - 只覆盖「有有效拜访」的组合：没有拜访的人不造目标，否则完成率会被 0 拉垮且无意义；
    - 示例目标 = ``max(1, round(当月实际完成门店数 × 系数))``，系数 0.80–1.30；
    - 已有 **MANUAL / IMPORT** 目标的行一律跳过，绝不覆盖真实目标；
    - 已有 **DEMO** 行则原地更新（重复点击结果不变，因为系数是确定性的）。
    """
    y = _clean_year(year or config.FIXED_YEAR_INT)
    actuals = _actual_store_counts(etl_conn, y)
    if not actuals:
        raise TargetError("当前库里没有可用拜访数据，无法生成示例目标")

    registry = employee_registry(etl_conn)
    conn = _connect()
    seeded = updated = skipped_manual = 0
    try:
        with conn:
            for row in actuals:
                eid, month, actual = row["employee_id"], int(row["month"]), int(row["actual_stores"])
                if actual <= 0 or not eid:
                    continue
                existing = conn.execute(
                    "SELECT id, data_source FROM sfa_monthly_target "
                    "WHERE employee_id=? AND year=? AND month=?", (eid, y, month)).fetchone()
                if existing and existing["data_source"] in (SOURCE_MANUAL, SOURCE_IMPORT):
                    skipped_manual += 1
                    continue
                target = max(1, round(actual * _demo_factor(eid, y, month)))
                name = registry[eid]["employee_name"] if eid in registry else None
                if existing:
                    conn.execute(
                        "UPDATE sfa_monthly_target SET target_store_count=?, config_status='ACTIVE',"
                        " configured_at=?, note=?, data_source=? WHERE id=?",
                        (target, _now(), DEMO_NOTE, SOURCE_DEMO, existing["id"]))
                    updated += 1
                else:
                    conn.execute(
                        "INSERT INTO sfa_monthly_target (employee_id, employee_name, year, month, "
                        "target_store_count, config_status, configured_at, note, data_source) "
                        "VALUES (?,?,?,?,?,'ACTIVE',?,?,?)",
                        (eid, name, y, month, target, _now(), DEMO_NOTE, SOURCE_DEMO))
                    seeded += 1
    finally:
        conn.close()

    return {
        "seeded": seeded, "updated": updated, "skipped_real_targets": skipped_manual,
        "year": y,
        "months": sorted({int(r["month"]) for r in actuals}),
        "employees": len({r["employee_id"] for r in actuals}),
        "data_source": SOURCE_DEMO,
        "formula": ("示例目标 = max(1, round(该客户经理当月实际完成门店数 × 系数))，"
                    f"系数由「客户经理ID|年份|月份」哈希得出，范围 "
                    f"{DEMO_FACTOR_MIN:.2f}–{DEMO_FACTOR_MIN + DEMO_FACTOR_SPAN:.2f}"),
        "note": DEMO_NOTE,
    }


def clear_demo_targets() -> dict:
    """只删示例目标，不动任何手工录入 / 导入的真实目标。"""
    conn = _connect()
    try:
        with conn:
            cur = conn.execute("DELETE FROM sfa_monthly_target WHERE data_source = ?",
                               (SOURCE_DEMO,))
        return {"deleted": cur.rowcount, "data_source": SOURCE_DEMO,
                "note": "已清除示例目标；真实录入/导入的目标未被触碰。"}
    finally:
        conn.close()


def targets_for_period_detail(year: int, month: int) -> dict[str, dict]:
    """同 ``targets_for_period``，但带数据来源，供页面标「示例」角标。

    返回 ``{employee_id: {"target": int, "source": "DEMO" | "MANUAL" | "IMPORT"}}``。
    """
    try:
        conn = _connect()
    except (sqlite3.Error, OSError):
        return {}
    try:
        rows = conn.execute(
            "SELECT employee_id, target_store_count, data_source FROM sfa_monthly_target "
            "WHERE year=? AND month=? AND config_status='ACTIVE'", (year, month)).fetchall()
        return {r["employee_id"]: {"target": int(r["target_store_count"]),
                                   "source": r["data_source"] or SOURCE_MANUAL}
                for r in rows}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _display_path() -> str:
    """配置库的相对路径（测试把库指到临时目录时回退为绝对路径，不抛异常）。"""
    try:
        return str(TARGET_DB.relative_to(config.ROOT.parent))
    except ValueError:
        return str(TARGET_DB)


def targets_map_for_months(year: int, months) -> dict[int, dict[str, dict]]:
    """一次查询取出多个月的目标（含来源），避免「按月开库」把接口拖慢。

    返回 ``{month: {employee_id: {"target": int, "source": str}}}``。
    月度趋势 / 人员明细都要按「人 × 月」取目标，逐个开 SQLite 连接会慢一个量级。
    """
    wanted = [int(m) for m in months]
    if not wanted:
        return {}
    try:
        conn = _connect()
    except (sqlite3.Error, OSError):
        return {}
    try:
        rows = conn.execute(
            "SELECT month, employee_id, target_store_count, data_source "
            "FROM sfa_monthly_target WHERE year=? AND config_status='ACTIVE' "
            "AND month IN (" + ",".join("?" * len(wanted)) + ")",
            (year, *wanted)).fetchall()
        out: dict[int, dict[str, dict]] = {}
        for r in rows:
            out.setdefault(int(r["month"]), {})[r["employee_id"]] = {
                "target": int(r["target_store_count"]),
                "source": r["data_source"] or SOURCE_MANUAL,
            }
        return out
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def target_meta() -> dict:
    """配置库自身的元信息（供页面展示「目标来自哪个库、最后更新时间」）。"""
    if not TARGET_DB.exists():
        return {"db_path": _display_path(),
                "exists": False, "last_write": None, "row_count": 0, "demo_rows": 0}
    stat = os.stat(TARGET_DB)
    count = demo = 0
    try:
        conn = _connect()
        try:
            count = conn.execute("SELECT COUNT(*) FROM sfa_monthly_target").fetchone()[0]
            demo = conn.execute("SELECT COUNT(*) FROM sfa_monthly_target "
                                "WHERE data_source = ?", (SOURCE_DEMO,)).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        count = demo = 0
    return {
        "db_path": _display_path(),
        "exists": True,
        "last_write": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
        "row_count": count,
        "demo_rows": demo,
    }
