"""问题理解：确定性规则解析（战区内 / 年份 / 月份 / 客户 / 意图）。

为什么不把这一层交给 LLM
------------------------
1. 「战区B」不能变成「战区A」，「10 月」不能变成「1–10 月」——这类关键词错了，
   后面所有数字都错。规则可复算、可写单测、可在面试现场逐条对照。
2. LLM 仍然参与：当配置了模型时，``understand_question`` 会用 LLM 补全
   「意图分类 + 关注维度」，但 **实体（战区/年份/月份/客户）以规则为准**，
   模型给不一致的值会被丢弃并记一条 ``intent_llm_overridden``。
3. 没有模型时整条链路照常工作，只是 ``understood_by`` 为 ``rules``。

数据边界：本模块不连库、不算数，只做文本 → 结构的映射。
客户名清单来自 ``dim_customer``（首次使用时读取并缓存），不硬编码猜测。
"""
from __future__ import annotations

import re
import sqlite3

from server import config

# 五大战区（与 BI 页面同源，直接引用 config，避免两处名单不一致）
REGIONS = tuple(config.IN_SCOPE_REGIONS)

# 明确不在本 BI 范围内的战区：识别到要如实说明，而不是静默当成全国
OUT_OF_SCOPE_REGIONS = ("战区F", "战区G")

# 市场部（ERP ``dept_short`` 的实际取值，15 个；可从库中核对）
DEPARTMENTS = tuple(f"市场部{i:03d}" for i in (1, 4, 6, 9, 11, 15, 16, 18, 20, 22, 25, 27, 32, 34, 36))

# 客户类型 / 渠道关键词 → 供计划层挑选工具
SEGMENT_WORDS = {
    "clubz": ("clubz", "会员店", "会员客户", "仓储会员"),
    "nka": ("nka", "重点客户", "全国连锁"),
    "lka": ("lka", "地方连锁", "区域商超", "商超"),
    "wholesale": ("批发", "档口", "经销", "分销", "进销存"),
}

QUARTERS = {
    "q1": (1, 2, 3), "一季度": (1, 2, 3), "第一季度": (1, 2, 3),
    "q2": (4, 5, 6), "二季度": (4, 5, 6), "第二季度": (4, 5, 6),
    "q3": (7, 8, 9), "三季度": (7, 8, 9), "第三季度": (7, 8, 9),
    "q4": (10, 11, 12), "四季度": (10, 11, 12), "第四季度": (10, 11, 12),
}

CUSTOMER_CACHE: list[str] | None = None


def _norm(text: str) -> str:
    """统一全角/大小写，便于关键词匹配。"""
    table = str.maketrans("０１２３４５６７８９Ｑ", "0123456789Q")
    return (text or "").translate(table).lower()


def customer_names(conn: sqlite3.Connection | None = None) -> list[str]:
    """客户名清单（ETL ``dim_customer`` + 演示补充库的 4 家会员店）。

    只取一次并缓存；拿不到库时返回空列表，客户识别降级为「不识别」，
    不会因为读不到维度表而让整条链路失败。
    """
    global CUSTOMER_CACHE
    if CUSTOMER_CACHE is not None:
        return CUSTOMER_CACHE
    names: list[str] = []
    own = conn is None
    try:
        if own:
            conn = sqlite3.connect(config.db_uri(), uri=True, timeout=10)
            # ext 库要显式 ATTACH 才能读；缺失就跳过（未生成演示补充数据时正常）
            try:
                ext = config.ROOT / "data" / "demo_ext_v1.db"
                if ext.exists():
                    conn.execute(f"ATTACH DATABASE 'file:{ext}?mode=ro' AS ext")
            except sqlite3.Error:
                pass
        names += [r[0] for r in conn.execute(
            "SELECT customer_name FROM dim_customer WHERE customer_name <> 'UNKNOWN'")]
        try:
            names += [r[0] for r in conn.execute(
                "SELECT customer_name FROM ext.dim_customer_ext")]
        except sqlite3.Error:
            pass                      # 没挂 ext 库（未生成补充数据）时忽略
    except sqlite3.Error:
        names = []
    finally:
        if own and conn is not None:
            conn.close()
    # 去重 + 长名优先（「会员客户D」必须优先于「KA客户A」命中）
    CUSTOMER_CACHE = sorted({n for n in names if n}, key=len, reverse=True)
    return CUSTOMER_CACHE


