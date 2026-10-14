"""Multi-user accounts, runtime settings and contacts."""
from __future__ import annotations

import base64
import sqlite3
from typing import List, Optional

from . import security
from .db import connection

ROLES = ("admin", "operator", "viewer")


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def serialize_user(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"] if "role" in row.keys() else "admin",
        "display_name": row["display_name"],
        "is_active": bool(row["is_active"]),
        "totp_enabled": bool(row["totp_enabled"]),
        "must_change_password": bool(row["must_change_password"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_login_at": row["last_login_at"],
    }


def list_users() -> List[sqlite3.Row]:
    with connection() as c:
        return list(c.execute("SELECT * FROM users ORDER BY id ASC").fetchall())


def count_users() -> int:
    with connection() as c:
        return int(c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"])


def count_active_admins(exclude_user_id: Optional[int] = None) -> int:
    with connection() as c:
        if exclude_user_id is None:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND is_active = 1"
            ).fetchone()
        else:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND is_active = 1 AND id != ?",
                (exclude_user_id,),
            ).fetchone()
        return int(row["n"])


def create_user(
    username: str,
    password_hash: str,
    *,
    role: str = "operator",
    display_name: Optional[str] = None,
    totp_secret: Optional[str] = None,
    totp_enabled: bool = False,
    must_change_password: bool = False,
) -> int:
    if role not in ROLES:
        raise ValueError(f"invalid role {role}")
    now = security.now_epoch()
    enc = security.encrypt_secret(totp_secret.encode("utf-8")) if totp_secret else None
    with connection() as c:
        cur = c.execute(
            "INSERT INTO users (username, password_hash, totp_secret_enc, totp_enabled,"
            " created_at, updated_at, role, display_name, is_active, must_change_password)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (
                username,
                password_hash,
                enc,
                1 if totp_enabled else 0,
                now,
                now,
                role,
                display_name,
                1 if must_change_password else 0,
            ),
        )
        return int(cur.lastrowid)


def username_taken(username: str) -> bool:
    with connection() as c:
        return c.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone() is not None


def set_user_active(user_id: int, active: bool) -> None:
    with connection() as c:
        c.execute(
            "UPDATE users SET is_active = ?, updated_at = ? WHERE id = ?",
            (1 if active else 0, security.now_epoch(), user_id),
        )
        if not active:
            c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def set_user_role(user_id: int, role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"invalid role {role}")
    with connection() as c:
        c.execute(
            "UPDATE users SET role = ?, updated_at = ? WHERE id = ?",
            (role, security.now_epoch(), user_id),
        )


def set_user_profile(user_id: int, display_name: Optional[str]) -> None:
    with connection() as c:
        c.execute(
            "UPDATE users SET display_name = ?, updated_at = ? WHERE id = ?",
            (display_name, security.now_epoch(), user_id),
        )


def set_password(user_id: int, password_hash: str, *, must_change: bool = False) -> None:
    with connection() as c:
        c.execute(
            "UPDATE users SET password_hash = ?, must_change_password = ?, updated_at = ? WHERE id = ?",
            (password_hash, 1 if must_change else 0, security.now_epoch(), user_id),
        )
        c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def clear_must_change_password(user_id: int) -> None:
    with connection() as c:
        c.execute(
            "UPDATE users SET must_change_password = 0, updated_at = ? WHERE id = ?",
            (security.now_epoch(), user_id),
        )


def delete_user(user_id: int) -> bool:
    with connection() as c:
        c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user_id,))
        c.execute("UPDATE call_sessions SET owner_user_id = NULL WHERE owner_user_id = ?", (user_id,))
        c.execute("UPDATE sms_outbox SET sender_user_id = NULL WHERE sender_user_id = ?", (user_id,))
        c.execute("UPDATE api_keys SET created_by = NULL WHERE created_by = ?", (user_id,))
        cur = c.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return cur.rowcount == 1


def touch_last_login(user_id: int) -> None:
    with connection() as c:
        c.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (security.now_epoch(), user_id))


def set_totp(user_id: int, secret_plain: str) -> None:
    """Enable with a newly confirmed secret and clear any old codes."""
    enc = security.encrypt_secret(secret_plain.encode("utf-8"))
    with connection() as c:
        # The replay guard is deliberately kept here: confirm() spends a step on
        # this same secret a moment earlier, and clearing it would re-open that
        # step for a replayed login. disable_totp and replace_totp reset it when
        # the secret actually goes away.
        c.execute("UPDATE users SET totp_secret_enc=?,totp_enabled=1,updated_at=? WHERE id=?",
                  (enc, security.now_epoch(), user_id))
        c.execute("DELETE FROM recovery_codes WHERE user_id=?", (user_id,))


def disable_totp(user_id: int) -> None:
    with connection() as c:
        c.execute(
            "UPDATE users SET totp_enabled = 0, totp_secret_enc = NULL, totp_last_step = NULL, updated_at = ? WHERE id = ?",
            (security.now_epoch(), user_id),
        )
        c.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user_id,))


