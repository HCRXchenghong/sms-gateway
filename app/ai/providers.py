"""Provider clients for chat, ASR and TTS.

All three channels speak the OpenAI-compatible HTTP shape, which covers OpenAI
itself plus DeepSeek, Moonshot, Qwen, SiliconFlow, Azure OpenAI and most
self-hosted gateways. A MockProvider keeps every code path testable without
network access or an API key.
"""
from __future__ import annotations

import asyncio
import io
import logging
import math
import wave
from dataclasses import dataclass
from typing import AsyncIterator, List, Optional, Sequence

import httpx

log = logging.getLogger(__name__)


class AIProviderError(RuntimeError):
    """Raised when a provider call fails."""


@dataclass
class ChatMessage:
    role: str  # system | user | assistant
    content: str

    def to_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


class BaseProvider:
    name = "base"

    async def chat(self, messages: Sequence[ChatMessage], **kwargs) -> str:
        raise NotImplementedError

    async def transcribe(self, audio: bytes, *, filename: str = "audio.wav", language: Optional[str] = None) -> str:
        raise NotImplementedError

    async def synthesize(self, text: str, **kwargs) -> bytes:
        raise NotImplementedError

    async def close(self) -> None:
        return None


class OpenAICompatibleProvider(BaseProvider):
    """Talks to any OpenAI-compatible /v1 endpoint."""

    name = "openai"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        timeout: float = 30.0,
        chat_model: str = "gpt-4o-mini",
        asr_model: str = "whisper-1",
        tts_model: str = "tts-1",
        tts_voice: str = "alloy",
        tts_format: str = "wav",
        extra_headers: Optional[dict] = None,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout = timeout
        self.chat_model = chat_model
        self.asr_model = asr_model
        self.tts_model = tts_model
        self.tts_voice = tts_voice
        self.tts_format = tts_format or "wav"
        self._extra_headers = extra_headers or {}
        self._client = httpx.AsyncClient(timeout=timeout)

    def _headers(self, json_body: bool = True) -> dict:
        headers = dict(self._extra_headers)
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    async def close(self) -> None:
        await self._client.aclose()

    def _url(self, path: str) -> str:
        if not self.base_url:
            raise AIProviderError("AI base_url is not configured")
        return f"{self.base_url}/{path.lstrip('/')}"

    async def chat(self, messages: Sequence[ChatMessage], **kwargs) -> str:
        payload = {
            "model": kwargs.get("model") or self.chat_model,
            "messages": [m.to_dict() for m in messages],
            "temperature": kwargs.get("temperature", 0.4),
            "max_tokens": kwargs.get("max_tokens", 800),
            "stream": False,
        }
        try:
            resp = await self._client.post(self._url("/chat/completions"), json=payload, headers=self._headers())
        except httpx.HTTPError as e:
            raise AIProviderError(f"chat request failed: {e}")
        if resp.status_code >= 400:
            raise AIProviderError(f"chat HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise AIProviderError(f"unexpected chat response shape: {e}")

    async def chat_stream(self, messages: Sequence[ChatMessage], **kwargs) -> AsyncIterator[str]:
        payload = {
            "model": kwargs.get("model") or self.chat_model,
            "messages": [m.to_dict() for m in messages],
            "temperature": kwargs.get("temperature", 0.4),
            "stream": True,
        }
        try:
            async with self._client.stream(
                "POST", self._url("/chat/completions"), json=payload, headers=self._headers()
            ) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise AIProviderError(f"chat HTTP {resp.status_code}: {body[:300].decode('utf-8', 'replace')}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        return
                    import json

                    try:
                        delta = json.loads(chunk)["choices"][0]["delta"].get("content")
                    except (KeyError, IndexError, ValueError, TypeError):
                        continue
                    if delta:
                        yield delta
        except httpx.HTTPError as e:
            raise AIProviderError(f"chat stream failed: {e}")

    async def transcribe(self, audio: bytes, *, filename: str = "audio.wav", language: Optional[str] = None) -> str:
        form = {"model": self.asr_model}
        if language:
            form["language"] = language
        files = {"file": (filename, io.BytesIO(audio), "audio/wav")}
        try:
            resp = await self._client.post(
                self._url("/audio/transcriptions"), data=form, files=files, headers=self._headers(json_body=False)
            )
        except httpx.HTTPError as e:
            raise AIProviderError(f"asr request failed: {e}")
        if resp.status_code >= 400:
            raise AIProviderError(f"asr HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        text = data.get("text")
        if text is None:
            raise AIProviderError(f"unexpected asr response shape: {list(data)[:6]}")
        return str(text)

    async def synthesize(self, text: str, **kwargs) -> bytes:
        payload = {
            "model": kwargs.get("model") or self.tts_model,
            "input": text,
            "voice": kwargs.get("voice") or self.tts_voice,
            "response_format": kwargs.get("format") or self.tts_format,
        }
        try:
            resp = await self._client.post(self._url("/audio/speech"), json=payload, headers=self._headers())
        except httpx.HTTPError as e:
            raise AIProviderError(f"tts request failed: {e}")
        if resp.status_code >= 400:
            raise AIProviderError(f"tts HTTP {resp.status_code}: {resp.text[:300]}")
        if not resp.content:
            raise AIProviderError("tts returned an empty body")
        return resp.content


class MockProvider(BaseProvider):
    """Deterministic offline provider used by tests and for evaluation.

    Chat echoes a summarised form of the last user turn so conversation flow is
    observable. ASR returns a fixed transcript. TTS renders a short WAV whose
    length tracks the input text, which is enough to verify the plumbing.
    """

    name = "mock"

    def __init__(self, latency: float = 0.0, reply_prefix: str = "【模拟回复】"):
        self.latency = latency
        self.reply_prefix = reply_prefix
        self.calls: List[dict] = []

    async def chat(self, messages: Sequence[ChatMessage], **kwargs) -> str:
        if self.latency:
            await asyncio.sleep(self.latency)
        last_user = next(
            (m.content for m in reversed(list(messages)) if m.role == "user"), ""
        )
        self.calls.append({"channel": "chat", "messages": [m.to_dict() for m in messages]})
        return f"{self.reply_prefix}你刚才说的是「{last_user.strip()[:60]}」。这是离线模拟回复，配置真实的 AI 供应商后会返回模型生成的内容。"

    async def chat_stream(self, messages: Sequence[ChatMessage], **kwargs) -> AsyncIterator[str]:
        reply = await self.chat(messages, **kwargs)
        for piece in reply:
            yield piece
            await asyncio.sleep(0)

    async def transcribe(self, audio: bytes, *, filename: str = "audio.wav", language: Optional[str] = None) -> str:
        if self.latency:
            await asyncio.sleep(self.latency)
        seconds = _wav_duration(audio)
        self.calls.append({"channel": "asr", "bytes": len(audio), "seconds": seconds})
        return f"这是模拟转写结果，音频时长约 {seconds:.1f} 秒。"

    async def synthesize(self, text: str, **kwargs) -> bytes:
        if self.latency:
            await asyncio.sleep(self.latency)
        self.calls.append({"channel": "tts", "text": text})
        return _render_placeholder_wav(text)


def _wav_duration(audio: bytes) -> float:
    try:
        with wave.open(io.BytesIO(audio)) as handle:
            rate = handle.getframerate() or 16000
            return handle.getnframes() / float(rate)
    except (wave.Error, EOFError):
        return 0.0


def _render_placeholder_wav(text: str, rate: int = 16000) -> bytes:
    """Render a tone whose length follows the text, as a stand-in for speech."""
    seconds = max(0.4, min(6.0, 0.18 * max(1, len(text))))
    frames = int(rate * seconds)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        data = bytearray()
        for i in range(frames):
            envelope = 0.28
            fade = int(rate * 0.02)
            if i < fade:
                envelope *= i / max(1, fade)
            elif i > frames - fade:
                envelope *= (frames - i) / max(1, fade)
            sample = envelope * math.sin(2 * math.pi * 440.0 * i / rate)
            sample += 0.5 * envelope * math.sin(2 * math.pi * 660.0 * i / rate)
            value = int(max(-1.0, min(1.0, sample)) * 32767)
            data += value.to_bytes(2, "little", signed=True)
        handle.writeframes(bytes(data))
    return buf.getvalue()
