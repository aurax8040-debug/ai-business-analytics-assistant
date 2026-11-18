"""综合经营分析 V2 —— 战区 → 市场部 → 客户类型 交叉分析。

与 V1（``analysis_queries``，六维度金额分析）并存互不影响：
V1 只回答「按什么拆解、金额多少」，本模块回答「预算 vs 实际 TE、差多少」。

口径（全部只读，不改任何原始数据）：
- 实际销量：``fact_erp_shipment.shipment_te``，``year`` 年、``region_scope_status='IN_SCOPE'``。
- 当期预算：``budget_te_v1.fact_te_budget_monthly.budget_te``，整数**毫米 TE**，
  读出后统一 ÷1000 转 TE；``in_observation='N'`` 的月份表示预算已下达但业务未发生。
- 达成率 = 当期实际 TE ÷ 当期预算 TE；预算为空返回 ``None``（前端显示「—」）。
- 同比 = (本期实际 ÷ 去年同期实际 − 1) × 100；去年同期为空返回 ``None``。
- 客户类型按 ERP 字段互斥分四类展示（见 ``SEGMENTS``），合计恒等于当前范围，不需要再去重。
  兜底桶 Others 当前恒空，不铺空行骨架（见 ``SEGMENT_DISPLAY_KEYS``）。
- 批发按档口（``stall_code``）归属战区/市场部；客户主体汇总时按客户去重，
  档口是事实行的归属列而不是可加总的第二层，因此不会重复计算。
"""
from __future__ import annotations

import csv
import io
import sqlite3
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs

from . import config

BUDGET_DB = config.ROOT / "data" / "budget_te_v1.db"
# 演示补充数据集（tools/build_demo_ext.py 生成）：产地、ClubZ 会员店、2025 预算、
# 客户集团映射。独立成库、只读挂载，ERP 原始库一个字节都不动。
EXT_DB = config.ROOT / "data" / "demo_ext_v1.db"

REGIONS = config.IN_SCOPE_REGIONS
MONTHS_ALL = tuple(range(1, 13))
# 各年的实际数据截止月：2026 只到 10 月（11–12 月无出货），2025 是完整 12 个月。
# 2025 仍保留在这里，但只作为「同比基期」在内部取数，不再作为可切换年份。
CAP_BY_YEAR = {2026: config.COMPARABLE_LAST_MONTH, 2025: 12}
OBS_LAST_MONTH = config.COMPARABLE_LAST_MONTH      # 兼容旧引用：2026 年的截止月

# 本页只展示 Y26 一个年度。2025 的预算是按实际反推的推算值，达成率必然挤在
# 94%–104%，放出来会让人误读成「2025 全面达标」，所以从筛选入口撤掉；
# 但同比仍需要 2025 同期**实际**出货，那条取数路径保留（见 _fact）。
SUPPORTED_YEARS = (2026,)

# 客户类型五分类：互斥四条规则 + 兜底。
# 依据只有 ERP 字段（customer_type / channel_type），不按客户名称猜。
SEGMENTS = (
    {"key": "clubz", "label": "ClubZ",
     "rule": "customer_type = ClubZ；仓储会员业态（会员客户 A–D）"},
    {"key": "nka", "label": "NKA", "rule": "customer_type = NKA"},
    {"key": "lka", "label": "LKA", "rule": "customer_type = Others 且 channel_type = 商超/终端"},
    {"key": "wholesale", "label": "批发客户", "rule": "channel_type = 批发"},
    {"key": "others", "label": "Others", "rule": "其余客户（当前为空）"},
)
SEGMENT_LABEL = {s["key"]: s["label"] for s in SEGMENTS}
SEGMENT_KEYS = tuple(s["key"] for s in SEGMENTS)

# 「Others」是兜底桶（四条规则都没命中的客户）。当前口径下它恒为空，
# 页面不再给 15 个市场部各铺一条全「—」的空行 —— 那是纯噪音，还会让读者
# 以为「有一类客户没数据」。注意这里只去掉**展示骨架**：
# 若将来真有客户落进兜底桶，事实聚合（fact/bud 循环）会自己建出该行，
# 不会被静默丢掉，全国合计仍等于各类之和。
SEGMENT_HIDDEN_FROM_SKELETON = ("others",)
SEGMENT_DISPLAY_KEYS = tuple(k for k in SEGMENT_KEYS
                             if k not in SEGMENT_HIDDEN_FROM_SKELETON)

# 两个事实源列名一致，共用同一条 CASE，避免预算与实际分叉
SEGMENT_SQL = """CASE
    WHEN customer_type = 'ClubZ' THEN 'clubz'
    WHEN channel_type = '批发' THEN 'wholesale'
    WHEN customer_type = 'NKA' THEN 'nka'
    WHEN channel_type = '商超/终端' THEN 'lka'
    ELSE 'others' END"""

SORTABLE = {
    "name": "name", "te": "te", "budget": "budget_te", "attainment": "attainment_pct",
    "yoy": "yoy_pct", "last_te": "last_te",
}
MAX_PAGE_SIZE = 500
DEFAULT_PAGE_SIZE = 200

