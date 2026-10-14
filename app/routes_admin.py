"""Administrator console: accounts, audit trail, runtime settings."""
from __future__ import annotations

import asyncio
import re
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from . import repo, repo_admin, repo_calls, security
from .auth import CurrentUser, get_current_user, require_role
from .config import settings
from .db import connection

router = APIRouter(tags=["admin"])

AdminUser = Depends(require_role("admin"))

# Settings the console may change at runtime without a restart.
EDITABLE_SETTINGS = {
    "audio.output_device": str,
    "audio.input_device": str,
    "audio.prompt_volume": float,
    "audio.backend": str,
    "audio.record_enabled": bool,
    "voice.tts_mode": int,
    "voice.prompt_into_call": bool,
    "voice.max_concurrent_calls": int,
    "ai.enabled": bool,
    "ai.provider": str,
    "ai.chat.base_url": str,
    "ai.chat.model": str,
    "ai.chat.system_prompt": str,
    "ai.asr.base_url": str,
    "ai.asr.model": str,
    "ai.tts.base_url": str,
    "ai.tts.model": str,
    "ai.tts.voice": str,
    "ai.tts.engine": str,
    "ai.tts.format": str,
    "ai.auto_reply_sms": bool,
    "ai.auto_answer_call": bool,
    # Endpoint pool behaviour
    "ai.failover_enabled": bool,
    "ai.breaker.failure_threshold": int,
    "ai.breaker.cooldown_seconds": float,
    "ai.call_log_retention_hours": int,
    "ai.call_max_seconds": int,
    # Real-time translation
    "translate.mode": str,
    "translate.cadence": str,
    "translate.my_language": str,
    "translate.peer_language": str,
    "translate.stream_degrade_to_sentence": bool,
    # Recording, transcript and minutes are three independent switches
    "recording.policy": str,
    "recording.announce": bool,
    "recording.default_enabled": bool,
    "transcript.save": bool,
    "summary.save": bool,
    "summary.auto_generate": bool,
    # Inbound handling
    "inbound.mode": str,
    "inbound.auto_answer_delay_seconds": float,
    "inbound.ai_max_seconds": int,
}

SECRET_SETTINGS = {
    "ai.chat.api_key",
    "ai.asr.api_key",
    "ai.tts.api_key",
}


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


class CreateUserBody(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=8, max_length=128)
    role: str = Field(default="operator")
    display_name: Optional[str] = Field(default=None, max_length=64)
    must_change_password: bool = True


class UpdateUserBody(BaseModel):
    role: Optional[str] = None
    display_name: Optional[str] = None
    is_active: Optional[bool] = None


class ResetPasswordBody(BaseModel):
    password: str = Field(..., min_length=8, max_length=128)
    must_change_password: bool = True


class ChangeOwnPasswordBody(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=8, max_length=128)


@router.get("/app/admin/users")
async def admin_list_users(request: Request, user: CurrentUser = AdminUser):
    rows = await asyncio.to_thread(repo_admin.list_users)
    return {"items": [repo_admin.serialize_user(r) for r in rows], "count": len(rows)}


@router.post("/app/admin/users", status_code=201)
async def admin_create_user(body: CreateUserBody, request: Request, user: CurrentUser = AdminUser):
    username_error = security.validate_username(body.username)
    if username_error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, username_error)
    password_error = security.validate_password(body.password)
    if password_error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, password_error)
    if body.role not in repo_admin.ROLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"role must be one of {list(repo_admin.ROLES)}")
    if await asyncio.to_thread(repo_admin.username_taken, body.username):
        raise HTTPException(status.HTTP_409_CONFLICT, "用户名已存在")
    password_hash = await asyncio.to_thread(security.hash_password, body.password)
    new_id = await asyncio.to_thread(
        repo_admin.create_user,
        body.username,
        password_hash,
        role=body.role,
        display_name=body.display_name,
        must_change_password=body.must_change_password,
    )
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request), "user.create", body.username, 201
    )
    row = await asyncio.to_thread(repo.get_user_by_id, new_id)
    return repo_admin.serialize_user(row)


@router.patch("/app/admin/users/{user_id}")
async def admin_update_user(user_id: int, body: UpdateUserBody, request: Request, user: CurrentUser = AdminUser):
    target = await asyncio.to_thread(repo.get_user_by_id, user_id)
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")

    if body.role is not None:
        if body.role not in repo_admin.ROLES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"role must be one of {list(repo_admin.ROLES)}")
        demoting_last_admin = (
            target["role"] == "admin" and body.role != "admin" and repo_admin.count_active_admins(user_id) == 0
        )
        if demoting_last_admin:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "必须保留至少一个启用的管理员账号")
        await asyncio.to_thread(repo_admin.set_user_role, user_id, body.role)

    if body.is_active is not None:
        if user_id == user.id and not body.is_active:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能停用自己的账号")
        if not body.is_active and target["role"] == "admin" and repo_admin.count_active_admins(user_id) == 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "必须保留至少一个启用的管理员账号")
        await asyncio.to_thread(repo_admin.set_user_active, user_id, body.is_active)

    if body.display_name is not None:
        await asyncio.to_thread(repo_admin.set_user_profile, user_id, body.display_name or None)

    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request), "user.update", str(user_id), 200
    )
    row = await asyncio.to_thread(repo.get_user_by_id, user_id)
    return repo_admin.serialize_user(row)


