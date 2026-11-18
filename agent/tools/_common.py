"""工具层公共设施：连接、视图、期间校验、标准返回结构。

**口径一致性**是这里最要紧的事：Agent 算出来的达成率必须和 BI 页面上看到的一样。
做法不是「再写一遍同样的 SQL」（那迟早会分叉），而是：

1. 连接复用 ``server.db.connect()``（它是 BI 的取数入口，带 ``mode=ro`` 与投影视图）；
2. 预算/实际直接读 ``analysis_grid._prepare()`` 建好的 ``v_budget`` / ``v_fact``，
   这两个视图就是「战区销售达成跟进」页用的同一份数据；
3. 客户类型分类复用 ``analysis_grid.SEGMENT_SQL`` 这条 CASE，不另写一套；
4. 每个工具除了返回数字，还返回 ``caliber``（口径说明）与 ``data_source``（取数来源）。

期间校验刻意严格：2026 年只有 1–10 月有实际出货，问 11 月不是「返回 0」，
而是抛 ``PeriodNotObserved``，让 Agent 明确说「这个月还没有业务发生」。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from server import analysis_grid, config
from server.db import connect

# 与 BI 页面同一条分类规则（ClubZ / NKA / LKA / 批发 / Others）
SEGMENT_SQL = analysis_grid.SEGMENT_SQL
SEGMENT_LABEL = analysis_grid.SEGMENT_LABEL

SUPPORTED_YEARS = analysis_grid.SUPPORTED_YEARS
CAP_BY_YEAR = analysis_grid.CAP_BY_YEAR

TOOL_VERSION = "agent-tools-v1"


class ToolError(Exception):
    """工具级错误：带机器可读的 code，交给 Agent 明确说明，而不是返回空数据。"""

    def __init__(self, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    def as_dict(self) -> dict:
        return {"type": self.code, "message": self.message, "hint": self.hint}


# --------------------------------------------------------------------- 连接
def open_conn() -> sqlite3.Connection:
    """打开只读连接，并挂上 BI 用的预算/补充库与两个投影视图。"""
    conn = connect()
    try:
        analysis_grid._prepare(conn)          # v_fact / v_budget（ATTACH bud + ext）
    except sqlite3.Error:
        pass                                   # 预算库缺失时工具会各自降级
    _ensure_erp_view(conn)
    return conn


def _ensure_erp_view(conn: sqlite3.Connection) -> None:
    """``a_erp``：v_fact 的加料版（多 cost_amount / gross_profit / shipment_no）。

    v_fact 缺金额成本列，而「毛利率」必须由 ERP 出货口径算，所以单建一个视图；
    ``data_source`` 保留下来，可以随时把演示补充数据（DEMO_EXT）剔掉复算。
    """
    try:
        conn.execute("SELECT 1 FROM temp.sqlite_master WHERE name = 'a_erp'").fetchone()
        if conn.execute("SELECT count(*) FROM temp.sqlite_master WHERE name='a_erp'"
                        ).fetchone()[0]:
            return
    except sqlite3.Error:
        pass

    base = """
        SELECT year, month, region_name_std, region_scope_status, dept_short,
               customer_id, customer_short, customer_type, channel_type, category,
               sku_code, sku_name, stall_code, shipment_te, sales_boxes, sales_amount,
               cost_amount, gross_profit, data_source, shipment_no
        FROM main.fact_erp_shipment"""
    ext = ""
    try:
        conn.execute("SELECT count(*) FROM ext.dim_customer_ext").fetchone()
        ext = """
        UNION ALL
        SELECT year, month, region_name_std, 'IN_SCOPE' AS region_scope_status, dept_short,
               customer_id, customer_short, customer_type, channel_type, category,
               sku_code, sku_name, '' AS stall_code, shipment_te, sales_boxes, sales_amount,
               cost_amount, gross_profit, data_source, shipment_no
        FROM ext.fact_shipment_ext"""
    except sqlite3.Error:
        ext = ""
    conn.execute(f"CREATE TEMP VIEW a_erp AS {base}{ext}")


# --------------------------------------------------------------------- 期间
def resolve_period(year: Any = None, months: Any = None) -> dict:
    """校验并归一统计期间。

    返回 ``{"year", "months", "label", "cap", "notes"}``。
    规则：
    - 年份只支持 2026（与 BI 页面一致）；其它年份直接报错，不静默改成 2026；
    - 月份缺省 = 累计到数据截止月（1–10 月）；
    - 显式选了截止月之后的月份：能用的月份照用，被排除的写进 notes，
      全部越界则报 ``PeriodNotObserved``。
    """
    y = int(year) if year not in (None, "", 0) else config.FIXED_YEAR_INT
    if y not in SUPPORTED_YEARS:
        raise ToolError(
            "UnsupportedYear",
            f"{y} 年不在本 Demo 的数据范围内（当前只交付 2026 年 1–10 月）",
            "请把问题限定在 2026 年；2025 年只有实际出货，没有可用的 TE 预算",
        )
    cap = CAP_BY_YEAR.get(y, config.COMPARABLE_LAST_MONTH)
    notes: list[str] = []

    if months:
        raw = [int(m) for m in months if str(m).strip() not in ("", "None")]
        bad = [m for m in raw if m < 1 or m > 12]
        if bad:
            raise ToolError("InvalidMonth", f"月份 {bad} 不合法（1–12）")
        observed = sorted({m for m in raw if 1 <= m <= cap})
        dropped = sorted({m for m in raw if m > cap})
        if not observed:
            raise ToolError(
                "PeriodNotObserved",
                f"{y} 年 {dropped} 月尚未发生业务（ERP 出货数据截止 {y}-{cap:02d}），"
                f"无法计算该期间的达成率",
                "换一个已发生的月份，或改为累计口径（不指定月份）",
            )
        if dropped:
            notes.append(f"{y} 年 {dropped} 月尚未发生业务，已从统计期间剔除")
        months_out = observed
    else:
        months_out = list(range(1, cap + 1))

    return {"year": y, "months": months_out, "label": period_label(y, months_out),
            "cap": cap, "notes": notes}


def period_label(year: int, months: list[int]) -> str:
    if not months:
        return f"{year} 年（无观测月份）"
    if len(months) == 1:
        return f"{year} 年 {months[0]} 月"
    if months == list(range(1, len(months) + 1)):
        return f"{year} 年 1–{months[-1]} 月累计"
    return f"{year} 年 " + "、".join(str(m) for m in months) + " 月"


def month_keys(period: dict) -> list[int]:
    return [period["year"] * 100 + m for m in period["months"]]


def month_marks(months: list[int]) -> tuple[str, list]:
    """生成 ``month IN (?,?,...)``；空列表返回永不命中的条件。"""
    if not months:
        return "0=1", []
    return f"month IN ({','.join('?' for _ in months)})", list(months)


# --------------------------------------------------------------------- 过滤
def scope_clause(region: str = "", department: str = "",
                 table: str = "erp") -> tuple[str, list]:
    """战区 / 市场部过滤。POS、SFA、批发表的列名不同，按 table 选。"""
    col_region = "region_name_std"
    col_dept = {"erp": "dept_short", "pos": "market_department",
                "sfa": "market_department", "wholesale": None}.get(table, "dept_short")
    sql, args = ["region_scope_status = 'IN_SCOPE'"], []
    if region:
        sql.append(f"{col_region} = ?")
        args.append(region)
    if department:
        if not col_dept:
            raise ToolError("UnsupportedFilter",
                            f"{table} 数据没有市场部维度，无法按市场部筛选")
        sql.append(f"{col_dept} = ?")
        args.append(department)
    return " AND ".join(sql), args


def like_escape(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --------------------------------------------------------------------- 结果
def envelope(tool_name: str, *, args: dict, period: dict, scope: dict,
             data_source: str, caliber: str, metrics: dict,
             rows: dict | None = None, notes: list[str] | None = None,
             tables: list[str] | None = None) -> dict:
    """工具的标准返回结构。所有工具都必须走这里，字段才可能一致。"""
    notes_all = list(period.get("notes") or []) + list(notes or [])
    return {
        "ok": True,
        "tool": tool_name,
        "version": TOOL_VERSION,
        "args": args,
        "period": {"year": period["year"], "months": period["months"],
                   "label": period["label"]},
        "scope": scope,
        "data_source": data_source,
        "caliber": caliber,
        "metrics": metrics,
        "rows": rows or {},
        "tables": tables or [],
        "notes": notes_all,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def fail(tool_name: str, err: ToolError, args: dict | None = None) -> dict:
    return {"ok": False, "tool": tool_name, "args": args or {},
            "error": err.as_dict(),
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds")}


# --------------------------------------------------------------------- 数值
def r2(v) -> float | None:
    return None if v is None else round(float(v), 2)


def r4(v) -> float | None:
    return None if v is None else round(float(v), 4)


def pct(num, den) -> float | None:
    """达成率 = 实际 ÷ 预算 × 100；分母为 0 或缺失时返回 None（页面显示「—」）。"""
    if num is None or den in (None, 0):
        return None
    return round(float(num) / float(den) * 100, 4)


def yoy(cur, base) -> float | None:
    """同比 = (本期 ÷ 去年同期 − 1) × 100；去年同期为 0 或缺失时返回 None。"""
    if cur is None or base in (None, 0):
        return None
    return round((float(cur) / float(base) - 1) * 100, 4)


def fmt_te(v, digits: int = 2) -> str:
    return "—" if v is None else f"{float(v):,.{digits}f}"
