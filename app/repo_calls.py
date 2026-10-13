"""Call detail record (CDR) persistence."""
from __future__ import annotations

import json
import sqlite3
from typing import List, Optional

from . import security
from .db import connection
from .voice.state import CallSession


def _sim_snapshot(c: sqlite3.Connection, device_id: Optional[int]) -> tuple[Optional[str], Optional[str]]:
    if device_id is None:
        return None, None
    row = c.execute(
        "SELECT sim_key, sim_number_manual, sim_number_auto FROM devices WHERE id = ?",
        (device_id,),
    ).fetchone()
    if not row:
        return None, None
    return row["sim_key"], row["sim_number_manual"] or row["sim_number_auto"]


def insert_call(
    session: CallSession,
    *,
    owner_user_id: Optional[int] = None,
    origin: Optional[str] = None,
) -> int:
    """Create the CDR row when a call starts. Returns the row id."""
    with connection() as c:
        sim_key, sim_number = _sim_snapshot(c, session.device_id)
        cur = c.execute(
            "INSERT INTO call_sessions (call_id, device_id, sim_key, sim_number, direction, remote,"
            " state, created_at, ringing_at, owner_user_id, origin)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session.call_id,
                session.device_id,
                sim_key,
                sim_number,
                session.direction.value,
                session.remote,
                session.state.value,
                session.created_at,
                session.ringing_at,
                owner_user_id,
                origin,
            ),
        )
        return int(cur.lastrowid)


def update_call(
    call_id: str,
    *,
    state: Optional[str] = None,
    remote: Optional[str] = None,
    ringing_at: Optional[int] = None,
    answered_at: Optional[int] = None,
    ended_at: Optional[int] = None,
    end_reason: Optional[str] = None,
    duration: Optional[int] = None,
    tts_log: Optional[list] = None,
    transcript: Optional[list] = None,
    recording_path: Optional[str] = None,
) -> None:
    fields: list[str] = []
    params: list[object] = []
    if state is not None:
        fields.append("state = ?")
        params.append(state)
    if remote is not None:
        fields.append("remote = ?")
        params.append(remote)
    if ringing_at is not None:
        fields.append("ringing_at = ?")
        params.append(ringing_at)
    if answered_at is not None:
        fields.append("answered_at = ?")
        params.append(answered_at)
    if ended_at is not None:
        fields.append("ended_at = ?")
        params.append(ended_at)
    if end_reason is not None:
        fields.append("end_reason = ?")
        params.append(end_reason)
    if duration is not None:
        fields.append("duration = ?")
        params.append(duration)
    if tts_log is not None:
        fields.append("tts_log = ?")
        params.append(json.dumps(tts_log, ensure_ascii=False))
    if transcript is not None:
        fields.append("transcript = ?")
        params.append(json.dumps(transcript, ensure_ascii=False))
    if recording_path is not None:
        fields.append("recording_path = ?")
        params.append(recording_path)
    if not fields:
        return
    params.append(call_id)
    with connection() as c:
        c.execute(f"UPDATE call_sessions SET {', '.join(fields)} WHERE call_id = ?", params)


def persist_session(session: CallSession) -> None:
    """Upsert a whole session. Used when the call ends."""
    with connection() as c:
        exists = c.execute("SELECT id FROM call_sessions WHERE call_id = ?", (session.call_id,)).fetchone()
    if not exists:
        insert_call(session)
    update_call(
        session.call_id,
        state=session.state.value,
        remote=session.remote,
        ringing_at=session.ringing_at,
        answered_at=session.answered_at,
        ended_at=session.ended_at,
        end_reason=session.end_reason.value if session.end_reason else None,
        duration=session.duration,
        tts_log=session.tts_log,
        transcript=session.transcript,
        recording_path=session.recording_path,
    )


def get_call(call_id: str) -> Optional[sqlite3.Row]:
    with connection() as c:
        return c.execute("SELECT * FROM call_sessions WHERE call_id = ?", (call_id,)).fetchone()


def list_calls(
    limit: int = 50,
    device_id: Optional[int] = None,
    remote: Optional[str] = None,
    direction: Optional[str] = None,
    since: Optional[int] = None,
) -> List[sqlite3.Row]:
    sql = (
        "SELECT c.*, d.number AS device_number, d.label AS device_label "
        "FROM call_sessions c LEFT JOIN devices d ON d.id = c.device_id"
    )
    where: list[str] = []
    params: list[object] = []
    if device_id is not None:
        where.append("c.device_id = ?")
        params.append(device_id)
    if remote:
        where.append("c.remote LIKE ?")
        params.append(f"%{remote.strip()}%")
    if direction in ("inbound", "outbound"):
        where.append("c.direction = ?")
        params.append(direction)
    if since is not None:
        where.append("c.created_at > ?")
        params.append(since)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY c.created_at DESC LIMIT ?"
    params.append(limit)
    with connection() as c:
        return list(c.execute(sql, params).fetchall())


def serialize_call(row: sqlite3.Row) -> dict:
    def _json(raw: Optional[str]) -> list:
        if not raw:
            return []
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return []

    number = row["device_number"]
    label = row["device_label"]
    return {
        "id": row["id"],
        "call_id": row["call_id"],
        "device_id": row["device_id"],
        "device": {
            "id": row["device_id"],
            "code": f"{int(number):03d}" if number is not None else None,
            "display_name": label or (f"编号{int(number):03d}" if number is not None else None),
        }
        if row["device_id"] is not None
        else None,
        "sim_number": row["sim_number"],
        "direction": row["direction"],
        "remote": row["remote"],
        "state": row["state"],
        "end_reason": row["end_reason"],
        "created_at": row["created_at"],
        "ringing_at": row["ringing_at"],
        "answered_at": row["answered_at"],
        "ended_at": row["ended_at"],
        "duration": row["duration"],
        "tts_log": _json(row["tts_log"]),
        "transcript": _json(row["transcript"]),
        "recording_path": row["recording_path"],
        "owner_user_id": row["owner_user_id"],
        "origin": row["origin"],
    }


def call_stats(days: int = 7) -> dict:
    cutoff = security.now_epoch() - max(1, days) * 86400
    with connection() as c:
        row = c.execute(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN direction='outbound' THEN 1 ELSE 0 END) AS outbound,"
            " SUM(CASE WHEN direction='inbound' THEN 1 ELSE 0 END) AS inbound,"
            " SUM(CASE WHEN answered_at IS NOT NULL THEN 1 ELSE 0 END) AS answered,"
            " COALESCE(SUM(duration), 0) AS talk_seconds"
            " FROM call_sessions WHERE created_at >= ?",
            (cutoff,),
        ).fetchone()
    total = int(row["total"] or 0)
    answered = int(row["answered"] or 0)
    return {
        "days": days,
        "total": total,
        "outbound": int(row["outbound"] or 0),
        "inbound": int(row["inbound"] or 0),
        "answered": answered,
        "answer_rate": round(answered / total, 4) if total else 0.0,
        "talk_seconds": int(row["talk_seconds"] or 0),
    }


def cleanup_expired_calls(retention_days: int) -> int:
    cutoff = security.now_epoch() - max(1, int(retention_days)) * 86400
    with connection() as c:
        return int(c.execute("DELETE FROM call_sessions WHERE created_at < ?", (cutoff,)).rowcount or 0)