@router.post("/app/admin/users/{user_id}/password")
async def admin_reset_password(
    user_id: int, body: ResetPasswordBody, request: Request, user: CurrentUser = AdminUser
):
    target = await asyncio.to_thread(repo.get_user_by_id, user_id)
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    password_error = security.validate_password(body.password)
    if password_error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, password_error)
    password_hash = await asyncio.to_thread(security.hash_password, body.password)
    await asyncio.to_thread(
        repo_admin.set_password, user_id, password_hash, must_change=body.must_change_password
    )
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request), "user.reset_password", str(user_id), 200
    )
    return {"ok": True, "must_change_password": body.must_change_password}


@router.post("/app/admin/users/{user_id}/totp/reset")
async def admin_reset_totp(user_id: int, request: Request, user: CurrentUser = AdminUser):
    target = await asyncio.to_thread(repo.get_user_by_id, user_id)
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    await asyncio.to_thread(repo_admin.disable_totp, user_id)
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request), "user.totp_reset", str(user_id), 200
    )
    return {"ok": True, "totp_enabled": False}


@router.delete("/app/admin/users/{user_id}")
async def admin_delete_user(user_id: int, request: Request, user: CurrentUser = AdminUser):
    if user_id == user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能删除自己的账号")
    target = await asyncio.to_thread(repo.get_user_by_id, user_id)
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    if target["role"] == "admin" and repo_admin.count_active_admins(user_id) == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "必须保留至少一个启用的管理员账号")
    await asyncio.to_thread(repo_admin.delete_user, user_id)
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request), "user.delete", target["username"], 200
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Self service
# ---------------------------------------------------------------------------


@router.get("/app/account/me")
async def account_me(request: Request, user: CurrentUser = Depends(get_current_user)):
    row = await asyncio.to_thread(repo.get_user_by_id, user.id)
    payload = repo_admin.serialize_user(row)
    payload["session_expires_at"] = None
    return payload


@router.post("/app/account/password")
async def account_change_password(
    body: ChangeOwnPasswordBody, request: Request, user: CurrentUser = Depends(get_current_user)
):
    row = await asyncio.to_thread(repo.get_user_by_id, user.id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    if not await asyncio.to_thread(security.verify_password, body.current_password, row["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "当前密码不正确")
    password_error = security.validate_password(body.new_password)
    if password_error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, password_error)
    if await asyncio.to_thread(security.verify_password, body.new_password, row["password_hash"]):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "新密码不能与当前密码相同")
    password_hash = await asyncio.to_thread(security.hash_password, body.new_password)
    await asyncio.to_thread(repo_admin.set_password, user.id, password_hash, must_change=False)
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request), "account.password", None, 200
    )
    return {"ok": True, "note": "所有会话已失效，请重新登录"}


# ---------------------------------------------------------------------------
# Audit and settings
# ---------------------------------------------------------------------------


@router.get("/app/admin/audit")
async def admin_audit(request: Request, user: CurrentUser = AdminUser, limit: int = 100, action: Optional[str] = None):
    if limit < 1 or limit > 1000:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "limit must be 1..1000")
    with connection() as c:
        if action:
            rows = c.execute(
                "SELECT * FROM audit_log WHERE action LIKE ? ORDER BY at DESC LIMIT ?", (f"%{action}%", limit)
            ).fetchall()
        else:
            rows = c.execute("SELECT * FROM audit_log ORDER BY at DESC LIMIT ?", (limit,)).fetchall()
    return {"items": [dict(r) for r in rows], "count": len(rows)}


@router.get("/app/admin/settings")
async def admin_get_settings(request: Request, user: CurrentUser = AdminUser):
    stored = await asyncio.to_thread(repo_admin.list_settings)
    effective: dict = {}
    for key, caster in EDITABLE_SETTINGS.items():
        raw = stored.get(key)
        if raw is None:
            effective[key] = _default_for(key)
            continue
        effective[key] = _coerce(raw, caster)
    for key in SECRET_SETTINGS:
        effective[key] = "set" if stored.get(key) == "***" else ""
    return {"settings": effective, "editable": sorted(EDITABLE_SETTINGS), "secrets": sorted(SECRET_SETTINGS)}


