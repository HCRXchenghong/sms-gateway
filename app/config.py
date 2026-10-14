"""Application configuration loaded from env / defaults."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

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
    modem_at_port: Optional[str] = None
    modem_baudrate: int = 115200
    modem_probe_interval_seconds: int = 30
    modem_at_timeout_seconds: float = 10.0
    modem_send_timeout_seconds: float = 60.0

    # Auth
    session_secret_file: Path = Path("/etc/sms-gateway/secret.key")
    session_cookie_name: str = "sms_session"
    session_max_age_seconds: int = 8 * 3600
    # Mobile sessions outlive browser ones so the app does not re-auth daily.
    mobile_session_max_age_seconds: int = 30 * 24 * 3600
    bcrypt_cost: int = 12
    login_lock_window_seconds: int = 300
    login_lock_max_attempts: int = 5
    login_lock_duration_seconds: int = 900

    # API rate limiting
    api_send_rate_limit_per_minute: int = 30

    # Recovery codes
    totp_enroll_ttl_seconds: int = 600
    recovery_codes_count: int = 10

    # Message retention
    message_retention_days: int = 7

    # Development / preview switch: when true, no real serial port is opened.
    mock_modem: bool = False
    mock_modem_count: int = 1

    # Voice calls
    voice_enabled: bool = True
    voice_dial_timeout_seconds: float = 45.0
    voice_answer_timeout_seconds: float = 15.0
    voice_tts_timeout_seconds: float = 60.0
    # 1 = UCS2 hex (safe for Chinese), 2 = literal text in voice_tts_text_encoding
    voice_tts_mode: int = 1
    voice_tts_text_encoding: str = "gb2312"
    voice_tts_enabled: bool = True
    voice_clcc_poll_interval_seconds: float = 1.0
    # A single empty AT+CLCC reply must not end a call. SIMCom modules answer
    # with no rows for a moment while ATA/ATD is still being processed, which
    # used to stamp every answered inbound call as hung up in the same second
    # it was picked up. Polling is only a fallback behind the URCs, so require
    # a grace window plus a streak of empty replies before trusting it.
    voice_clcc_absent_grace_seconds: float = 2.0
    voice_clcc_absent_threshold: int = 3
    voice_ring_timeout_seconds: int = 60
    voice_dial_timeout_answer_seconds: int = 90
    voice_volume_max: int = 5
    voice_volume_default: int = 3
    # Audio channel handed to AT+CSDVC. Values are firmware specific; 0 keeps
    # the module default and skips the command entirely.
    voice_audio_channel: int = 0
    voice_max_concurrent_calls: int = 4

    # Audio subsystem
    audio_backend: str = "auto"  # auto | pulse | alsa | none
    audio_output_device: str = ""
    audio_input_device: str = ""
    audio_prompt_dir: Path = Path("assets/prompts")
    audio_prompt_volume: float = 0.8
    audio_record_enabled: bool = False
    audio_record_dir: Path = Path("/var/lib/sms-gateway/recordings")
    audio_sample_rate: int = 16000
    # Play prompts through the server sound card as well as pushing them to
    # clients. Useful when the operator sits at the machine running the modem.
    audio_local_prompts: bool = True
    # Speak prompts into the call through the module synthesiser.
    voice_prompt_into_call: bool = False

    # AI providers. Keys are read from env; secrets are encrypted at rest.
    ai_enabled: bool = True
    ai_chat_base_url: str = "https://api.openai.com/v1"
    ai_chat_model: str = "gpt-4o-mini"
    ai_chat_api_key: str = ""
    ai_asr_base_url: str = "https://api.openai.com/v1"
    ai_asr_model: str = "whisper-1"
    ai_asr_api_key: str = ""
    ai_tts_base_url: str = "https://api.openai.com/v1"
    ai_tts_model: str = "tts-1"
    ai_tts_voice: str = "alloy"
    ai_tts_api_key: str = ""
    ai_tts_format: str = "wav"
    # auto picks the OpenAI-compatible client when a key is present, otherwise
    # falls back to the offline mock so every feature stays testable.
    ai_provider: str = "auto"
    ai_chat_system_prompt: str = (
        "你是一个电话语音助手，代表网关的操作者与来电者或短信发件人沟通。"
        "回答要口语化、简短，控制在两句话以内，方便语音合成播报。"
        "不要使用表情符号、Markdown 或列表。"
    )
    ai_request_timeout_seconds: float = 30.0
    # provider | module | none. module uses the A7670 built-in synthesiser.
    ai_tts_engine: str = "module"
    ai_auto_reply_sms: bool = False
    ai_auto_answer_call: bool = False

    # Endpoint failover. Every capability keeps an ordered pool; a timeout,
    # rate limit or 5xx opens the circuit for that endpoint and the request
    # falls through to the next one. After the cooldown a single probe is let
    # through, and success closes the circuit again.
    ai_failover_enabled: bool = True
    ai_breaker_failure_threshold: int = 3
    ai_breaker_cooldown_seconds: float = 30.0
    ai_call_log_retention_hours: int = 72

    # Real-time translation. Mode is off | subtitle (translate the far end to
    # text) | voice (play each side the other's language). Cadence is
    # sentence (translate after a short utterance) or stream (translate while
    # the other side is still speaking).
    translate_mode: str = "off"
    translate_cadence: str = "sentence"
    translate_my_language: str = "zh"
    translate_peer_language: str = "en"
    # When no stream-capable endpoint survives failover, either drop back to
    # sentence cadence or stop translating. Dropping back keeps the call
    # usable; the console shows which one happened.
    translate_stream_degrade_to_sentence: bool = True
    translate_max_sentence_seconds: float = 12.0
    translate_silence_end_seconds: float = 0.7

    # Recording. Policy is forced | optional | disabled and decides whether
    # audio is kept at all. Announcement is separate: silent keeps recording
    # without telling the far end, announced plays a notice into the call.
    recording_policy: str = "optional"
    recording_announce: bool = False
    recording_default_enabled: bool = True
    # Text and minutes are independent of the audio file, so a call can be
    # transcribed and summarised without keeping the recording.
    transcript_save: bool = True
    summary_save: bool = True
    summary_auto_generate: bool = True

    # Inbound handling: manual | auto_answer | auto_answer_ai.
    inbound_mode: str = "manual"
    inbound_auto_answer_delay_seconds: float = 3.0
    inbound_ai_max_seconds: int = 300
    # What the AI is supposed to achieve when it picks the phone up itself.
    # Empty falls back to a built-in purpose: identify the caller, find out
    # what they want, promise a call back.
    inbound_ai_purpose: str = ""
    inbound_ai_opener: str = ""
    # Outbound AI tasks hang up once the stated purpose is met, and never run
    # longer than this even if the conversation is still going.
    ai_call_max_seconds: int = 600
    # Extra instructions for the minutes. Empty uses the built-in template.
    summary_template: str = ""

    @property
    def db_path(self) -> Path:
        return self.data_dir / "data.db"

    @property
    def log_path(self) -> Path:
        return self.log_dir / "app.log"


settings = Settings()
