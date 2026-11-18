"""能力边界：把「平台有什么数据、没有什么数据」写成代码，而不是写在提示词里。

为什么需要这一层
----------------
跨模块分析最容易被诟病的不是算错，而是**用相近指标顶替不存在的指标**：

- 问「库存周转怎么样」，平台只有月末库存，没有周转天数，却答一段批发出库；
- 问「竞品抢了多少份额」，平台没有任何竞品数据，却答一段自己的达成率。

前者是口径错位，后者是编造。两种都会在面试里被一句「你这个数从哪来的」问穿。
所以这里把每个能力边界的「有什么 / 缺什么 / 现在能给什么」三句话固定下来，
由报告统一渲染到「数据限制」段，模型无权改写（提示词可以被绕过，代码常量不行）。

维护约定
--------
``have`` 字段必须能在 ``agent/tools/`` 里找到对应数据源；改了数据源要同步改这里，
``tests/test_21_cross_module.py::CapabilityTruthTest`` 会校验关键 `have` 与工具
``source`` 的一致性，避免文档和数据两头漂。
"""
from __future__ import annotations

# 每个话题：触发词 + 三句话（有什么 / 缺什么 / 现在能给什么）
# 顺序即渲染顺序，重要的放前面。
TOPICS: dict[str, dict] = {
    "supply_chain": {
        "label": "供应链与库存",
        "words": ("库存", "周转", "在途", "库龄", "缺货", "断货", "仓库",
                  "备货", "供应链", "到货", "物流", "库存天数"),
        "have": "全国月末库存（数量与金额，按月）、批发出库与下游采购（可分战区）",
        "missing": "库存周转天数、在途库存、库龄结构、分战区/分门店库存、缺货率",
        "offer": "能算「批发出库 − 下游采购」判断渠道是压货还是消化，"
                 "能看全国月末库存金额的月度变化；周转天数需要日均出库成本，"
                 "本平台没有按天的出库流水，故不计算",
    },
    "competitor": {
        "label": "竞品与市场份额",
        "words": ("竞品", "竞对", "竞争对手", "市场份额", "市占", "竞品价格",
                  "对手", "同行", "份额"),
        "have": "仅本公司口径的 ERP 出货、POS 终端销售、SFA 拜访与批发出库",
        "missing": "竞品销量与价格、市场份额、终端货架占比",
        "offer": "只能用本公司同比/环比与内部结构（KA vs 批发、品类毛利）间接推断，"
                 "任何「份额」类结论都不成立",
    },
    "promotion": {
        "label": "促销与费用投入",
        "words": ("促销", "活动", "档期", "费用", "核销", "投入产出", "roi",
                  "陈列费", "促销费"),
        "have": "SFA 拜访里的陈列位置与形式记录、预算 TE（演示用模拟数据）",
        "missing": "促销费用金额、活动档期与核销明细、单店促销销量增量",
        "offer": "能看陈列执行率与拜访覆盖，但不能计算促销 ROI；"
                 "「这场活动带来了多少增量」需要费用与对照店，本平台两者都没有",
    },
    "forecast": {
        "label": "预测与目标推演",
        "words": ("预测", "预估", "下个月能", "能不能完成", "预计完成",
                  "全年能做", "推算"),
        "have": "历史月度实际、月度预算（MOCK_BUDGET_TE）、同比基数",
        "missing": "在手订单、产能与到货计划、季节性模型",
        "offer": "只能按「已发生月份的实际 + 剩余月份预算」做缺口算术，"
                 "这是假设推演不是预测，报告里会标注为推算",
    },
}

# 渲染顺序
ORDER = ("supply_chain", "competitor", "promotion", "forecast")


def detect(question: str) -> list[str]:
    """返回问题触达的能力边界 key（可能多个，按 ORDER 排序）。

    只做关键词命中，不做语义判断——宁可多提示一句「这块没有数据」，
    也不要在用户问库存周转时答一段出货。
    """
    q = (question or "").lower()
    hit = [k for k in ORDER if any(w in q for w in TOPICS[k]["words"])]
    return hit


def describe(keys: list[str]) -> list[dict]:
    """把 key 展开成结构化条目，供报告「数据限制」段渲染。"""
    out: list[dict] = []
    for k in keys or []:
        t = TOPICS.get(k)
        if not t:
            continue
        out.append({
            "topic": k,
            "label": t["label"],
            "have": t["have"],
            "missing": t["missing"],
            "offer": t["offer"],
            # 一句话版：放在「暂无数据支持」里，读者扫一眼就知道差什么
            "limit": f"{t['label']}：本平台有{t['have']}；"
                     f"没有{t['missing']}。{t['offer']}。",
        })
    return out


def limits(keys: list[str]) -> list[str]:
    """一句话版边界说明列表（可直接进 data_gaps / caveats）。"""
    return [d["limit"] for d in describe(keys)]


def topics() -> list[str]:
    """供测试与文档枚举，避免文档里写的话题和代码不一致。"""
    return list(ORDER)