# 产地取值（对应 ext.dim_sku_origin.origin）。产地挂在 SKU 上，不拆事实行，
# 所以筛产地只会减少实际 TE，不会改变任何一个 SKU 的数值。
ORIGINS = ("产区A", "产区B", "产区C", "产区D")
ORIGIN_NOTE = ("产地按 SKU 标注（一个 SKU 一个主产地）。预算只下达到"
               "「客户×市场部×月」，没有 SKU 维度，因此筛产地时预算按该范围内"
               "「该产地实际 TE 占比」等比分摊，属于估算口径，页脚已标注。")


# --------------------------------------------------------------------- 参数
class Params:
    """轻量筛选解析：只接受白名单字段，未知值一律忽略并回落默认。"""

    def __init__(self, qs: str):
        q = parse_qs(qs or "", keep_blank_values=True)
        one = lambda k, d="": (q.get(k) or [d])[0].strip()  # noqa: E731

        self.year = int(one("year", "2026") or 2026)
        # 只有 2026 在本页可选；旧书签带 year=2025 一律归一到 2026，
        # 并在 meta.year_note 里说明，而不是静默返回 2025 的数据。
        self.year_note = ""
        if self.year not in SUPPORTED_YEARS:
            self.year_note = (f"{self.year} 年不在本页展示范围（仅 Y26），"
                              f"已按 2026 年取数")
            self.year = 2026

        raw_months = [m.strip() for m in (q.get("months") or [""])[0].split(",") if m.strip()]
        if raw_months and "all" not in raw_months:
            months = sorted({int(m) for m in raw_months if m.isdigit() and 1 <= int(m) <= 12})
        else:
            months = []
        self.months = tuple(months)
        self.month_scope = tuple(months) if months else MONTHS_ALL

        self.region = one("region")
        if self.region and self.region not in REGIONS:
            self.region = ""
        self.dept = one("dept")
        self.category = one("category")
        if self.category not in ("品类A", "品类B", "品类C"):
            self.category = ""
        self.origin = one("origin")
        if self.origin not in ORIGINS:
            self.origin = ""
        self.segment = one("segment")
        if self.segment and self.segment not in SEGMENT_KEYS:
            self.segment = ""
        self.q = one("q")[:40]

        self.sort = one("sort", "te")
        if self.sort not in SORTABLE:
            self.sort = "te"
        self.order = "asc" if one("order", "desc").lower() == "asc" else "desc"

        try:
            self.page = max(1, int(one("page", "1")))
        except ValueError:
            self.page = 1
        try:
            self.size = min(MAX_PAGE_SIZE, max(20, int(one("size", str(DEFAULT_PAGE_SIZE)))))
        except ValueError:
            self.size = DEFAULT_PAGE_SIZE

    def as_dict(self) -> dict:
        return {
            "year": self.year, "months": list(self.months), "region": self.region,
            "dept": self.dept, "category": self.category, "origin": self.origin,
            "segment": self.segment, "q": self.q,
        }


def _where(p: Params, cols: dict) -> tuple[str, list]:
    """事实表与预算表共用一套条件；``cols`` 给出各表列名差异。"""
    sql, args = ["1=1"], []
    if p.region:
        sql.append(f"{cols['region']} = ?")
        args.append(p.region)
    if p.dept:
        sql.append(f"{cols['dept']} = ?")
        args.append(p.dept)
    if p.category and cols.get("category"):
        sql.append(f"{cols['category']} = ?")
        args.append(p.category)
    if p.segment:
        sql.append(f"({SEGMENT_SQL}) = ?")
        args.append(p.segment)
    return " AND ".join(sql), args


def _origin_clause(p: Params) -> tuple[str, list]:
    """产地条件只作用于事实表（预算没有 SKU 维度），无补充库时明确查空。"""
    if not p.origin:
        return "1=1", []
    if not EXT_DB.exists():
        return "0=1", []
    return ("sku_code IN (SELECT sku_code FROM ext.dim_sku_origin WHERE origin = ?)",
            [p.origin])


def _cap(p: Params) -> int:
    """当前年份的实际数据截止月：2026 → 10（11–12 月未发生），2025 → 12。"""
    return CAP_BY_YEAR.get(p.year, OBS_LAST_MONTH)


def _observed(months: tuple[int, ...], year: int | None = None) -> tuple[int, ...]:
    """把月份裁到数据截止月：2026 年 11–12 月无实际出货，参与统计会制造假同比。

    空入参 = 未选月份（累计），取 1–截止月；
    显式选了截止月之后的月份则返回空元组，让上层如实返回空值 —— 不能悄悄回落成累计，
    否则「查 2026 年 11 月」会显示成一份看起来正常的 1–10 月数据，是用假数据冒充结果。
    """
    cap = CAP_BY_YEAR.get(year or 2026, OBS_LAST_MONTH)
    if not months:
        return tuple(range(1, cap + 1))
    return tuple(m for m in months if m <= cap)


