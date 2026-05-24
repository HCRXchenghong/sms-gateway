"""Password / TOTP / API-key cryptographic helpers."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from typing import Tuple

import bcrypt
import pyotp
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import settings


# ---------------------------------------------------------------------------
# Master key (used to encrypt TOTP secrets at rest)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=settings.bcrypt_cost)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# TOTP
# ---------------------------------------------------------------------------


def new_totp_secret() -> str:
    """Return a base32-encoded 160-bit secret."""
    return pyotp.random_base32(length=32)  # 32 chars b32 ~ 160 bits


def totp_provisioning_uri(secret: str, username: str, issuer: str = "SMS-Gateway") -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


def verify_totp(secret: str, code: str, drift: int = 1) -> bool:
    if not code or not code.isdigit() or len(code) != 6:
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=drift)


# ---------------------------------------------------------------------------
# Recovery codes
# ---------------------------------------------------------------------------


def generate_recovery_codes(count: int) -> Tuple[list[str], list[str]]:
    """Return (plaintext_codes, hashes). Plaintext is shown once."""
    codes = []
    hashes = []
    for _ in range(count):
        raw = secrets.token_urlsafe(10)  # ~13 chars, ~78 bits
        codes.append(raw)
        hashes.append(hash_recovery_code(raw))
    return codes, hashes


def hash_recovery_code(code: str) -> str:
    # Recovery codes are themselves high-entropy, so SHA-256 is sufficient.
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def verify_recovery_code(code: str, hashed: str) -> bool:
    return hmac.compare_digest(hash_recovery_code(code), hashed)


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------


def generate_api_key() -> Tuple[str, str, str, str]:
    """Generate a new API key.

    Returns
    -------
    (plaintext_key, salt_hex, hash_hex, prefix)
    """
    raw = secrets.token_urlsafe(32)  # ~43 chars, 256 bits
    plaintext = f"smsgw_{raw}"
    salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + plaintext).encode("utf-8")).hexdigest()
    prefix = plaintext[:8]
    return plaintext, salt, h, prefix


def verify_api_key(plaintext: str, salt: str, expected_hash: str) -> bool:
    h = hashlib.sha256((salt + plaintext).encode("utf-8")).hexdigest()
    return hmac.compare_digest(h, expected_hash)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def new_session_id() -> str:
    return secrets.token_urlsafe(32)


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# ---------------------------------------------------------------------------
# Time utilities
# ---------------------------------------------------------------------------


def now_epoch() -> int:
    return int(time.time())
