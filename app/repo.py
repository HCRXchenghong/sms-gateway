"""Database access functions (synchronous, intended to be cheap).

The whole app uses asyncio, so where these are called from request handlers
we wrap them in ``asyncio.to_thread``.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable, List, Optional

from . import security
from .db import connection
from .pdu import IncomingSms


# ---------------------------------------------------------------------------
# Users / setup
# ---------------------------------------------------------------------------


def has_any_user() -> bool:
    with connection() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return row["n"] > 0


def get_user_by_username(username: str) -> Optional[sqlite3.Row]:
    with connection() as c:
        return c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def get_user_by_id(user_id: int) -> Optional[sqlite3.Row]:
    with connection() as c:
        return c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def create_user_with_pending_totp(username: str, password_hash: str, totp_secret: str) -> int:
    """Create a user. TOTP is created encrypted but enabled=0 until confirmed."""
    enc = security.encrypt_secret(totp_secret.encode("utf-8"))
    now = security.now_epoch()
    with connection() as c:
        cur = c.execute(
            "INSERT INTO users (username, password_hash, totp_secret_enc, totp_enabled, created_at, updated_at)"
            " VALUES (?, ?, ?, 0, ?, ?)",
            (username, password_hash, enc, now, now),
        )
        return int(cur.lastrowid)


def enable_totp(user_id: int) -> None:
    with connection() as c:
        c.execute(
            "UPDATE users SET totp_enabled = 1, updated_at = ? WHERE id = ?",
            (security.now_epoch(), user_id),
        )


def update_password(user_id: int, password_hash: str) -> None:
    with connection() as c:
        c.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (password_hash, security.now_epoch(), user_id),
        )


def replace_totp(user_id: int, secret_plain: str) -> None:
    enc = security.encrypt_secret(secret_plain.encode("utf-8"))
    with connection() as c:
        c.execute(
            "UPDATE users SET totp_secret_enc = ?, totp_enabled = 0, updated_at = ? WHERE id = ?",
            (enc, security.now_epoch(), user_id),
        )


def get_totp_secret(user_id: int) -> Optional[str]:
    with connection() as c:
        row = c.execute("SELECT totp_secret_enc FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row or not row["totp_secret_enc"]:
            return None
        return security.decrypt_secret(row["totp_secret_enc"]).decode("utf-8")


# ---------------------------------------------------------------------------
# Recovery codes
# ---------------------------------------------------------------------------


def reset_recovery_codes(user_id: int, hashes: Iterable[str]) -> None:
    with connection() as c:
        c.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user_id,))
        c.executemany(
            "INSERT INTO recovery_codes (user_id, code_hash) VALUES (?, ?)",
            [(user_id, h) for h in hashes],
        )


def consume_recovery_code(user_id: int, plain: str) -> bool:
    h = security.hash_recovery_code(plain)
    with connection() as c:
        row = c.execute(
            "SELECT id FROM recovery_codes WHERE user_id = ? AND code_hash = ? AND used_at IS NULL",
            (user_id, h),
        ).fetchone()
        if not row:
            return False
        c.execute(
            "UPDATE recovery_codes SET used_at = ? WHERE id = ?",
            (security.now_epoch(), row["id"]),
        )
        return True


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def create_session(user_id: int, ttl_seconds: int, ip: str, ua: str) -> str:
    sid = security.new_session_id()
    now = security.now_epoch()
    with connection() as c:
        c.execute(
            "INSERT INTO sessions (sid, user_id, created_at, expires_at, ip, user_agent) VALUES (?, ?, ?, ?, ?, ?)",
            (sid, user_id, now, now + ttl_seconds, ip, ua),
        )
    return sid


def get_session(sid: str) -> Optional[sqlite3.Row]:
    with connection() as c:
        return c.execute(
            "SELECT s.*, u.username FROM sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.sid = ? AND s.expires_at > ?",
            (sid, security.now_epoch()),
        ).fetchone()


def delete_session(sid: str) -> None:
    with connection() as c:
        c.execute("DELETE FROM sessions WHERE sid = ?", (sid,))


def gc_sessions() -> None:
    with connection() as c:
        c.execute("DELETE FROM sessions WHERE expires_at <= ?", (security.now_epoch(),))


# ---------------------------------------------------------------------------
# Login attempts / lockout
# ---------------------------------------------------------------------------


def record_login_attempt(username: str, ip: str, success: bool) -> None:
    with connection() as c:
        c.execute(
            "INSERT INTO login_attempts (username, ip, success, at) VALUES (?, ?, ?, ?)",
            (username, ip, 1 if success else 0, security.now_epoch()),
        )


def is_locked(username: str, ip: str, window_s: int, max_failed: int) -> bool:
    cutoff = security.now_epoch() - window_s
    with connection() as c:
        row_u = c.execute(
            "SELECT COUNT(*) AS n FROM login_attempts WHERE username = ? AND success = 0 AND at >= ?",
            (username, cutoff),
        ).fetchone()
        row_i = c.execute(
            "SELECT COUNT(*) AS n FROM login_attempts WHERE ip = ? AND success = 0 AND at >= ?",
            (ip, cutoff),
        ).fetchone()
    return (row_u["n"] >= max_failed) or (row_i["n"] >= max_failed * 2)


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------


def create_api_key(label: str, scopes: List[str], created_by: int) -> tuple[str, sqlite3.Row]:
    plaintext, salt, h, prefix = security.generate_api_key()
    now = security.now_epoch()
    scopes_str = ",".join(scopes)
    with connection() as c:
        cur = c.execute(
            "INSERT INTO api_keys (label, key_hash, salt, prefix, scopes, created_at, created_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (label, h, salt, prefix, scopes_str, now, created_by),
        )
        row = c.execute("SELECT * FROM api_keys WHERE id = ?", (cur.lastrowid,)).fetchone()
    return plaintext, row


def list_api_keys() -> List[sqlite3.Row]:
    with connection() as c:
        return list(
            c.execute(
                "SELECT id, label, prefix, scopes, created_at, expires_at, revoked_at, last_used_at FROM api_keys ORDER BY id DESC"
            ).fetchall()
        )


def revoke_api_key(key_id: int) -> None:
    with connection() as c:
        c.execute("UPDATE api_keys SET revoked_at = ? WHERE id = ?", (security.now_epoch(), key_id))


def find_api_key_by_plaintext(plaintext: str) -> Optional[sqlite3.Row]:
    """Look up an API key. Iterates active keys (small N) since salt is per-key."""
    now = security.now_epoch()
    with connection() as c:
        rows = c.execute(
            "SELECT * FROM api_keys WHERE revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?)",
            (now,),
        ).fetchall()
    for row in rows:
        if security.verify_api_key(plaintext, row["salt"], row["key_hash"]):
            return row
    return None


def touch_api_key(key_id: int) -> None:
    with connection() as c:
        c.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (security.now_epoch(), key_id))


# ---------------------------------------------------------------------------
# SMS
# ---------------------------------------------------------------------------


def insert_inbox(sms: IncomingSms) -> int:
    now = security.now_epoch()
    with connection() as c:
        cur = c.execute(
            "INSERT INTO sms_inbox (sender, content, received_at, stored_at, raw_pdu, decode_error, udh_ref, udh_total, udh_seq)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sms.sender,
                sms.content,
                sms.received_at,
                now,
                sms.raw_pdu,
                1 if sms.decode_error else 0,
                sms.udh_ref,
                sms.udh_total,
                sms.udh_seq,
            ),
        )
        return int(cur.lastrowid)


def list_inbox(limit: int = 50, since: Optional[int] = None) -> List[sqlite3.Row]:
    with connection() as c:
        if since is not None:
            return list(
                c.execute(
                    "SELECT * FROM sms_inbox WHERE received_at > ? ORDER BY received_at ASC LIMIT ?",
                    (since, limit),
                ).fetchall()
            )
        return list(
            c.execute(
                "SELECT * FROM sms_inbox ORDER BY received_at DESC LIMIT ?", (limit,)
            ).fetchall()
        )


def get_message(message_id: int) -> Optional[sqlite3.Row]:
    with connection() as c:
        return c.execute("SELECT * FROM sms_inbox WHERE id = ?", (message_id,)).fetchone()


def queue_outbox(
    recipient: str,
    content: str,
    sender_user_id: Optional[int],
    sender_api_key_id: Optional[int],
) -> int:
    with connection() as c:
        cur = c.execute(
            "INSERT INTO sms_outbox (recipient, content, status, queued_at, sender_user_id, sender_api_key_id)"
            " VALUES (?, ?, 'queued', ?, ?, ?)",
            (recipient, content, security.now_epoch(), sender_user_id, sender_api_key_id),
        )
        return int(cur.lastrowid)


def mark_outbox(out_id: int, status: str, segments: int = 1, error: Optional[str] = None) -> None:
    with connection() as c:
        c.execute(
            "UPDATE sms_outbox SET status = ?, segments = ?, error = ?, sent_at = ? WHERE id = ?",
            (status, segments, error, security.now_epoch() if status == "sent" else None, out_id),
        )


def list_outbox(limit: int = 50) -> List[sqlite3.Row]:
    with connection() as c:
        return list(
            c.execute(
                "SELECT * FROM sms_outbox ORDER BY queued_at DESC LIMIT ?", (limit,)
            ).fetchall()
        )


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def audit(
    actor_kind: str,
    actor_id: Optional[int],
    actor_label: Optional[str],
    ip: Optional[str],
    action: str,
    target: Optional[str] = None,
    status_code: Optional[int] = None,
) -> None:
    with connection() as c:
        c.execute(
            "INSERT INTO audit_log (at, actor_kind, actor_id, actor_label, ip, action, target, status_code)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (security.now_epoch(), actor_kind, actor_id, actor_label, ip, action, target, status_code),
        )