# ---------------------------------------------------------------------------
# Runtime settings (audio devices, AI providers, prompt config)
# ---------------------------------------------------------------------------


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    with connection() as c:
        row = c.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def set_setting(key: str, value: Optional[str]) -> None:
    with connection() as c:
        c.execute(
            "INSERT INTO app_settings (key, value, secret, updated_at) VALUES (?, ?, 0, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, secret = 0,"
            " updated_at = excluded.updated_at",
            (key, value, security.now_epoch()),
        )


def get_secret(key: str) -> Optional[str]:
    with connection() as c:
        row = c.execute("SELECT value, secret FROM app_settings WHERE key = ?", (key,)).fetchone()
    if not row or not row["value"]:
        return None
    if not row["secret"]:
        return row["value"]
    try:
        return security.decrypt_secret(base64.b64decode(row["value"])).decode("utf-8")
    except Exception:
        return None


def set_secret(key: str, plaintext: Optional[str]) -> None:
    if plaintext is None:
        with connection() as c:
            c.execute("DELETE FROM app_settings WHERE key = ?", (key,))
        return
    blob = base64.b64encode(security.encrypt_secret(plaintext.encode("utf-8"))).decode("ascii")
    with connection() as c:
        c.execute(
            "INSERT INTO app_settings (key, value, secret, updated_at) VALUES (?, ?, 1, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, secret = 1,"
            " updated_at = excluded.updated_at",
            (key, blob, security.now_epoch()),
        )


def list_settings(include_secrets: bool = False) -> dict:
    with connection() as c:
        rows = c.execute("SELECT key, value, secret, updated_at FROM app_settings ORDER BY key").fetchall()
    out: dict = {}
    for row in rows:
        if row["secret"] and not include_secrets:
            out[row["key"]] = "***" if row["value"] else ""
            continue
        if row["secret"]:
            out[row["key"]] = get_secret(row["key"])
        else:
            out[row["key"]] = row["value"]
    return out


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------


def serialize_contact(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "number": row["number"],
        "group_name": row["group_name"],
        "note": row["note"],
        "favorite": bool(row["favorite"]),
        "owner_user_id": row["owner_user_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_contacts(
    owner_user_id: Optional[int] = None,
    query: Optional[str] = None,
    group_name: Optional[str] = None,
) -> List[sqlite3.Row]:
    sql = "SELECT * FROM contacts"
    where: list[str] = []
    params: list[object] = []
    if owner_user_id is not None:
        where.append("(owner_user_id = ? OR owner_user_id IS NULL)")
        params.append(owner_user_id)
    if group_name:
        where.append("group_name = ?")
        params.append(group_name)
    if query:
        where.append("(name LIKE ? OR number LIKE ?)")
        params.extend([f"%{query}%", f"%{query}%"])
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY favorite DESC, name COLLATE NOCASE ASC"
    with connection() as c:
        return list(c.execute(sql, params).fetchall())


def get_contact(contact_id: int) -> Optional[sqlite3.Row]:
    with connection() as c:
        return c.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()


def create_contact(
    name: str,
    number: str,
    *,
    owner_user_id: Optional[int] = None,
    group_name: Optional[str] = None,
    note: Optional[str] = None,
    favorite: bool = False,
) -> sqlite3.Row:
    now = security.now_epoch()
    with connection() as c:
        cur = c.execute(
            "INSERT INTO contacts (owner_user_id, name, number, group_name, note, favorite, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (owner_user_id, name, number, group_name, note, 1 if favorite else 0, now, now),
        )
        return c.execute("SELECT * FROM contacts WHERE id = ?", (cur.lastrowid,)).fetchone()


def update_contact(
    contact_id: int,
    *,
    name: Optional[str] = None,
    number: Optional[str] = None,
    group_name: Optional[str] = None,
    note: Optional[str] = None,
    favorite: Optional[bool] = None,
) -> Optional[sqlite3.Row]:
    fields: list[str] = []
    params: list[object] = []
    if name is not None:
        fields.append("name = ?")
        params.append(name)
    if number is not None:
        fields.append("number = ?")
        params.append(number)
    if group_name is not None:
        fields.append("group_name = ?")
        params.append(group_name)
    if note is not None:
        fields.append("note = ?")
        params.append(note)
    if favorite is not None:
        fields.append("favorite = ?")
        params.append(1 if favorite else 0)
    if not fields:
        return get_contact(contact_id)
    fields.append("updated_at = ?")
    params.append(security.now_epoch())
    params.append(contact_id)
    with connection() as c:
        cur = c.execute(f"UPDATE contacts SET {', '.join(fields)} WHERE id = ?", params)
        if cur.rowcount == 0:
            return None
        return c.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()


def delete_contact(contact_id: int) -> bool:
    with connection() as c:
        return c.execute("DELETE FROM contacts WHERE id = ?", (contact_id,)).rowcount == 1


def list_contact_groups(owner_user_id: Optional[int] = None) -> List[str]:
    with connection() as c:
        if owner_user_id is None:
            rows = c.execute(
                "SELECT DISTINCT group_name FROM contacts WHERE group_name IS NOT NULL ORDER BY group_name"
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT DISTINCT group_name FROM contacts WHERE group_name IS NOT NULL"
                " AND (owner_user_id = ? OR owner_user_id IS NULL) ORDER BY group_name",
                (owner_user_id,),
            ).fetchall()
    return [r["group_name"] for r in rows]
