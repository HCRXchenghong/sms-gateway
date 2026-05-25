"""Auth helpers: session cookie & API key dependencies."""
from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request, status

from . import repo
from .config import settings


@dataclass
class CurrentUser:
    id: int
    username: str
    sid: str


@dataclass
class CurrentApiKey:
    id: int
    label: str
    prefix: str
    scopes: set[str]
    allowed_device_ids: Optional[set[int]]
    send_limit: Optional[int]
    send_count: int


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def get_current_user(request: Request) -> CurrentUser:
    sid = request.cookies.get(settings.session_cookie_name)
    if not sid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not_authenticated")
    row = await asyncio.to_thread(repo.get_session, sid)
    if not row:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session_expired")
    return CurrentUser(id=row["user_id"], username=row["username"], sid=sid)


async def get_optional_user(request: Request) -> Optional[CurrentUser]:
    try:
        return await get_current_user(request)
    except HTTPException:
        return None


def _extract_api_key(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    xkey = request.headers.get("x-api-key")
    if xkey:
        return xkey.strip()
    return None


async def require_api_key(request: Request, scope: str) -> CurrentApiKey:
    plain = _extract_api_key(request)
    if not plain:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing_api_key")
    row: Optional[sqlite3.Row] = await asyncio.to_thread(repo.find_api_key_by_plaintext, plain)
    if not row:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_api_key")
    scopes = set((row["scopes"] or "").split(","))
    if scope not in scopes:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "insufficient_scope")
    await asyncio.to_thread(repo.touch_api_key, row["id"])
    return CurrentApiKey(
        id=row["id"],
        label=row["label"],
        prefix=row["prefix"],
        scopes=scopes,
        allowed_device_ids=repo.parse_device_ids(row["allowed_device_ids"]),
        send_limit=row["send_limit"],
        send_count=row["send_count"],
    )


async def require_scope_read(request: Request) -> CurrentApiKey:
    return await require_api_key(request, "sms:read")


async def require_scope_send(request: Request) -> CurrentApiKey:
    return await require_api_key(request, "sms:send")
