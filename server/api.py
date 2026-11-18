"""HTTP JSON 接口层：路由、参数解析、异常与响应包装。

所有筛选、聚合、比率计算都在后端完成；前端只消费结果，不重算指标。
"""
from __future__ import annotations

import json
import sqlite3
import traceback
from datetime import datetime
from typing import Callable
from urllib.parse import parse_qs, urlparse

from . import (analysis_grid, analysis_queries, board_filters, budget_queries, cockpit_insight,
               cockpit_queries, config, filters, overview_queries, pos_queries, queries,
               region_queries, sc_queries, sfa_profile_queries, sfa_visit_queries,
               sku_gap_queries, targets, wholesale_queries)
from .db import connect

ROUTES: dict[str, Callable] = {
    "/api/health": queries.health,
    "/api/meta": queries.meta,
    "/api/scope": queries.scope,
    "/api/kpi": queries.kpi,
    "/api/trend": queries.trend,
    "/api/region": queries.region,
    "/api/channel": queries.channel,
    "/api/category": queries.category,
    "/api/sku": queries.sku,
    "/api/customer": queries.customer,
    "/api/detail": queries.detail,
}

# 综合经营分析 V1：六维度统一分析接口（战区只是其中一个 dimension）。
# 与 /api/region/analysis 并存，保证已验收的战区页面/接口不 404；
# ROUTES 与 /api/dashboard 的响应结构保持 V1.1 原样，不因新增页面而变化。
ANALYSIS_ROUTES: dict[str, Callable] = {
    "/api/analysis": analysis_queries.analysis,
    # 综合经营分析 V2：明细支持服务端排序与关键词搜索，独立于驾驶舱的 /api/detail。
    "/api/analysis/detail": analysis_queries.detail,
}

# 集团总览（驾驶舱 V2）：跨模块 KPI 聚合。它不改动任何既有路由与口径，
# 只是把 ERP / POS / SFA / 批发各自已验收的查询结果并排放在一行卡片里。
OVERVIEW_ROUTES: dict[str, Callable] = {
    "/api/overview": overview_queries.overview,
}

# 战区销售分析 V1 的兼容路由（region.html 已改为跳转页，接口保留不删）。
REGION_ROUTES: dict[str, Callable] = {
    "/api/region/analysis": region_queries.analysis,
}

# ---------------------------------------------------------------------------
# 三个新看板：每个看板有自己的筛选白名单（board_filters.ALLOWED），
# 不共用综合经营分析的 7 个筛选项，也不改动上面任何既有路由。
# 值 = (看板标识, 处理函数)；看板标识决定可用筛选与排序字段。
# ---------------------------------------------------------------------------
BOARD_ROUTES: dict[str, tuple[str, Callable]] = {
    "/api/sfa/visit": ("sfa_visit", sfa_visit_queries.analysis),
    "/api/sfa/visit/detail": ("sfa_visit", sfa_visit_queries.detail),
    "/api/sfa/visit/drill": ("sfa_visit", sfa_visit_queries.drill),
    "/api/sfa/visit/options": ("sfa_visit", sfa_visit_queries.meta_options),
    "/api/sfa/profile": ("sfa_profile", sfa_profile_queries.analysis),
    "/api/sfa/profile/detail": ("sfa_profile", sfa_profile_queries.detail),
    "/api/sfa/profile/options": ("sfa_profile", sfa_profile_queries.meta_options),
    "/api/wholesale": ("wholesale", wholesale_queries.analysis),
    "/api/wholesale/detail": ("wholesale", wholesale_queries.detail),
    "/api/wholesale/options": ("wholesale", wholesale_queries.meta_options),
    # POS 门店动销（阶段五）：只读对齐版 POS，27 家五大战区门店
    "/api/pos": ("pos", pos_queries.analysis),
    "/api/pos/detail": ("pos", pos_queries.detail),
    "/api/pos/drill": ("pos", pos_queries.drill),
    "/api/pos/options": ("pos", pos_queries.meta_options),
}