class SettingsBody(BaseModel):
    settings: dict


@router.put("/app/admin/settings")
async def admin_put_settings(body: SettingsBody, request: Request, user: CurrentUser = AdminUser):
    applied: dict = {}
    for key, value in body.settings.items():
        if key in SECRET_SETTINGS:
            if value in (None, ""):
                await asyncio.to_thread(repo_admin.set_secret, key, None)
                applied[key] = "cleared"
            else:
                await asyncio.to_thread(repo_admin.set_secret, key, str(value))
                applied[key] = "set"
            continue
        if key not in EDITABLE_SETTINGS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"setting {key} is not editable")
        caster = EDITABLE_SETTINGS[key]
        try:
            coerced = _coerce(value, caster)
        except (TypeError, ValueError):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid value for {key}")
        await asyncio.to_thread(repo_admin.set_setting, key, _stringify(coerced, caster))
        applied[key] = coerced
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request), "settings.update",
        ",".join(sorted(applied)), 200,
    )
    await _apply_settings(request)
    return {"ok": True, "applied": applied}


async def _apply_settings(request: Request) -> None:
    """Push persisted settings into the running process.

    Every editable key maps onto the settings singleton by turning dots into
    underscores, so code reading settings.translate_mode sees a console change
    without a restart. Audio and AI additionally rebuild their clients, and the
    endpoint router reloads its pool.
    """
    stored = await asyncio.to_thread(repo_admin.list_settings)
    for key, caster in EDITABLE_SETTINGS.items():
        raw = stored.get(key)
        if raw is None:
            continue
        attr = key.replace(".", "_")
        if not hasattr(settings, attr):
            continue
        try:
            setattr(settings, attr, _coerce(raw, caster))
        except (TypeError, ValueError):
            continue
    audio = getattr(request.app.state, "audio", None)
    if audio is not None:
        await audio.load()
    ai = getattr(request.app.state, "ai", None)
    if ai is not None:
        await ai.reload()


def _default_for(key: str):
    mapping = {
        "audio.output_device": settings.audio_output_device,
        "audio.input_device": settings.audio_input_device,
        "audio.prompt_volume": settings.audio_prompt_volume,
        "audio.backend": settings.audio_backend,
        "audio.record_enabled": settings.audio_record_enabled,
        "voice.tts_mode": settings.voice_tts_mode,
        "voice.prompt_into_call": settings.voice_prompt_into_call,
        "voice.max_concurrent_calls": settings.voice_max_concurrent_calls,
        "ai.enabled": settings.ai_enabled,
        "ai.provider": settings.ai_provider,
        "ai.chat.base_url": settings.ai_chat_base_url,
        "ai.chat.model": settings.ai_chat_model,
        "ai.chat.system_prompt": settings.ai_chat_system_prompt,
        "ai.asr.base_url": settings.ai_asr_base_url,
        "ai.asr.model": settings.ai_asr_model,
        "ai.tts.base_url": settings.ai_tts_base_url,
        "ai.tts.model": settings.ai_tts_model,
        "ai.tts.voice": settings.ai_tts_voice,
        "ai.tts.engine": settings.ai_tts_engine,
        "ai.tts.format": settings.ai_tts_format,
        "ai.auto_reply_sms": settings.ai_auto_reply_sms,
        "ai.auto_answer_call": settings.ai_auto_answer_call,
    }
    if key in mapping:
        return mapping[key]
    # Keys added later follow the dotted-name convention, so they resolve
    # without another entry in the table above.
    return getattr(settings, key.replace(".", "_"), None)


def _coerce(raw, caster):
    if caster is bool:
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if caster is float:
        return float(raw)
    if caster is int:
        return int(float(raw))
    return str(raw)


def _stringify(value, caster) -> str:
    if caster is bool:
        return "1" if value else "0"
    return str(value)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@router.get("/app/admin/stats")
async def admin_stats(request: Request, user: CurrentUser = Depends(get_current_user)):
    devices = await asyncio.to_thread(repo.list_devices)
    call_stats = await asyncio.to_thread(repo_calls.call_stats, 7)
    users = await asyncio.to_thread(repo_admin.list_users)
    with connection() as c:
        inbox = c.execute("SELECT COUNT(*) AS n FROM sms_inbox").fetchone()["n"]
        outbox = c.execute("SELECT COUNT(*) AS n FROM sms_outbox").fetchone()["n"]
    calls = getattr(request.app.state, "calls", None)
    return {
        "devices_total": len(devices),
        "devices_online": sum(1 for d in devices if d["status"] == "online"),
        "sms_inbox": int(inbox),
        "sms_outbox": int(outbox),
        "calls": call_stats,
        "active_calls": len(calls.active_sessions()) if calls else 0,
        "users_total": len(users),
        "users_active": sum(1 for u in users if u["is_active"]),
    }