def _prepare(conn: sqlite3.Connection) -> bool:
    """把预算库与演示补充库挂到连接上，并建两张统一视图。

    ``v_fact``   = ERP 原始出货事实 + ext 里的 ClubZ 事实（``data_source`` 可区分）
    ``v_budget`` = 主预算库 + ext 里的 2025 全年预算 / ClubZ 预算

    视图建在 temp schema 上，主库仍是 ``mode=ro``，不会产生任何写入。
    返回是否成功挂载了补充库（视图总是尽力创建，缺库时退化成纯原始数据）。
    """
    try:
        got = conn.execute(
            "SELECT count(*) FROM temp.sqlite_master WHERE name IN ('v_fact', 'v_budget')"
        ).fetchone()[0]
        if got == 2:
            return EXT_DB.exists()
    except sqlite3.Error:
        pass

    conn.execute(f"ATTACH DATABASE 'file:{BUDGET_DB}?mode=ro' AS bud")
    has_ext = EXT_DB.exists()
    if has_ext:
        conn.execute(f"ATTACH DATABASE 'file:{EXT_DB}?mode=ro' AS ext")

    ext_fact = ("""
        UNION ALL
        SELECT shipment_no, year, month, region_name_std, 'IN_SCOPE' AS region_scope_status,
               dept_short, customer_type, customer_id, customer_short, channel_type,
               category, sku_code, sku_name, '' AS stall_code, '' AS stall_name,
               shipment_te, sales_boxes, sales_amount, data_source
        FROM ext.fact_shipment_ext""" if has_ext else "")

    ext_budget = ("""
        UNION ALL
        SELECT year, month, region_name, dept_short, customer_key, customer_id,
               customer_name, customer_type, channel_type, budget_te,
               in_observation, data_source
        FROM ext.fact_budget_ext""" if has_ext else "")

    conn.execute(f"DROP VIEW IF EXISTS temp.v_fact")
    conn.execute(f"DROP VIEW IF EXISTS temp.v_budget")
    conn.execute(f"""
        CREATE TEMP VIEW v_fact AS
        SELECT shipment_no, year, month, region_name_std, region_scope_status,
               dept_short, customer_type, customer_id, customer_short, channel_type,
               category, sku_code, sku_name, stall_code, stall_name,
               shipment_te, sales_boxes, sales_amount, data_source
        FROM main.fact_erp_shipment{ext_fact}""")
    conn.execute(f"""
        CREATE TEMP VIEW v_budget AS
        SELECT year, month, region_name, dept_short, customer_key, customer_id,
               customer_name, customer_type, channel_type, budget_te,
               in_observation, data_source
        FROM bud.fact_te_budget_monthly{ext_budget}""")
    return has_ext


def _has_ext(conn: sqlite3.Connection) -> bool:
    try:
        return conn.execute(
            "SELECT count(*) FROM ext.sqlite_master WHERE name = 'dim_sku_origin'"
        ).fetchone()[0] == 1
    except sqlite3.Error:
        return False


def _month_clause(months: tuple[int, ...], col: str = "month") -> tuple[str, list]:
    if not months:
        # 调用方（_fact/_budget/detail_rows）只会传入 _observed() 的结果，
        # 走到这里说明所选月份全部落在观测范围外：期间未发生业务，命中零行。
        return "0=1", []
    marks = ",".join("?" for _ in months)
    return f"{col} IN ({marks})", list(months)


# --------------------------------------------------------------------- 取数
FACT_COLS = {
    "region": "region_name_std", "dept": "dept_short", "category": "category",
}


def _fact(conn: sqlite3.Connection, p: Params,
          ignore_origin: bool = False) -> dict[tuple, float]:
    """(战区, 市场部, 客户类型) → 实际 TE；按年取，同比另取上年。

    同比必须「同期」：本年与上年都用同一批月份，且不超过数据截止月，
    否则会拿「2025 全年 12 个月」比「2026 年 1–10 月」，同比天然失真。
    ``ignore_origin=True`` 用于给产地分摊预算取分母（该范围内的全产地实际）。
    """
    where, args = _where(p, FACT_COLS)
    owhere, oargs = ("1=1", []) if ignore_origin else _origin_clause(p)
    mwhere, margs = _month_clause(_observed(p.month_scope, p.year))
    out: dict[tuple, float] = {}
    for year in (p.year, p.year - 1):
        sql = (f"SELECT region_name_std AS r, dept_short AS d, {SEGMENT_SQL} AS seg, "
               f"SUM(shipment_te) AS te FROM v_fact "
               f"WHERE year = ? AND region_scope_status = 'IN_SCOPE' "
               f"AND {where} AND {mwhere} AND {owhere} GROUP BY 1, 2, 3")
        for r, d, seg, te in conn.execute(sql, [year, *args, *margs, *oargs]):
            out[(year, r, d, seg)] = float(te or 0)
    return out