# ---------------------------------------------------------------------------
# 模拟销售预算 V1（MOCK_BUDGET）：6 个只读接口，全部走独立预算库。
# 预算库以只读 URI ATTACH 到 ETL 连接上（bud 别名），不写入任何库；
# 这批路由不参与 filters.parse，参数各自校验（见 budget_queries）。
# ---------------------------------------------------------------------------
# AI 经营驾驶舱首页：三个只读接口。
# /api/cockpit 一次返回 KPI / 战区 / 对比 / 趋势 / KA / 利润 / 异常，
# 保证「同一筛选下各块数字互相能对上」，前端不重算任何指标。
# /api/cockpit/insight 只在用户点「生成经营分析」时调用，不随页面加载自动执行。
# 综合经营分析 V2：战区 → 市场部 → 客户类型 的预算达成交叉表。
# 用独立 Params 解析（支持年份 2025/2026、多月份、五分类），不复用 filters.parse，
# 因此与 V1 的 /api/analysis 完全隔离，互不影响。
ANALYSIS_GRID_ROUTES: dict[str, Callable] = {
    "/api/analysis/grid": lambda conn, qs: analysis_grid.envelope(
        analysis_grid.build(conn, analysis_grid.Params(qs)),
        analysis_grid.Params(qs)),
    "/api/analysis/grid/detail": lambda conn, qs: analysis_grid.envelope(
        analysis_grid.detail_rows(conn, analysis_grid.Params(qs),
                                  (parse_qs(qs, keep_blank_values=True).get("node")
                                   or [""])[0].strip()),
        analysis_grid.Params(qs), {"drill": "customer"}),
    "/api/analysis/grid/lines": lambda conn, qs: analysis_grid.envelope(
        analysis_grid.customer_lines(
            conn, analysis_grid.Params(qs),
            (parse_qs(qs, keep_blank_values=True).get("node") or [""])[0].strip(),
            (parse_qs(qs, keep_blank_values=True).get("customer") or [""])[0].strip()),
        analysis_grid.Params(qs), {"drill": "lines"}),
    "/api/analysis/grid/options": lambda conn, qs: {
        "ok": True, "data": analysis_grid.options(conn),
        "meta": {"page": "business-analysis-v2"}},
    "/api/analysis/grid/export": lambda conn, qs: {
        "ok": True,
        "data": {"csv": analysis_grid.to_csv(conn, analysis_grid.Params(qs))
                 .decode("utf-8-sig"),
                 "filename": "战区销售达成跟进_预算达成.csv"},
        "meta": {"page": "business-analysis-v2"}},
}

COCKPIT_ROUTES: dict[str, Callable] = {
    "/api/cockpit": lambda conn, qs: cockpit_queries.build(conn, qs),
    "/api/cockpit/options": lambda conn, qs: cockpit_queries.options(conn),
    "/api/cockpit/insight": lambda conn, qs: cockpit_insight.build(conn, qs),
}

BUDGET_ROUTES: dict[str, Callable] = {
    "/api/budget/meta": budget_queries.meta,
    "/api/budget/summary": budget_queries.summary,
    "/api/budget/customer": budget_queries.customer,
    "/api/budget/period": budget_queries.period,
    "/api/budget/assumption": budget_queries.assumption,
    "/api/budget/consistency": budget_queries.consistency,
}

# ---------------------------------------------------------------------------
# 供应链看板 P5 + V2 数据链路（MOCK_SUPPLY_CHAIN）：13 个只读接口，
# 全部走独立供应链库。
# 供应链库以只读 URI ATTACH 到 ETL 连接上（sc 别名，BOM 果子用量取主库 dim_sku），
# 不写入任何库；参数各自校验（month ∈ 2026-01~10），不走 filters.parse。
# V2 新增：production（生产+入库）/ trace（批次追溯）/ ledger（包材流水）/
# forecast（包材采购预测，观察点固定 2026-10 月末）。
# ---------------------------------------------------------------------------
SC_ROUTES: dict[str, Callable] = {
    "/api/sc/summary": sc_queries.summary,
    "/api/sc/trend": sc_queries.trend,
    "/api/sc/arrival": sc_queries.arrival,
    "/api/sc/aging": sc_queries.aging,
    "/api/sc/warehouses": sc_queries.warehouses,
    "/api/sc/packaging": sc_queries.packaging,
    "/api/sc/bom": sc_queries.bom,
    "/api/sc/capacity": sc_queries.capacity,
    "/api/sc/batches": sc_queries.batches,
    "/api/sc/production": sc_queries.production,
    "/api/sc/trace": sc_queries.trace,
    "/api/sc/ledger": sc_queries.ledger,
    "/api/sc/forecast": sc_queries.forecast,
}

