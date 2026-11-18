"""执行前的数据可用性与 Demo 只读权限检查。"""
from __future__ import annotations

from . import data_governance
from .tools._common import open_conn


def check(state: dict) -> dict:
    """只读检查必需视图、查询期间覆盖和演示权限边界。"""
    workflow = (state.get("supervisor_decision") or {}).get("workflow") or ""
    permission_check = data_governance.permission_preflight(state)
    principal = permission_check.get("principal") or {}
    permission = {
        "status": permission_check.get("status") or "PERMISSION_DENIED",
        "mode": "DEMO_READ_ONLY",
        "enforcement": "DEMO_BACKEND_RBAC_READ_ONLY",
        "reason": permission_check.get("message") or "Demo 权限预检",
        "write_actions_allowed": False,
        "principal": principal,
        "effective_scope": {
            "regions": principal.get("regions") or ["全国"],
            "departments": principal.get("departments") or ["全部"],
            "stores": principal.get("stores") or ["全部"],
            "domains": principal.get("domains") or [],
        },
    }
    if permission_check.get("status") != "AVAILABLE":
        return {"status": "PERMISSION_DENIED", "permission": permission,
                "checks": [], "message": permission_check.get("message")}
    if workflow != "budget_diagnosis":
        return {"status": "AVAILABLE", "permission": permission, "checks": [],
                "message": "权限预检通过；每个工具仍会在 SQL 前强制收敛授权范围"}

    year = int(state.get("year") or 0)
    months = list(state.get("month") or [])
    checks: list[dict] = []
    try:
        conn = open_conn()
        try:
            for name, table in (("ERP 实际", "a_erp"), ("销售预算", "v_budget")):
                row = conn.execute(
                    f"SELECT MIN(month), MAX(month), COUNT(*) FROM {table} WHERE year = ?",
                    (year,)).fetchone()
                min_month, max_month, count = row if row else (None, None, 0)
                covered = bool(count) and all(min_month <= m <= max_month for m in months)
                checks.append({"name": name, "table": table,
                               "status": "AVAILABLE" if covered else "UNAVAILABLE",
                               "rows": int(count or 0), "min_month": min_month,
                               "max_month": max_month, "requested_months": months})
        finally:
            conn.close()
    except Exception as exc:
        return {"status": "UNAVAILABLE", "permission": permission, "checks": checks,
                "message": f"只读数据预检失败：{type(exc).__name__}: {exc}"}

    unavailable = [item for item in checks if item["status"] != "AVAILABLE"]
    return {"status": "UNAVAILABLE" if unavailable else "AVAILABLE",
            "permission": permission, "checks": checks,
            "message": ("预算与 ERP 数据覆盖请求期间" if not unavailable
                        else "预算或 ERP 数据未覆盖请求期间")}
