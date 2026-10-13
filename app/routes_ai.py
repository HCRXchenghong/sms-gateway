"""AI endpoints: chat, speech recognition, speech synthesis."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import repo
from .audio import prompts as prompt_registry
from .auth import (
    CurrentApiKey,
    CurrentUser,
    get_current_user,
    require_role,
    require_scope_ai,
)
from .ai.engine import AIError, ai_engine
from .voice.manager import VoiceError

router = APIRouter(tags=["ai"])

MAX_AUDIO_BYTES = 25 * 1024 * 1024


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _engine(request: Request):
    engine = getattr(request.app.state, "ai", None) or ai_engine
    return engine


class ChatTurn(BaseModel):
    role: str = Field(..., pattern="^(system|user|assistant)$")
    content: str = Field(..., min_length=1, max_length=8000)


class ChatBody(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)
    history: List[ChatTurn] = Field(default_factory=list, max_length=40)
    system: Optional[str] = Field(default=None, max_length=4000)
    temperature: float = Field(default=0.4, ge=0.0, le=2.0)
    max_tokens: int = Field(default=800, ge=1, le=4000)


class TtsBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    voice: Optional[str] = Field(default=None, max_length=64)


class SpeakAiBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    engine: str = Field(default="module", pattern="^(module|audio)$")


@router.get("/app/ai/status")
async def ai_status(request: Request, user: CurrentUser = Depends(get_current_user)):
    return _engine(request).status()


@router.get("/app/ai/health")
async def ai_health(request: Request, user: CurrentUser = Depends(require_role("admin"))):
    return await _engine(request).health()


@router.post("/app/ai/chat")
async def ai_chat(body: ChatBody, request: Request, user: CurrentUser = Depends(get_current_user)):
    started = time.monotonic()
    try:
        reply = await _engine(request).chat(
            body.message,
            history=[t.model_dump() for t in body.history],
            system=body.system,
            temperature=body.temperature,
            max_tokens=body.max_tokens,
        )
    except AIError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, {"error": {"code": "ai_error", "message": str(e)}})
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request), "ai.chat", None, 200
    )
    return {
        "reply": reply,
        "provider": _engine(request).kind,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


@router.post("/app/ai/asr")
async def ai_asr(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    file: UploadFile = File(...),
    language: Optional[str] = None,
):
    audio = await file.read()
    if not audio:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "上传的音频为空")
    if len(audio) > MAX_AUDIO_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "音频超过 25MB 上限")
    try:
        text = await _engine(request).transcribe(
            audio, filename=file.filename or "audio.wav", language=language
        )
    except AIError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, {"error": {"code": "ai_error", "message": str(e)}})
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request), "ai.asr", None, 200
    )
    return {"text": text, "bytes": len(audio), "provider": _engine(request).kind}


@router.post("/app/ai/tts")
async def ai_tts(request: Request, user: CurrentUser = Depends(get_current_user)):
    """Synthesise speech. Sent as JSON so the body stays typed."""
    body = await request.json()
    parsed = TtsBody(**body)
    try:
        audio = await _engine(request).synthesize(parsed.text, voice=parsed.voice)
    except AIError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, {"error": {"code": "ai_error", "message": str(e)}})
    return Response(content=audio, media_type="audio/wav")


@router.post("/app/ai/tts/prompt/{prompt_id}")
async def ai_render_prompt(
    prompt_id: str, request: Request, user: CurrentUser = Depends(require_role("admin"))
):
    """Replace a prompt tone with real speech rendered by the AI TTS voice.

    The spoken file is written as <id>.tts.wav, which prompt_path prefers over
    the synthesised tone, so the tone stays available as a fallback.
    """
    spec = prompt_registry.resolve_prompt(prompt_id)
    if spec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown prompt {prompt_id}")
    if not spec.text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该提示音没有可合成的文本")
    audio = getattr(request.app.state, "audio", None)
    if audio is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "audio subsystem unavailable")
    try:
        data = await _engine(request).synthesize(spec.text)
    except AIError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, {"error": {"code": "ai_error", "message": str(e)}})
    target = Path(audio.prompt_dir) / f"{prompt_id}.tts.wav"
    target.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(target.write_bytes, data)
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request), "ai.render_prompt", prompt_id, 200
    )
    return {"ok": True, "prompt": prompt_id, "bytes": len(data), "path": str(target)}


@router.post("/app/voice/{device_id}/ai/speak")
async def ai_speak_into_call(
    device_id: int, body: SpeakAiBody, request: Request, user: CurrentUser = Depends(get_current_user)
):
    """Ask the model, then play the answer to the far end.

    engine=module uses the A7670 built-in synthesiser, which needs no sound
    card. engine=audio renders cloud TTS and plays it through the card bound to
    that module, which sounds better but requires the audio path to be wired.
    """
    calls = getattr(request.app.state, "calls", None)
    audio = getattr(request.app.state, "audio", None)
    if calls is None or audio is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "voice subsystem unavailable")
    try:
        reply = await _engine(request).chat(body.text)
    except AIError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, {"error": {"code": "ai_error", "message": str(e)}})

    session = calls.session_for_device(device_id)
    if session is not None:
        session.transcript.append(
            {"role": "user", "content": body.text, "at": int(time.time())}
        )
        session.transcript.append(
            {"role": "assistant", "content": reply, "at": int(time.time())}
        )

    if body.engine == "module":
        try:
            await calls.speak(device_id, reply)
        except VoiceError as e:
            raise HTTPException(status.HTTP_409_CONFLICT, str(e))
        channel = "module_tts"
    else:
        try:
            data = await _engine(request).synthesize(reply)
        except AIError as e:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, {"error": {"code": "ai_error", "message": str(e)}})
        target = Path(audio.prompt_dir) / "ai_reply.wav"
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, data)
        result = await audio.play_file(target, wait=True, timeout=60, tag="ai_reply")
        if not result.ok:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                {"error": {"code": "playback_failed", "message": result.error or "unknown"}},
            )
        channel = "cloud_tts"
    return {"reply": reply, "channel": channel, "provider": _engine(request).kind}


# ---------------------------------------------------------------------------
# API key endpoints
# ---------------------------------------------------------------------------


@router.post("/api/v1/ai/chat")
async def api_ai_chat(
    body: ChatBody, request: Request, key: CurrentApiKey = Depends(require_scope_ai)
):
    try:
        reply = await _engine(request).chat(
            body.message, history=[t.model_dump() for t in body.history], system=body.system
        )
    except AIError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, {"error": {"code": "ai_error", "message": str(e)}})
    return {"reply": reply, "provider": _engine(request).kind}


@router.post("/api/v1/ai/tts")
async def api_ai_tts(body: TtsBody, request: Request, key: CurrentApiKey = Depends(require_scope_ai)):
    try:
        audio = await _engine(request).synthesize(body.text, voice=body.voice)
    except AIError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, {"error": {"code": "ai_error", "message": str(e)}})
    return Response(content=audio, media_type="audio/wav")


@router.post("/api/v1/ai/asr")
async def api_ai_asr(
    request: Request,
    key: CurrentApiKey = Depends(require_scope_ai),
    file: UploadFile = File(...),
    language: Optional[str] = None,
):
    audio = await file.read()
    if not audio:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "上传的音频为空")
    try:
        text = await _engine(request).transcribe(audio, filename=file.filename or "audio.wav", language=language)
    except AIError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, {"error": {"code": "ai_error", "message": str(e)}})
    return {"text": text, "bytes": len(audio)}
