"""Agent 工具包。

十三个工具覆盖经营分析、增长机会、价格异常、跨战区经验，以及供应链 V2 的库存、BOM、需求和采购建议。
每个工具都是「一个函数 + 一份 JSON Schema + 一句取数来源」，由 ``registry`` 统一注册。
新增工具只需再加一个模块并在 ``registry._load()`` 里 import，Agent/前端无需改动。
"""
from __future__ import annotations

from . import registry  # noqa: F401  （import 即完成十个工具的注册）

__all__ = ["registry"]
