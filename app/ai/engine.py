"""AI engine facade.

Callers ask the engine for chat, transcription, synthesis, translation or a
summary and never pick a vendor. The engine resolves a capability to its pool
of endpoints and lets the router walk that pool, so a provider that times out
or rate limits fails over to the next one without the caller knowing.

Configuration comes from the database first and the environment second, and
the first load turns the old flat ai.* settings into endpoint rows so an
existing install keeps working after the upgrade.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, List, Optional, Sequence

from .. import repo_admin, repo_ai
from ..config import settings
from .providers import (
    AIProviderError,
    BaseProvider,
    ChatMessage,
    MockProvider,
    OpenAICompatibleProvider,
)
from .registry import CapabilityRouter, Endpoint, NoEndpointConfigured, NoStreamEndpoint, router

log = logging.getLogger(__name__)

__all__ = ["AIEngine", "AIError", "ai_engine", "NoStreamEndpoint"]


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

# Stands in for a pool endpoint when nothing is configured, so the offline mock
# keeps every feature exercisable without an API key.
_FALLBACK_ENDPOINT = Endpoint(
    id=0, capability="fallback", name="离线模拟", kind="local", base_url="", model="mock"
)


def _cast(raw, caster):
    if caster is bool:
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if caster is str:
        return "" if raw is None else str(raw)
    return raw


class AIEngine:
    def __init__(self, router: Optional[CapabilityRouter] = None) -> None:
        self._router = router or globals()["router"]
        self._fallback: BaseProvider = MockProvider()
        self._kind = "mock"
        self._config: dict = {}
        self._lock = asyncio.Lock()
        self._loaded = False

    # -- configuration --------------------------------------------------

    async def load(self) -> None:
        """Resolve configuration, seed the pool once, and refresh the router."""
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

        await self._router.refresh()

        # An explicit provider choice always wins. "auto" prefers the endpoint
        # pool when an admin has built one, then a single keyed provider, then
        # the offline mock. Endpoints are never seeded implicitly: turning on a
        # key must not quietly start routing calls to whatever base_url happens
        # to be in the settings. The console offers a one-click migration.
        explicit = (config.get("ai.provider") or "auto").strip().lower()
        pooled = any(self._router.endpoints(cap) for cap in repo_ai.CAPABILITIES)
        has_key = any(config.get(k) for k in ("ai.chat.api_key", "ai.asr.api_key", "ai.tts.api_key"))
        if explicit == "auto":
            kind = "pool" if pooled else ("openai" if has_key else "mock")
        elif explicit == "pool":
            kind = "pool" if pooled else ("openai" if has_key else "mock")
        else:
            kind = explicit
        if not config.get("ai.enabled", True) and kind != "mock":
            kind = "disabled"

        async with self._lock:
            old = self._fallback
            if kind in ("mock", "disabled", "pool"):
                self._fallback = MockProvider()
            else:
                self._fallback = OpenAICompatibleProvider(
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
            if old is not None and old is not self._fallback:
                try:
                    await old.close()
                except Exception:
                    pass
        log.info("ai engine ready provider=%s endpoints=%s", kind, pooled)

    async def reload(self) -> None:
        await self.load()

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def provider(self) -> BaseProvider:
        """The fallback provider. Kept for callers that predate the pool."""
        return self._fallback

    @property
    def router(self) -> CapabilityRouter:
        return self._router

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
            "pool": self._router.status(),
        }

    def _require(self) -> BaseProvider:
        if self._kind == "disabled":
            raise AIError("AI 功能已在后台关闭")
        return self._fallback

    async def _run(
        self,
        capability: str,
        work: Callable[[BaseProvider, Endpoint], Awaitable[Any]],
        *,
        require_stream: bool = False,
        call_id: Optional[str] = None,
    ) -> Any:
        if self._kind == "disabled":
            raise AIError("AI 功能已在后台关闭")
        if self._kind != "pool" and not self._router.endpoints(capability):
            # Single provider mode ("mock" or the legacy "openai"): there is no
            # pool to walk, so call the configured provider directly.
            try:
                return await work(self._require(), _FALLBACK_ENDPOINT)
            except AIProviderError as e:
                raise AIError(str(e))
        try:
            return await self._router.run(
                capability, work, require_stream=require_stream, call_id=call_id
            )
        except NoEndpointConfigured:
            # The pool is configured for other capabilities but not this one,
            # so fall back rather than failing the call outright.
            try:
                return await work(self._require(), _FALLBACK_ENDPOINT)
            except AIProviderError as e:
                raise AIError(str(e))
        except AIProviderError as e:
            raise AIError(str(e))

    # -- channels -------------------------------------------------------

    def _messages(
        self, text: str, *, history: Optional[Sequence[dict]] = None, system: Optional[str] = None
    ) -> List[ChatMessage]:
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
        return messages

    async def chat(
        self,
        text: str,
        *,
        history: Optional[Sequence[dict]] = None,
        system: Optional[str] = None,
        temperature: float = 0.4,
        max_tokens: int = 800,
        call_id: Optional[str] = None,
    ) -> str:
        messages = self._messages(text, history=history, system=system)

        async def work(provider: BaseProvider, endpoint: Endpoint) -> str:
            reply = await provider.chat(
                messages,
                model=endpoint.model or None,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return (reply or "").strip()

        return await self._run("chat", work, call_id=call_id)

    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str = "audio.wav",
        language: Optional[str] = None,
        call_id: Optional[str] = None,
    ) -> str:
        if not audio:
            raise AIError("空的音频数据")

        async def work(provider: BaseProvider, endpoint: Endpoint) -> str:
            return await provider.transcribe(audio, filename=filename, language=language)

        return await self._run("asr", work, call_id=call_id)

    async def synthesize(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
        call_id: Optional[str] = None,
    ) -> bytes:
        if not (text or "").strip():
            raise AIError("合成文本为空")

        async def work(provider: BaseProvider, endpoint: Endpoint) -> bytes:
            chosen = voice or endpoint.params.get("voice") or None
            return await provider.synthesize(text, voice=chosen)

        return await self._run("tts", work, call_id=call_id)

    async def translate(
        self,
        text: str,
        *,
        source: Optional[str] = None,
        target: str,
        glossary: Optional[Sequence[str]] = None,
        call_id: Optional[str] = None,
    ) -> str:
        """Translate one utterance. Empty input returns empty output."""
        text = (text or "").strip()
        if not text:
            return ""
        prompt = build_translation_prompt(source=source, target=target, glossary=glossary)
        messages = [ChatMessage("system", prompt), ChatMessage("user", text)]

        async def work(provider: BaseProvider, endpoint: Endpoint) -> str:
            reply = await provider.chat(
                messages,
                model=endpoint.model or None,
                temperature=float(endpoint.params.get("temperature", 0.1)),
                max_tokens=int(endpoint.params.get("max_tokens", 600)),
            )
            return (reply or "").strip()

        return await self._run("translate", work, call_id=call_id)

    async def summarize(
        self,
        transcript: Sequence[dict],
        *,
        purpose: Optional[str] = None,
        template: Optional[str] = None,
        call_id: Optional[str] = None,
    ) -> dict:
        """Turn a transcript into a summary, decisions, follow-ups and outcome."""
        from .summary import build_summary_prompt, parse_summary

        payload = build_summary_prompt(transcript, purpose=purpose, template=template)
        messages = [ChatMessage("system", payload["system"]), ChatMessage("user", payload["user"])]

        async def work(provider: BaseProvider, endpoint: Endpoint) -> str:
            return await provider.chat(
                messages,
                model=endpoint.model or None,
                temperature=float(endpoint.params.get("temperature", 0.2)),
                max_tokens=int(endpoint.params.get("max_tokens", 1200)),
            )

        raw = await self._run("summary", work, call_id=call_id)
        return parse_summary(raw)

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
        report["pool"] = self._router.status()
        return report


LANGUAGE_NAMES = {
    "zh": "中文",
    "en": "英文",
    "ja": "日文",
    "ko": "韩文",
    "fr": "法文",
    "de": "德文",
    "es": "西班牙文",
    "ru": "俄文",
    "pt": "葡萄牙文",
    "ar": "阿拉伯文",
    "it": "意大利文",
    "th": "泰文",
    "vi": "越南文",
    "id": "印尼文",
}


def build_translation_prompt(
    *, source: Optional[str] = None, target: str, glossary: Optional[Sequence[str]] = None
) -> str:
    """System prompt for the translate capability.

    The output goes straight into a text-to-speech engine or a subtitle line, so
    the model must return nothing but the translation.
    """
    target_name = LANGUAGE_NAMES.get(target, target)
    source_name = LANGUAGE_NAMES.get(source, source) if source else None
    lines = [
        f"你是电话通话的实时翻译。把用户给出的话翻译成{target_name}。",
        "只输出译文本身，不要解释、不要引号、不要标注说话人，不要输出原文。",
        "这是口语通话，译文要自然、简短、可以直接朗读出来。",
        "保留数字、电话号码、金额、日期和专有名词的原始形式。",
        "如果输入本身就是目标语言，原样输出。",
        "如果输入没有实际内容（只有语气词或空白），输出空字符串。",
    ]
    if source_name:
        lines.insert(1, f"源语言通常是{source_name}，但以实际内容为准。")
    terms = [t.strip() for t in (glossary or []) if t and t.strip()]
    if terms:
        lines.append("术语对照（必须照此翻译）：" + "；".join(terms[:40]))
    return "\n".join(lines)


ai_engine = AIEngine()
