"""Web routes: setup wizard, login, dashboard, websocket, device and key mgmt."""
from __future__ import annotations

import asyncio
import base64
import io
import re
from pathlib import Path
from typing import Optional

import qrcode
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

from . import repo, security
from .auth import CurrentUser, get_current_user, get_optional_user
from .config import settings
from .hub import hub
from .modem import ModemError

router = APIRouter()

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

PASSWORD_RE = re.compile(r"^(?=.*[a-z])(?=.*[A-Z]).{6,}$")
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


def _key_payload(row) -> dict:
    allowed = repo.parse_device_ids(row["allowed_device_ids"])
    return {
        "id": row["id"],
        "label": row["label"],
        "prefix": row["prefix"],
        "scopes": (row["scopes"] or "").split(","),
        "allowed_device_ids": None if allowed is None else sorted(allowed),
        "send_limit": row["send_limit"],
        "send_count": row["send_count"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "revoked_at": row["revoked_at"],
        "last_used_at": row["last_used_at"],
    }


async def _scan_devices(request: Request) -> dict:
    scan_error = None
    modems = getattr(request.app.state, "modems", None) or getattr(request.app.state, "modem", None)
    if modems and not settings.mock_modem:
        try:
            await modems.scan_once()
        except Exception as e:
            scan_error = str(e)

    rows = await asyncio.to_thread(repo.list_devices)
    items = [repo.serialize_device(r) for r in rows]
    return {
        "items": items,
        "devices_total": len(items),
        "devices_online": sum(1 for d in items if d["status"] == "online"),
        "scan_error": scan_error,
    }


async def _clean_key_policy(scopes: list[str], allowed_device_ids: Optional[list[int]]) -> tuple[list[str], Optional[list[int]]]:
    valid = {"sms:read", "sms:send"}
    bad = [s for s in scopes if s not in valid]
    if bad:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid scopes: {bad}")
    cleaned_scopes = [s for s in ("sms:read", "sms:send") if s in set(scopes)]
    if not cleaned_scopes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "at least one scope is required")
    if allowed_device_ids is None:
        return cleaned_scopes, None
    ids = sorted({int(x) for x in allowed_device_ids})
    if not ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "select at least one SIM or choose all")
    rows = await asyncio.to_thread(repo.list_devices)
    existing = {int(r["id"]) for r in rows}
    missing = [x for x in ids if x not in existing]
    if missing:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown device ids: {missing}")
    return cleaned_scopes, ids


def _clean_key_expiry(expires_at: Optional[int]) -> Optional[int]:
    if expires_at is None:
        return None
    if expires_at <= security.now_epoch():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "expires_at must be in the future")
    return int(expires_at)


@router.get("/", include_in_schema=False)
async def root(request: Request):
    has_user = await asyncio.to_thread(repo.has_any_user)
    if not has_user:
        return RedirectResponse("/setup", status_code=302)
    user = await get_optional_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/app", status_code=302)


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
        err = "用户名需 3-32 位，仅包含字母、数字、下划线、点和短横线"
    elif password != password_confirm:
        err = "两次输入的密码不一致"
    elif not PASSWORD_RE.match(password):
        err = "密码至少 6 位，并且必须同时包含大写和小写字母"
    if err:
        return templates.TemplateResponse(
            "setup_step1.html",
            {"request": request, "error": err},
            status_code=400,
        )

    secret = security.new_totp_secret()
    pwhash = await asyncio.to_thread(security.hash_password, password)
    user_id = await asyncio.to_thread(repo.create_user_with_pending_totp, username, pwhash, secret)
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
    return templates.TemplateResponse("setup_step3.html", {"request": request, "recovery_codes": plain_codes})


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
        return templates.TemplateResponse("login.html", {"request": request, "error": "账号或密码不正确"}, status_code=401)
    if not await asyncio.to_thread(security.verify_password, password, user["password_hash"]):
        await asyncio.to_thread(repo.record_login_attempt, username, ip, False)
        return templates.TemplateResponse("login.html", {"request": request, "error": "账号或密码不正确"}, status_code=401)

    second_ok = False
    if recovery.strip():
        second_ok = await asyncio.to_thread(repo.consume_recovery_code, user["id"], recovery.strip())
    elif code.strip():
        secret = await asyncio.to_thread(repo.get_totp_secret, user["id"])
        if secret:
            second_ok = security.verify_totp(secret, code.strip())
    if not second_ok:
        await asyncio.to_thread(repo.record_login_attempt, username, ip, False)
        return templates.TemplateResponse("login.html", {"request": request, "error": "二次验证失败"}, status_code=401)

    await asyncio.to_thread(repo.record_login_attempt, username, ip, True)
    sid = await asyncio.to_thread(repo.create_session, user["id"], settings.session_max_age_seconds, ip, ua)
    await asyncio.to_thread(repo.audit, "user", user["id"], username, ip, "login", None, 200)
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


@router.get("/app", response_class=HTMLResponse)
async def app_page(request: Request, user: CurrentUser = Depends(get_current_user)):
    return templates.TemplateResponse("app.html", {"request": request, "user": user})


@router.get("/app/devices")
async def app_devices_list(user: CurrentUser = Depends(get_current_user)):
    rows = await asyncio.to_thread(repo.list_devices)
    return {"items": [repo.serialize_device(r) for r in rows]}


@router.post("/app/devices/scan")
async def app_devices_scan(request: Request, user: CurrentUser = Depends(get_current_user)):
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request), "device.scan", None, 200
    )
    return await _scan_devices(request)