# ---------------------------------------------------------------------------
# 机会引擎 · 机会一 SKU 渗透（AI_ANALYSIS_LAYER_V1）：3 个只读接口。
# 全部读 ETL 主库（fact_pos_sales / fact_sfa_visit / metric_pos_yoy），
# 六步流水线均为确定性规则，不调用 LLM；研判文案为模板初稿、标待验证。
# ---------------------------------------------------------------------------
GAP_ROUTES: dict[str, Callable] = {
    "/api/gap/meta": sku_gap_queries.meta,
    "/api/gap/skus": sku_gap_queries.skus,
    "/api/gap/card": sku_gap_queries.card,
}

# 客户经理月度目标配置：GET 查询走 ROUTES 风格，写入走 POST /api/sfa/targets*。
TARGET_GET_ROUTES: dict[str, Callable] = {
    "/api/sfa/targets": targets.list_targets,
}
TARGET_POST_ROUTES: dict[str, Callable] = {
    "/api/sfa/targets": targets.upsert_targets,
    "/api/sfa/targets/import": targets.import_csv,
    "/api/sfa/targets/delete": targets.delete_target,
    # 示例目标（DEMO）：库里没有真实目标时用于让完成率图表可读，全程带来源标记、可一键清除。
    "/api/sfa/targets/seed-demo": targets.seed_demo_targets,
    "/api/sfa/targets/clear-demo": targets.clear_demo_targets,
}

# ---------------------------------------------------------------------------
# AI 经营助手（LangGraph Agent V1）：只读接口，不改动上面任何既有路由。
# 依赖 langgraph / openai，未安装时这些路由会返回明确错误而不是让服务起不来
# （agent 包内部做了 import 保护，见 server/api.py 的 _agent_mod）。
# ---------------------------------------------------------------------------
AGENT_GET_ROUTES: dict[str, Callable] = {
    "/api/agent/health": lambda conn, qs: _agent_api().health(),
    "/api/agent/tools": lambda conn, qs: _agent_api().tool_catalog(),
    # P8：页面注册表（前端自带同一份，接口用于校验与后续页面级开关）
    "/api/agent/pages": lambda conn, qs: _agent_api().page_catalog(),
    "/api/agent/kpi": lambda conn, qs: _agent_api().kpi_summary(qs.get("scene") or ""),
    "/api/agent/actions": lambda conn, qs: _agent_api().action_tasks(qs.get("run_id") or ""),
}
AGENT_POST_PATHS = ("/api/agent/chat", "/api/agent/stream", "/api/agent/feedback",
                    "/api/agent/actions")


def _agent_api():
    """延迟导入：没装 langgraph 时只有 agent 接口不可用，BI 页面照常。"""
    from agent import api as agent_api
    return agent_api


# 一次性返回驾驶舱首屏所需的全部区块（单次连接，减少往返）
# 经营明细表数据量较大，单独走 /api/detail，分页时不重复拉取全部图表。
DASHBOARD_SECTIONS = ("kpi", "trend", "region", "category", "sku", "customer")


def _flat_qs(raw: str) -> dict[str, str]:
    return {k: v[-1] for k, v in parse_qs(raw, keep_blank_values=True).items()}