def _budget(conn: sqlite3.Connection, p: Params,
            fact_all: dict[tuple, float] | None = None,
            fact_scoped: dict[tuple, float] | None = None) -> dict[tuple, float]:
    """(战区, 市场部, 客户类型) → 预算 TE（预算库缺省时返回空，不补 0）。

    预算只下达到「客户×市场部×月」，没有 SKU 维度。因此筛了产地之后，
    预算按该节点「所选产地实际 TE ÷ 该节点全部产地实际 TE」等比分摊 ——
    这是估算口径，``meta.budget_allocation`` 会标成 ``origin_prorated``，
    前端页脚也会写明，不冒充分下达值。
    """
    if not BUDGET_DB.exists():
        return {}
    where, args = _where(p, {"region": "region_name", "dept": "dept_short",
                             "category": None})
    mwhere, margs = _month_clause(_observed(p.month_scope, p.year))
    out: dict[tuple, float] = {}
    sql = (f"SELECT region_name, dept_short, {SEGMENT_SQL}, SUM(budget_te) / 1000.0 "
           f"FROM v_budget WHERE year = ? AND {where} AND {mwhere} "
           f"GROUP BY 1, 2, 3")
    for r, d, seg, te in conn.execute(sql, [p.year, *args, *margs]):
        out[(r, d, seg)] = float(te or 0)

    if p.origin and fact_all is not None and fact_scoped is not None:
        for key in list(out):
            base = fact_all.get((p.year, *key), 0)
            picked = fact_scoped.get((p.year, *key), 0)
            if not base or not picked:
                # 该节点在这个产地既无实际、也无从分摊 —— 预算按缺失处理，不填 0 伪装
                out.pop(key)
            else:
                out[key] = out[key] * picked / base
    return out


