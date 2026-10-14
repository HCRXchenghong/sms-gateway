"""Password / TOTP / API-key cryptographic helpers."""
from __future__ import annotations

import binascii
import hashlib
import hmac
import os
import re
import secrets
import time
from pathlib import Path
from typing import Optional, Tuple

import bcrypt
import pyotp
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import settings

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
PASSWORD_RE = re.compile(r"^(?=.*[a-z])(?=.*[A-Z]).{8,}$")


def validate_username(username: str) -> Optional[str]:
    """Return a Chinese error message, or None when the username is acceptable."""
    if not username or not USERNAME_RE.match(username):
        return "用户名需 3-32 位，仅包含字母、数字、下划线、点和短横线"
    return None


def validate_password(password: str) -> Optional[str]:
    if not password or len(password) < 8:
        return "密码至少 8 位"
    if not PASSWORD_RE.match(password):
        return "密码至少 8 位，并且必须同时包含大写和小写字母"
    return None


def load_or_create_master_key() -> bytes:
    """Load the local AES-256 master key, generating one on first run."""
    p: Path = settings.session_secret_file
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        data = p.read_bytes()
        if len(data) >= 32:
            return data[:32]
    key = secrets.token_bytes(32)
    p.write_bytes(key)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return key


def encrypt_secret(plaintext: bytes) -> bytes:
    """AES-GCM encrypt; output = nonce(12) || ciphertext || tag(16)."""
    key = load_or_create_master_key()
    aes = AESGCM(key)
    nonce = secrets.token_bytes(12)
    ct = aes.encrypt(nonce, plaintext, None)
    return nonce + ct


def decrypt_secret(blob: bytes) -> bytes:
    key = load_or_create_master_key()
    aes = AESGCM(key)
    nonce, ct = blob[:12], blob[12:]
    return aes.decrypt(nonce, ct, None)


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(
        plain.encode("utf-8"), bcrypt.gensalt(rounds=settings.bcrypt_cost)
    ).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def new_totp_secret() -> str:
    """Return a base32-encoded 160-bit secret."""
    return pyotp.random_base32(length=32)


def totp_provisioning_uri(secret: str, username: str, issuer: str = "SMS-Gateway") -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


def verify_totp(secret: str, code: str, drift: int = 1) -> bool:
    """Return False for malformed input instead of raising."""
    return verify_totp_step(secret, code, drift) is not None


def verify_totp_step(secret: str, code: str, drift: int = 1) -> Optional[int]:
    """Return the time step a code belongs to, or None when it does not match.

    Callers that must refuse a reused code need the step, not just a yes/no:
    recording the last accepted step is what stops a code that was already
    spent during enrollment from logging the account in again.
    """
    if not code or not code.isdigit() or len(code) != 6:
        return None
    try:
        totp = pyotp.TOTP(secret)
    except (ValueError, TypeError, binascii.Error):
        return None
    now = time.time()
    for offset in range(-abs(int(drift)), abs(int(drift)) + 1):
        when = now + offset * totp.interval
        try:
            candidate = str(totp.at(when))
        except (ValueError, TypeError, binascii.Error):
            return None
        if hmac.compare_digest(candidate, code):
            # pyotp.timecode() only accepts datetime; TOTP.at() already derived
            # this step from the raw timestamp, so derive it the same way.
            return int(when // totp.interval)
    return None


def generate_recovery_codes(count: int) -> Tuple[list[str], list[str]]:
    """Return (plaintext_codes, hashes). Plaintext is shown once."""
    codes = []
    hashes = []
    for _ in range(count):
        raw = secrets.token_urlsafe(10)
        codes.append(raw)
        hashes.append(hash_recovery_code(raw))
    return codes, hashes


def hash_recovery_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def verify_recovery_code(code: str, hashed: str) -> bool:
    return hmac.compare_digest(hash_recovery_code(code), hashed)


def generate_api_key() -> Tuple[str, str, str, str]:
    """Generate a new API key, returning plaintext, salt, hash, and prefix."""
    raw = secrets.token_urlsafe(32)
    plaintext = f"smsgw_{raw}"
    salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + plaintext).encode("utf-8")).hexdigest()
    prefix = plaintext[:8]
    return plaintext, salt, h, prefix


def verify_api_key(plaintext: str, salt: str, expected_hash: str) -> bool:
    h = hashlib.sha256((salt + plaintext).encode("utf-8")).hexdigest()
    return hmac.compare_digest(h, expected_hash)


def new_session_id() -> str:
    return secrets.token_urlsafe(32)


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def now_epoch() -> int:
    return int(time.time())
