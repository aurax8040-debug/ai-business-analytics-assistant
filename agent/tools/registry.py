"""工具注册表：schema 声明 + 参数校验 + 统一执行入口。

每个工具都必须在这里登记，才能被 Agent 调用、被前端展示、被评测集覆盖。
Agent 不会「直接 import 某个函数」——它只看得到 ``spec_list()`` 给出的名字与参数，
这样换模型、换框架（甚至换成 Dify 工具）时，业务实现不用动。
"""
from __future__ import annotations

import sqlite3
import time
import traceback
from typing import Any, Callable

from .. import intent
from ._common import ToolError, fail, open_conn

TOOLS: dict[str, dict] = {}

# 六个工具共用的筛选参数（region / department / year / months 语义完全一致）
_SCOPE_PROPS = {
    "region": {"type": "string", "description": "战区，五选一：战区A/战区B/战区C/战区D/战区E"},
    "department": {"type": "string", "description": "市场部，如 北京/河北/天津/上海"},
    "customer": {"type": "string", "description": "客户名称，如 KA客户A/KA客户C/品类A贸易"},
    "year": {"type": "integer", "description": "年份，当前只支持 2026"},
    "months": {"type": "array", "items": {"type": "integer"},
               "description": "月份列表，如 [10]；不传表示累计 1–10 月"},
    "segment": {"type": "string",
                "description": "客户类型：clubz / nka / lka / wholesale"},
}


def register(name: str, description: str, props: dict, required: list[str],
             fn: Callable, *, source: str, returns: str,
             access_class: str = "DETAIL", public: bool = True) -> None:
    TOOLS[name] = {
        "name": name,
        "description": description,
        "parameters": {"type": "object",
                       "properties": props,
                       "required": required,
                       "additionalProperties": False},
        "fn": fn,
        "source": source,
        "returns": returns,
        "access_class": access_class,
        "public": bool(public),
    }


def spec_list() -> list[dict]:
    """给 LLM 的工具清单（OpenAI function calling 格式）。"""
    return [{"type": "function",
             "function": {"name": t["name"], "description": t["description"],
                          "parameters": t["parameters"]}}
            for t in TOOLS.values() if t.get("public", True)]


def catalog() -> list[dict]:
    """给前端/文档的工具清单（人读版）。"""
    return [{"name": t["name"], "description": t["description"],
             "parameters": t["parameters"]["properties"],
             "required": t["parameters"]["required"],
             "source": t["source"], "returns": t["returns"],
             "access_class": t.get("access_class") or "DETAIL"}
            for t in TOOLS.values() if t.get("public", True)]


def public_count() -> int:
    """返回公开给 Supervisor、Planner 与前端的业务工具数量。"""
    return sum(1 for tool in TOOLS.values() if tool.get("public", True))


def _coerce_args(spec: dict, raw: dict) -> dict:
    """参数校验：类型、白名单、范围。非法参数抛 ToolError，不送到 SQL 里。

    空值（``""`` / ``None`` / ``[]``）先剔除再判「未知参数」：
    计划层会把统一的筛选条件原样透传，某个工具不支持某维度时传进来的是空串，
    那属于「没筛」而不是「乱传参数」。
    """
    schema = spec["parameters"]
    props = schema["properties"]
    given = {k: v for k, v in (raw or {}).items() if v not in (None, "", [], ())}
    unknown = [k for k in given if k not in props]
    if unknown:
        raise ToolError("UnknownArgument",
                        f"{spec['name']} 不接受参数 {unknown}",
                        f"可用参数：{sorted(props)}")

    out: dict[str, Any] = {}
    for key, meta in props.items():
        val = given.get(key)
        if val is None:
            continue
        kind = meta.get("type")
        if kind == "integer":
            try:
                out[key] = int(val)
            except (TypeError, ValueError):
                raise ToolError("InvalidArgument", f"参数 {key} 必须是整数，收到 {val!r}")
        elif kind == "array":
            if isinstance(val, (int, str)):
                val = [val]
            if not isinstance(val, (list, tuple)):
                raise ToolError("InvalidArgument", f"参数 {key} 必须是数组，收到 {val!r}")
            try:
                out[key] = sorted({int(v) for v in val})
            except (TypeError, ValueError):
                raise ToolError("InvalidArgument", f"参数 {key} 的元素必须是整数")
        elif kind == "boolean":
            if isinstance(val, bool):
                out[key] = val
            else:
                out[key] = str(val).strip().lower() in ("1", "true", "yes", "y", "是")
        else:
            out[key] = str(val).strip()

    missing = [k for k in schema["required"] if k not in out]
    if missing:
        raise ToolError("MissingArgument", f"缺少必填参数 {missing}")

    if "region" in out and out["region"] not in intent.REGIONS:
        raise ToolError("InvalidRegion",
                        f"战区「{out['region']}」不在本 BI 范围内",
                        f"可用战区：{'/'.join(intent.REGIONS)}（不含战区F、战区G）")
    for region_key in ("home_region", "compare_region"):
        if region_key in out and out[region_key] not in intent.REGIONS:
            raise ToolError("InvalidRegion", f"{region_key} 战区「{out[region_key]}」不在本 BI 范围内",
                            f"可用战区：{'/'.join(intent.REGIONS)}")
    if "department" in out and out["department"] not in intent.DEPARTMENTS:
        raise ToolError("InvalidDepartment",
                        f"市场部「{out['department']}」不存在",
                        f"可用市场部：{'、'.join(intent.DEPARTMENTS)}")
    if "segment" in out and out["segment"] not in ("clubz", "nka", "lka", "wholesale"):
        raise ToolError("InvalidSegment", f"客户类型 {out['segment']} 不合法")
    if "top_n" in out and not (1 <= out["top_n"] <= 50):
        raise ToolError("InvalidArgument", "top_n 需在 1–50 之间")
    return out


