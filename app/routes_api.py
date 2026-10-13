"""HTTP API protected by API Key."""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from . import repo, repo_threads
from .auth import CurrentApiKey, require_scope_read, require_scope_send
from .config import settings
from .modem import ModemError
from .rate_limit import WindowLimiter

router = APIRouter(prefix="/api/v1", tags=["api"])

_send_limiter = WindowLimiter(limit=settings.api_send_rate_limit_per_minute, window_seconds=60)


class SendSmsRequest(BaseModel):
    to: str = Field(..., min_length=1, max_length=32)
    content: str = Field(..., min_length=1, max_length=4096)
    device_id: Optional[int] = Field(default=None, description="Optional device id. Omit to use the first online device.")

    @field_validator("to")
    @classmethod
    def validate_to(cls, v: str) -> str:
        digits = v[1:] if v.startswith("+") else v
        if not digits.isdigit():
            raise ValueError("recipient must be digits with optional leading '+'")
        return v


class SendSmsResponse(BaseModel):
    message_id: int
    device_id: int
    status: str
    segments: int


@router.get("/health")
async def health(request: Request):
    modem = request.app.state.modem
    devices = await asyncio.to_thread(repo.list_devices)
    online = [d for d in devices if d["status"] == "online"]
    return {
        "status": "ok" if online else "degraded",
        "modem": "connected" if online else "disconnected",
        "modem_port": modem.port,
        "devices_total": len(devices),
        "devices_online": len(online),
        "uptime_seconds": int(time.monotonic()),
    }


@router.get("/devices")
async def api_devices(key: CurrentApiKey = Depends(require_scope_read)):
    rows = await asyncio.to_thread(repo.list_devices)
    items = [repo.serialize_device(r) for r in rows if _key_allows_device(key, r["id"])]
    return {"items": items, "count": len(items)}


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
    requested_device_id = await _resolve_send_device_id(key, body.device_id)
    if not await asyncio.to_thread(repo.reserve_api_send, key.id):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": {"code": "quota_exhausted", "message": "API key send quota exhausted"}},
        )
    out_id = await asyncio.to_thread(repo.queue_outbox, requested_device_id, body.to, body.content, None, key.id)
    modem = request.app.state.modem
    try:
        selected_device_id, segments = await modem.send_sms(body.to, body.content, requested_device_id)
    except ModemError as e:
        await asyncio.to_thread(repo.release_api_send, key.id)
        await asyncio.to_thread(repo.mark_outbox, out_id, "failed", 1, str(e))
        await asyncio.to_thread(
            repo.audit, "api_key", key.id, key.prefix, _client_ip(request), "sms.send", str(out_id), 502
        )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail={"error": {"code": "modem_error", "message": str(e)}},
        )
    await asyncio.to_thread(repo.set_outbox_device, out_id, selected_device_id)
    await asyncio.to_thread(repo.mark_outbox, out_id, "sent", segments, None)
    await asyncio.to_thread(repo_threads.touch_thread, body.to, selected_device_id, int(time.time()))
    await asyncio.to_thread(
        repo.audit, "api_key", key.id, key.prefix, _client_ip(request), "sms.send", str(out_id), 200
    )
    return SendSmsResponse(message_id=out_id, device_id=selected_device_id, status="sent", segments=segments)


@router.get("/sms/inbox")
async def api_inbox(
    request: Request,
    since: Optional[int] = None,
    limit: int = 50,
    device_id: Optional[int] = None,
    key: CurrentApiKey = Depends(require_scope_read),
):
    if limit < 1 or limit > 500:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"error": {"code": "bad_limit", "message": "limit must be 1..500"}},
        )
    if device_id is not None and not _key_allows_device(key, device_id):
        raise _device_forbidden()
    device_ids = key.allowed_device_ids if device_id is None else None
    rows = await asyncio.to_thread(repo.list_inbox, limit, since, device_id, device_ids)
    items = [
        {
            "id": r["id"],
            "device_id": r["device_id"],
            "device": _device_from_message_row(r),
            "sim_key": r["sim_key"],
            "sim_number": r["sim_number"],
            "sim_iccid": r["sim_iccid"],
            "sim_imsi": r["sim_imsi"],
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


@router.get("/sms/outbox")
async def api_outbox(
    request: Request,
    limit: int = 50,
    device_id: Optional[int] = None,
    key: CurrentApiKey = Depends(require_scope_read),
):
    if limit < 1 or limit > 500:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"error": {"code": "bad_limit", "message": "limit must be 1..500"}},
        )
    if device_id is not None and not _key_allows_device(key, device_id):
        raise _device_forbidden()
    device_ids = key.allowed_device_ids if device_id is None else None
    rows = await asyncio.to_thread(repo.list_outbox, limit, device_id, device_ids)
    items = [
        {
            "id": r["id"],
            "device_id": r["device_id"],
            "device": _device_from_message_row(r),
            "sim_key": r["sim_key"],
            "sim_number": r["sim_number"],
            "sim_iccid": r["sim_iccid"],
            "sim_imsi": r["sim_imsi"],
            "recipient": r["recipient"],
            "content": r["content"],
            "status": r["status"],
            "segments": r["segments"],
            "queued_at": r["queued_at"],
            "sent_at": r["sent_at"],
            "error": r["error"],
        }
        for r in rows
    ]
    await asyncio.to_thread(
        repo.audit, "api_key", key.id, key.prefix, _client_ip(request), "sms.outbox", None, 200
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
    if row["device_id"] is not None and not _key_allows_device(key, row["device_id"]):
        raise _device_forbidden()
    return {
        "id": row["id"],
        "device_id": row["device_id"],
        "device": _device_from_message_row(row),
        "sim_key": row["sim_key"],
        "sim_number": row["sim_number"],
        "sim_iccid": row["sim_iccid"],
        "sim_imsi": row["sim_imsi"],
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


def _key_allows_device(key: CurrentApiKey, device_id: Optional[int]) -> bool:
    if device_id is None:
        return True
    return key.allowed_device_ids is None or int(device_id) in key.allowed_device_ids


async def _resolve_send_device_id(key: CurrentApiKey, requested_device_id: Optional[int]) -> int:
    if requested_device_id is not None:
        if not _key_allows_device(key, requested_device_id):
            raise _device_forbidden()
        return requested_device_id
    rows = await asyncio.to_thread(repo.list_devices)
    for row in rows:
        if row["status"] == "online" and _key_allows_device(key, row["id"]):
            return int(row["id"])
    raise HTTPException(
        status.HTTP_502_BAD_GATEWAY,
        detail={"error": {"code": "no_allowed_online_device", "message": "no allowed online SIM is available"}},
    )


def _device_forbidden() -> HTTPException:
    return HTTPException(
        status.HTTP_403_FORBIDDEN,
        detail={"error": {"code": "device_not_allowed", "message": "API key is not allowed to use this SIM"}},
    )


def _device_from_message_row(row) -> Optional[dict]:
    if row["device_id"] is None:
        return None
    number = row["device_number"]
    label = row["device_label"]
    sim_number = row["sim_number"] or row["device_sim_number_manual"] or row["device_sim_number_auto"]
    return {
        "id": row["device_id"],
        "number": number,
        "code": f"{int(number):03d}" if number is not None else None,
        "display_name": label or (f"编号{int(number):03d}" if number is not None else None),
        "sim_key": row["sim_key"] or row["device_sim_key"],
        "sim_number": sim_number,
        "iccid": row["sim_iccid"] or row["device_iccid"],
        "imsi": row["sim_imsi"] or row["device_imsi"],
    }
