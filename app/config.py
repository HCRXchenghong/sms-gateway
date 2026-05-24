"""Application configuration loaded from env / defaults."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration."""

    model_config = SettingsConfigDict(
        env_prefix="SMSGW_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Network
    host: str = "0.0.0.0"
    port: int = 8443

    # Storage
    data_dir: Path = Path("/var/lib/sms-gateway")
    log_dir: Path = Path("/var/log/sms-gateway")
    config_dir: Path = Path("/etc/sms-gateway")

    # TLS
    tls_cert: Optional[Path] = None
    tls_key: Optional[Path] = None

    # Modem
    modem_at_port: Optional[str] = None  # e.g. /dev/ttyUSB2; auto-detected if None
    modem_baudrate: int = 115200
    modem_probe_interval_seconds: int = 30
    modem_at_timeout_seconds: float = 10.0
    modem_send_timeout_seconds: float = 60.0

    # Auth
    session_secret_file: Path = Path("/etc/sms-gateway/secret.key")
    session_cookie_name: str = "sms_session"
    session_max_age_seconds: int = 8 * 3600
    bcrypt_cost: int = 12
    login_lock_window_seconds: int = 300
    login_lock_max_attempts: int = 5
    login_lock_duration_seconds: int = 900

    # API rate limiting
    api_send_rate_limit_per_minute: int = 30

    # Recovery codes
    recovery_codes_count: int = 10

    # Development / preview switch: when true, no real serial port is opened;
    # the modem layer becomes a no-op stub that lets you exercise the web UI.
    mock_modem: bool = False

    @property
    def db_path(self) -> Path:
        return self.data_dir / "data.db"

    @property
    def log_path(self) -> Path:
        return self.log_dir / "app.log"


settings = Settings()
