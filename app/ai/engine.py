"""AI engine facade.

One object owns the provider instance and resolves configuration from the
database first and the environment second, so an admin can repoint chat, ASR
or TTS at a different vendor from the web console without a restart.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional, Sequence

from .. import repo_admin
from ..config import settings
from .providers import (
    AIProviderError,
    BaseProvider,
    ChatMessage,
    MockProvider,
    OpenAICompatibleProvider,
)

log = logging.getLogger(__name__)

__all__ = ["AIEngine", "AIError", "ai_engine"]


class AIError(RuntimeError):
    """User-facing AI failure."""


_DEFAULTS = {
    "ai.provider": ("ai_provider", str),
    "ai.enabled": ("ai_enabled", bool),
    "ai.chat.base_url": ("ai_chat_base_url", str),
    "ai.chat.model": ("ai_chat_model", str),
    "ai.asr.base_url": ("ai_asr_base_url", str),
    "ai.asr.model": ("ai_asr_model", str),
    "ai.tts.base_url": ("ai_tts_base_url", str),
    "ai.tts.model": ("ai_tts_model", str),
    "ai.tts.voice": ("ai_tts_voice", str),
    "ai.tts.format": ("ai_tts_format", str),
    "ai.tts.engine": ("ai_tts_engine", str),
    "ai.chat.system_prompt": ("ai_chat_system_prompt", str),
}


def _cast(raw, caster):
    if caster is bool:
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if caster is str:
        return "" if raw is None else str(raw)
    return raw


class AIEngine:
    def __init__(self) -> None:
        self._provider: BaseProvider = MockProvider()
        self._kind = "mock"
        self._config: dict = {}
        self._lock = asyncio.Lock()
        self._loaded = False

    # -- configuration --------------------------------------------------

    async def load(self) -> None:
        """Resolve configuration and (re)build the provider."""
        config: dict = {}
        for key, (attr, caster) in _DEFAULTS.items():
            raw = await asyncio.to_thread(repo_admin.get_setting, key)
            config[key] = _cast(raw if raw is not None else getattr(settings, attr), caster)
        for key, attr in (
            ("ai.chat.api_key", "ai_chat_api_key"),
            ("ai.asr.api_key", "ai_asr_api_key"),
            ("ai.tts.api_key", "ai_tts_api_key"),
        ):
            secret = await asyncio.to_thread(repo_admin.get_secret, key)
            config[key] = secret if secret else getattr(settings, attr, "")

        kind = (config.get("ai.provider") or "auto").strip().lower()
        if kind == "auto":
            has_key = any(config.get(k) for k in ("ai.chat.api_key", "ai.asr.api_key", "ai.tts.api_key"))
            kind = "openai" if has_key else "mock"
        if not config.get("ai.enabled", True) and kind != "mock":
            kind = "disabled"

        async with self._lock:
            old = self._provider
            if kind == "mock":
                self._provider = MockProvider()
            elif kind == "disabled":
                self._provider = MockProvider()
            else:
                self._provider = OpenAICompatibleProvider(
                    base_url=config.get("ai.chat.base_url") or config.get("ai.tts.base_url") or "",
                    api_key=config.get("ai.chat.api_key") or config.get("ai.tts.api_key") or "",
                    timeout=float(settings.ai_request_timeout_seconds),
                    chat_model=config.get("ai.chat.model") or "gpt-4o-mini",
                    asr_model=config.get("ai.asr.model") or "whisper-1",
                    tts_model=config.get("ai.tts.model") or "tts-1",
                    tts_voice=config.get("ai.tts.voice") or "alloy",
                    tts_format=config.get("ai.tts.format") or "wav",
                )
            self._kind = kind
            self._config = config
            self._loaded = True
            if old is not None and old is not self._provider:
                try:
                    await old.close()
                except Exception:
                    pass
        log.info("ai engine ready provider=%s", kind)

    async def reload(self) -> None:
        await self.load()

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def provider(self) -> BaseProvider:
        return self._provider

    def status(self) -> dict:
        """Configuration snapshot with every secret redacted."""
        cfg = dict(self._config)
        for key in list(cfg):
            if "api_key" in key:
                value = cfg[key] or ""
                cfg[key] = f"{value[:6]}…({len(value)} chars)" if value else ""
        return {
            "provider": self._kind,
            "loaded": self._loaded,
            "tts_engine": cfg.get("ai.tts.engine"),
            "config": cfg,
        }

    def _require(self) -> BaseProvider:
        if self._kind == "disabled":
            raise AIError("AI 功能已在后台关闭")
        return self._provider

    # -- channels -------------------------------------------------------

    async def chat(
        self,
        text: str,
        *,
        history: Optional[Sequence[dict]] = None,
        system: Optional[str] = None,
        temperature: float = 0.4,
        max_tokens: int = 800,
    ) -> str:
        provider = self._require()
        messages: List[ChatMessage] = []
        system_prompt = system if system is not None else self._config.get("ai.chat.system_prompt")
        if system_prompt:
            messages.append(ChatMessage("system", system_prompt))
        for turn in history or []:
            role = turn.get("role")
            content = turn.get("content")
            if role in ("user", "assistant", "system") and content:
                messages.append(ChatMessage(role, str(content)))
        messages.append(ChatMessage("user", text))
        try:
            reply = await provider.chat(messages, temperature=temperature, max_tokens=max_tokens)
        except AIProviderError as e:
            raise AIError(str(e))
        return (reply or "").strip()

    async def transcribe(
        self, audio: bytes, *, filename: str = "audio.wav", language: Optional[str] = None
    ) -> str:
        provider = self._require()
        if not audio:
            raise AIError("空的音频数据")
        try:
            return await provider.transcribe(audio, filename=filename, language=language)
        except AIProviderError as e:
            raise AIError(str(e))

    async def synthesize(self, text: str, *, voice: Optional[str] = None) -> bytes:
        provider = self._require()
        if not (text or "").strip():
            raise AIError("合成文本为空")
        try:
            return await provider.synthesize(text, voice=voice)
        except AIProviderError as e:
            raise AIError(str(e))

    async def health(self) -> dict:
        """Probe each channel so the console can show a green or red light."""
        report = {"provider": self._kind, "chat": "unknown", "asr": "unknown", "tts": "unknown"}
        if self._kind == "disabled":
            report.update(chat="disabled", asr="disabled", tts="disabled")
            return report
        try:
            reply = await self.chat("健康检查，请回复 ok", max_tokens=16)
            report["chat"] = "ok" if reply else "empty"
            report["chat_sample"] = reply[:60]
        except AIError as e:
            report["chat"] = f"error: {e}"
        try:
            audio = await self.synthesize("健康检查")
            report["tts"] = "ok" if audio else "empty"
            report["tts_bytes"] = len(audio or b"")
        except AIError as e:
            report["tts"] = f"error: {e}"
        # ASR is probed with our own synthesised audio when TTS works.
        try:
            sample = await self.synthesize("一二三")
            text = await self.transcribe(sample)
            report["asr"] = "ok" if text else "empty"
            report["asr_sample"] = text[:60]
        except AIError as e:
            report["asr"] = f"error: {e}"
        return report


ai_engine = AIEngine()
