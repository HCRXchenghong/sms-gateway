"""Storage for per-capability model endpoints and their call log.

Kept separate from repo_admin because the AI pool has its own lifecycle:
endpoints are ordered, enabled and disabled independently per capability, and
the call log grows fast enough to need pruning on write.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Iterable, List, Optional

from . import security
from .config import settings
from .db import connection

CAPABILITIES = ("asr", "translate", "tts", "chat", "summary")


def serialize_endpoint(row: sqlite3.Row, *, with_secret: bool = False) -> dict:
    params = {}
    try:
        params = json.loads(row["params"] or "{}")
    except (ValueError, TypeError):
        params = {}
    out = {
        "id": row["id"],
        "capability": row["capability"],
        "name": row["name"],
        "kind": row["kind"] or "api",
        "base_url": row["base_url"] or "",
        "model": row["model"] or "",
        "params": params,
        "priority": row["priority"],
        "enabled": bool(row["enabled"]),
        "supports_stream": bool(row["supports_stream"]),
        "timeout_seconds": float(row["timeout_seconds"] or settings.ai_request_timeout_seconds),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "has_api_key": bool(row["api_key_enc"]),
    }
    if with_secret and row["api_key_enc"]:
        try:
            out["api_key"] = security.decrypt_secret(row["api_key_enc"]).decode("utf-8")
        except Exception:
            out["api_key"] = ""
    return out


def list_endpoints(
    capability: Optional[str] = None,
    *,
    enabled_only: bool = False,
    with_secret: bool = False,
) -> List[dict]:
    """Endpoint rows. with_secret decrypts the API key and is for internal use
    only; the console never sees it."""
    query = "SELECT * FROM ai_endpoints"
    clauses, args = [], []
    if capability:
        clauses.append("capability = ?")
        args.append(capability)
    if enabled_only:
        clauses.append("enabled = 1")
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY capability, priority, id"
    with connection() as c:
        rows = c.execute(query, args).fetchall()
    return [serialize_endpoint(r, with_secret=with_secret) for r in rows]


def get_endpoint(endpoint_id: int) -> Optional[dict]:
    with connection() as c:
        row = c.execute("SELECT * FROM ai_endpoints WHERE id = ?", (int(endpoint_id),)).fetchone()
    return serialize_endpoint(row, with_secret=True) if row else None


def get_endpoint_row(endpoint_id: int) -> Optional[sqlite3.Row]:
    with connection() as c:
        return c.execute("SELECT * FROM ai_endpoints WHERE id = ?", (int(endpoint_id),)).fetchone()


def create_endpoint(
    *,
    capability: str,
    name: str,
    kind: str = "api",
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    params: Optional[dict] = None,
    priority: int = 100,
    enabled: bool = True,
    supports_stream: bool = False,
    timeout_seconds: Optional[float] = None,
) -> int:
    if capability not in CAPABILITIES:
        raise ValueError(f"unknown capability {capability!r}")
    now = security.now_epoch()
    enc = security.encrypt_secret(api_key.encode("utf-8")) if api_key else None
    with connection() as c:
        cur = c.execute(
            "INSERT INTO ai_endpoints"
            " (capability,name,kind,base_url,api_key_enc,model,params,priority,enabled,"
            "  supports_stream,timeout_seconds,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                capability,
                name,
                kind,
                base_url,
                enc,
                model,
                json.dumps(params or {}, ensure_ascii=False),
                int(priority),
                1 if enabled else 0,
                1 if supports_stream else 0,
                float(timeout_seconds if timeout_seconds is not None else settings.ai_request_timeout_seconds),
                now,
                now,
            ),
        )
        return int(cur.lastrowid)


def update_endpoint(endpoint_id: int, **fields) -> bool:
    """Update the named columns. api_key=None means leave the stored key alone."""
    allowed = {
        "name", "kind", "base_url", "model", "params", "priority",
        "enabled", "supports_stream", "timeout_seconds", "capability", "api_key",
    }
    sets: List[str] = []
    args: List[object] = []
    for key, value in fields.items():
        if key not in allowed or value is None:
            continue
        if key == "api_key":
            if value == "":
                sets.append("api_key_enc = NULL")
            else:
                sets.append("api_key_enc = ?")
                args.append(security.encrypt_secret(str(value).encode("utf-8")))
        elif key == "params":
            sets.append("params = ?")
            args.append(json.dumps(value or {}, ensure_ascii=False))
        elif key in ("enabled", "supports_stream"):
            sets.append(f"{key} = ?")
            args.append(1 if value else 0)
        else:
            sets.append(f"{key} = ?")
            args.append(value)
    if not sets:
        return False
    sets.append("updated_at = ?")
    args.append(security.now_epoch())
    args.append(int(endpoint_id))
    with connection() as c:
        cur = c.execute(f"UPDATE ai_endpoints SET {', '.join(sets)} WHERE id = ?", args)
        return cur.rowcount > 0


def delete_endpoint(endpoint_id: int) -> bool:
    with connection() as c:
        cur = c.execute("DELETE FROM ai_endpoints WHERE id = ?", (int(endpoint_id),))
        return cur.rowcount > 0


def count_endpoints(capability: Optional[str] = None) -> int:
    with connection() as c:
        if capability:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM ai_endpoints WHERE capability = ?", (capability,)
            ).fetchone()
        else:
            row = c.execute("SELECT COUNT(*) AS n FROM ai_endpoints").fetchone()
    return int(row["n"] or 0)


# -- call log ---------------------------------------------------------------


def log_call(
    *,
    capability: str,
    endpoint_id: Optional[int],
    endpoint_name: str,
    ok: bool,
    latency_ms: Optional[int] = None,
    error: Optional[str] = None,
    call_id: Optional[str] = None,
) -> None:
    now = security.now_epoch()
    with connection() as c:
        c.execute(
            "INSERT INTO ai_call_log"
            " (capability,endpoint_id,endpoint_name,ok,latency_ms,error,call_id,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (capability, endpoint_id, endpoint_name, 1 if ok else 0, latency_ms,
             (error or "")[:500] or None, call_id, now),
        )
        # Prune on write so the table cannot grow without bound on a busy box.
        horizon = now - int(settings.ai_call_log_retention_hours) * 3600
        c.execute("DELETE FROM ai_call_log WHERE created_at < ?", (horizon,))


def endpoint_stats(hours: int = 1) -> dict:
    """Roll up the call log per endpoint: counts, error rate, average latency."""
    horizon = security.now_epoch() - hours * 3600
    with connection() as c:
        rows = c.execute(
            "SELECT endpoint_id, endpoint_name, capability,"
            "       COUNT(*) AS calls, SUM(ok) AS oks, AVG(latency_ms) AS avg_ms,"
            "       MAX(latency_ms) AS max_ms, MAX(created_at) AS last_at"
            " FROM ai_call_log WHERE created_at >= ?"
            " GROUP BY endpoint_id, endpoint_name, capability",
            (horizon,),
        ).fetchall()
    out: dict = {}
    for row in rows:
        calls = int(row["calls"] or 0)
        oks = int(row["oks"] or 0)
        out[str(row["endpoint_id"])] = {
            "endpoint_id": row["endpoint_id"],
            "endpoint_name": row["endpoint_name"],
            "capability": row["capability"],
            "calls": calls,
            "ok": oks,
            "failed": calls - oks,
            "error_rate": round((calls - oks) / calls, 4) if calls else 0.0,
            "avg_latency_ms": int(row["avg_ms"] or 0),
            "max_latency_ms": int(row["max_ms"] or 0),
            "last_call_at": row["last_at"],
            "window_hours": hours,
        }
    return out


def recent_calls(limit: int = 100, capability: Optional[str] = None) -> List[dict]:
    limit = max(1, min(int(limit), 500))
    query = (
        "SELECT id,capability,endpoint_id,endpoint_name,ok,latency_ms,error,call_id,created_at"
        " FROM ai_call_log"
    )
    args: List[object] = []
    if capability:
        query += " WHERE capability = ?"
        args.append(capability)
    query += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with connection() as c:
        rows = c.execute(query, args).fetchall()
    return [
        {
            "id": r["id"],
            "capability": r["capability"],
            "endpoint_id": r["endpoint_id"],
            "endpoint_name": r["endpoint_name"],
            "ok": bool(r["ok"]),
            "latency_ms": r["latency_ms"],
            "error": r["error"],
            "call_id": r["call_id"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


# -- one-time migration from the flat ai.* settings -------------------------


def seed_from_legacy_settings(get_setting, get_secret) -> int:
    """Turn the old single-provider settings into endpoint rows.

    Runs only while the table is empty, so an admin who has already built a
    pool never has it overwritten. Returns how many endpoints were created.
    """
    if count_endpoints():
        return 0
    created = 0
    base = get_setting("ai.chat.base_url") or settings.ai_chat_base_url
    asr_base = get_setting("ai.asr.base_url") or settings.ai_asr_base_url
    tts_base = get_setting("ai.tts.base_url") or settings.ai_tts_base_url
    chat_key = get_secret("ai.chat.api_key") or settings.ai_chat_api_key
    asr_key = get_secret("ai.asr.api_key") or settings.ai_asr_api_key
    tts_key = get_secret("ai.tts.api_key") or settings.ai_tts_api_key
    if not any([chat_key, asr_key, tts_key]):
        return 0

    plan = (
        ("chat", "默认对话", base, chat_key, get_setting("ai.chat.model") or settings.ai_chat_model, {}, False),
        ("summary", "默认总结", base, chat_key, get_setting("ai.chat.model") or settings.ai_chat_model, {}, False),
        ("translate", "默认翻译", base, chat_key, get_setting("ai.chat.model") or settings.ai_chat_model, {}, False),
        ("asr", "默认识别", asr_base, asr_key, get_setting("ai.asr.model") or settings.ai_asr_model, {}, False),
        (
            "tts", "默认合成", tts_base, tts_key,
            get_setting("ai.tts.model") or settings.ai_tts_model,
            {
                "voice": get_setting("ai.tts.voice") or settings.ai_tts_voice,
                "format": get_setting("ai.tts.format") or settings.ai_tts_format,
            },
            False,
        ),
    )
    for capability, name, url, key, model, params, stream in plan:
        if not key and capability not in ("translate", "summary"):
            continue
        create_endpoint(
            capability=capability,
            name=name,
            kind="api",
            base_url=url or "",
            api_key=key or "",
            model=model or "",
            params=params,
            priority=100,
            enabled=True,
            supports_stream=stream,
        )
        created += 1
    return created