def parse_region(q: str) -> str:
    for r in REGIONS:
        if r in q:
            return r
    return ""


def parse_out_of_scope_region(q: str) -> str:
    for r in OUT_OF_SCOPE_REGIONS:
        if r in q:
            return r
    return ""


def parse_department(q: str) -> str:
    """市场部：要求出现「XX市场部」或裸名字，且不与战区名冲突。"""
    for d in DEPARTMENTS:
        if f"{d}市场部" in q:
            return d
    # 裸部门名（如「河北 10 月」）；「河北」同时也是省份，业务上等价
    for d in DEPARTMENTS:
        if d in q:
            return d
    return ""


def parse_year(q: str) -> int | None:
    m = re.search(r"(20\d{2})\s*年", q)
    if m:
        return int(m.group(1))
    m = re.search(r"\by(\d{2})\b", q) or re.search(r"y(\d{2})", q)
    if m:
        return 2000 + int(m.group(1))
    m = re.search(r"(?<!\d)(\d{2})\s*年", q)
    if m:
        return 2000 + int(m.group(1))
    return None


def parse_months(q: str) -> list[int]:
    """月份解析：优先季度 / 半年，其次「X 月」「X、Y 月」「X-Y 月」。"""
    for key, months in QUARTERS.items():
        if key in q:
            return list(months)
    if "上半年" in q:
        return [1, 2, 3, 4, 5, 6]
    if "下半年" in q:
        return [7, 8, 9, 10, 11, 12]

    # 「8-10 月」区间
    m = re.search(r"(\d{1,2})\s*[-~到至]\s*(\d{1,2})\s*月", q)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if 1 <= a <= 12 and 1 <= b <= 12 and a <= b:
            return list(range(a, b + 1))

    # 「10 月」「10月份」「10、11 月」
    found = [int(x) for x in re.findall(r"(\d{1,2})\s*月", q)]
    found = [m for m in found if 1 <= m <= 12]
    return sorted(set(found))


def parse_customer(q: str, conn: sqlite3.Connection | None = None) -> str:
    """客户名最长匹配；返回客户名（不是 id），由工具层负责解析成 id。

    ``q`` 可能已被 ``_norm`` 转成小写，所以两边都比小写。
    """
    low = (q or "").lower()
    for name in customer_names(conn):
        if name.lower() in low:
            return name
    # 公开 Demo 仅保留虚构会员客户的短名别名。
    for alias, full in (("会员a", "会员客户A"), ("会员b", "会员客户B"),
                        ("会员c", "会员客户C"), ("会员d", "会员客户D")):
        if alias in low:
            return full
    return ""


def parse_segments(q: str) -> list[str]:
    hits = []
    for seg, words in SEGMENT_WORDS.items():
        if any(w in q for w in words):
            hits.append(seg)
    return hits


def classify_cross_module(q: str) -> str:
    """识别「必须跨模块才能回答」的问题（P9）。

    顺序：先看更具体的组合，再退回单模块。
    这里只判断「要不要跨」，不判断「能不能答」——能力边界由工具决定。
    """
    low = _norm(q)
    has_sfa = any(w in low for w in ("拜访", "陈列", "sfa", "铺货"))
    has_pos = any(w in low for w in ("pos", "动销", "终端销售", "sell-out", "sellout", "零售"))
    has_ka = any(w in low for w in ("ka", "商超", "重点客户"))
    has_wh = any(w in low for w in ("批发", "档口", "经销", "二批"))
    has_erp = any(w in low for w in ("出货", "发货", "sell-in", "sellin"))
    has_profit = any(w in low for w in ("毛利", "利润", "利润率", "赚"))

    if has_sfa and (has_pos or "门店" in low):
        return "sfa_vs_pos"
    if has_erp and (has_pos or "动销" in low):
        return "erp_vs_pos"
    if has_wh and (has_ka or "商超" in low or "终端" in low):
        return "ka_vs_wholesale"
    if has_profit and any(w in low for w in ("战区", "客户", "哪个", "谁", "结构", "拆分")):
        return "profit_mix"
    return ""


