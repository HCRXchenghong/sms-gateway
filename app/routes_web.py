"""Web routes: setup wizard, login (with TOTP), dashboard, websocket, key mgmt."""
from __future__ import annotations

import asyncio
import base64
import io
import re
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import qrcode

from . import repo, security
from .auth import CurrentUser, get_current_user, get_optional_user
from .config import settings
from .hub import hub
from .modem import ModemError

router = APIRouter()

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


PASSWORD_RE = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)(?=.*[^A-Za-z0-9]).{12,}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _qr_data_url(text: str) -> str:
    img = qrcode.make(text)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


# =============================================================================
# Root & redirect logic
# =============================================================================


@router.get("/", include_in_schema=False)
async def root(request: Request):
    has_user = await asyncio.to_thread(repo.has_any_user)
    if not has_user:
        return RedirectResponse("/setup", status_code=302)
    user = await get_optional_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/app", status_code=302)


# =============================================================================
# Setup (first-run, single user creation + TOTP)
# =============================================================================


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    if await asyncio.to_thread(repo.has_any_user):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("setup_step1.html", {"request": request, "error": None})


@router.post("/setup", response_class=HTMLResponse)
async def setup_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
):
    if await asyncio.to_thread(repo.has_any_user):
        return RedirectResponse("/login", status_code=302)
    err = None
    if not USERNAME_RE.match(username):
        err = "用户名需 3-32 位，仅含字母/数字/下划线/点/短横"
    elif password != password_confirm:
        err = "两次输入的密码不一致"
    elif not PASSWORD_RE.match(password):
        err = "密码至少 12 位，且必须包含字母、数字、特殊字符各 1 个"
    if err:
        return templates.TemplateResponse(
            "setup_step1.html",
            {"request": request, "error": err},
            status_code=400,
        )

    secret = security.new_totp_secret()
    pwhash = await asyncio.to_thread(security.hash_password, password)
    user_id = await asyncio.to_thread(
        repo.create_user_with_pending_totp, username, pwhash, secret
    )
    uri = security.totp_provisioning_uri(secret, username)
    qr = _qr_data_url(uri)
    resp = templates.TemplateResponse(
        "setup_step2.html",
        {
            "request": request,
            "username": username,
            "secret": secret,
            "qr_data_url": qr,
            "user_id": user_id,
            "error": None,
        },
    )
    # Bind the setup confirmation step to this browser to prevent another
    # client from racing the TOTP confirmation while it's pending.
    setup_cookie = security.new_session_id()
    await asyncio.to_thread(
        repo.create_session,
        user_id,
        600,
        _client_ip(request),
        request.headers.get("user-agent", ""),
    )
    resp.set_cookie(
        "smsgw_setup",
        setup_cookie,
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return resp


@router.post("/setup/confirm", response_class=HTMLResponse)
async def setup_confirm(
    request: Request,
    user_id: int = Form(...),
    code: str = Form(...),
):
    # Confirmation only runs while the user is in the pending state.
    user = await asyncio.to_thread(repo.get_user_by_id, user_id)
    if not user or user["totp_enabled"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_setup")
    secret = await asyncio.to_thread(repo.get_totp_secret, user_id)
    if not secret or not security.verify_totp(secret, code):
        return templates.TemplateResponse(
            "setup_step2_error.html",
            {"request": request, "user_id": user_id, "error": "验证码不正确，请重试"},
            status_code=400,
        )
    await asyncio.to_thread(repo.enable_totp, user_id)
    plain_codes, hashes = security.generate_recovery_codes(settings.recovery_codes_count)
    await asyncio.to_thread(repo.reset_recovery_codes, user_id, hashes)
    return templates.TemplateResponse(
        "setup_step3.html",
        {"request": request, "recovery_codes": plain_codes},
    )


# =============================================================================
# Login / logout
# =============================================================================


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if not await asyncio.to_thread(repo.has_any_user):
        return RedirectResponse("/setup", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    code: str = Form(""),
    recovery: str = Form(""),
):
    ip = _client_ip(request)
    ua = request.headers.get("user-agent", "")
    locked = await asyncio.to_thread(
        repo.is_locked,
        username,
        ip,
        settings.login_lock_window_seconds,
        settings.login_lock_max_attempts,
    )
    if locked:
        await asyncio.to_thread(repo.record_login_attempt, username, ip, False)
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "登录失败次数过多，请稍后重试"},
            status_code=429,
        )

    user = await asyncio.to_thread(repo.get_user_by_username, username)
    if not user or not user["totp_enabled"]:
        await asyncio.to_thread(repo.record_login_attempt, username, ip, False)
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "账号或密码不正确"},
            status_code=401,
        )
    if not await asyncio.to_thread(security.verify_password, password, user["password_hash"]):
        await asyncio.to_thread(repo.record_login_attempt, username, ip, False)
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "账号或密码不正确"},
            status_code=401,
        )

    second_ok = False
    if recovery.strip():
        second_ok = await asyncio.to_thread(repo.consume_recovery_code, user["id"], recovery.strip())
    elif code.strip():
        secret = await asyncio.to_thread(repo.get_totp_secret, user["id"])
        if secret:
            second_ok = security.verify_totp(secret, code.strip())
    if not second_ok:
        await asyncio.to_thread(repo.record_login_attempt, username, ip, False)
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "二次验证失败"},
            status_code=401,
        )

    await asyncio.to_thread(repo.record_login_attempt, username, ip, True)
    sid = await asyncio.to_thread(
        repo.create_session, user["id"], settings.session_max_age_seconds, ip, ua
    )
    await asyncio.to_thread(
        repo.audit, "user", user["id"], username, ip, "login", None, 200
    )
    resp = RedirectResponse("/app", status_code=302)
    resp.set_cookie(
        settings.session_cookie_name,
        sid,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return resp


@router.post("/logout")
async def logout(request: Request):
    sid = request.cookies.get(settings.session_cookie_name)
    if sid:
        await asyncio.to_thread(repo.delete_session, sid)
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(settings.session_cookie_name)
    return resp


# =============================================================================
# Dashboard pages (HTML; logic in JS)
# =============================================================================


@router.get("/app", response_class=HTMLResponse)
async def app_page(request: Request, user: CurrentUser = Depends(get_current_user)):
    return templates.TemplateResponse("app.html", {"request": request, "user": user})


# Inbox JSON for in-app fetch (session-authenticated)
@router.get("/app/inbox")
async def app_inbox(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    limit: int = 100,
):
    if limit < 1 or limit > 500:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "limit must be 1..500")
    rows = await asyncio.to_thread(repo.list_inbox, limit, None)
    return JSONResponse(
        {
            "items": [
                {
                    "id": r["id"],
                    "sender": r["sender"],
                    "content": r["content"],
                    "received_at": r["received_at"],
                    "decode_error": bool(r["decode_error"]),
                }
                for r in rows
            ]
        }
    )


