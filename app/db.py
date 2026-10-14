"""SQLite persistence layer."""
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
CREATE TABLE IF NOT EXISTS totp_enrollments (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    sid TEXT NOT NULL,
    secret_enc BLOB NOT NULL,
    expires_at INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,
    totp_secret_enc BLOB,
    totp_enabled    INTEGER NOT NULL DEFAULT 0,
    totp_last_step  INTEGER,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    role            TEXT NOT NULL DEFAULT 'admin',
    display_name    TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    last_login_at   INTEGER
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
    key_hash    TEXT UNIQUE NOT NULL,
    salt        TEXT NOT NULL,
    prefix      TEXT NOT NULL,
    scopes      TEXT NOT NULL,
    allowed_device_ids TEXT,
    send_limit  INTEGER,
    send_count  INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER,
    revoked_at  INTEGER,
    last_used_at INTEGER,
    created_by  INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_apikey_revoked ON api_keys(revoked_at);

CREATE TABLE IF NOT EXISTS devices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    number          INTEGER UNIQUE NOT NULL,
    stable_key      TEXT UNIQUE NOT NULL,
    label           TEXT,
    sim_key         TEXT,
    sim_number_auto TEXT,
    sim_number_manual TEXT,
    iccid           TEXT,
    imsi            TEXT,
    imei            TEXT,
    model           TEXT,
    manufacturer    TEXT,
    at_port         TEXT,
    usb_path        TEXT,
    status          TEXT NOT NULL DEFAULT 'offline',
    last_error      TEXT,
    first_seen_at   INTEGER NOT NULL,
    last_seen_at    INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_devices_status ON devices(status);
CREATE INDEX IF NOT EXISTS idx_devices_stable ON devices(stable_key);

CREATE TABLE IF NOT EXISTS sms_inbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   INTEGER REFERENCES devices(id),
    sim_key     TEXT,
    sim_iccid   TEXT,
    sim_imsi    TEXT,
    sim_number  TEXT,
    sender      TEXT NOT NULL,
    content     TEXT NOT NULL,
    received_at INTEGER NOT NULL,
    stored_at   INTEGER NOT NULL,
    raw_pdu     TEXT,
    decode_error INTEGER NOT NULL DEFAULT 0,
    udh_ref     INTEGER,
    udh_total   INTEGER,
    udh_seq     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_inbox_received ON sms_inbox(received_at);
CREATE INDEX IF NOT EXISTS idx_inbox_device ON sms_inbox(device_id);

CREATE TABLE IF NOT EXISTS sms_outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   INTEGER REFERENCES devices(id),
    sim_key     TEXT,
    sim_iccid   TEXT,
    sim_imsi    TEXT,
    sim_number  TEXT,
    recipient   TEXT NOT NULL,
    content     TEXT NOT NULL,
    status      TEXT NOT NULL,
    error       TEXT,
    segments    INTEGER NOT NULL DEFAULT 1,
    queued_at   INTEGER NOT NULL,
    sent_at     INTEGER,
    sender_user_id INTEGER REFERENCES users(id),
    sender_api_key_id INTEGER REFERENCES api_keys(id)
);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON sms_outbox(status);
CREATE INDEX IF NOT EXISTS idx_outbox_queued ON sms_outbox(queued_at);
CREATE INDEX IF NOT EXISTS idx_outbox_device ON sms_outbox(device_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          INTEGER NOT NULL,
    actor_kind  TEXT NOT NULL,
    actor_id    INTEGER,
    actor_label TEXT,
    ip          TEXT,
    action      TEXT NOT NULL,
    target      TEXT,
    status_code INTEGER
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(at);

CREATE TABLE IF NOT EXISTS call_sessions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id        TEXT UNIQUE NOT NULL,
    device_id      INTEGER REFERENCES devices(id),
    sim_key        TEXT,
    sim_number     TEXT,
    direction      TEXT NOT NULL,
    remote         TEXT,
    state          TEXT NOT NULL,
    end_reason     TEXT,
    created_at     INTEGER NOT NULL,
    ringing_at     INTEGER,
    answered_at    INTEGER,
    ended_at       INTEGER,
    duration       INTEGER NOT NULL DEFAULT 0,
    tts_log        TEXT,
    transcript     TEXT,
    recording_path TEXT,
    owner_user_id  INTEGER REFERENCES users(id),
    origin         TEXT,
    -- How the recording decision was reached and what the far end was told.
    -- Kept on the call row so an audit can show the notice text, when it was
    -- played and whether playback actually succeeded.
    recording_reason    TEXT,
    recording_announced INTEGER NOT NULL DEFAULT 0,
    recording_notice    TEXT,
    recording_notice_at INTEGER,
    recording_notice_ok INTEGER,
    recording_notice_error TEXT,
    -- Translation and minutes for this call.
    translate_mode TEXT,
    summary        TEXT,
    summary_at     INTEGER,
    -- Set when an AI task placed or handled the call.
    task_id        INTEGER,
    task_purpose   TEXT,
    task_achieved  INTEGER,
    task_result    TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_created ON call_sessions(created_at);
CREATE INDEX IF NOT EXISTS idx_calls_device ON call_sessions(device_id);
CREATE INDEX IF NOT EXISTS idx_calls_remote ON call_sessions(remote);

CREATE TABLE IF NOT EXISTS ai_call_tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    reference     TEXT UNIQUE,
    device_id     INTEGER REFERENCES devices(id),
    direction     TEXT NOT NULL DEFAULT 'outbound',
    number        TEXT,
    purpose       TEXT NOT NULL,
    context       TEXT,
    language      TEXT,
    peer_language TEXT,
    template      TEXT,
    max_seconds   INTEGER,
    status        TEXT NOT NULL DEFAULT 'queued',
    call_id       TEXT,
    result        TEXT,
    achieved      INTEGER,
    summary       TEXT,
    error         TEXT,
    created_by    TEXT,
    owner_user_id INTEGER REFERENCES users(id),
    created_at    INTEGER NOT NULL,
    started_at    INTEGER,
    finished_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ai_task_status ON ai_call_tasks(status, created_at);
CREATE INDEX IF NOT EXISTS idx_ai_task_call ON ai_call_tasks(call_id);

CREATE TABLE IF NOT EXISTS contacts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    number        TEXT NOT NULL,
    group_name    TEXT,
    note          TEXT,
    favorite      INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contacts_owner ON contacts(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_contacts_number ON contacts(number);

CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    secret     INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);

-- Per-capability model endpoints. One capability (speech recognition,
-- translation, synthesis, call decisions, summaries) can have several
-- endpoints behind it, tried in priority order, so a vendor that rate limits
-- or times out falls through to the next one instead of breaking the call.
-- "api" and "local" are the same OpenAI-compatible shape; the kind only tells
-- the console whether the endpoint is an outside service or our own inference
-- box.
CREATE TABLE IF NOT EXISTS ai_endpoints (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    capability      TEXT NOT NULL,
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'api',
    base_url        TEXT NOT NULL DEFAULT '',
    api_key_enc     BLOB,
    model           TEXT NOT NULL DEFAULT '',
    params          TEXT NOT NULL DEFAULT '{}',
    priority        INTEGER NOT NULL DEFAULT 100,
    enabled         INTEGER NOT NULL DEFAULT 1,
    supports_stream INTEGER NOT NULL DEFAULT 0,
    timeout_seconds REAL NOT NULL DEFAULT 30.0,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_endpoint_cap ON ai_endpoints(capability, priority, id);

CREATE TABLE IF NOT EXISTS ai_call_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    capability    TEXT NOT NULL,
    endpoint_id   INTEGER,
    endpoint_name TEXT,
    ok            INTEGER NOT NULL,
    latency_ms    INTEGER,
    error         TEXT,
    call_id       TEXT,
    created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_call_log_created ON ai_call_log(created_at);
CREATE INDEX IF NOT EXISTS idx_ai_call_log_endpoint ON ai_call_log(endpoint_id, created_at);

CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    peer        TEXT NOT NULL,
    device_id   INTEGER REFERENCES devices(id),
    last_at     INTEGER NOT NULL,
    unread      INTEGER NOT NULL DEFAULT 0,
    archived    INTEGER NOT NULL DEFAULT 0,
    draft       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_conv_peer ON conversations(peer, device_id);
"""


def _ensure_dirs() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    try:
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
            _migrate(conn)
            conn.commit()
        _initialized = True


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply additive migrations for existing local SQLite databases."""

    def columns(table: str) -> set[str]:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    inbox_cols = columns("sms_inbox")
    if "device_id" not in inbox_cols:
        conn.execute("ALTER TABLE sms_inbox ADD COLUMN device_id INTEGER REFERENCES devices(id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inbox_device ON sms_inbox(device_id)")
    if "sim_key" not in inbox_cols:
        conn.execute("ALTER TABLE sms_inbox ADD COLUMN sim_key TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inbox_sim ON sms_inbox(sim_key)")
    if "sim_iccid" not in inbox_cols:
        conn.execute("ALTER TABLE sms_inbox ADD COLUMN sim_iccid TEXT")
    if "sim_imsi" not in inbox_cols:
        conn.execute("ALTER TABLE sms_inbox ADD COLUMN sim_imsi TEXT")
    if "sim_number" not in inbox_cols:
        conn.execute("ALTER TABLE sms_inbox ADD COLUMN sim_number TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inbox_sim ON sms_inbox(sim_key)")

    outbox_cols = columns("sms_outbox")
    if "device_id" not in outbox_cols:
        conn.execute("ALTER TABLE sms_outbox ADD COLUMN device_id INTEGER REFERENCES devices(id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_device ON sms_outbox(device_id)")
    if "sim_key" not in outbox_cols:
        conn.execute("ALTER TABLE sms_outbox ADD COLUMN sim_key TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_sim ON sms_outbox(sim_key)")
    if "sim_iccid" not in outbox_cols:
        conn.execute("ALTER TABLE sms_outbox ADD COLUMN sim_iccid TEXT")
    if "sim_imsi" not in outbox_cols:
        conn.execute("ALTER TABLE sms_outbox ADD COLUMN sim_imsi TEXT")
    if "sim_number" not in outbox_cols:
        conn.execute("ALTER TABLE sms_outbox ADD COLUMN sim_number TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_sim ON sms_outbox(sim_key)")

    api_cols = columns("api_keys")
    if "allowed_device_ids" not in api_cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN allowed_device_ids TEXT")
    if "send_limit" not in api_cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN send_limit INTEGER")
    if "send_count" not in api_cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN send_count INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_users_single_admin
        BEFORE INSERT ON users
        WHEN (SELECT COUNT(*) FROM users) >= 1
        BEGIN
            SELECT RAISE(ABORT, 'only one admin user allowed');
        END;
        """
    )

    device_cols = columns("devices")
    if "sim_key" not in device_cols:
        conn.execute("ALTER TABLE devices ADD COLUMN sim_key TEXT")
    conn.execute(
        """
        UPDATE devices
        SET sim_key = CASE
            WHEN iccid IS NOT NULL AND TRIM(iccid) != '' THEN 'iccid:' || TRIM(iccid)
            WHEN imsi IS NOT NULL AND TRIM(imsi) != '' THEN 'imsi:' || TRIM(imsi)
            WHEN sim_number_auto IS NOT NULL AND TRIM(sim_number_auto) != '' THEN 'msisdn:' || TRIM(sim_number_auto)
            ELSE sim_key
        END
        WHERE sim_key IS NULL
        """
    )

    # The upstream schema allowed exactly one admin account. Multi-user account
    # management is a product requirement, so the guard is removed and the
    # existing account keeps full admin rights through the role column.
    conn.execute("DROP TRIGGER IF EXISTS trg_users_single_admin")

    user_cols = columns("users")
    for column, ddl in (
        ("role", "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'"),
        ("display_name", "ALTER TABLE users ADD COLUMN display_name TEXT"),
        ("is_active", "ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1"),
        ("must_change_password", "ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0"),
        ("last_login_at", "ALTER TABLE users ADD COLUMN last_login_at INTEGER"),
        ("totp_last_step", "ALTER TABLE users ADD COLUMN totp_last_step INTEGER"),
    ):
        if column not in user_cols:
            conn.execute(ddl)

    call_cols = columns("call_sessions")
    for column, ddl in (
        ("recording_reason", "ALTER TABLE call_sessions ADD COLUMN recording_reason TEXT"),
        ("recording_announced", "ALTER TABLE call_sessions ADD COLUMN recording_announced INTEGER NOT NULL DEFAULT 0"),
        ("recording_notice", "ALTER TABLE call_sessions ADD COLUMN recording_notice TEXT"),
        ("recording_notice_at", "ALTER TABLE call_sessions ADD COLUMN recording_notice_at INTEGER"),
        ("recording_notice_ok", "ALTER TABLE call_sessions ADD COLUMN recording_notice_ok INTEGER"),
        ("recording_notice_error", "ALTER TABLE call_sessions ADD COLUMN recording_notice_error TEXT"),
        ("translate_mode", "ALTER TABLE call_sessions ADD COLUMN translate_mode TEXT"),
        ("summary", "ALTER TABLE call_sessions ADD COLUMN summary TEXT"),
        ("summary_at", "ALTER TABLE call_sessions ADD COLUMN summary_at INTEGER"),
        ("task_id", "ALTER TABLE call_sessions ADD COLUMN task_id INTEGER"),
        ("task_purpose", "ALTER TABLE call_sessions ADD COLUMN task_purpose TEXT"),
        ("task_achieved", "ALTER TABLE call_sessions ADD COLUMN task_achieved INTEGER"),
        ("task_result", "ALTER TABLE call_sessions ADD COLUMN task_result TEXT"),
    ):
        if column not in call_cols:
            conn.execute(ddl)


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    """Yield a sqlite3 connection with WAL, foreign keys, and row factory."""
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