def classify_intent(q: str) -> dict:
    """意图分类：返回 type / focus / wants_cause / cross。

    分类顺序刻意从「具体看板」到「综合」——问句里出现 POS/拜访/批发时，
    用户要的是那个模块的证据，而不是又一份预算达成表。
    """
    wants_cause = any(w in q for w in ("原因", "为什么", "为何", "怎么回", "问题出", "分析"))
    focus: list[str] = []
    for word, key in (("战区", "region"), ("大区", "region"), ("市场部", "department"),
                      ("客户", "customer"), ("门店", "store"), ("品类", "category"),
                      ("sku", "sku"), ("产地", "origin"), ("渠道", "channel")):
        if word in q:
            focus.append(key)

    growth_match = re.search(r"(?:增加|增长|提升|上调)\s*(\d+(?:\.\d+)?)\s*%", q)
    pieces_match = re.search(r"(\d{1,2})\s*粒装", q)
    if ("预算" in q and any(w in q for w in ("pos", "动销", "终端"))
            and any(w in q for w in ("相加", "对比", "达成", "口径"))):
        kind = "attainment_review"
    elif (any(w in q for w in ("价格明显高", "价格偏高", "价格异常", "高于同类门店",
                                "比同类门店贵", "价格竞争力", "价格高"))
            and any(w in q for w in ("门店", "sku", "同款", "销量", "ka"))):
        kind = "price_anomaly"
    elif ("铺货" in q and "pos" in q and any(w in q for w in ("没有", "无", "缺少"))):
        kind = "growth_opportunity"
    elif ("高增长" in q and "sku" in q
            and any(w in q for w in ("铺货", "上架", "门店", "机会"))):
        kind = "growth_opportunity"
    elif growth_match and any(w in q for w in ("需求", "原料", "包材", "sku")):
        kind = "supply_chain_growth"
    elif (any(w in q for w in ("可能不足", "短缺风险", "会影响多少生产", "哪些原料",
                                "哪些水果原料", "生产风险"))
          or (any(w in q for w in ("不足", "短缺", "缺料"))
              and any(w in q for w in ("影响生产", "影响多少生产", "受缺料", "sku",
                                        "在途", "延期", "生产")))):
        kind = "supply_chain_shortage"
    elif any(w in q for w in ("包材", "采购预测", "采购多少", "缺料")):
        kind = "supply_chain_forecast"
    elif any(w in q for w in ("拜访", "陈列", "sfa", "执行率", "铺货", "门店画像")):
        kind = "sfa_review"
    elif any(w in q for w in ("pos", "终端", "动销", "零售", "sell-out", "sellout")):
        kind = "pos_review"
    elif any(w in q for w in ("批发", "档口", "库存", "进销存", "经销商")):
        kind = "wholesale_review"
    elif any(w in q for w in ("客户", "top", "排名", "贡献")) and \
            any(w in q for w in ("未达成", "缺口", "差", "掉", "落后")):
        kind = "customer_focus"
    elif any(w in q for w in ("达成", "预算", "完成率", "缺口", "差额", "未达标",
                              "超", "完成", "进度")):
        kind = "attainment_review"
    elif any(w in q for w in ("客户", "top", "排名")) or parse_customer(q):
        kind = "customer_focus"
    elif any(w in q for w in ("概况", "整体", "怎么样", "经营情况", "总览")):
        kind = "overview"
    else:
        kind = "attainment_review"      # 默认按最常用的经营问题处理

    return {"type": kind, "focus": focus, "wants_cause": wants_cause,
            "segments": parse_segments(q), "cross": classify_cross_module(q),
            "growth_pct": float(growth_match.group(1)) if growth_match else None,
            "category": parse_category(q),
            "pieces_per_box": int(pieces_match.group(1)) if pieces_match else None}


