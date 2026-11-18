"""Agent 人工反馈日志：只写独立 JSONL，不接触任何业务数据库。"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path

FEEDBACK_PATH = Path(__file__).resolve().parents[1] / "logs" / "agent_feedback.jsonl"
ACTIONS = {"accept", "modified", "reject", "reanalyze", "defer"}
STATUS_BY_ACTION = {"accept": "ACCEPTED", "modified": "MODIFIED", "reject": "REJECTED",
                    "reanalyze": "PENDING_REANALYSIS", "defer": "DEFERRED"}
_LOCK = threading.Lock()


class FeedbackError(ValueError):
    pass


def record(payload: dict) -> dict:
    action = str(payload.get("action") or "").strip()
    run_id = str(payload.get("run_id") or "").strip()
    if action not in ACTIONS:
        raise FeedbackError(f"action 必须是 {sorted(ACTIONS)} 之一")
    if not run_id:
        raise FeedbackError("run_id 不能为空")
    note = str(payload.get("note") or "").strip()
    if len(note) > 500:
        raise FeedbackError("note 不能超过 500 字")
    row = {
        "feedback_id": uuid.uuid4().hex,
        "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "run_id": run_id,
        "thread_id": str(payload.get("thread_id") or "")[:64],
        "scene": str(payload.get("scene") or "")[:40],
        "question": str(payload.get("question") or "")[:500],
        "action": action,
        "suggestion_id": str(payload.get("suggestion_id") or "report")[:80],
        "feedback_status": STATUS_BY_ACTION[action],
        "feedback_reason": note,
        "modified_content": str(payload.get("modified_content") or "")[:1000],
        "note": note,
        "effect": "feedback_only_no_business_data_write",
    }
    FEEDBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        with FEEDBACK_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row