def accepts(name: str, key: str) -> bool:
    """该工具是否声明了这个参数。计划层用它做「按工具裁剪参数」。"""
    spec = TOOLS.get(name)
    return bool(spec) and key in spec["parameters"]["properties"]


def filter_args(name: str, args: dict) -> dict:
    """按工具 schema 裁剪参数。

    为什么需要：计划层手里是一份「统一的当前范围」（region/customer/ka_system/sku…），
    但不是每个工具都认每个维度。直接透传会触发 ``UnknownArgument``，
    让一个本来能答的问题因为「多传了一个 page 上的筛选项」而失败。
    """
    spec = TOOLS.get(name)
    if not spec:
        return {}
    props = spec["parameters"]["properties"]
    return {k: v for k, v in (args or {}).items()
            if k in props and v not in (None, "", [], ())}


def run(name: str, args: dict, conn: sqlite3.Connection | None = None,
        permission_context: dict | None = None) -> dict:
    """执行单个工具。**任何异常都收敛成标准失败结构**，不向外抛。"""
    spec = TOOLS.get(name)
    if spec is None:
        return fail(name, ToolError("UnknownTool", f"未注册的工具 {name}",
                                    f"可用工具：{sorted(TOOLS)}"))
    started = time.perf_counter()
    own = conn is None
    try:
        authorized_args = args or {}
        if permission_context is not None:
            from .. import data_governance
            authorization = data_governance.authorize_tool(
                name, authorized_args, permission_context)
            if not authorization.get("allowed"):
                error = authorization.get("error") or {}
                raise ToolError(error.get("type") or "PermissionDenied",
                                error.get("message") or "无权执行该查询",
                                error.get("detail") or "权限过滤在 SQL 执行前完成")
            authorized_args = authorization.get("args") or {}
        clean = _coerce_args(spec, authorized_args)
        if own:
            conn = open_conn()
        result = spec["fn"](conn, **clean)
    except ToolError as exc:
        result = fail(name, exc, args)
    except sqlite3.Error as exc:
        result = fail(name, ToolError("DatabaseError", f"数据库查询失败：{exc}",
                                      "确认 ETL 数据库与预算库文件存在且可读"), args)
    except Exception as exc:                                  # pragma: no cover
        result = fail(name, ToolError("UnexpectedError", f"{type(exc).__name__}: {exc}",
                                      traceback.format_exc(limit=2)), args)
    finally:
        if own and conn is not None:
            conn.close()
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result


# 注册顺序 = 文档与前端展示顺序；与计划层的默认顺序一致
def _load() -> None:
    from . import (budget_tools, customer_tools, erp_tools, pos_tools,  # noqa: F401
                   cross_region_tools, growth_tools, price_tools, sfa_tools,
                   supply_chain_tools, wholesale_tools)


_load()