def handle(path: str, raw_query: str = "") -> tuple[int, dict]:
    """返回 (http_status, payload)。可被测试直接调用，无需起服务。"""
    known = set(ROUTES) | set(REGION_ROUTES) | set(ANALYSIS_ROUTES) | set(BOARD_ROUTES) \
        | set(OVERVIEW_ROUTES) | set(TARGET_GET_ROUTES) | set(BUDGET_ROUTES) \
        | set(SC_ROUTES) | set(GAP_ROUTES) | set(COCKPIT_ROUTES) | set(ANALYSIS_GRID_ROUTES) \
        | set(AGENT_GET_ROUTES) \
        | {"/api/dashboard"}
    if path not in known:
        return 404, {"ok": False, "error": f"未知接口: {path}",
                     "available": sorted(known)}

    qs = _flat_qs(raw_query)

    if path in AGENT_GET_ROUTES:
        try:
            return 200, AGENT_GET_ROUTES[path](None, qs)
        except ImportError as exc:
            return 503, {"ok": False,
                         "error": {"type": "AgentUnavailable",
                                   "message": f"AI 经营助手依赖未安装：{exc}",
                                   "hint": "pip install -r requirements-agent.txt"}}
        except Exception as exc:                               # pragma: no cover
            return 500, {"ok": False, "error": {"type": type(exc).__name__,
                                                "message": str(exc)}}

    # 新看板：先按看板白名单解析，再交给对应查询函数；不走 filters.parse。
    if path in BOARD_ROUTES:
        board, fn = BOARD_ROUTES[path]
        try:
            bf = board_filters.parse_board(qs, board)
        except board_filters.BoardFilterError as exc:
            return 400, {"ok": False, "error": str(exc), "type": "BoardFilterError"}
        conn = None
        try:
            conn = connect()
            if path.endswith("/drill"):
                # SFA 下钻按客户经理，POS 下钻按门店；两者都只取一个主键值。
                key = (qs.get("employee") or qs.get("store") or "").strip()
                return 200, fn(conn, bf, key)
            return 200, fn(conn, bf)
        except board_filters.BoardFilterError as exc:
            return 400, {"ok": False, "error": str(exc), "type": "BoardFilterError"}
        except sqlite3.Error as exc:
            return 500, {"ok": False, "error": f"数据库查询失败: {exc}",
                         "type": "sqlite3.Error"}
        except Exception as exc:                               # pragma: no cover
            return 500, {"ok": False, "error": str(exc), "type": type(exc).__name__,
                         "trace": traceback.format_exc(limit=3)}
        finally:
            if conn is not None:
                conn.close()

    if path in ANALYSIS_GRID_ROUTES:
        conn = None
        try:
            conn = connect()
            return 200, ANALYSIS_GRID_ROUTES[path](conn, raw_query)
        except sqlite3.Error as exc:
            return 500, {"ok": False, "error": f"经营分析查询失败: {exc}",
                         "type": "sqlite3.Error"}
        except Exception as exc:                               # pragma: no cover
            return 500, {"ok": False, "error": str(exc), "type": type(exc).__name__,
                         "trace": traceback.format_exc(limit=3)}
        finally:
            if conn is not None:
                conn.close()

    if path in COCKPIT_ROUTES:
        conn = None
        try:
            conn = connect()
            return 200, COCKPIT_ROUTES[path](conn, qs)
        except cockpit_queries.CockpitError as exc:
            return 400, {"ok": False, "error": str(exc), "type": "CockpitError"}
        except sqlite3.Error as exc:
            return 500, {"ok": False, "error": f"驾驶舱查询失败: {exc}", "type": "sqlite3.Error"}
        except Exception as exc:                               # pragma: no cover
            return 500, {"ok": False, "error": str(exc), "type": type(exc).__name__,
                         "trace": traceback.format_exc(limit=3)}
        finally:
            if conn is not None:
                conn.close()

    if path in BUDGET_ROUTES:
        conn = None
        try:
            conn = connect()
            return 200, BUDGET_ROUTES[path](conn, qs)
        except budget_queries.BudgetError as exc:
            return 400, {"ok": False, "error": str(exc), "type": "BudgetError"}
        except sqlite3.Error as exc:
            return 500, {"ok": False, "error": f"预算查询失败: {exc}", "type": "sqlite3.Error"}
        except Exception as exc:                               # pragma: no cover
            return 500, {"ok": False, "error": str(exc), "type": type(exc).__name__,
                         "trace": traceback.format_exc(limit=3)}
        finally:
            if conn is not None:
                conn.close()

    if path in GAP_ROUTES:
        conn = None
        try:
            conn = connect()
            return 200, GAP_ROUTES[path](conn, qs)
        except sku_gap_queries.GapError as exc:
            return 400, {"ok": False, "error": str(exc), "type": "GapError"}
        except sqlite3.Error as exc:
            return 500, {"ok": False, "error": f"机会查询失败: {exc}",
                         "type": "sqlite3.Error"}
        except Exception as exc:                               # pragma: no cover
            return 500, {"ok": False, "error": str(exc), "type": type(exc).__name__,
                         "trace": traceback.format_exc(limit=3)}
        finally:
            if conn is not None:
                conn.close()

    if path in SC_ROUTES:
        conn = None
        try:
            conn = connect()
            return 200, SC_ROUTES[path](conn, qs)
        except sc_queries.ScError as exc:
            return 400, {"ok": False, "error": str(exc), "type": "ScError"}
        except sqlite3.Error as exc:
            return 500, {"ok": False, "error": f"供应链查询失败: {exc}",
                         "type": "sqlite3.Error"}
        except Exception as exc:                               # pragma: no cover
            return 500, {"ok": False, "error": str(exc), "type": type(exc).__name__,
                         "trace": traceback.format_exc(limit=3)}
        finally:
            if conn is not None:
                conn.close()

    if path in TARGET_GET_ROUTES:
        conn = None
        try:
            conn = connect()
            year = _int_or_none(qs.get("year"))
            month = _int_or_none(qs.get("month"))
            payload = targets.list_targets(conn, year=year, month=month,
                                           employee_id=qs.get("employee") or None)
            return 200, {"ok": True, "data": payload, "meta": targets_meta(payload)}
        except targets.TargetError as exc:
            return 400, {"ok": False, "error": str(exc), "type": "TargetError"}
        except sqlite3.Error as exc:
            return 500, {"ok": False, "error": f"数据库读取失败: {exc}",
                         "type": "sqlite3.Error"}
        finally:
            if conn is not None:
                conn.close()

    try:
        f = filters.parse(qs)
    except filters.FilterError as exc:
        return 400, {"ok": False, "error": str(exc), "type": "FilterError"}

    conn = None
    try:
        conn = connect()
        if path == "/api/dashboard":
            data: dict = {}
            meta_payload: dict = {}
            for section in DASHBOARD_SECTIONS:
                payload = ROUTES["/api/" + section](conn, f)
                data[section] = payload["data"]
                meta_payload[section] = payload["meta"]
            return 200, {
                "ok": True,
                "data": data,
                "meta": {
                    "filters": f.as_dict(),
                    "region_label": f.region_label,
                    "scope_note": config.SCOPE_NOTE,
                    "year": config.FIXED_YEAR,
                    "year_int": config.FIXED_YEAR_INT,
                    "period": config.PERIOD_LABEL,
                    "sections": meta_payload,
                    "endpoints": sorted(ROUTES),
                },
            }
        if path in OVERVIEW_ROUTES:
            return 200, OVERVIEW_ROUTES[path](conn, f)
        if path in ANALYSIS_ROUTES:
            return 200, ANALYSIS_ROUTES[path](conn, f)
        if path in REGION_ROUTES:
            return 200, REGION_ROUTES[path](conn, f)
        status, payload = 200, ROUTES[path](conn, f)
        return status, payload
    except sqlite3.Error as exc:
        return 500, {"ok": False, "error": f"数据库查询失败: {exc}", "type": "sqlite3.Error"}
    except Exception as exc:                                   # pragma: no cover
        return 500, {"ok": False, "error": str(exc),
                     "type": type(exc).__name__,
                     "trace": traceback.format_exc(limit=3)}
    finally:
        if conn is not None:
            conn.close()


