"""Auth helpers: session cookie & API key dependencies."""
from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, Request, status

from . import repo
from .config import settings


@dataclass
class CurrentUser:
    id: int
    username: str
    sid: str
    role: str = "admin"
    display_name: Optional[str] = None
    must_change_password: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


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


def _session_token(request: Request) -> Optional[str]:
    """Session id from the cookie, a bearer token, or a query parameter.

    The Flutter app cannot rely on cookies alone, so a session id may also be
    presented as Authorization: Bearer <sid>. API keys always carry the smsgw_
    prefix, which keeps the two namespaces apart.
    """
    cookie = request.cookies.get(settings.session_cookie_name)
    if cookie:
        return cookie
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token and not token.startswith("smsgw_"):
            return token
    query_token = request.query_params.get("token")
    if query_token and not query_token.startswith("smsgw_"):
        return query_token
    return None


async def get_current_user(request: Request) -> CurrentUser:
    sid = _session_token(request)
    if not sid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not_authenticated")
    row = await asyncio.to_thread(repo.get_session, sid)
    if not row:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session_expired")
    if not row["is_active"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "account_disabled")
    return CurrentUser(
        id=row["user_id"],
        username=row["username"],
        sid=sid,
        role=row["role"] or "admin",
        display_name=row["display_name"],
        must_change_password=bool(row["must_change_password"]),
    )


def require_role(*roles: str):
    """Dependency factory enforcing that the caller holds one of these roles."""
    allowed = set(roles)

    async def dependency(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "insufficient_role")
        return user

    return dependency


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


async def require_scope_voice_read(request: Request) -> CurrentApiKey:
    return await require_api_key(request, "voice:read")


async def require_scope_voice_dial(request: Request) -> CurrentApiKey:
    return await require_api_key(request, "voice:dial")


async def require_scope_ai(request: Request) -> CurrentApiKey:
    return await require_api_key(request, "ai:use")
