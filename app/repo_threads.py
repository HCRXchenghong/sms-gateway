"""Conversation threads built on top of the inbox and outbox tables.

The Flutter app shows a messenger-style list, so inbound and outbound traffic
for the same peer has to be merged. Numbers arrive in several shapes
(+8613800138000, 13800138000, 138-0013-8000) and all of them must land in one
thread, which is what peer_key is for.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Dict, List, Optional

from . import security
from .db import connection

_NON_DIGIT = re.compile(r"[^0-9]")


def peer_key(raw: Optional[str]) -> str:
    """Canonical grouping key for a phone number."""
    if not raw:
        return ""
    digits = _NON_DIGIT.sub("", raw)
    if len(digits) == 13 and digits.startswith("86"):
        digits = digits[2:]
    if len(digits) == 12 and digits.startswith("0086"):
        digits = digits[4:]
    return digits


def display_peer(raw: Optional[str]) -> str:
    return (raw or "").strip() or "未知号码"


def bump_unread(peer: str, device_id: Optional[int], at: int) -> None:
    key = peer_key(peer)
    if not key:
        return
    now = security.now_epoch()
    with connection() as c:
        row = c.execute(
            "SELECT id FROM conversations WHERE peer = ? AND (device_id IS ? OR device_id = ?)",
            (key, device_id, device_id),
        ).fetchone()
        if row:
            c.execute(
                "UPDATE conversations SET unread = unread + 1, last_at = ? WHERE id = ?",
                (max(at, now), row["id"]),
            )
        else:
            c.execute(
                "INSERT INTO conversations (peer, device_id, last_at, unread, archived)"
                " VALUES (?, ?, ?, 1, 0)",
                (key, device_id, max(at, now)),
            )


def touch_thread(peer: str, device_id: Optional[int], at: int) -> None:
    """Record outbound activity without changing the unread counter."""
    key = peer_key(peer)
    if not key:
        return
    with connection() as c:
        c.execute(
            "INSERT INTO conversations (peer, device_id, last_at, unread, archived)"
            " VALUES (?, ?, ?, 0, 0)"
            " ON CONFLICT(peer, device_id) DO UPDATE SET last_at = MAX(conversations.last_at, excluded.last_at)",
            (key, device_id, at),
        )


def mark_read(peer: str, device_id: Optional[int] = None) -> int:
    key = peer_key(peer)
    if not key:
        return 0
    with connection() as c:
        if device_id is None:
            cur = c.execute("UPDATE conversations SET unread = 0 WHERE peer = ?", (key,))
        else:
            cur = c.execute(
                "UPDATE conversations SET unread = 0 WHERE peer = ? AND device_id IS ?",
                (key, device_id),
            )
        return int(cur.rowcount or 0)


def set_draft(peer: str, device_id: Optional[int], draft: Optional[str]) -> None:
    key = peer_key(peer)
    if not key:
        return
    with connection() as c:
        c.execute(
            "INSERT INTO conversations (peer, device_id, last_at, unread, archived, draft)"
            " VALUES (?, ?, ?, 0, 0, ?)"
            " ON CONFLICT(peer, device_id) DO UPDATE SET draft = excluded.draft",
            (key, device_id, security.now_epoch(), draft),
        )


_UNION_SQL = """
SELECT sender AS peer, content, received_at AS at, 'in' AS direction,
       id AS message_id, device_id, decode_error AS error
FROM sms_inbox
UNION ALL
SELECT recipient AS peer, content, COALESCE(sent_at, queued_at) AS at,
       CASE WHEN status = 'sent' THEN 'out' ELSE 'out_pending' END AS direction,
       id AS message_id, device_id, 0 AS error
