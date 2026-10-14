"""Storage for AI call tasks.

A task is one phone call with a stated purpose. The app, the console and the
open API all create tasks through here, so a third party driving a call and an
operator tapping a button produce the same record and the same result shape.
"""
from __future__ import annotations

import json
import sqlite3
from typing import List, Optional

from . import security
from .db import connection

STATUSES = (
    "queued", "dialing", "in_call", "finished",
    "failed", "cancelled", "no_answer", "rejected", "busy",
)


def serialize_task(row: sqlite3.Row) -> dict:
    def _field(key, default=None):
        try:
            value = row[key]
        except (IndexError, KeyError):
            return default
        return default if value is None else value

    summary_raw = _field("summary")
    summary = None
    if summary_raw:
        try:
            summary = json.loads(summary_raw)
        except (ValueError, TypeError):
            summary = {"summary": str(summary_raw), "parsed": False}
    achieved = _field("achieved")
    return {
        "id": row["id"],
        "reference": _field("reference"),
        "device_id": _field("device_id"),
        "direction": _field("direction", "outbound"),
        "number": _field("number"),
        "purpose": _field("purpose", ""),
        "context": _field("context"),
        "language": _field("language"),
        "peer_language": _field("peer_language"),
        "template": _field("template"),
        "max_seconds": _field("max_seconds"),
        "status": _field("status", "queued"),
        "call_id": _field("call_id"),
        "result": _field("result"),
        "achieved": None if achieved is None else bool(achieved),
        "summary": summary,
        "error": _field("error"),
        "created_by": _field("created_by"),
        "owner_user_id": _field("owner_user_id"),
        "created_at": row["created_at"],
        "started_at": _field("started_at"),
        "finished_at": _field("finished_at"),
    }


def create_task(
    *,
    purpose: str,
    number: Optional[str] = None,
    direction: str = "outbound",
    device_id: Optional[int] = None,
    context: Optional[str] = None,
    language: Optional[str] = None,
    peer_language: Optional[str] = None,
    template: Optional[str] = None,
    max_seconds: Optional[int] = None,
    reference: Optional[str] = None,
    created_by: Optional[str] = None,
    owner_user_id: Optional[int] = None,
    status: str = "queued",
) -> int:
    now = security.now_epoch()
    with connection() as c:
        cur = c.execute(
            "INSERT INTO ai_call_tasks"
            " (reference,device_id,direction,number,purpose,context,language,peer_language,"
            "  template,max_seconds,status,created_by,owner_user_id,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (reference, device_id, direction, number, purpose.strip(), context,
             language, peer_language, template, max_seconds, status,
             created_by, owner_user_id, now),
        )
        return int(cur.lastrowid)


def find_by_reference(reference: str) -> Optional[dict]:
    """Idempotency lookup so a retried API call does not place a second call."""
    if not reference:
        return None
    with connection() as c:
        row = c.execute(
            "SELECT * FROM ai_call_tasks WHERE reference = ?", (reference,)
        ).fetchone()
    return serialize_task(row) if row else None


def get_task(task_id: int) -> Optional[dict]:
    with connection() as c:
        row = c.execute("SELECT * FROM ai_call_tasks WHERE id = ?", (int(task_id),)).fetchone()
    return serialize_task(row) if row else None


def get_task_by_call(call_id: str) -> Optional[dict]:
    with connection() as c:
        row = c.execute(
            "SELECT * FROM ai_call_tasks WHERE call_id = ? ORDER BY id DESC LIMIT 1", (call_id,)
        ).fetchone()
    return serialize_task(row) if row else None


def update_task(task_id: int, **fields) -> bool:
    allowed = {
        "status", "call_id", "result", "achieved", "summary", "error",
        "device_id", "started_at", "finished_at", "number",
    }
    sets: List[str] = []
    params: List[object] = []
    for key, value in fields.items():
        if key not in allowed or value is None:
            continue
        if key == "summary":
            sets.append("summary = ?")
            params.append(json.dumps(value, ensure_ascii=False))
        elif key == "achieved":
            sets.append("achieved = ?")
            params.append(1 if value else 0)
        else:
            sets.append(f"{key} = ?")
            params.append(value)
    if not sets:
        return False
    params.append(int(task_id))
    with connection() as c:
        cur = c.execute(f"UPDATE ai_call_tasks SET {', '.join(sets)} WHERE id = ?", params)
        return cur.rowcount > 0


def list_tasks(
    limit: int = 50,
    status: Optional[str] = None,
    owner_user_id: Optional[int] = None,
) -> List[dict]:
    limit = max(1, min(int(limit), 200))
    sql = "SELECT * FROM ai_call_tasks"
    where: List[str] = []
    params: List[object] = []
    if status:
        where.append("status = ?")
        params.append(status)
    if owner_user_id is not None:
        where.append("(owner_user_id = ? OR owner_user_id IS NULL)")
        params.append(int(owner_user_id))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with connection() as c:
        rows = c.execute(sql, params).fetchall()
    return [serialize_task(r) for r in rows]


def list_pending(limit: int = 20) -> List[dict]:
    """Tasks that still need a worker to pick them up."""
    with connection() as c:
        rows = c.execute(
            "SELECT * FROM ai_call_tasks WHERE status = 'queued' ORDER BY id LIMIT ?",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
    return [serialize_task(r) for r in rows]


def cleanup_finished(retention_days: int) -> int:
    horizon = security.now_epoch() - max(1, int(retention_days)) * 86400
    with connection() as c:
        cur = c.execute(
            "DELETE FROM ai_call_tasks WHERE finished_at IS NOT NULL AND finished_at < ?",
            (horizon,),
        )
        return cur.rowcount