# Outbox JSON
@router.get("/app/outbox")
async def app_outbox(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
):
    rows = await asyncio.to_thread(repo.list_outbox, 100)
    return JSONResponse(
        {
            "items": [
                {
                    "id": r["id"],
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
        }
    )


class WebSendBody(BaseModel):
    to: str = Field(..., min_length=1, max_length=32)
    content: str = Field(..., min_length=1, max_length=4096)


@router.post("/app/send")
async def app_send(
    request: Request,
    body: WebSendBody,
    user: CurrentUser = Depends(get_current_user),
):
    digits = body.to[1:] if body.to.startswith("+") else body.to
    if not digits.isdigit():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "recipient must be digits")
    out_id = await asyncio.to_thread(
        repo.queue_outbox, body.to, body.content, user.id, None
    )
    modem = request.app.state.modem
    try:
        segments = await modem.send_sms(body.to, body.content)
    except ModemError as e:
        await asyncio.to_thread(repo.mark_outbox, out_id, "failed", 1, str(e))
        await asyncio.to_thread(
            repo.audit, "user", user.id, user.username, _client_ip(request), "sms.send", str(out_id), 502
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    await asyncio.to_thread(repo.mark_outbox, out_id, "sent", segments, None)
    await hub.publish(
        "sms.sent",
        {
            "id": out_id,
            "recipient": body.to,
            "content": body.content,
            "segments": segments,
            "status": "sent",
        },
    )
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request), "sms.send", str(out_id), 200
    )
    return {"message_id": out_id, "segments": segments, "status": "sent"}


# =============================================================================
# API key management (UI)
# =============================================================================


@router.get("/app/keys")
async def app_keys_list(user: CurrentUser = Depends(get_current_user)):
    rows = await asyncio.to_thread(repo.list_api_keys)
    return {
        "items": [
            {
                "id": r["id"],
                "label": r["label"],
                "prefix": r["prefix"],
                "scopes": (r["scopes"] or "").split(","),
                "created_at": r["created_at"],
                "revoked_at": r["revoked_at"],
                "last_used_at": r["last_used_at"],
            }
            for r in rows
        ]
    }


class CreateKeyBody(BaseModel):
    label: str = Field(..., min_length=1, max_length=64)
    scopes: list[str] = Field(default_factory=lambda: ["sms:read", "sms:send"])


@router.post("/app/keys")
async def app_keys_create(
    body: CreateKeyBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
):
    valid = {"sms:read", "sms:send"}
    bad = [s for s in body.scopes if s not in valid]
    if bad:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid scopes: {bad}")
    plain, row = await asyncio.to_thread(
        repo.create_api_key, body.label, body.scopes, user.id
    )
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request), "apikey.create", str(row["id"]), 201
    )
    return {"id": row["id"], "label": row["label"], "key": plain, "scopes": body.scopes}


@router.delete("/app/keys/{key_id}")
async def app_keys_revoke(
    key_id: int,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
):
    await asyncio.to_thread(repo.revoke_api_key, key_id)
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request), "apikey.revoke", str(key_id), 200
    )
    return {"ok": True}


# =============================================================================
# WebSocket for realtime push
# =============================================================================


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    sid = ws.cookies.get(settings.session_cookie_name)
    if not sid:
        await ws.close(code=4401)
        return
    row = await asyncio.to_thread(repo.get_session, sid)
    if not row:
        await ws.close(code=4401)
        return
    await ws.accept()
    await hub.register(ws)
    try:
        while True:
            # We don't expect messages; ping to keep connection alive
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await hub.unregister(ws)
