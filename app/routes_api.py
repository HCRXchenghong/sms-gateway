"""HTTP API protected by API Key."""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from . import repo
from .auth import CurrentApiKey, require_scope_read, require_scope_send
from .config import settings
from .modem import ModemError
from .rate_limit import WindowLimiter

router = APIRouter(prefix="/api/v1", tags=["api"])

_send_limiter = WindowLimiter(limit=settings.api_send_rate_limit_per_minute, window_seconds=60)


class SendSmsRequest(BaseModel):
    to: str = Field(..., min_length=1, max_length=32)
    content: str = Field(..., min_length=1, max_length=4096)

    @field_validator("to")
    @classmethod
    def validate_to(cls, v: str) -> str:
        digits = v[1:] if v.startswith("+") else v
        if not digits.isdigit():
            raise ValueError("recipient must be digits with optional leading '+'")
        return v


class SendSmsResponse(BaseModel):
    message_id: int
    status: str
    segments: int


@router.get("/health")
async def health(request: Request):
    modem = request.app.state.modem
    return {
        "status": "ok" if modem.connected else "degraded",
        "modem": "connected" if modem.connected else "disconnected",
        "modem_port": modem.port,
        "uptime_seconds": int(time.monotonic()),
    }


@router.post("/sms/send", response_model=SendSmsResponse)
async def api_send_sms(
    body: SendSmsRequest,
    request: Request,
    key: CurrentApiKey = Depends(require_scope_send),
):
    allowed, retry = _send_limiter.check(f"key:{key.id}")
    if not allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": {"code": "rate_limited", "message": "too many requests"}},
            headers={"Retry-After": str(retry)},
        )
    out_id = await asyncio.to_thread(
        repo.queue_outbox, body.to, body.content, None, key.id
    )
    modem = request.app.state.modem
    try:
        segments = await modem.send_sms(body.to, body.content)
    except ModemError as e:
        await asyncio.to_thread(repo.mark_outbox, out_id, "failed", 1, str(e))
        await asyncio.to_thread(
            repo.audit, "api_key", key.id, key.prefix, _client_ip(request), "sms.send", str(out_id), 502
        )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail={"error": {"code": "modem_error", "message": str(e)}},
        )
    await asyncio.to_thread(repo.mark_outbox, out_id, "sent", segments, None)
    await asyncio.to_thread(
        repo.audit, "api_key", key.id, key.prefix, _client_ip(request), "sms.send", str(out_id), 200
    )
    return SendSmsResponse(message_id=out_id, status="sent", segments=segments)


@router.get("/sms/inbox")
async def api_inbox(
    request: Request,
    since: Optional[int] = None,
    limit: int = 50,
    key: CurrentApiKey = Depends(require_scope_read),
):
    if limit < 1 or limit > 500:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"error": {"code": "bad_limit", "message": "limit must be 1..500"}},
        )
    rows = await asyncio.to_thread(repo.list_inbox, limit, since)
    items = [
        {
            "id": r["id"],
            "sender": r["sender"],
            "content": r["content"],
            "received_at": r["received_at"],
            "stored_at": r["stored_at"],
            "decode_error": bool(r["decode_error"]),
        }
        for r in rows
    ]
    await asyncio.to_thread(
        repo.audit, "api_key", key.id, key.prefix, _client_ip(request), "sms.list", None, 200
    )
    return {"items": items, "count": len(items)}


@router.get("/sms/messages/{message_id}")
async def api_message(
    message_id: int,
    request: Request,
    key: CurrentApiKey = Depends(require_scope_read),
):
    row = await asyncio.to_thread(repo.get_message, message_id)
    if not row:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "not_found", "message": "message not found"}},
        )
    return {
        "id": row["id"],
        "sender": row["sender"],
        "content": row["content"],
        "received_at": row["received_at"],
        "stored_at": row["stored_at"],
        "raw_pdu": row["raw_pdu"],
        "decode_error": bool(row["decode_error"]),
    }


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
