"""四个新看板的筛选器：按看板区分可用筛选，避免筛选器机械复制。

与 ``filters.py``（全国经营驾驶舱 / 综合经营分析）完全独立：
- 那两个页面的入参解析与 SQL 逐字保持不变；
- 本模块只服务 SFA 拜访分析、SFA 门店画像、批发经营分析、POS 门店动销。

统一规则
--------
- 年份固定 Y26（2026），任何其他年份直接拒绝；
- 月份 1–10（数据截止 2026-10-31），越界拒绝，不补零不预测；
- 战区只允许五大战区，战区F / 战区G属 OUT_OF_SCOPE，不在白名单内；
- 所有值以 ``?`` 绑定进 SQL，不做字符串拼接；
- 排序字段走白名单，关键词只做 LIKE 参数绑定。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import config

SORT_WHITELIST: dict[str, tuple[str, ...]] = {
    "sfa_visit": (
        "employee_id", "employee_name", "war_zone", "market_department",
        "target_store_count", "visited_store_count", "visit_count",
        "completion_rate", "gap", "repeat_visit_count", "coverage_rate",
    ),
    "sfa_profile": (
        "store_id", "store_name", "ka_system", "war_zone", "market_department",
        "store_level", "visit_date", "manager_virtual_id", "focus_sku_name",
        "focus_retail_price", "visit_count",
    ),
    "wholesale": (
        "full_date", "month", "customer_name", "stall_name", "sku_code", "sku_name",
        "category", "purchase_qty", "purchase_amount", "sales_qty", "sales_amount",
        "cost_amount", "gross_profit", "ending_qty", "inventory_amount",
        "doc_id", "doc_count",
    ),
    "pos": (
        "store_id", "store_name", "ka_system", "war_zone", "market_department",
        "month", "category", "sku_code", "sku_name", "sales_boxes", "sales_amount",
        "sales_te", "unit_price", "sku_count", "store_count", "share",
        "mom", "momentum", "amount_first", "amount_second",
    ),
}

# 每个看板真正允许的筛选参数（前端也只渲染这些）
ALLOWED: dict[str, frozenset[str]] = {
    "sfa_visit": frozenset({
        "months", "regions", "market_department", "employee", "metric",
        "page", "limit", "sort", "order", "q",
    }),
    "sfa_profile": frozenset({
        "months", "regions", "market_department", "employee", "ka", "sku", "category",
        "page", "limit", "sort", "order", "q", "section",
    }),
    "wholesale": frozenset({
        "months", "regions", "customer", "stall", "sku", "category", "kind",
        "page", "limit", "sort", "order", "q",
    }),
    "pos": frozenset({
        "months", "regions", "ka", "category", "sku", "store", "kind",
        "page", "limit", "sort", "order", "q",
    }),
}

# kind 的合法取值按看板区分：批发是三类单据，POS 是两种明细粒度。
KINDS: dict[str, tuple[str, ...]] = {
    "wholesale": ("purchase", "sales", "inventory"),
    "pos": ("store", "sku"),
}

MAX_SEARCH_LEN = 40
MAX_TEXT_LEN = 60


class BoardFilterError(ValueError):
    """看板筛选参数非法。"""


@dataclass(frozen=True)
class BoardFilters:
    board: str = "sfa_visit"
    year: int = config.FIXED_YEAR_INT
    months: tuple[int, ...] = ()
    regions: tuple[str, ...] = config.IN_SCOPE_REGIONS
    market_department: str | None = None
    employee: str | None = None
    ka: str | None = None
    sku: str | None = None
    category: str | None = None
    customer: str | None = None
    stall: str | None = None
    store: str | None = None
    metric: str = "amount"
    kind: str = "purchase"
    section: str = "all"
    page: int = 1
    limit: int = 10
    sort: str | None = None
    order: str = "asc"
    q: str | None = None

    @property
    def region_label(self) -> str:
        if len(self.regions) == len(config.IN_SCOPE_REGIONS):
            return config.EXCLUDED_REGION_NOTE
        return "、".join(self.regions) + "（不含战区F、战区G）"

    @property
    def month_label(self) -> str:
        if not self.months:
            return "Y26 全期（1–10 月）"
        return "Y26 · " + "、".join(f"{m} 月" for m in self.months)

    def as_dict(self) -> dict:
        return {
            "board": self.board, "year": self.year, "months": list(self.months),
            "regions": list(self.regions), "market_department": self.market_department,
            "employee": self.employee, "ka": self.ka, "sku": self.sku,
            "category": self.category, "customer": self.customer, "stall": self.stall,
            "store": self.store,
            "metric": self.metric, "kind": self.kind, "section": self.section,
            "page": self.page, "limit": self.limit, "sort": self.sort,
            "order": self.order, "q": self.q,
        }


def _one(qs: dict[str, str], key: str) -> str | None:
    value = qs.get(key)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _text(qs: dict[str, str], key: str) -> str | None:
    value = _one(qs, key)
    if value and len(value) > MAX_TEXT_LEN:
        raise BoardFilterError(f"{key} 最长 {MAX_TEXT_LEN} 个字符")
    return value


def parse_board(qs: dict[str, str], board: str) -> BoardFilters:
    if board not in ALLOWED:
        raise BoardFilterError(f"未知看板 {board!r}")

    unexpected = sorted(set(qs) - ALLOWED[board] - {"year", "region", "regions"})
    if unexpected:
        raise BoardFilterError(
            f"{board} 看板不支持这些筛选参数: {unexpected}（该看板只渲染相关筛选器）")

    year = _one(qs, "year")
    if year and year != str(config.FIXED_YEAR_INT):
        raise BoardFilterError(
            f"本期只展示 Y26（{config.FIXED_YEAR_INT} 年），不接受 {year!r} 或跨年对比")

    raw_regions = _one(qs, "regions") or _one(qs, "region") or ""
    if raw_regions:
        picked = tuple(x.strip() for x in raw_regions.split(",") if x.strip())
        bad = [x for x in picked if x not in config.IN_SCOPE_REGIONS]
        if bad:
            raise BoardFilterError(f"战区只展示五大战区，非法值: {bad}")
        regions = tuple(dict.fromkeys(picked))
    else:
        regions = config.IN_SCOPE_REGIONS

    months: tuple[int, ...] = ()
    raw_months = _one(qs, "months")
    if raw_months:
        picked_months: list[int] = []
        for token in raw_months.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                month = int(token)
            except ValueError as exc:
                raise BoardFilterError(f"months 必须为 1–10 的整数，收到 {token!r}") from exc
            if month < config.COMPARABLE_FIRST_MONTH or month > config.COMPARABLE_LAST_MONTH:
                raise BoardFilterError(
                    f"月份 {month} 超出 Y26 数据范围（1–{config.COMPARABLE_LAST_MONTH} 月），"
                    "不补零、不预测")
            picked_months.append(month)
        if not picked_months:
            raise BoardFilterError("months 不能为空列表")
        months = tuple(sorted(set(picked_months)))

    try:
        limit = int(_one(qs, "limit") or "10")
        page = int(_one(qs, "page") or "1")
    except ValueError as exc:
        raise BoardFilterError("limit / page 必须为整数") from exc
    limit = max(1, min(limit, 50))
    page = max(1, min(page, 1000))

    sort = _one(qs, "sort")
    if sort and sort not in SORT_WHITELIST[board]:
        raise BoardFilterError(f"sort 不在 {board} 可排序字段内，收到 {sort!r}")
    order = (_one(qs, "order") or "asc").lower()
    if order not in ("asc", "desc"):
        raise BoardFilterError(f"order 只能是 asc / desc，收到 {order!r}")
    q = _one(qs, "q")
    if q and len(q) > MAX_SEARCH_LEN:
        raise BoardFilterError(f"搜索关键词最多 {MAX_SEARCH_LEN} 个字符")

    metric = _one(qs, "metric") or "amount"
    if metric not in ("amount", "qty"):
        raise BoardFilterError("metric 只能是 amount / qty")

    kind_default = KINDS.get(board, ("purchase",))[0]
    kind = _one(qs, "kind") or kind_default
    if board in KINDS and kind not in KINDS[board]:
        raise BoardFilterError(
            f"{board} 明细 kind 只能是 {' / '.join(KINDS[board])}，收到 {kind!r}")

    section = _one(qs, "section") or "all"
    if section not in ("all", "ka", "level", "price", "display", "joint"):
        raise BoardFilterError("section 只能是 all / ka / level / price / display / joint")

    return BoardFilters(
        board=board, year=config.FIXED_YEAR_INT, months=months, regions=regions,
        market_department=_text(qs, "market_department"), employee=_text(qs, "employee"),
        ka=_text(qs, "ka"), sku=_text(qs, "sku"), category=_text(qs, "category"),
        customer=_text(qs, "customer"), stall=_text(qs, "stall"),
        store=_text(qs, "store"), metric=metric,
        kind=kind, section=section, page=page, limit=limit, sort=sort, order=order, q=q,
    )


# ------------------------------------------------------------------ WHERE 构造
def _month_clause(f: BoardFilters, date_col: str = "month") -> tuple[str, list]:
    if f.months:
        return f"{date_col} IN ({','.join('?' * len(f.months))})", list(f.months)
    return f"{date_col} BETWEEN ? AND ?", [config.COMPARABLE_FIRST_MONTH,
                                           config.COMPARABLE_LAST_MONTH]


def where_sfa_visit(f: BoardFilters) -> tuple[str, list]:
    """SFA 拜访事实的 WHERE。

    有效拜访口径在这里一次性写清：
    - 必须 ``store_id`` 非空（缺门店不能算拜访到店）；
    - 必须 ``visit_date`` 非空（缺日期无法归属自然月）；
    - 必须 ``visit_status = '已拜访'``；
    - 年份固定 2026（按 visit_date 前 4 位判断，visit_date 为 ISO 文本）；
    - 战区在五大战区白名单内（OUT_OF_SCOPE / UNKNOWN 自然被排除）。

    ``fact_sfa_visit`` 没有 month 列，月份一律由 ``visit_date`` 派生，不另建映射。
    """
    clauses = [
        "v.visit_date IS NOT NULL",
        "substr(v.visit_date, 1, 4) = ?",
        "TRIM(COALESCE(v.store_id, '')) <> ''",
        "v.visit_status = '已拜访'",
        "v.region_name_std IN (" + ",".join("?" * len(f.regions)) + ")",
    ]
    params: list = [str(config.FIXED_YEAR_INT), *f.regions]
    if f.months:
        clauses.append(
            "CAST(substr(v.visit_date, 6, 2) AS INTEGER) IN ("
            + ",".join("?" * len(f.months)) + ")")
        params.extend(f.months)
    else:
        clauses.append("CAST(substr(v.visit_date, 6, 2) AS INTEGER) BETWEEN ? AND ?")
        params.extend([config.COMPARABLE_FIRST_MONTH, config.COMPARABLE_LAST_MONTH])
    if f.market_department:
        clauses.append("v.market_department = ?")
        params.append(f.market_department)
    if f.employee:
        clauses.append("v.manager_virtual_id = ?")
        params.append(f.employee)
    if f.ka:
        clauses.append("v.ka_system = ?")
        params.append(f.ka)
    if f.sku:
        clauses.append("v.focus_sku_code = ?")
        params.append(f.sku)
    if f.category:
        clauses.append("v.focus_sku_code IN (SELECT sku_code FROM dim_sku WHERE category = ?)")
        params.append(f.category)
    return " AND ".join(clauses), params


def where_sfa_profile(f: BoardFilters) -> tuple[str, list]:
    """门店画像与拜访分析共用同一套有效拜访口径，不另立第二套筛选语义。"""
    return where_sfa_visit(f)


def where_wholesale(f: BoardFilters, alias: str = "t", date_col: str = "month") -> tuple[str, list]:
    """批发三类事实共用 WHERE：Y26 + 月份 + 五大战区 + 客户 / 档口 / SKU / 品类。"""
    clauses = [
        f"{alias}.year = ?",
        f"{alias}.region_scope_status = 'IN_SCOPE'",
        f"{alias}.region_name_std IN (" + ",".join("?" * len(f.regions)) + ")",
    ]
    params: list = [config.FIXED_YEAR_INT, *f.regions]
    month_sql, month_params = _month_clause(f, date_col)
    clauses.append(month_sql)
    params.extend(month_params)
    if f.customer:
        clauses.append(f"{alias}.customer_id = ?")
        params.append(f.customer)
    if f.stall:
        clauses.append(f"{alias}.stall_id = ?")
        params.append(f.stall)
    if f.sku:
        clauses.append(f"{alias}.sku_code = ?")
        params.append(f.sku)
    if f.category:
        clauses.append(f"{alias}.category = ?")
        params.append(f.category)
    return " AND ".join(clauses), params


def where_pos(f: BoardFilters, alias: str = "p") -> tuple[str, list]:
    """POS 终端销售（Sell-out）的 WHERE。

    四条硬边界，缺一条就会算出「看起来更大」的假数：
    1. ``p.year = 2026``：P05 自然年，Y25 45,000 行不进本期；
    2. ``region_scope_status = 'IN_SCOPE'``：P01 五大战区，战区F/战区G 13,067 行排除；
    3. ``pos_link_status = 'POS_ALIGNED'``：P12 只用对齐版 POS（33 家 SFA-ST-xxxxx），
       原 V3 的 ST-00001~33 与本版交集为 0，绝不混算；
    4. 再按 ``region_name_std`` 落五大战区白名单，与筛选器联动。

    POS 无记录 ≠ 零销售：本函数只返回"有记录的门店"，
    435 家 SFA_ONLY 门店不在任何 POS 分母里（P13）。
    """
    clauses = [
        f"{alias}.year = ?",
        f"{alias}.region_scope_status = 'IN_SCOPE'",
        f"{alias}.pos_link_status = 'POS_ALIGNED'",
        f"{alias}.region_name_std IN (" + ",".join("?" * len(f.regions)) + ")",
    ]
    params: list = [config.FIXED_YEAR_INT, *f.regions]
    month_sql, month_params = _month_clause(f, f"{alias}.month")
    clauses.append(month_sql)
    params.extend(month_params)
    if f.ka:
        clauses.append(f"{alias}.ka_system = ?")
        params.append(f.ka)
    if f.sku:
        clauses.append(f"{alias}.sku_code = ?")
        params.append(f.sku)
    if f.category:
        clauses.append(f"{alias}.category = ?")
        params.append(f.category)
    if f.store:
        clauses.append(f"{alias}.store_id = ?")
        params.append(f.store)
    return " AND ".join(clauses), params