# --------------------------------------------------------------------- 组装
def _pct(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0:
        return None
    return round(num / den * 100, 4)


def _yoy(cur: float | None, base: float | None) -> float | None:
    if cur is None or base is None or base == 0:
        return None
    return round((cur / base - 1) * 100, 4)


def _node(name: str, level: str, key: str) -> dict:
    return {"name": name, "level": level, "key": key,
            "te": None, "budget_te": None, "last_te": None,
            "attainment_pct": None, "yoy_pct": None, "gap_te": None,
            "budget_missing": False, "actual_missing": False}


def _finish(n: dict) -> dict:
    te, bud, last = n["te"], n["budget_te"], n["last_te"]
    n["attainment_pct"] = _pct(te, bud)
    n["yoy_pct"] = _yoy(te, last)
    n["gap_te"] = None if (te is None or bud is None) else round(te - bud, 2)
    n["budget_missing"] = bud is None
    n["actual_missing"] = te is None
    if te is not None:
        n["te"] = round(te, 2)
    if last is not None:
        n["last_te"] = round(last, 2)
    if bud is not None:
        n["budget_te"] = round(bud, 2)
    return n


def build(conn: sqlite3.Connection, p: Params) -> dict:
    has_ext = _prepare(conn)
    fact = _fact(conn, p)
    fact_all = _fact(conn, p, ignore_origin=True) if p.origin else fact
    bud = _budget(conn, p, fact_all, fact)

    # 已观测月份：2026 年 11–12 月无出货，分子分母都排除，避免达成率被稀释
    obs = list(_observed(p.month_scope, p.year))

    tree: dict[str, dict] = {}
    for name in REGIONS:
        if p.region and name != p.region:
            continue
        tree[name] = _node(name, "region", f"r:{name}")
        tree[name]["children"] = {}

    for (year, r, d, seg), te in fact.items():
        if r not in tree or year != p.year:
            continue
        dept = tree[r]["children"].setdefault(
            d or "（未归属）", _node(d or "（未归属）", "dept", f"r:{r}|d:{d}"))
        dept.setdefault("children", {})
        node = dept["children"].setdefault(seg, _node(SEGMENT_LABEL.get(seg, seg),
                                                      "segment", f"r:{r}|d:{d}|s:{seg}"))
        node["te"] = (node["te"] or 0) + te
        dept["te"] = (dept["te"] or 0) + te
        tree[r]["te"] = (tree[r]["te"] or 0) + te

    for (year, r, d, seg), te in fact.items():
        if r not in tree or year != p.year - 1:
            continue
        dept = tree[r]["children"].get(d or "（未归属）")
        if dept is None:
            continue
        node = dept["children"].get(seg)
        if node is None:
            # 去年同期有、今年无：保留该行，实际显示「—」，同比显示 -100%
            node = dept["children"].setdefault(
                seg, _node(SEGMENT_LABEL.get(seg, seg), "segment", f"r:{r}|d:{d}|s:{seg}"))
        node["last_te"] = (node["last_te"] or 0) + te
        dept["last_te"] = (dept["last_te"] or 0) + te
        tree[r]["last_te"] = (tree[r]["last_te"] or 0) + te

    for (r, d, seg), te in bud.items():
        if r not in tree:
            continue
        dept = tree[r]["children"].setdefault(
            d or "（未归属）", _node(d or "（未归属）", "dept", f"r:{r}|d:{d}"))
        dept.setdefault("children", {})
        node = dept["children"].setdefault(seg, _node(SEGMENT_LABEL.get(seg, seg),
                                                      "segment", f"r:{r}|d:{d}|s:{seg}"))
        node["budget_te"] = (node["budget_te"] or 0) + te
        dept["budget_te"] = (dept["budget_te"] or 0) + te
        tree[r]["budget_te"] = (tree[r]["budget_te"] or 0) + te

    # 客户类型固定骨架：即使某类为 0 也保留行，值用 None（不填 0 伪装业务结果）
    # 筛了某一类就只保留那一类，避免出现其他类的「—」空行干扰阅读；
    # 未筛选时保留四类骨架 —— 兜底桶 Others 当前恒空，不再逐市场部铺空行。
    skeleton = (p.segment,) if p.segment else SEGMENT_DISPLAY_KEYS
    for region in tree.values():
        for dept in region["children"].values():
            for seg in skeleton:
                dept["children"].setdefault(
                    seg, _node(SEGMENT_LABEL[seg], "segment",
                               f"{dept['key']}|s:{seg}"))

    # 树节点也带 depth：前端展开/缩进/样式都靠它，避免两侧各自推断层级
    for region in tree.values():
        region["depth"] = 0
        _finish(region)
        region["children"] = [v for v in region["children"].values()]
        for dept in region["children"]:
            dept["depth"] = 1
            _finish(dept)
            dept["children"] = sorted(
                (_finish(n) for n in dept["children"].values()),
                key=lambda n: SEGMENT_KEYS.index(_seg_of(n)))
            for seg in dept["children"]:
                seg["depth"] = 2
        region["children"].sort(key=lambda n: -(n["te"] or -1))

    regions = sorted(tree.values(), key=lambda n: -(n["te"] or -1))
    # 先累加、后算比率：否则 _finish 会拿着空值算达成率，翻页前的合计永远是「—」
    national = _node("全国合计", "total", "total")
    for src in regions:
        for field in ("te", "budget_te", "last_te"):
            if src[field] is not None:
                national[field] = (national[field] or 0) + src[field]
    national = _finish(national)

    rows = _flatten(regions, p.q)
    total_rows = len(rows)
    start = (p.page - 1) * p.size
    page_rows = rows[start:start + p.size] if p.size < total_rows else rows

    return {
        "rows": page_rows,
        "total_rows": total_rows,
        "page": p.page,
        "page_size": p.size,
        "regions": regions,
        "national": national,
        "segments": [dict(s) for s in SEGMENTS if s["key"] in SEGMENT_DISPLAY_KEYS],
        "months_observed": obs,
        "budget_available": bool(bud),
        "budget_batch": _budget_batch(),
        "demo_ext": _demo_ext(conn, has_ext),
        "notes": _notes(p, has_ext),
    }


def _seg_of(node: dict) -> str:
    return node["key"].split("|s:")[-1]


def _flatten(regions: list[dict], q: str) -> list[dict]:
    """展平为「战区 / 市场部 / 客户类型」三种行，供前端表格逐行渲染与排序。"""
    out: list[dict] = []
    for r in regions:
        out.append({**_row(r, 0), "expanded": True})
        for d in r["children"]:
            out.append({**_row(d, 1), "parent": r["key"], "expanded": True})
            for s in d["children"]:
                out.append({**_row(s, 2), "parent": d["key"]})
    if q:
        # 关键词只用于定位：命中则连同父级一起保留
        hit = [x for x in out if q in x["name"]]
        keep: set[str] = set()
        for x in hit:
            keep.add(x["key"])
            if x.get("parent"):
                keep.add(x["parent"])
                for p in out:
                    if p["key"] == x["parent"] and p.get("parent"):
                        keep.add(p["parent"])
        out = [x for x in out if x["key"] in keep]
    return out


def _row(n: dict, level: int) -> dict:
    return {"key": n["key"], "name": n["name"], "depth": level,
            "te": n["te"], "budget_te": n["budget_te"], "last_te": n["last_te"],
            "attainment_pct": n["attainment_pct"], "yoy_pct": n["yoy_pct"],
            "gap_te": n["gap_te"], "budget_missing": n["budget_missing"],
            "actual_missing": n["actual_missing"]}


def _budget_batch() -> dict:
    if not BUDGET_DB.exists():
        return {"available": False}
    conn = sqlite3.connect(f"file:{BUDGET_DB}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT batch_id, budget_version, data_source, generated_at, year, "
            "unit, unit_scale, seed, notice FROM te_budget_batch LIMIT 1").fetchone()
    except sqlite3.Error:
        row = None
    finally:
        conn.close()
    if not row:
        return {"available": False}
    return {"available": True, "batch_id": row[0], "version": row[1],
            "data_source": row[2], "generated_at": row[3], "year": row[4],
            "unit": row[5], "unit_scale": row[6], "seed": row[7], "notice": row[8],
            "ext_years": []}


def _demo_ext(conn: sqlite3.Connection, has_ext: bool) -> dict:
    """补充数据集的自我说明：前端页脚与验收报告都用这份数字，不手写。"""
    if not has_ext:
        return {"available": False}
    try:
        meta = dict(conn.execute("SELECT key, value FROM ext.meta_demo_ext").fetchall())
    except sqlite3.Error:
        return {"available": False}
    try:
        cz_te = float(meta.get("clubz_te_2026") or 0)
    except ValueError:
        cz_te = 0.0
    origins = dict(conn.execute(
        "SELECT origin, count(*) FROM ext.dim_sku_origin GROUP BY 1 "
        "ORDER BY count(*) DESC").fetchall())
    return {
        "available": True,
        "dataset": "DEMO_EXT",
        "origin_skus": int(meta.get("origin_sku_count") or 0),
        "origins": origins,
        "clubz_customers": int(meta.get("clubz_customer_count") or 0),
        "clubz_te": round(cz_te, 2),
        "customer_groups": conn.execute(
            "SELECT count(DISTINCT group_name) FROM ext.dim_customer_group").fetchone()[0],
        "note": "演示补充数据（DEMO_EXT），与 ERP 原始事实分层存放，可按 data_source 剔除。",
    }


def _notes(p: Params, has_ext: bool = True) -> dict:
    return {
        "caliber": "实际 = ERP 出货 TE；预算 = 年初 TE 预算（MOCK_BUDGET_TE，毫米 TE ÷1000）；"
                   "达成率 = 实际 ÷ 预算；同比 = 本期 ÷ 去年同期 − 1。",
        "segment_rule": "四类互斥展示：ClubZ（customer_type=ClubZ，仓储会员店）"
                        "/ NKA（customer_type=NKA）/ LKA（Others 且商超终端）"
                        "/ 批发客户（channel_type=批发）；"
                        "合计恒等于当前范围，不做二次去重。"
                        "兜底桶 Others（其余客户）当前为空，不再逐市场部铺空行；"
                        "若确有客户落入兜底桶，该行会自动出现而不是被丢掉。",
        "budget_months": f"达成率分母只取已观测月份（{p.year} 年 1–{_cap(p)} 月）；"
                         "2026 年 11–12 月预算已下达但业务未发生，不计入分母。",
        "origin": ORIGIN_NOTE,
        "demo_ext": ("含演示补充数据：产地标注 72 个 SKU、ClubZ 会员店 4 家、"
                     "客户集团映射；原始 ERP 库未做任何写入。"
                     if has_ext else "未检测到演示补充库，仅展示 ERP 原始事实。"),
        "sources": [
            "实际：fact_erp_shipment（data_source=MOCK_DATA，ERP_V3 样例数据）",
            "预算：budget_te_v1.fact_te_budget_monthly（MOCK_BUDGET_TE，仅 2026）",
            "补充：demo_ext_v1（data_source=DEMO_EXT，产地 / ClubZ / 客户集团）",
        ],
        "filters": p.as_dict(),
    }


# --------------------------------------------------------------------- 下钻
def detail_rows(conn: sqlite3.Connection, p: Params, node: str) -> dict:
    """按右侧点击的节点下钻：KA 系统 → 客户 → ERP 明细（批发到档口）。"""
    _prepare(conn)
    where, args = _where(p, FACT_COLS)
    owhere, oargs = _origin_clause(p)
    mwhere, margs = _month_clause(_observed(p.month_scope, p.year))

    parts = node.split("|")
    scope = {}
    for part in parts:
        if part.startswith("r:"):
            scope["region"] = part[2:]
        elif part.startswith("d:"):
            scope["dept"] = part[2:]
        elif part.startswith("s:"):
            scope["segment"] = part[2:]

    extra, extra_args = [], []
    if scope.get("region"):
        extra.append("region_name_std = ?")
        extra_args.append(scope["region"])
    if scope.get("dept"):
        extra.append("dept_short = ?")
        extra_args.append(scope["dept"])
    if scope.get("segment"):
        extra.append(f"({SEGMENT_SQL}) = ?")
        extra_args.append(scope["segment"])

    sql = (f"SELECT customer_id, customer_short, customer_type, channel_type, "
           f"region_name_std, dept_short, stall_code, stall_name, "
           f"SUM(shipment_te) AS te, COUNT(*) AS rows_, MAX(month) AS last_month "
           f"FROM v_fact "
           f"WHERE year = ? AND region_scope_status = 'IN_SCOPE' "
           f"AND {where} AND {mwhere} AND {owhere} AND {' AND '.join(extra) or '1=1'} "
           f"GROUP BY customer_id, customer_short, customer_type, channel_type, "
           f"region_name_std, dept_short, stall_code, stall_name "
           f"ORDER BY te DESC")
    rows = [dict(zip(("customer_id", "customer", "customer_type", "channel_type",
                      "region", "dept", "stall_code", "stall_name", "te",
                      "rows", "last_month"), r))
            for r in conn.execute(sql, [p.year, *args, *margs, *oargs, *extra_args])]

    by_customer: dict[str, dict] = {}
    for r in rows:
        c = by_customer.setdefault(r["customer_id"], {
            "customer_id": r["customer_id"], "customer": r["customer"],
            "customer_type": r["customer_type"], "channel_type": r["channel_type"],
            "te": 0.0, "stalls": [], "regions": set(), "depts": set(),
        })
        c["te"] += r["te"] or 0
        c["regions"].add(r["region"])
        c["depts"].add(r["dept"])
        if r["stall_code"]:
            c["stalls"].append({"stall_code": r["stall_code"], "stall": r["stall_name"],
                                "region": r["region"], "dept": r["dept"],
                                "te": round(r["te"] or 0, 2)})

    customers = []
    for c in by_customer.values():
        c["te"] = round(c["te"], 2)
        c["regions"] = sorted(x for x in c["regions"] if x)
        c["depts"] = sorted(x for x in c["depts"] if x)
        c["stalls"].sort(key=lambda s: -s["te"])
        c["share_pct"] = _pct(c["te"], sum(x["te"] for x in by_customer.values()) or None)
        customers.append(c)
    customers.sort(key=lambda c: -c["te"])

    # KA 系统 / 客户集团：客户先归到集团，再列客户。批发客户一个主体一个集团。
    gmap: dict[str, tuple] = {}
    if _has_ext(conn):
        gmap = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
            "SELECT customer_id, group_name, group_type, group_note "
            "FROM ext.dim_customer_group")}
    for c in customers:
        g = gmap.get(c["customer_id"])
        c["group"] = g[0] if g else c["customer"]
        c["group_type"] = g[1] if g else (c["customer_type"] or "")
        c["group_note"] = g[2] if g else ""
        c["stall_count"] = len(c["stalls"])

    total_te = sum(c["te"] for c in customers) or None
    buckets: dict[str, dict] = {}
    for c in customers:
        g = buckets.setdefault(c["group"], {
            "group": c["group"], "group_type": c["group_type"],
            "group_note": c["group_note"], "te": 0.0, "customers": []})
        g["te"] += c["te"]
        g["customers"].append({k: c[k] for k in (
            "customer_id", "customer", "customer_type", "channel_type",
            "te", "share_pct", "stall_count")} | {"stalls": c["stalls"][:6]})
    groups = []
    for g in buckets.values():
        g["te"] = round(g["te"], 2)
        g["share_pct"] = _pct(g["te"], total_te)
        g["customer_count"] = len(g["customers"])
        groups.append(g)
    groups.sort(key=lambda g: -g["te"])

    return {
        "scope": scope,
        "groups": groups,
        "customers": customers,
        "total_te": round(sum(c["te"] for c in customers), 2),
        "is_wholesale": scope.get("segment") == "wholesale",
        "note": ("批发客户按档口所在地归属战区/市场部；客户主体一行是去重后的合计，"
                 "档口明细只用于展示归属，不与主体相加。"
                 if scope.get("segment") == "wholesale"
                 else "第一层为客户集团（KA 系统）；同一集团下的门店主体合并展示，"
                      "点开看该集团下的客户及其 ERP 明细。"),
    }


