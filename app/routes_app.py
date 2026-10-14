"""Endpoints for the Flutter app: token auth, contacts and conversations."""
from __future__ import annotations

import asyncio
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from . import repo, repo_admin, repo_threads, security
from .auth import CurrentUser, get_current_user
from .config import settings
from .login_service import authenticate, client_ip, issue_session, user_payload
from .hub import hub

router = APIRouter(tags=["app"])


# ---------------------------------------------------------------------------
# Token authentication
# ---------------------------------------------------------------------------


class LoginBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)
    code: str = ""
    recovery: str = ""


@router.post("/api/v1/auth/login")
async def auth_login(body: LoginBody, request: Request):
    result = await authenticate(request, body.username, body.password, body.code, body.recovery)
    if not result.ok:
        raise HTTPException(result.status_code, {"error": {"code": "login_failed", "message": result.error}})
    sid, max_age = await issue_session(result.user, request, mobile=True)
    return {
        "token": sid,
        "token_type": "bearer",
        "expires_in": max_age,
        "must_change_password": result.must_change_password,
        "user": user_payload(result.user),
        "server": {
            "name": "VoiceSMS Gateway",
            "version": "1.0.0",
            "voice_enabled": settings.voice_enabled,
        },
    }


@router.post("/api/v1/auth/logout")
async def auth_logout(request: Request, user: CurrentUser = Depends(get_current_user)):
    await asyncio.to_thread(repo.delete_session, user.sid)
    return {"ok": True}


@router.get("/api/v1/auth/me")
async def auth_me(request: Request, user: CurrentUser = Depends(get_current_user)):
    row = await asyncio.to_thread(repo.get_user_by_id, user.id)
    devices = await asyncio.to_thread(repo.list_devices)
    calls = getattr(request.app.state, "calls", None)
    audio = getattr(request.app.state, "audio", None)
    return {
        "user": repo_admin.serialize_user(row),
        "capabilities": {
            "voice": settings.voice_enabled,
            "sms": True,
            "ai": settings.ai_enabled,
            "admin": user.is_admin,
        },
        "devices": [repo.serialize_device(d) for d in devices],
        "active_calls": [s.to_dict() for s in calls.active_sessions()] if calls else [],
        "audio": {"backend": audio.backend, "output_device": audio.output_device} if audio else None,
    }


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------


class ContactBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    number: str = Field(..., min_length=2, max_length=32)
    group_name: Optional[str] = Field(default=None, max_length=32)
    note: Optional[str] = Field(default=None, max_length=256)
    favorite: bool = False


class ContactPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    number: Optional[str] = Field(default=None, min_length=2, max_length=32)
    group_name: Optional[str] = Field(default=None, max_length=32)
    note: Optional[str] = Field(default=None, max_length=256)
    favorite: Optional[bool] = None


@router.get("/app/contacts")
async def contacts_list(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    q: Optional[str] = None,
    group: Optional[str] = None,
):
    rows = await asyncio.to_thread(repo_admin.list_contacts, user.id, q, group)
    groups = await asyncio.to_thread(repo_admin.list_contact_groups, user.id)
    return {"items": [repo_admin.serialize_contact(r) for r in rows], "groups": groups}


@router.post("/app/contacts", status_code=201)
async def contacts_create(body: ContactBody, request: Request, user: CurrentUser = Depends(get_current_user)):
    row = await asyncio.to_thread(
        repo_admin.create_contact,
        body.name,
        body.number,
        owner_user_id=user.id,
        group_name=body.group_name,
        note=body.note,
        favorite=body.favorite,
    )
    return repo_admin.serialize_contact(row)


@router.patch("/app/contacts/{contact_id}")
async def contacts_update(
    contact_id: int, body: ContactPatch, request: Request, user: CurrentUser = Depends(get_current_user)
):
    row = await asyncio.to_thread(
        repo_admin.update_contact,
        contact_id,
        name=body.name,
        number=body.number,
        group_name=body.group_name,
        note=body.note,
        favorite=body.favorite,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "contact not found")
    return repo_admin.serialize_contact(row)


