"""SQLite persistence layer.

Uses synchronous sqlite3 wrapped behind a thread executor where needed.
SQLite WAL mode + short transactions keep contention low for our workload.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import settings

_lock = threading.Lock()
_initialized = False


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,
    totp_secret_enc BLOB,                -- AES-GCM(nonce||ciphertext||tag)
    totp_enabled    INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery_codes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash   TEXT NOT NULL,
    used_at     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_recovery_user ON recovery_codes(user_id);

CREATE TABLE IF NOT EXISTS sessions (
    sid         TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    ip          TEXT,
    user_agent  TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_expires ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS login_attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL,
    ip          TEXT,
    success     INTEGER NOT NULL,
    at          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_user_time ON login_attempts(username, at);
CREATE INDEX IF NOT EXISTS idx_login_ip_time ON login_attempts(ip, at);

CREATE TABLE IF NOT EXISTS api_keys (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT NOT NULL,
    key_hash    TEXT UNIQUE NOT NULL,    -- sha256(salt + key)
    salt        TEXT NOT NULL,
    prefix      TEXT NOT NULL,           -- first 8 chars of key, for display
    scopes      TEXT NOT NULL,           -- comma-separated: sms:read,sms:send
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER,
    revoked_at  INTEGER,
    last_used_at INTEGER,
    created_by  INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_apikey_revoked ON api_keys(revoked_at);

CREATE TABLE IF NOT EXISTS sms_inbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sender      TEXT NOT NULL,
    content     TEXT NOT NULL,
    received_at INTEGER NOT NULL,        -- modem-provided timestamp (epoch)
    stored_at   INTEGER NOT NULL,        -- server-side epoch
    raw_pdu     TEXT,
    decode_error INTEGER NOT NULL DEFAULT 0,
    udh_ref     INTEGER,                 -- concatenation reference, NULL for single
    udh_total   INTEGER,
    udh_seq     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_inbox_received ON sms_inbox(received_at);

CREATE TABLE IF NOT EXISTS sms_outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient   TEXT NOT NULL,
    content     TEXT NOT NULL,
    status      TEXT NOT NULL,           -- queued | sending | sent | failed
    error       TEXT,
    segments    INTEGER NOT NULL DEFAULT 1,
    queued_at   INTEGER NOT NULL,
    sent_at     INTEGER,
    sender_user_id INTEGER REFERENCES users(id),
    sender_api_key_id INTEGER REFERENCES api_keys(id)
);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON sms_outbox(status);
CREATE INDEX IF NOT EXISTS idx_outbox_queued ON sms_outbox(queued_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          INTEGER NOT NULL,
    actor_kind  TEXT NOT NULL,           -- user | api_key | system
    actor_id    INTEGER,
    actor_label TEXT,                    -- username or key prefix
    ip          TEXT,
    action      TEXT NOT NULL,
    target      TEXT,
    status_code INTEGER
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(at);
"""


def _ensure_dirs() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    try:
        # 0700 on Linux; on Windows this is best effort.
        settings.data_dir.chmod(0o700)
    except OSError:
        pass


def init_db() -> None:
    """Create tables if missing. Safe to call repeatedly."""
    global _initialized
    with _lock:
        if _initialized:
            return
        _ensure_dirs()
        with connection() as conn:
            conn.executescript(SCHEMA)
            conn.commit()
        _initialized = True


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    """Yield a sqlite3 connection with sane defaults.

    - WAL for concurrent readers
    - Foreign keys enforced
    - Row factory = sqlite3.Row
    """
    db_path: Path = settings.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=30.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
    finally:
        conn.close()