# --------------------------------------------------------------------- 出货行
LINE_LIMIT = 120


def customer_lines(conn: sqlite3.Connection, p: Params, node: str,
                   customer_id: str) -> dict:
    """下钻最后一层：某客户在当前筛选范围内的 ERP 出货行（月 × SKU 汇总）。

    这是「KA 系统 → 客户 → ERP 明细」的终点，也是面试时最能说明
    「每个数字都能点回到单据」的一层。行数上限 120，按 TE 降序。
    """
    _prepare(conn)
    where, args = _where(p, FACT_COLS)
    owhere, oargs = _origin_clause(p)
    mwhere, margs = _month_clause(_observed(p.month_scope, p.year))

    extra, extra_args = ["customer_id = ?"], [customer_id]
    for part in node.split("|"):
        if part.startswith("r:"):
            extra.append("region_name_std = ?")
            extra_args.append(part[2:])
        elif part.startswith("d:"):
            extra.append("dept_short = ?")
            extra_args.append(part[2:])
        elif part.startswith("s:"):
            extra.append(f"({SEGMENT_SQL}) = ?")
            extra_args.append(part[2:])

    base = (f"FROM v_fact WHERE year = ? AND region_scope_status = 'IN_SCOPE' "
            f"AND {where} AND {mwhere} AND {owhere} AND {' AND '.join(extra)}")
    params = [p.year, *args, *margs, *oargs, *extra_args]

    name = conn.execute(
        f"SELECT customer_short FROM v_fact WHERE customer_id = ? LIMIT 1",
        (customer_id,)).fetchone()
    total = conn.execute(f"SELECT SUM(shipment_te), SUM(sales_amount), COUNT(*) {base}",
                         params).fetchone()
    rows = conn.execute(
        f"SELECT month, category, sku_name, SUM(sales_boxes), SUM(shipment_te), "
        f"SUM(sales_amount) {base} GROUP BY month, sku_name "
        f"ORDER BY month, SUM(shipment_te) DESC LIMIT {LINE_LIMIT}", params).fetchall()

    by_month: dict[int, float] = {}
    for r in rows:
        by_month[r[0]] = by_month.get(r[0], 0.0) + (r[4] or 0)
    return {
        "customer_id": customer_id,
        "customer": name[0] if name else customer_id,
        "total_te": round(total[0] or 0, 2),
        "total_amount": round(total[1] or 0, 2),
        "line_count": len(rows),
        "source_rows": total[2],
        "months": [{"month": m, "te": round(v, 2)} for m, v in sorted(by_month.items())],
        "lines": [{"month": r[0], "category": r[1], "sku": r[2],
                   "boxes": round(r[3] or 0), "te": round(r[4] or 0, 2),
                   "amount": round(r[5] or 0, 2)} for r in rows],
        "note": f"按「月 × SKU」汇总，最多 {LINE_LIMIT} 行；TE 与金额均来自 ERP 出货事实。",
    }


