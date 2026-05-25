"""Database access functions."""
from __future__ import annotations

import sqlite3
from typing import Iterable, List, Optional

from . import security
from .db import connection
from .pdu import IncomingSms


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


def _encode_device_ids(device_ids: Optional[Iterable[int]]) -> Optional[str]:
    if device_ids is None:
        return None
    ids = sorted({int(x) for x in device_ids})
    return ",".join(str(x) for x in ids)


def parse_device_ids(raw: Optional[str]) -> Optional[set[int]]:
    """Return None for all devices, or a concrete set for restricted keys."""
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return set()
    return {int(x) for x in raw.split(",") if x.strip().isdigit()}


def create_api_key(
    label: str,
    scopes: List[str],
    created_by: int,
    allowed_device_ids: Optional[List[int]] = None,
    send_limit: Optional[int] = None,
    expires_at: Optional[int] = None,
) -> tuple[str, sqlite3.Row]:
    plaintext, salt, h, prefix = security.generate_api_key()
    now = security.now_epoch()
    scopes_str = ",".join(scopes)
    allowed = _encode_device_ids(allowed_device_ids)
    with connection() as c:
        cur = c.execute(
            "INSERT INTO api_keys (label, key_hash, salt, prefix, scopes, allowed_device_ids, send_limit, expires_at, created_at, created_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (label, h, salt, prefix, scopes_str, allowed, send_limit, expires_at, now, created_by),
        )
        row = c.execute("SELECT * FROM api_keys WHERE id = ?", (cur.lastrowid,)).fetchone()
    return plaintext, row


def list_api_keys() -> List[sqlite3.Row]:
    with connection() as c:
        return list(
            c.execute(
                "SELECT id, label, prefix, scopes, allowed_device_ids, send_limit, send_count, created_at, expires_at, revoked_at, last_used_at FROM api_keys ORDER BY id DESC"
            ).fetchall()
        )


def update_api_key_policy(
    key_id: int,
    label: str,
    scopes: List[str],
    allowed_device_ids: Optional[List[int]],
    send_limit: Optional[int],
    expires_at: Optional[int],
) -> Optional[sqlite3.Row]:
    with connection() as c:
        cur = c.execute(
            "UPDATE api_keys SET label = ?, scopes = ?, allowed_device_ids = ?, send_limit = ?, expires_at = ? WHERE id = ? AND revoked_at IS NULL",
            (label, ",".join(scopes), _encode_device_ids(allowed_device_ids), send_limit, expires_at, key_id),
        )
        if cur.rowcount == 0:
            return None
        return c.execute(
            "SELECT id, label, prefix, scopes, allowed_device_ids, send_limit, send_count, created_at, expires_at, revoked_at, last_used_at "
            "FROM api_keys WHERE id = ?",
            (key_id,),
        ).fetchone()


def revoke_api_key(key_id: int) -> None:
    with connection() as c:
        c.execute("UPDATE api_keys SET revoked_at = ? WHERE id = ?", (security.now_epoch(), key_id))


def delete_api_key(key_id: int) -> bool:
    with connection() as c:
        c.execute("UPDATE sms_outbox SET sender_api_key_id = NULL WHERE sender_api_key_id = ?", (key_id,))
        cur = c.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
        return cur.rowcount == 1


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


def reserve_api_send(key_id: int) -> bool:
    """Atomically consume one send quota. Unlimited keys have send_limit NULL."""
    with connection() as c:
        cur = c.execute(
            "UPDATE api_keys SET send_count = send_count + 1, last_used_at = ? "
            "WHERE id = ? AND revoked_at IS NULL AND (send_limit IS NULL OR send_count < send_limit)",
            (security.now_epoch(), key_id),
        )
        return cur.rowcount == 1


def release_api_send(key_id: int) -> None:
    with connection() as c:
        c.execute(
            "UPDATE api_keys SET send_count = CASE WHEN send_count > 0 THEN send_count - 1 ELSE 0 END WHERE id = ?",
            (key_id,),
        )


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


