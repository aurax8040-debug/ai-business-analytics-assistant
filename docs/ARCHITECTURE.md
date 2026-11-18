# 架构与可信边界

## 运行链路

1. `server/` 以只读模式加载仓库内的 Mock SQLite 数据。
2. `agent/semantics.py` 与 `agent/intent.py` 解析指标、维度、期间和歧义。
3. `agent/supervisor.py` 决定直接工具查询或进入 Workflow。
4. `agent/tools/` 提供 13 个公开只读业务工具。
5. Checker 与数据治理模块完成复算、质量和权限校验。
6. `agent/report.py` 组织事实、推测、建议和边界；`agent/charts.py` 只消费已验证工具结果。
7. `agent/actions.py` 记录需人工确认的本地 Demo 任务，不连接外部平台。

## 关键设计决策

- 数字由 SQL 和确定性函数计算，LLM 不做业务计算。
- 权限在后端工具执行前校验，页面参数不能提升权限。
- ERP Sell-in、POS Sell-out 与预算分别治理。
- 图表是已验证工具结构化数据的直接投影，不从报告文本二次提取。
- LLM 失败只影响自然语言表达，不影响数据查询、Checker 和模板报告。

## 已知限制

- Demo RBAC 是静态身份映射，不是生产 SSO。
- 数据均为 Mock，不能验证真实业务提升。
- 行动闭环只写本地日志，不执行真实任务。
- 公开版测试聚焦安全和核心链路，不包含私有项目的历史黄金值与原始验收截图。