# --------------------------------------------------------------------- 导出
def to_csv(conn: sqlite3.Connection, p: Params) -> bytes:
    data = build(conn, p)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["层级", "战区", "市场部", "客户类型", "同期销量(TE)", "当期预算(TE)",
                "当期销量(TE)", "当期完成率(%)", "同比(%)", "预算缺口(TE)"])
    for r in data["rows"]:
        depth = r["depth"]
        w.writerow([
            ["战区", "市场部", "客户类型"][depth],
            r["name"] if depth == 0 else "",
            r["name"] if depth == 1 else "",
            r["name"] if depth == 2 else "",
            "" if r["last_te"] is None else f"{r['last_te']:.2f}",
            "" if r["budget_te"] is None else f"{r['budget_te']:.2f}",
            "" if r["te"] is None else f"{r['te']:.2f}",
            "" if r["attainment_pct"] is None else f"{r['attainment_pct']:.2f}",
            "" if r["yoy_pct"] is None else f"{r['yoy_pct']:.2f}",
            "" if r["gap_te"] is None else f"{r['gap_te']:.2f}",
        ])
    n = data["national"]
    w.writerow(["合计", "全国合计", "", "",
                "" if n["last_te"] is None else f"{n['last_te']:.2f}",
                "" if n["budget_te"] is None else f"{n['budget_te']:.2f}",
                "" if n["te"] is None else f"{n['te']:.2f}",
                "" if n["attainment_pct"] is None else f"{n['attainment_pct']:.2f}",
                "" if n["yoy_pct"] is None else f"{n['yoy_pct']:.2f}",
                "" if n["gap_te"] is None else f"{n['gap_te']:.2f}"])
    return buf.getvalue().encode("utf-8-sig")


