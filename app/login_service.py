"""Shared credential checking for the web form and the mobile token endpoint.

Both entry points must apply the same lockout policy and the same second
factor rules, so the logic lives here once. TOTP is opt-in for all accounts,
including the first administrator.
"""
from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from typing import Optional

from fastapi import Request

from . import repo, repo_admin, security
from .config import settings


@dataclass
class LoginResult:
    ok: bool
    user: Optional[sqlite3.Row] = None
    error: Optional[str] = None
    status_code: int = 401
    must_change_password: bool = False
    code: str = "login_failed"


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _check_totp(user_id: int, secret: str, code: str, *, spend: bool) -> bool:
    """Verify a TOTP code against the replay guard, optionally spending it.

    The guard records the newest time step that enrollment or a 2FA reset has
    already consumed, and only ever moves forward. A code from a spent step is
    refused, which is what stops the code shown during enrollment from being
    replayed at the login form for the rest of its 30 second window.

    Login checks the guard but does not spend the step. Spending it there would
    lock out an operator who signs in to the web console and then to the phone
    app inside the same window, which is a worse problem than the one it solves.
    """
    code = (code or "").strip()
    if not code:
        return False
    step = security.verify_totp_step(secret, code)
    if step is None:
        return False
    last = await asyncio.to_thread(repo.get_totp_last_step, user_id)
    if last is not None and step <= last:
        return False
    if spend:
        await asyncio.to_thread(repo.mark_totp_step_used, user_id, step)
    return True


async def verify_totp_once(user_id: int, secret: str, code: str) -> bool:
    """Verify and spend a TOTP step. Used by enrollment and by disabling 2FA."""
    return await _check_totp(user_id, secret, code, spend=True)


async def verify_totp_fresh(user_id: int, secret: str, code: str) -> bool:
    """Verify a TOTP code without spending its step. Used at login."""
    return await _check_totp(user_id, secret, code, spend=False)


async def authenticate(
    request: Request,
    username: str,
    password: str,
    code: str = "",
    recovery: str = "",
) -> LoginResult:
    """Verify credentials plus optional second factor, recording attempts."""
    ip = client_ip(request)
    username = (username or "").strip()

    locked = await asyncio.to_thread(
        repo.is_locked, username, ip, settings.login_lock_window_seconds, settings.login_lock_max_attempts
    )
    if locked:
        await asyncio.to_thread(repo.record_login_attempt, username, ip, False)
        return LoginResult(ok=False, error="登录失败次数过多，请稍后重试", status_code=429)

    user = await asyncio.to_thread(repo.get_user_by_username, username)
    if not user:
        await asyncio.to_thread(repo.record_login_attempt, username, ip, False)
        return LoginResult(ok=False, error="账号或密码不正确")
    if not user["is_active"]:
        await asyncio.to_thread(repo.record_login_attempt, username, ip, False)
        return LoginResult(ok=False, error="账号已被停用，请联系管理员", status_code=403)
    if not await asyncio.to_thread(security.verify_password, password, user["password_hash"]):
        await asyncio.to_thread(repo.record_login_attempt, username, ip, False)
        return LoginResult(ok=False, error="账号或密码不正确")

    if user["totp_enabled"]:
        if not (code or recovery).strip():
            return LoginResult(ok=False, error="请输入二次验证码或恢复码", code="totp_required")
        secret = await asyncio.to_thread(repo.get_totp_secret, user["id"])
        if not secret:
            # Enabled without a usable secret is a broken account, not a pass.
            await asyncio.to_thread(repo.record_login_attempt, username, ip, False)
            return LoginResult(ok=False, error="两步验证配置异常，请联系管理员", status_code=500, code="totp_broken")
        second_ok = False
        if (code or "").strip():
            second_ok = await verify_totp_fresh(user["id"], secret, code)
        if not second_ok and (recovery or "").strip():
            second_ok = await asyncio.to_thread(repo.consume_recovery_code, user["id"], recovery.strip())
        if not second_ok:
            await asyncio.to_thread(repo.record_login_attempt, username, ip, False)
            return LoginResult(ok=False, error="二次验证码不正确", code="totp_invalid")

    await asyncio.to_thread(repo.record_login_attempt, username, ip, True)
    await asyncio.to_thread(repo_admin.touch_last_login, user["id"])
    await asyncio.to_thread(repo.audit, "user", user["id"], username, ip, "login", None, 200)
    return LoginResult(
        ok=True,
        user=user,
        must_change_password=bool(user["must_change_password"]),
    )


async def issue_session(user: sqlite3.Row, request: Request, *, mobile: bool = False) -> tuple[str, int]:
    """Create a session row and return (session_id, max_age_seconds)."""
    max_age = (
        settings.mobile_session_max_age_seconds if mobile else settings.session_max_age_seconds
    )
    sid = await asyncio.to_thread(
        repo.create_session,
        user["id"],
        max_age,
        client_ip(request),
        request.headers.get("user-agent", ""),
    )
    return sid, max_age


def user_payload(user: sqlite3.Row) -> dict:
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"] or "admin",
        "display_name": user["display_name"],
        "totp_enabled": bool(user["totp_enabled"]),
        "must_change_password": bool(user["must_change_password"]),
    }