def _norm(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def build_sim_key(
    *,
    iccid: Optional[str] = None,
    imsi: Optional[str] = None,
    sim_number_auto: Optional[str] = None,
) -> Optional[str]:
    """Build a stable SIM identity. ICCID is preferred because it follows the card."""
    iccid = _norm(iccid)
    imsi = _norm(imsi)
    sim_number_auto = _norm(sim_number_auto)
    if iccid:
        return f"iccid:{iccid}"
    if imsi:
        return f"imsi:{imsi}"
    if sim_number_auto:
        return f"msisdn:{sim_number_auto}"
    return None


def _device_sim_key(row: sqlite3.Row) -> Optional[str]:
    try:
        existing = _norm(row["sim_key"])
    except (KeyError, IndexError):
        existing = None
    return existing or build_sim_key(
        iccid=row["iccid"],
        imsi=row["imsi"],
        sim_number_auto=row["sim_number_auto"],
    )


def _device_sim_snapshot(c: sqlite3.Connection, device_id: Optional[int]) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    if device_id is None:
        return None, None, None, None
    row = c.execute(
        "SELECT sim_key, sim_number_auto, sim_number_manual, iccid, imsi FROM devices WHERE id = ?",
        (device_id,),
    ).fetchone()
    if not row:
        return None, None, None, None
    return (
        _device_sim_key(row),
        _norm(row["iccid"]),
        _norm(row["imsi"]),
        _norm(row["sim_number_manual"]) or _norm(row["sim_number_auto"]),
    )


def _current_sim_filter() -> str:
    return "(d.sim_key IS NULL OR (m.sim_key IS NOT NULL AND m.sim_key = d.sim_key))"


def _next_device_number(c: sqlite3.Connection) -> int:
    row = c.execute("SELECT COALESCE(MAX(number), 0) + 1 AS n FROM devices").fetchone()
    return int(row["n"])


def device_display_name(row: sqlite3.Row) -> str:
    label = (row["label"] or "").strip()
    if label:
        return label
    return f"编号{int(row['number']):03d}"


def upsert_device(
    stable_key: str,
    at_port: Optional[str],
    usb_path: Optional[str],
    status: str = "online",
) -> sqlite3.Row:
    now = security.now_epoch()
    with connection() as c:
        row = c.execute("SELECT * FROM devices WHERE stable_key = ?", (stable_key,)).fetchone()
        if row:
            c.execute(
                "UPDATE devices SET at_port = ?, usb_path = ?, status = ?, last_error = NULL, "
                "last_seen_at = ?, updated_at = ? WHERE id = ?",
                (at_port, usb_path, status, now, now, row["id"]),
            )
            return c.execute("SELECT * FROM devices WHERE id = ?", (row["id"],)).fetchone()
        number = _next_device_number(c)
        cur = c.execute(
            "INSERT INTO devices (number, stable_key, at_port, usb_path, status, first_seen_at, last_seen_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (number, stable_key, at_port, usb_path, status, now, now, now),
        )
        return c.execute("SELECT * FROM devices WHERE id = ?", (cur.lastrowid,)).fetchone()


def update_device_identity(
    device_id: int,
    *,
    sim_number_auto: Optional[str] = None,
    iccid: Optional[str] = None,
    imsi: Optional[str] = None,
    imei: Optional[str] = None,
    model: Optional[str] = None,
    manufacturer: Optional[str] = None,
) -> None:
    now = security.now_epoch()
    incoming_sim_key = build_sim_key(iccid=iccid, imsi=imsi, sim_number_auto=sim_number_auto)
    with connection() as c:
        row = c.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        previous_sim_key = _device_sim_key(row) if row else None
        sim_changed = bool(incoming_sim_key and previous_sim_key and incoming_sim_key != previous_sim_key)
        if sim_changed:
            c.execute(
                "UPDATE devices SET sim_key = ?, sim_number_auto = ?, sim_number_manual = NULL, "
                "iccid = ?, imsi = ?, imei = COALESCE(?, imei), "
                "model = COALESCE(?, model), manufacturer = COALESCE(?, manufacturer), "
                "last_seen_at = ?, updated_at = ? WHERE id = ?",
                (
                    incoming_sim_key,
                    _norm(sim_number_auto),
                    _norm(iccid),
                    _norm(imsi),
                    imei,
                    model,
                    manufacturer,
                    now,
                    now,
                    device_id,
                ),
            )
        else:
            c.execute(
                "UPDATE devices SET sim_key = COALESCE(?, sim_key), sim_number_auto = COALESCE(?, sim_number_auto), "
                "iccid = COALESCE(?, iccid), imsi = COALESCE(?, imsi), imei = COALESCE(?, imei), "
                "model = COALESCE(?, model), manufacturer = COALESCE(?, manufacturer), "
                "last_seen_at = ?, updated_at = ? WHERE id = ?",
                (incoming_sim_key, sim_number_auto, iccid, imsi, imei, model, manufacturer, now, now, device_id),
            )


def update_device_status(
    device_id: int,
    status: str,
    last_error: Optional[str] = None,
    at_port: Optional[str] = None,
) -> None:
    now = security.now_epoch()
    with connection() as c:
        if at_port is None:
            c.execute(
                "UPDATE devices SET status = ?, last_error = ?, last_seen_at = ?, updated_at = ? WHERE id = ?",
                (status, last_error, now, now, device_id),
            )
        else:
            c.execute(
                "UPDATE devices SET status = ?, last_error = ?, at_port = ?, last_seen_at = ?, updated_at = ? WHERE id = ?",
                (status, last_error, at_port, now, now, device_id),
            )


def update_device_note(
    device_id: int,
    label: Optional[str],
    sim_number_manual: Optional[str],
) -> Optional[sqlite3.Row]:
    now = security.now_epoch()
    label_clean = label.strip() if label is not None else None
    sim_clean = sim_number_manual.strip() if sim_number_manual is not None else None
    with connection() as c:
        c.execute(
            "UPDATE devices SET label = ?, sim_number_manual = ?, updated_at = ? WHERE id = ?",
            (label_clean or None, sim_clean or None, now, device_id),
        )
        return c.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()


def list_devices() -> List[sqlite3.Row]:
    with connection() as c:
        return list(c.execute("SELECT * FROM devices ORDER BY number ASC").fetchall())


def get_device(device_id: int) -> Optional[sqlite3.Row]:
    with connection() as c:
        return c.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()


def get_online_device(device_id: Optional[int] = None) -> Optional[sqlite3.Row]:
    with connection() as c:
        if device_id is not None:
            return c.execute(
                "SELECT * FROM devices WHERE id = ? AND status = 'online'",
                (device_id,),
            ).fetchone()
        return c.execute(
            "SELECT * FROM devices WHERE status = 'online' ORDER BY number ASC LIMIT 1"
        ).fetchone()


def serialize_device(row: sqlite3.Row) -> dict:
    sim_number = row["sim_number_manual"] or row["sim_number_auto"]
    return {
        "id": row["id"],
        "number": row["number"],
        "code": f"{int(row['number']):03d}",
        "label": row["label"],
        "display_name": device_display_name(row),
        "sim_key": _device_sim_key(row),
        "sim_number": sim_number,
        "sim_number_auto": row["sim_number_auto"],
        "sim_number_manual": row["sim_number_manual"],
        "iccid": row["iccid"],
        "imsi": row["imsi"],
        "imei": row["imei"],
        "model": row["model"],
        "manufacturer": row["manufacturer"],
        "at_port": row["at_port"],
        "usb_path": row["usb_path"],
        "status": row["status"],
        "last_error": row["last_error"],
        "first_seen_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
        "updated_at": row["updated_at"],
    }


def insert_inbox(device_id: Optional[int], sms: IncomingSms) -> int:
    now = security.now_epoch()
    with connection() as c:
        sim_key, sim_iccid, sim_imsi, sim_number = _device_sim_snapshot(c, device_id)
        cur = c.execute(
            "INSERT INTO sms_inbox (device_id, sim_key, sim_iccid, sim_imsi, sim_number, sender, content, received_at, stored_at, raw_pdu, decode_error, udh_ref, udh_total, udh_seq)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                device_id,
                sim_key,
                sim_iccid,
                sim_imsi,
                sim_number,
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


def list_inbox(
    limit: int = 50,
    since: Optional[int] = None,
    device_id: Optional[int] = None,
    device_ids: Optional[Iterable[int]] = None,
    current_sim_only: bool = True,
) -> List[sqlite3.Row]:
    with connection() as c:
        allowed_ids = None if device_ids is None else sorted({int(x) for x in device_ids})
        if allowed_ids == []:
            return []
        base = (
            "SELECT m.*, d.number AS device_number, d.label AS device_label, "
            "d.sim_key AS device_sim_key, d.sim_number_auto AS device_sim_number_auto, "
            "d.sim_number_manual AS device_sim_number_manual, d.iccid AS device_iccid, d.imsi AS device_imsi "
            "FROM sms_inbox m LEFT JOIN devices d ON d.id = m.device_id"
        )
        where = []
        params: list[object] = []
        if since is not None:
            where.append("m.received_at > ?")
            params.append(since)
        if device_id is not None:
            where.append("m.device_id = ?")
            params.append(device_id)
        elif allowed_ids is not None:
            placeholders = ",".join("?" for _ in allowed_ids)
            where.append(f"m.device_id IN ({placeholders})")
            params.extend(allowed_ids)
        if current_sim_only:
            where.append(_current_sim_filter())
        sql = base
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY m.received_at ASC" if since is not None else " ORDER BY m.received_at DESC"
        sql += " LIMIT ?"
        params.append(limit)
        return list(c.execute(sql, params).fetchall())


def get_message(message_id: int) -> Optional[sqlite3.Row]:
    with connection() as c:
        return c.execute(
            "SELECT m.*, d.number AS device_number, d.label AS device_label, "
            "d.sim_key AS device_sim_key, d.sim_number_auto AS device_sim_number_auto, "
            "d.sim_number_manual AS device_sim_number_manual, d.iccid AS device_iccid, d.imsi AS device_imsi "
            "FROM sms_inbox m LEFT JOIN devices d ON d.id = m.device_id WHERE m.id = ?",
            (message_id,),
        ).fetchone()


def queue_outbox(
    device_id: Optional[int],
    recipient: str,
    content: str,
    sender_user_id: Optional[int],
    sender_api_key_id: Optional[int],
) -> int:
    with connection() as c:
        sim_key, sim_iccid, sim_imsi, sim_number = _device_sim_snapshot(c, device_id)
        cur = c.execute(
            "INSERT INTO sms_outbox (device_id, sim_key, sim_iccid, sim_imsi, sim_number, recipient, content, status, queued_at, sender_user_id, sender_api_key_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)",
            (
                device_id,
                sim_key,
                sim_iccid,
                sim_imsi,
                sim_number,
                recipient,
                content,
                security.now_epoch(),
                sender_user_id,
                sender_api_key_id,
            ),
        )
        return int(cur.lastrowid)


def mark_outbox(out_id: int, status: str, segments: int = 1, error: Optional[str] = None) -> None:
    with connection() as c:
        c.execute(
            "UPDATE sms_outbox SET status = ?, segments = ?, error = ?, sent_at = ? WHERE id = ?",
            (status, segments, error, security.now_epoch() if status == "sent" else None, out_id),
        )


def set_outbox_device(out_id: int, device_id: int) -> None:
    with connection() as c:
        sim_key, sim_iccid, sim_imsi, sim_number = _device_sim_snapshot(c, device_id)
        c.execute(
            "UPDATE sms_outbox SET device_id = ?, sim_key = ?, sim_iccid = ?, sim_imsi = ?, sim_number = ? WHERE id = ?",
            (device_id, sim_key, sim_iccid, sim_imsi, sim_number, out_id),
        )


def list_outbox(
    limit: int = 50,
    device_id: Optional[int] = None,
    device_ids: Optional[Iterable[int]] = None,
    current_sim_only: bool = True,
) -> List[sqlite3.Row]:
    with connection() as c:
        allowed_ids = None if device_ids is None else sorted({int(x) for x in device_ids})
        if allowed_ids == []:
            return []
        sql = (
            "SELECT m.*, d.number AS device_number, d.label AS device_label, "
            "d.sim_key AS device_sim_key, d.sim_number_auto AS device_sim_number_auto, "
            "d.sim_number_manual AS device_sim_number_manual, d.iccid AS device_iccid, d.imsi AS device_imsi "
            "FROM sms_outbox m LEFT JOIN devices d ON d.id = m.device_id"
        )
        where: list[str] = []
        params: list[object] = []
        if device_id is not None:
            where.append("m.device_id = ?")
            params.append(device_id)
        elif allowed_ids is not None:
            placeholders = ",".join("?" for _ in allowed_ids)
            where.append(f"m.device_id IN ({placeholders})")
            params.extend(allowed_ids)
        if current_sim_only:
            where.append(_current_sim_filter())
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY m.queued_at DESC LIMIT ?"
        params.append(limit)
        return list(c.execute(sql, params).fetchall())


def cleanup_expired_messages(retention_days: int = 7) -> dict:
    """Delete SMS records older than the configured retention window."""
    cutoff = security.now_epoch() - max(1, int(retention_days)) * 86400
    with connection() as c:
        inbox = c.execute(
            "DELETE FROM sms_inbox WHERE COALESCE(stored_at, received_at) < ?",
            (cutoff,),
        ).rowcount
        outbox = c.execute(
            "DELETE FROM sms_outbox WHERE COALESCE(sent_at, queued_at) < ?",
            (cutoff,),
        ).rowcount
    return {"inbox_deleted": int(inbox or 0), "outbox_deleted": int(outbox or 0), "cutoff": cutoff}


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