# 「明确说了看全国」——用来区分「用户要看全国」和「用户什么都没说」。
# 后者应当先追问范围，而不是默默把全国累计当答案（那是最容易被误读的一种「硬答」）。
NATIONAL_WORDS = ("全国", "全公司", "全盘", "大盘", "五大战区", "所有战区", "整体经营")


def parse_national(q: str) -> bool:
    """问题里是否明确指向全国口径。``q`` 传入未归一化文本也可。"""
    low = _norm(q)
    return any(w in low for w in NATIONAL_WORDS)


# ---------------------------------------------------------------- 业务维度（P8）
# 取值全部来自事实表实际存在的值，不做「按常识猜」的枚举。
CHANNELS = ("商超/终端", "电商", "批发")
CATEGORIES = ("品类A", "品类B", "品类C")
POS_KA_SYSTEMS = tuple(
    f"KA系统{i:03d}" for i in (26, 40, 46, 47, 69, 71, 74, 75, 78, 83, 87, 97, 98, 103)
)


def parse_channel(q: str) -> str:
    """渠道：只认事实表里的三个取值。

    刻意**不**用「门店 / 终端」单独判定 —— 「终端动销」「门店排名」说的是分析对象，
    不是渠道筛选；把它们当成 channel 会让 POS 页的每个问题都被加上
    ``channel_type='商超/终端'``，看起来对、实际多了一层没人要求的过滤。
    """
    low = _norm(q)
    if "电商" in low or "线上渠道" in low or "线上销售" in low:
        return "电商"
    if "批发" in low:
        return "批发"
    if "商超" in low:
        return "商超/终端"
    if "渠道" in low and ("终端" in low or "线下" in low):
        return "商超/终端"
    return ""


def parse_category(q: str) -> str:
    low = _norm(q)
    for c in CATEGORIES:
        if c in low:
            return c
    return ""


def parse_ka_system(q: str) -> str:
    """KA 系统（仅 POS/SFA 有该字段；ERP 侧无 KA 维度，映射 PENDING）。"""
    low = _norm(q)
    hit = [k for k in POS_KA_SYSTEMS if k.lower() in low]
    if not hit:
        return ""
    # 最长优先：「华润万家」不能被「华润」这类短词抢先（当前清单里已无短词，仍保留规则）
    return sorted(hit, key=len, reverse=True)[0]


def _ctx_str(ctx: dict, key: str) -> str:
    v = ctx.get(key)
    return str(v).strip() if isinstance(v, (str, int, float)) and str(v).strip() else ""