class UpdateDeviceBody(BaseModel):
    label: Optional[str] = Field(default=None, max_length=64)
    sim_number_manual: Optional[str] = Field(default=None, max_length=32)


@router.patch("/app/devices/{device_id}")
async def app_device_update(
    device_id: int,
    body: UpdateDeviceBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
):
    row = await asyncio.to_thread(repo.update_device_note, device_id, body.label, body.sim_number_manual)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device not found")
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request), "device.update", str(device_id), 200
    )
    return repo.serialize_device(row)


@router.get("/app/inbox")
async def app_inbox(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    limit: int = 100,
    device_id: Optional[int] = None,
):
    if limit < 1 or limit > 500:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "limit must be 1..500")
    rows = await asyncio.to_thread(repo.list_inbox, limit, None, device_id)
    return JSONResponse(
        {
            "items": [
                {
                    "id": r["id"],
                    "device_id": r["device_id"],
                    "device": _device_from_message_row(r),
                    "sim_key": r["sim_key"],
                    "sim_number": r["sim_number"],
                    "sender": r["sender"],
                    "content": r["content"],
                    "received_at": r["received_at"],
                    "decode_error": bool(r["decode_error"]),
                }
                for r in rows
            ]
        }
    )


@router.get("/app/outbox")
async def app_outbox(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    device_id: Optional[int] = None,
):
    rows = await asyncio.to_thread(repo.list_outbox, 100, device_id)
    return JSONResponse(
        {
            "items": [
                {
                    "id": r["id"],
                    "device_id": r["device_id"],
                    "device": _device_from_message_row(r),
                    "sim_key": r["sim_key"],
                    "sim_number": r["sim_number"],
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
    device_id: Optional[int] = None


@router.post("/app/send")
async def app_send(
    request: Request,
    body: WebSendBody,
    user: CurrentUser = Depends(get_current_user),
):
    digits = body.to[1:] if body.to.startswith("+") else body.to
    if not digits.isdigit():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "recipient must be digits")
    out_id = await asyncio.to_thread(repo.queue_outbox, body.device_id, body.to, body.content, user.id, None)
    modems = request.app.state.modem
    try:
        selected_device_id, segments = await modems.send_sms(body.to, body.content, body.device_id)
    except ModemError as e:
        await asyncio.to_thread(repo.mark_outbox, out_id, "failed", 1, str(e))
        await asyncio.to_thread(
            repo.audit, "user", user.id, user.username, _client_ip(request), "sms.send", str(out_id), 502
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    await asyncio.to_thread(repo.set_outbox_device, out_id, selected_device_id)
    await asyncio.to_thread(repo.mark_outbox, out_id, "sent", segments, None)
    device = await asyncio.to_thread(repo.get_device, selected_device_id)
    await hub.publish(
        "sms.sent",
        {
            "id": out_id,
            "device_id": selected_device_id,
            "device": repo.serialize_device(device) if device else None,
            "recipient": body.to,
            "content": body.content,
            "segments": segments,
            "status": "sent",
        },
    )
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request), "sms.send", str(out_id), 200
    )
    return {"message_id": out_id, "device_id": selected_device_id, "segments": segments, "status": "sent"}


@router.get("/app/keys")
async def app_keys_list(user: CurrentUser = Depends(get_current_user)):
    rows = await asyncio.to_thread(repo.list_api_keys)
    return {"items": [_key_payload(r) for r in rows]}


class CreateKeyBody(BaseModel):
    label: str = Field(..., min_length=1, max_length=64)
    scopes: list[str] = Field(default_factory=lambda: ["sms:read", "sms:send"])
    allowed_device_ids: Optional[list[int]] = None
    send_limit: Optional[int] = Field(default=None, ge=1)
    expires_at: Optional[int] = Field(default=None, ge=1)


@router.post("/app/keys")
async def app_keys_create(
    body: CreateKeyBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
):
    scopes, allowed_device_ids = await _clean_key_policy(body.scopes, body.allowed_device_ids)
    expires_at = _clean_key_expiry(body.expires_at)
    plain, row = await asyncio.to_thread(
        repo.create_api_key,
        body.label,
        scopes,
        user.id,
        allowed_device_ids,
        body.send_limit,
        expires_at,
    )
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request), "apikey.create", str(row["id"]), 201
    )
    payload = _key_payload(row)
    payload["key"] = plain
    return payload


class UpdateKeyBody(BaseModel):
    label: str = Field(..., min_length=1, max_length=64)
    scopes: list[str] = Field(default_factory=lambda: ["sms:read", "sms:send"])
    allowed_device_ids: Optional[list[int]] = None
    send_limit: Optional[int] = Field(default=None, ge=1)
    expires_at: Optional[int] = Field(default=None, ge=1)


@router.patch("/app/keys/{key_id}")
async def app_keys_update(
    key_id: int,
    body: UpdateKeyBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
):
    scopes, allowed_device_ids = await _clean_key_policy(body.scopes, body.allowed_device_ids)
    expires_at = _clean_key_expiry(body.expires_at)
    row = await asyncio.to_thread(
        repo.update_api_key_policy,
        key_id,
        body.label,
        scopes,
        allowed_device_ids,
        body.send_limit,
        expires_at,
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "api key not found")
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request), "apikey.update", str(key_id), 200
    )
    return _key_payload(row)


@router.delete("/app/keys/{key_id}")
async def app_keys_delete(
    key_id: int,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
):
    deleted = await asyncio.to_thread(repo.delete_api_key, key_id)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "api key not found")
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request), "apikey.delete", str(key_id), 200
    )
    return {"ok": True}


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
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await hub.unregister(ws)
