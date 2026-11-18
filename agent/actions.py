"""Demo 行动任务事件流：只写独立 JSONL，不触碰业务库或外部通知。"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import date, datetime
from pathlib import Path

ACTION_PATH = Path(__file__).resolve().parents[1] / "logs" / "agent_action_tasks.jsonl"
_LOCK = threading.Lock()
_ALLOWED = {
    "CONFIRMED": {"IN_PROGRESS"},
    "IN_PROGRESS": {"COMPLETED"},
    "COMPLETED": {"REVIEWED"},
    "REVIEWED": set(),
}
_REVIEW_OUTCOMES = {"EFFECTIVE", "INEFFECTIVE", "PENDING"}


class ActionError(ValueError):
    pass


def _text(payload: dict, key: str, maximum: int, required: bool = False) -> str:
    value = str(payload.get(key) or "").strip()
    if required and not value:
        raise ActionError(f"{key} 不能为空")
    if len(value) > maximum:
        raise ActionError(f"{key} 不能超过 {maximum} 字")
    return value


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _boundary() -> dict:
    return {
        "effect": "demo_only_no_business_data_write",
        "external_notification": False,
        "business_system_write": False,
    }


def _append(event: dict) -> None:
    ACTION_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        with ACTION_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def _events() -> list[dict]:
    if not ACTION_PATH.exists():
        return []
    out: list[dict] = []
    for line in ACTION_PATH.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("task_id"):
            out.append(row)
    return out


def _materialize(events: list[dict]) -> dict:
    first = events[0]
    task = {
        "task_id": first["task_id"], "run_id": first.get("run_id", ""),
        "thread_id": first.get("thread_id", ""), "suggestion": first.get("suggestion", ""),
        "owner": first.get("owner", ""), "due_date": first.get("due_date", ""),
        "created_by": first.get("actor", ""), "created_at": first.get("recorded_at", ""),
        "status": "CONFIRMED", "result": "", "review_outcome": "", "review_note": "",
        "note": "", **_boundary(), "history": events,
    }
    for event in events:
        task["status"] = event.get("status") or task["status"]
        for key in ("note", "result", "review_outcome", "review_note", "owner", "due_date"):
            if event.get(key) not in (None, ""):
                task[key] = event[key]
        task["updated_at"] = event.get("recorded_at", task["created_at"])
    return task


def list_tasks(run_id: str = "") -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for event in _events():
        grouped.setdefault(event["task_id"], []).append(event)
    tasks = [_materialize(events) for events in grouped.values()]
    if run_id:
        tasks = [task for task in tasks if task["run_id"] == run_id]
    return sorted(tasks, key=lambda item: item.get("updated_at", ""), reverse=True)


def _get(task_id: str) -> dict:
    task = next((item for item in list_tasks() if item["task_id"] == task_id), None)
    if not task:
        raise ActionError("task_id 不存在")
    return task


def create(payload: dict) -> dict:
    if payload.get("confirmed") is not True:
        raise ActionError("必须由人工显式确认 confirmed=true 才能创建 Demo 待办")
    run_id = _text(payload, "run_id", 64, required=True)
    suggestion = _text(payload, "suggestion", 1000, required=True)
    owner = _text(payload, "owner", 80, required=True)
    due_date = _text(payload, "due_date", 10, required=True)
    try:
        date.fromisoformat(due_date)
    except ValueError as exc:
        raise ActionError("due_date 必须是 YYYY-MM-DD") from exc
    task_id = uuid.uuid4().hex[:16]
    event = {
        "event_id": uuid.uuid4().hex, "event": "CREATED", "task_id": task_id,
        "status": "CONFIRMED", "recorded_at": _now(), "run_id": run_id,
        "thread_id": _text(payload, "thread_id", 64), "suggestion": suggestion,
        "owner": owner, "due_date": due_date,
        "actor": _text(payload, "created_by", 80) or "demo_user",
        "note": _text(payload, "note", 500), **_boundary(),
    }
    _append(event)
    return _get(task_id)
def transition(payload: dict) -> dict:
    task_id = _text(payload, "task_id", 32, required=True)
    task = _get(task_id)
    target = _text(payload, "to_status", 20, required=True).upper()
    if target not in _ALLOWED.get(task["status"], set()):
        raise ActionError(f"不允许从 {task['status']} 跳转到 {target}")
    result = _text(payload, "result", 1200)
    review_outcome = _text(payload, "review_outcome", 20).upper()
    review_note = _text(payload, "review_note", 1200)
    if target == "COMPLETED" and not result:
        raise ActionError("完成任务必须记录 result")
    if target == "REVIEWED":
        if review_outcome not in _REVIEW_OUTCOMES:
            raise ActionError(f"review_outcome 必须是 {sorted(_REVIEW_OUTCOMES)} 之一")
        if not review_note:
            raise ActionError("效果复盘必须记录 review_note")
    event = {
        "event_id": uuid.uuid4().hex, "event": "TRANSITIONED", "task_id": task_id,
        "status": target, "recorded_at": _now(), "run_id": task["run_id"],
        "thread_id": task["thread_id"], "actor": _text(payload, "actor", 80) or "demo_user",
        "note": _text(payload, "note", 500), "result": result,
        "review_outcome": review_outcome, "review_note": review_note,
        **_boundary(),
    }
    _append(event)
    return _get(task_id)