def understand(question: str, context: dict | None = None,
               carry: dict | None = None) -> dict:
    """规则解析主入口。返回可直接写入 state 的字段。

    优先级（四条，全部可单测）：
    1. **问题优先**：问题里明确说出的条件覆盖其它来源；
    2. **明确全国即清空**：说「全国」时不再继承页面/上一轮的战区、市场部、客户，
       否则用户看到的是全国，AI 答的却是页面上选的那个战区；
    3. **上一轮次之**：``carry`` 是连续追问时从历史里回读的实体（见 followup.py），
       比页面上下文更接近用户本意（那是他自己刚说过的话）；
    4. **页面只作默认**：页面上下文只补「问题没说的」维度，不做任何强制过滤。
    """
    q = _norm(question)
    ctx = context or {}
    carried = carry or {}
    national = parse_national(q)

    def pick(key: str, parsed_value):
        """三来源合并：本轮 > 上一轮 > 页面。"""
        if parsed_value:
            return parsed_value
        if national:
            return "" if key not in ("year",) else parsed_value
        value = carried.get(key)
        if value not in (None, "", [], 0):
            return value
        return None      # 交给下面按类型取默认

    region = pick("region", parse_region(q))
    if region is None:
        region = "" if national else _ctx_str(ctx, "region")
    department = pick("department", parse_department(q))
    if department is None:
        department = "" if national else _ctx_str(ctx, "department")
    # 市场部自带战区归属时，把战区补上（河北 → 战区B），避免上下口径打架。
    # 归属以事实表为准（山东属战区C、湖北属战区E，与直觉不同，所以不能硬编码）。
    if department and not region:
        region = department_region(department)
    customer = pick("customer", parse_customer(q))
    if customer is None:
        customer = "" if national else _ctx_str(ctx, "customer")
    year = parse_year(q) or (carried.get("year") if not national else 0) \
        or int(ctx.get("year") or 0) or 0

    # 月份：问题里说了就用问题的；说了「累计/全年」视为清空页面月份；
    # 追问且本轮没提月份时，用上一轮说过的月份（比页面默认更贴近用户本意）
    q_months = parse_months(q)
    if q_months:
        months = q_months
    elif any(w in q for w in ("累计", "全年", "年初至今", "整年")):
        months = []
    elif not national and carried.get("month"):
        months = [int(m) for m in carried["month"]]
    else:
        months = [int(m) for m in (ctx.get("months") or [])]

    intent = classify_intent(q)
    out_of_scope = parse_out_of_scope_region(q)

    # 维度：问题优先，其次上一轮，最后页面默认
    def dim(key: str, parsed_value):
        if parsed_value:
            return parsed_value
        if national:
            return _ctx_str(ctx, key)
        return carried.get(key) or _ctx_str(ctx, key)

    channel = dim("channel", parse_channel(q))
    category = dim("category", parse_category(q))
    ka_system = dim("ka_system", parse_ka_system(q))
    # 门店 / SKU 名称过长且易误匹配，不从问题文本里猜，只在上一轮/页面里继承
    store = dim("store", "")
    sku = dim("sku", "")

    segments = intent.get("segments") or []
    segment = segments[0] if len(segments) == 1 else (
        (carried.get("segment") or _ctx_str(ctx, "segment")) if not segments else "")

    return {
        "region": region,
        "department": department,
        "customer": customer,
        "year": year,
        "month": months,
        "out_of_scope_region": out_of_scope,
        "explicit_national": national,
        "analysis_intent": intent,
        # ---- P8：页面上下文
        "page_id": _ctx_str(ctx, "page_id"),
        "page_name": _ctx_str(ctx, "page_name"),
        "module": _ctx_str(ctx, "module"),
        "channel": channel,
        "category": category,
        "ka_system": ka_system,
        "store": store,
        "sku": sku,
        "segment": segment,
        "page_filters": dict(ctx.get("filters") or {}),
    }


# 市场部 → 所属战区。**以 ERP 事实表实际取值为准**（见 _load_department_region），
# 下面这份只是读不到库时的兜底：注意山东属战区C、湖北属战区E，与「按地理常识猜」不同，
# 所以这张表不能凭印象写，必须从 fact_erp_shipment 核对。
DEPARTMENT_REGION_FALLBACK = {
    "上海": "战区A", "江苏": "战区A", "浙江": "战区A",
    "北京": "战区B", "天津": "战区B", "河北": "战区B",
    "山东": "战区C", "河南": "战区C",
    "成都": "战区D", "重庆": "战区D", "贵州": "战区D", "云南": "战区D",
    "陕西": "战区E", "新疆": "战区E", "湖北": "战区E",
}
_DEPT_REGION_CACHE: dict[str, str] | None = None


def _load_department_region() -> dict[str, str]:
    """从事实表读「市场部 → 战区」映射（只读、缓存），失败时用兜底表。"""
    global _DEPT_REGION_CACHE
    if _DEPT_REGION_CACHE is not None:
        return _DEPT_REGION_CACHE
    mapping: dict[str, str] = {}
    try:
        conn = sqlite3.connect(config.db_uri(), uri=True, timeout=10)
        try:
            for dept, region in conn.execute(
                    "SELECT DISTINCT dept_short, region_name_std FROM fact_erp_shipment "
                    "WHERE region_scope_status = 'IN_SCOPE'"):
                if dept and region:
                    mapping[dept] = region
        finally:
            conn.close()
    except sqlite3.Error:
        mapping = {}
    if not mapping:
        mapping = dict(DEPARTMENT_REGION_FALLBACK)
    _DEPT_REGION_CACHE = mapping
    return mapping


def department_region(department: str) -> str:
    return _load_department_region().get(department, "")