FROM sms_outbox
"""


def _fetch_all(limit: int = 5000, device_id: Optional[int] = None) -> List[sqlite3.Row]:
    sql = f"SELECT * FROM ({_UNION_SQL})"
    params: list[object] = []
    if device_id is not None:
        sql += " WHERE device_id = ?"
        params.append(device_id)
    sql += " ORDER BY at DESC LIMIT ?"
    params.append(limit)
    with connection() as c:
        return list(c.execute(sql, params).fetchall())


def list_conversations(
    limit: int = 100,
    device_id: Optional[int] = None,
    query: Optional[str] = None,
    contacts: Optional[List[dict]] = None,
) -> List[dict]:
    """Merge both directions into one row per peer, newest thread first."""
    rows = _fetch_all(limit=20000, device_id=device_id)
    meta: Dict[str, dict] = {}
    with connection() as c:
        for row in c.execute("SELECT peer, device_id, unread, draft, last_at FROM conversations").fetchall():
            meta.setdefault(row["peer"], {"unread": 0, "draft": None, "last_at": row["last_at"]})
            slot = meta[row["peer"]]
            slot["unread"] = max(int(slot["unread"] or 0), int(row["unread"] or 0))
            if row["draft"]:
                slot["draft"] = row["draft"]

    contact_names: Dict[str, str] = {}
    for contact in contacts or []:
        key = peer_key(contact.get("number"))
        if key and key not in contact_names:
            contact_names[key] = contact.get("name") or ""

    grouped: Dict[str, dict] = {}
    for row in rows:
        key = peer_key(row["peer"])
        if not key:
            continue
        slot = grouped.get(key)
        if slot is None:
            info = meta.get(key, {})
            slot = {
                "peer_key": key,
                "peer": display_peer(row["peer"]),
                "name": contact_names.get(key) or None,
                "last_content": row["content"],
                "last_at": row["at"],
                "last_direction": row["direction"],
                "unread": int(info.get("unread") or 0),
                "draft": info.get("draft"),
                "message_count": 0,
                "device_id": row["device_id"],
            }
            grouped[key] = slot
        slot["message_count"] += 1

    if query:
        needle = query.strip().lower()
        grouped = {
            key: slot
            for key, slot in grouped.items()
            if needle in key or needle in (slot["name"] or "").lower() or needle in slot["peer"].lower()
        }

    items = sorted(grouped.values(), key=lambda s: s["last_at"], reverse=True)
    return items[: max(1, limit)]


def get_thread(
    peer: str,
    limit: int = 200,
    before: Optional[int] = None,
    device_id: Optional[int] = None,
) -> List[dict]:
    """Return one conversation oldest-first, ready for a chat view."""
    key = peer_key(peer)
    rows = _fetch_all(limit=20000, device_id=device_id)
    matched = [r for r in rows if peer_key(r["peer"]) == key]
    if before is not None:
        matched = [r for r in matched if r["at"] < before]
    matched.sort(key=lambda r: (r["at"], r["message_id"]))
    tail = matched[-max(1, limit) :]
    return [
        {
            "id": r["message_id"],
            "direction": r["direction"],
            "peer": display_peer(r["peer"]),
            "content": r["content"],
            "at": r["at"],
            "device_id": r["device_id"],
            "failed": r["direction"] == "out_pending",
        }
        for r in tail
    ]


def search(query: str, limit: int = 50) -> List[dict]:
    """Full text-ish search across both directions."""
    needle = f"%{query.strip()}%"
    sql = f"SELECT * FROM ({_UNION_SQL}) WHERE content LIKE ? OR peer LIKE ? ORDER BY at DESC LIMIT ?"
    with connection() as c:
        rows = c.execute(sql, (needle, needle, limit)).fetchall()
    return [
        {
            "id": r["message_id"],
            "direction": r["direction"],
            "peer": display_peer(r["peer"]),
            "peer_key": peer_key(r["peer"]),
            "content": r["content"],
            "at": r["at"],
            "device_id": r["device_id"],
        }
        for r in rows
    ]


def thread_stats() -> dict:
    with connection() as c:
        unread = c.execute("SELECT COALESCE(SUM(unread), 0) AS n FROM conversations").fetchone()["n"]
        threads = c.execute("SELECT COUNT(*) AS n FROM conversations").fetchone()["n"]
    return {"unread": int(unread), "threads": int(threads)}
