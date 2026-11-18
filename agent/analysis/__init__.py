"""跨模块联合分析器（P9）。

只做「并列 / 对照 / 交集」，不做跨口径的加法与除法。见 ``cross.py`` 顶部说明。
"""
from . import cross  # noqa: F401

__all__ = ["cross"]