# --------------------------------------------------------------------- 对外
def options(conn: sqlite3.Connection) -> dict:
    has_ext = _prepare(conn)
    depts = [r[0] for r in conn.execute(
        "SELECT DISTINCT dept_short FROM v_fact "
        "WHERE region_scope_status = 'IN_SCOPE' AND dept_short IS NOT NULL "
        "ORDER BY dept_short")]
    origins = [{"value": "", "label": "全部产地"}]
    if has_ext:
        origins += [{"value": o, "label": o,
                     "note": f"{c} 个 SKU"}
                    for o, c in conn.execute(
                        "SELECT origin, count(*) FROM ext.dim_sku_origin "
                        "GROUP BY 1 ORDER BY count(*) DESC")]
    return {
        "years": [{"value": y, "label": f"{y}（Y{y % 100}）"} for y in SUPPORTED_YEARS],
        "months": [{"value": 0, "label": "累计"}] +
                  [{"value": m, "label": f"{m} 月"} for m in range(1, 13)],
        "regions": [{"value": r, "label": r} for r in REGIONS],
        "depts": [{"value": d, "label": d} for d in depts],
        "categories": [{"value": c, "label": c} for c in ("品类A", "品类B", "品类C")],
        "origins": origins,
        "segments": [{"value": s["key"], "label": s["label"]}
                     for s in SEGMENTS if s["key"] in SEGMENT_DISPLAY_KEYS],
        "defaults": {"year": 2026, "month": 0, "region": "", "dept": "",
                     "category": "", "origin": "", "segment": ""},
    }


def envelope(data: dict, p: Params, extra: dict | None = None) -> dict:
    out = {
        "ok": True,
        "data": data,
        "meta": {
            "page": "business-analysis-v2",
            "title": "战区销售达成跟进 · 预算达成交叉分析",
            "filters": p.as_dict(),
            "year": p.year,
            "years_supported": list(SUPPORTED_YEARS),
            "year_note": p.year_note,
            "month_scope": (f"累计 1–{_cap(p)} 月" if not p.months
                            else "、".join(f"{m} 月" for m in p.months)),
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "amount_sum_forbidden": True,
            "budget_source": "MOCK_BUDGET_TE",
            # 预算只下达到客户×市场部×月；筛产地时按实际占比分摊，口径必须标出来
            "budget_allocation": "origin_prorated" if p.origin else "as_issued",
            "demo_ext": data.get("demo_ext", {"available": False}),
        },
    }
    if extra:
        out["meta"].update(extra)
    return out
