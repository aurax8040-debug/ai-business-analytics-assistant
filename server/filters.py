"""全局筛选器：解析、白名单校验、参数化 WHERE 构造。

所有用户选择都以 ``?`` 绑定参数进入 SQL，不拼接字面量；
跨系统不适用的筛选器按冻结口径「不猜测」，只记录 ignored 说明。

V1.1：年份全局固定 Y26（2026 自然年 1–10 月）。
任何 year != Y26 的入参一律拒绝，WHERE 恒定带 ``year = 2026``，
从查询层杜绝其他年份数据进入页面。

战区销售分析 V1：新增可选 ``months``（1–10 月白名单），仅影响显式传参的请求；
不传时 WHERE 输出与 V1.1 逐字一致，全国经营驾驶舱保持原样。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import config


class FilterError(ValueError):
    """非法筛选参数。"""


# 综合经营分析 V2：明细表服务端排序白名单（列名直接进 ORDER BY，必须先解析为白名单）。
SORTABLE_COLUMNS = (
    "full_date", "month", "region_name_std", "sku_code", "sku_name", "category",
    "channel_type", "sales_boxes", "sales_amount", "customer_id", "customer_name",
    "shipment_te", "cost_amount", "gross_profit", "ka_system", "store_name", "sales_te",
)
MAX_SEARCH_LEN = 40


@dataclass(frozen=True)
class Filters:
    year: str = config.FIXED_YEAR
    regions: tuple[str, ...] = config.IN_SCOPE_REGIONS
    channel: str | None = None
    ka: str | None = None
    customer: str | None = None
    category: str | None = None
    sku: str | None = None
    pieces: int | None = None
    metric: str = "amount"
    limit: int = 10
    page: int = 1
    source: str = "pos"
    # 战区销售分析 V1：可选自然月白名单（1–10）。空元组 = 全期 1–10 月，
    # 与全国经营驾驶舱 V1.1 的默认 WHERE 逐字一致。
    months: tuple[int, ...] = ()
    # 综合经营分析 V1：当前分析维度（region / channel / ka / customer / category / sku）。
    # 只决定 GROUP BY 的拆解轴，不改变任何指标口径与 WHERE 条件。
    dimension: str = config.DEFAULT_DIMENSION
    # 综合经营分析 V2：仅作用于 /api/analysis/detail（驾驶舱明细不传这些参数）。
    sort: str | None = None
    order: str = "asc"
    q: str | None = None

    @property
    def year_int(self) -> int:
        return config.FIXED_YEAR_INT

    @property
    def region_label(self) -> str:
        if len(self.regions) == len(config.IN_SCOPE_REGIONS):
            return config.EXCLUDED_REGION_NOTE
        return "、".join(self.regions) + "（不含战区F、战区G）"

    def as_dict(self) -> dict:
        return {
            "year": self.year,
            "year_int": self.year_int,
            "regions": list(self.regions),
            "channel": self.channel,
            "ka": self.ka,
            "customer": self.customer,
            "category": self.category,
            "sku": self.sku,
            "pieces": self.pieces,
            "metric": self.metric,
            "limit": self.limit,
            "page": self.page,
            "months": list(self.months),
            "dimension": self.dimension,
            "sort": self.sort,
            "order": self.order,
            "q": self.q,
        }


def parse(qs: dict[str, str]) -> Filters:
    """从查询字符串构造 Filters；未知 / 越界值直接报错，不静默猜测。"""
    def one(key: str) -> str | None:
        v = qs.get(key)
        if v is None:
            return None
        v = v.strip()
        return v or None

    year = one("year") or config.FIXED_YEAR
    if year != config.FIXED_YEAR:
        raise FilterError(
            f"本期驾驶舱全局固定为 Y26（2026 年 1–10 月），不提供 {year!r} 或跨年对比")

    raw_regions = one("regions") or one("region") or ""
    if raw_regions:
        picked = tuple(x.strip() for x in raw_regions.split(",") if x.strip())
        bad = [x for x in picked if x not in config.IN_SCOPE_REGIONS]
        if bad:
            raise FilterError(f"战区只展示五大战区，非法值: {bad}")
        regions = tuple(dict.fromkeys(picked))
    else:
        regions = config.IN_SCOPE_REGIONS

    metric = one("metric") or "amount"
    if metric not in config.METRICS:
        raise FilterError(f"metric 只能是 amount / boxes / te，收到 {metric!r}")

    pieces_raw = one("pieces")
    pieces = None
    if pieces_raw:
        try:
            pieces = int(pieces_raw)
        except ValueError as exc:
            raise FilterError(f"pieces 必须为整数，收到 {pieces_raw!r}") from exc
        if pieces <= 0:
            raise FilterError("pieces 必须为正整数")

    try:
        limit = int(one("limit") or "10")
        page = int(one("page") or "1")
    except ValueError as exc:
        raise FilterError("limit / page 必须为整数") from exc
    limit = max(1, min(limit, 50))
    page = max(1, min(page, 1000))

    source = one("source") or "pos"
    if source not in ("erp", "pos"):
        raise FilterError("source 只能是 erp / pos")

    # 月度筛选：仅 2026 自然年 1–10 月（数据截止 2026-10-31），越界直接拒绝。
    raw_months = one("months")
    months: tuple[int, ...] = ()
    if raw_months:
        picked_months: list[int] = []
        for token in raw_months.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                m = int(token)
            except ValueError as exc:
                raise FilterError(f"months 必须为 1–10 的整数，收到 {token!r}") from exc
            if m < config.COMPARABLE_FIRST_MONTH or m > config.COMPARABLE_LAST_MONTH:
                raise FilterError(
                    f"月份 {m} 超出 Y26 数据范围（1–{config.COMPARABLE_LAST_MONTH} 月，"
                    "数据截止 2026-10-31），不补零、不预测")
            picked_months.append(m)
        if not picked_months:
            raise FilterError("months 不能为空列表")
        months = tuple(sorted(set(picked_months)))

    # 分析维度：白名单校验，非法值直接拒绝，不静默回退到默认维度。
    dimension = one("dimension") or config.DEFAULT_DIMENSION
    if dimension not in config.DIMENSION_KEYS:
        raise FilterError(
            f"dimension 只能是 {' / '.join(config.DIMENSION_KEYS)}，收到 {dimension!r}")

    # 明细排序 / 搜索：列名必须命中白名单（ORDER BY 不做字符串拼接兜底），
    # 关键词只做 LIKE 参数绑定，不进 SQL 字面量。
    sort = one("sort")
    if sort and sort not in SORTABLE_COLUMNS:
        raise FilterError(f"sort 不在可排序字段内，收到 {sort!r}")
    order = (one("order") or "asc").lower()
    if order not in ("asc", "desc"):
        raise FilterError(f"order 只能是 asc / desc，收到 {order!r}")
    q = one("q")
    if q and len(q) > MAX_SEARCH_LEN:
        raise FilterError(f"搜索关键词最多 {MAX_SEARCH_LEN} 个字符")

    return Filters(
        year=config.FIXED_YEAR, regions=regions, channel=one("channel"), ka=one("ka"),
        customer=one("customer"), category=one("category"), sku=one("sku"),
        pieces=pieces, metric=metric, limit=limit, page=page, source=source,
        months=months, dimension=dimension, sort=sort, order=order, q=q,
    )


def where(f: Filters, side: str) -> tuple[str, list, dict]:
    """构造该侧的 WHERE 片段（不含 WHERE 关键字）。

    side = 'erp' | 'pos'。返回 (sql, params, applicability)。
    恒定包含 ``year = 2026`` 与 ``month BETWEEN 1 AND 10``。
    """
    if side not in ("erp", "pos"):
        raise FilterError("side 只能是 erp / pos")

    clauses: list[str] = []
    params: list = []
    applied: dict[str, str] = {}
    ignored: dict[str, str] = {}

    clauses.append("year = ?")
    params.append(config.FIXED_YEAR_INT)
    applied["year"] = "固定 Y26 · 2026 自然年"

    if f.months:
        # 战区销售分析 V1 的月度筛选：与固定年份、五大战区同样以 ? 绑定参数进入 SQL。
        clauses.append("month IN (" + ",".join("?" * len(f.months)) + ")")
        params.extend(f.months)
        applied["period"] = "Y26 · " + "、".join(f"{m} 月" for m in f.months) \
            + " 自然月（数据截止 2026-10-31）"
    else:
        # 默认分支与 V1.1 完全一致：不传 months 时 SQL 与参数逐字不变。
        clauses.append("month BETWEEN ? AND ?")
        params.extend([config.COMPARABLE_FIRST_MONTH, config.COMPARABLE_LAST_MONTH])
        applied["period"] = config.PERIOD_SHORT + "自然月（数据截止 2026-10-31）"

    # P01：五大战区白名单
    clauses.append("region_name_std IN (" + ",".join("?" * len(f.regions)) + ")")
    params.extend(f.regions)
    applied["region"] = f.region_label

    if f.channel:
        clauses.append("channel_type = ?")
        params.append(f.channel)
        applied["channel"] = f.channel
        if side == "pos" and f.channel != "商超/终端":
            ignored["channel_pos"] = "POS 仅覆盖「商超/终端」渠道，其他渠道无数据（不按 0 计）"

    if f.category:
        clauses.append("category = ?")
        params.append(f.category)
        applied["category"] = f.category

    if f.sku:
        clauses.append("sku_code = ?")
        params.append(f.sku)
        applied["sku"] = f.sku

    if f.pieces is not None:
        clauses.append("pieces_per_box = ?")
        params.append(f.pieces)
        applied["pieces"] = f"{f.pieces} 粒装"

    if f.ka:
        if side == "pos":
            clauses.append("ka_system = ?")
            params.append(f.ka)
            applied["ka"] = f.ka
        else:
            ignored["ka"] = (
                "ERP 侧 KA 映射未确认（dim_customer.ka_mapping_status=PENDING、"
                "dim_ka.erp_customer_id 为空），不猜测映射，KA 筛选仅作用于 POS"
            )

    if f.customer:
        if side == "erp":
            clauses.append("customer_id = ?")
            params.append(f.customer)
            applied["customer"] = f.customer
        else:
            ignored["customer"] = "POS 事实表无客户字段，不跨系统推断，客户筛选仅作用于 ERP"

    return " AND ".join(clauses), params, {"applied": applied, "ignored": ignored}


def sql(sql_template: str, f: Filters, side: str) -> tuple[str, list, dict]:
    """把 ``{where}`` 占位替换成参数化条件。"""
    clause, params, applicability = where(f, side)
    return sql_template.replace("{where}", clause), params, applicability