def dispatch(full_url: str) -> tuple[int, dict]:
    u = urlparse(full_url)
    return handle(u.path, u.query)


def _int_or_none(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def targets_meta(payload: dict) -> dict:
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "target_db": targets.target_meta(),
        "summary": payload.get("summary", {}),
        "demo": payload.get("demo", {}),
        "note": payload.get("note"),
        "write_routes": sorted(TARGET_POST_ROUTES),
    }


MAX_BODY_BYTES = 512 * 1024


def handle_post(path: str, body: bytes,
                server_context: dict | None = None) -> tuple[int, dict]:
    """写入类接口：客户经理目标配置与独立 Agent 反馈日志。

    允许的 path 严格白名单；body 必须是 JSON；本函数是唯一会写盘的地方，
    目标配置只写 ``data/app_config.db``；Agent 反馈只写 ``logs/agent_feedback.jsonl``，
    两者都永不写 ETL、预算或供应链业务库。
    """
    agent_post = ("/api/agent/chat", "/api/agent/feedback", "/api/agent/actions")
    if path not in TARGET_POST_ROUTES and path not in agent_post:
        return 404, {"ok": False, "error": f"不支持写入的接口: {path}",
                     "available": sorted(TARGET_POST_ROUTES) + list(agent_post)}
    if len(body) > MAX_BODY_BYTES:
        return 413, {"ok": False, "error": f"请求体超过 {MAX_BODY_BYTES // 1024} KB 上限"}
    try:
        payload = json.loads(body.decode("utf-8-sig") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return 400, {"ok": False, "error": f"请求体不是合法 JSON: {exc}",
                     "type": "JSONDecodeError"}
    if not isinstance(payload, dict):
        return 400, {"ok": False, "error": "请求体必须是 JSON 对象", "type": "PayloadError"}

    # AI 经营助手：只读分析，不写任何库；模型调用失败会降级为规则模板
    if path == "/api/agent/chat":
        try:
            return _agent_api().chat(payload, server_context=server_context)
        except ImportError as exc:
            return 503, {"ok": False,
                         "error": {"type": "AgentUnavailable",
                                   "message": f"AI 经营助手依赖未安装：{exc}",
                                   "hint": "pip install -r requirements-agent.txt"}}
        except Exception as exc:                               # pragma: no cover
            traceback.print_exc()
            return 500, {"ok": False, "error": {"type": type(exc).__name__,
                                                "message": str(exc)}}

    if path == "/api/agent/feedback":
        try:
            return _agent_api().record_feedback(payload)
        except Exception as exc:                               # pragma: no cover
            return 500, {"ok": False, "error": {"type": type(exc).__name__,
                                                  "message": str(exc)}}

    if path == "/api/agent/actions":
        try:
            return _agent_api().action_command(payload)
        except Exception as exc:                               # pragma: no cover
            return 500, {"ok": False, "error": {"type": type(exc).__name__,
                                                  "message": str(exc)}}

    conn = None
    try:
        conn = connect()
        if path.endswith("/import"):
            csv_text = payload.get("csv")
            if not isinstance(csv_text, str) or not csv_text.strip():
                return 400, {"ok": False, "error": "缺少 csv 文本字段", "type": "TargetError"}
            result = targets.import_csv(conn, csv_text)
        elif path.endswith("/seed-demo"):
            result = targets.seed_demo_targets(conn, payload.get("year"))
        elif path.endswith("/clear-demo"):
            result = targets.clear_demo_targets()
        elif path.endswith("/delete"):
            result = targets.delete_target(payload.get("employee_id"),
                                           payload.get("year"), payload.get("month"))
        else:
            items = payload.get("items")
            if items is None and payload.get("employee_id"):
                items = [payload]
            if not isinstance(items, list):
                return 400, {"ok": False,
                             "error": "缺少 items 数组（或单条记录的 employee_id）",
                             "type": "TargetError"}
            for item in items:
                if not isinstance(item, dict):
                    return 400, {"ok": False, "error": "items 内每条必须是对象",
                                 "type": "TargetError"}
            result = targets.upsert_targets(conn, items)
        snapshot = targets.list_targets(conn)
        return 200, {"ok": True, "data": result,
                     "meta": {**targets_meta(snapshot),
                              "affected": result}}
    except targets.TargetError as exc:
        return 400, {"ok": False, "error": str(exc), "type": "TargetError"}
    except sqlite3.Error as exc:
        return 500, {"ok": False, "error": f"配置库写入失败: {exc}", "type": "sqlite3.Error"}
    except Exception as exc:                                   # pragma: no cover
        return 500, {"ok": False, "error": str(exc), "type": type(exc).__name__,
                     "trace": traceback.format_exc(limit=3)}
    finally:
        if conn is not None:
            conn.close()


def to_json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False,
                      default=str).encode("utf-8")