@router.delete("/app/contacts/{contact_id}")
async def contacts_delete(contact_id: int, request: Request, user: CurrentUser = Depends(get_current_user)):
    if not await asyncio.to_thread(repo_admin.delete_contact, contact_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "contact not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


class ConversationSelection(BaseModel):
    peers: List[str] = Field(..., min_length=1, max_length=500)


async def _sms_change(request: Request, user: CurrentUser, action: str, payload: dict):
    await asyncio.to_thread(repo.audit, "user", user.id, user.username, client_ip(request),
                            action, str(payload.get("peers", [])), 200)
    await hub.publish("sms.conversations_changed", payload)


@router.post("/app/conversations/bulk_delete")
async def conversations_delete(body: ConversationSelection, request: Request,
                               user: CurrentUser = Depends(get_current_user)):
    if any(not repo_threads.peer_key(peer) or len(peer) > 64 for peer in body.peers):
        raise HTTPException(400, "请选择有效的短信会话")
    try:
        result = await asyncio.to_thread(repo_threads.delete_conversations, body.peers)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    await _sms_change(request, user, "sms.delete_conversations", result)
    return result


@router.post("/app/conversations/delete_read")
async def conversations_delete_read(request: Request, user: CurrentUser = Depends(get_current_user)):
    result = await asyncio.to_thread(repo_threads.delete_conversations, read_only=True)
    await _sms_change(request, user, "sms.delete_read", result)
    return result


@router.delete("/app/conversations/{peer}")
async def conversation_delete(peer: str, request: Request, user: CurrentUser = Depends(get_current_user)):
    return await conversations_delete(ConversationSelection(peers=[peer]), request, user)


@router.post("/app/conversations/{peer}/unread")
async def conversation_unread(peer: str, request: Request, user: CurrentUser = Depends(get_current_user)):
    if not await asyncio.to_thread(repo_threads.mark_unread, peer):
        raise HTTPException(404, "会话不存在")
    await _sms_change(request, user, "sms.mark_unread", {"peers": [repo_threads.peer_key(peer)]})
    return {"ok": True}


@router.get("/app/conversations")
async def conversations_list(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    limit: int = 50,
    device_id: Optional[int] = None,
    q: Optional[str] = None,
):
    if limit < 1 or limit > 500:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "limit must be 1..500")
    contact_rows = await asyncio.to_thread(repo_admin.list_contacts, user.id)
    contacts = [repo_admin.serialize_contact(r) for r in contact_rows]
    items = await asyncio.to_thread(repo_threads.list_conversations, limit, device_id, q, contacts)
    stats = await asyncio.to_thread(repo_threads.thread_stats)
    return {"items": items, "count": len(items), "unread_total": stats["unread"]}


@router.get("/app/conversations/{peer}/messages")
async def conversation_messages(
    peer: str,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    limit: int = 100,
    before: Optional[int] = None,
    device_id: Optional[int] = None,
    mark_read: bool = True,
):
    if limit < 1 or limit > 500:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "limit must be 1..500")
    messages = await asyncio.to_thread(repo_threads.get_thread, peer, limit, before, device_id)
    if mark_read:
        cleared = await asyncio.to_thread(repo_threads.mark_read, peer, device_id)
        if cleared:
            await hub.publish("sms.conversations_changed", {"peers": [repo_threads.peer_key(peer)]})
    return {"peer": peer, "peer_key": repo_threads.peer_key(peer), "items": messages, "count": len(messages)}


@router.post("/app/conversations/{peer}/read")
async def conversation_read(peer: str, request: Request, user: CurrentUser = Depends(get_current_user)):
    cleared = await asyncio.to_thread(repo_threads.mark_read, peer)
    if cleared:
        await _sms_change(request, user, "sms.mark_read", {"peers": [repo_threads.peer_key(peer)]})
    return {"ok": True, "cleared": cleared}


class DraftBody(BaseModel):
    draft: Optional[str] = Field(default=None, max_length=4096)


@router.post("/app/conversations/{peer}/draft")
async def conversation_draft(
    peer: str, body: DraftBody, request: Request, user: CurrentUser = Depends(get_current_user)
):
    await asyncio.to_thread(repo_threads.set_draft, peer, None, body.draft)
    return {"ok": True}


@router.get("/app/search")
async def global_search(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    q: str = "",
    limit: int = 50,
):
    query = q.strip()
    if not query:
        return {"messages": [], "contacts": []}
    messages = await asyncio.to_thread(repo_threads.search, query, min(limit, 200))
    contact_rows = await asyncio.to_thread(repo_admin.list_contacts, user.id, query, None)
    return {
        "messages": messages,
        "contacts": [repo_admin.serialize_contact(r) for r in contact_rows],
    }
