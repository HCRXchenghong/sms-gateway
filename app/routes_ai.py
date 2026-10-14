"""AI endpoints: chat, speech recognition, speech synthesis."""
from __future__ import annotations

import asyncio
import io
import time
import wave
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import repo, repo_admin, repo_ai
from .audio import prompts as prompt_registry
from .auth import (
    CurrentApiKey,
    CurrentUser,
    get_current_user,
    require_role,
    require_scope_ai,
)
from .ai.engine import AIError, ai_engine
from .ai.providers import ChatMessage
from .ai.registry import Endpoint, build_endpoint_provider
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


# ---------------------------------------------------------------------------
# Admin: the per-capability endpoint pool
# ---------------------------------------------------------------------------


class EndpointBody(BaseModel):
    capability: str = Field(..., pattern="^(asr|translate|tts|chat|summary)$")
    name: str = Field(..., min_length=1, max_length=64)
    kind: str = Field(default="api", pattern="^(api|local)$")
    base_url: str = Field(default="", max_length=400)
    model: str = Field(default="", max_length=128)
    params: dict = Field(default_factory=dict)
    priority: int = Field(default=100, ge=0, le=9999)
    enabled: bool = True
    supports_stream: bool = False
    timeout_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    # Empty or omitted keeps the stored key on update; an explicit "" clears it.
    api_key: Optional[str] = Field(default=None, max_length=512)


class EndpointPatch(BaseModel):
    capability: Optional[str] = Field(default=None, pattern="^(asr|translate|tts|chat|summary)$")
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    kind: Optional[str] = Field(default=None, pattern="^(api|local)$")
    base_url: Optional[str] = Field(default=None, max_length=400)
    model: Optional[str] = Field(default=None, max_length=128)
    params: Optional[dict] = None
    priority: Optional[int] = Field(default=None, ge=0, le=9999)
    enabled: Optional[bool] = None
    supports_stream: Optional[bool] = None
    timeout_seconds: Optional[float] = Field(default=None, ge=1.0, le=300.0)
    api_key: Optional[str] = Field(default=None, max_length=512)


class TranslateBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    target: str = Field(default="zh", max_length=16)
    source: Optional[str] = Field(default=None, max_length=16)
    glossary: List[str] = Field(default_factory=list, max_length=40)


def _silent_wav(seconds: float = 0.4, rate: int = 16000) -> bytes:
    """A short silent clip, enough to exercise an ASR endpoint end to end."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


def _pool(request: Request):
    return _engine(request).router


@router.get("/app/admin/ai/endpoints")
async def admin_list_ai_endpoints(request: Request, user: CurrentUser = Depends(require_role("admin"))):
    """Every configured endpoint plus its live circuit state.

    API keys are never returned; has_api_key tells the console whether one is
    stored so the field can show a placeholder instead of an empty box.
    """
    engine = _engine(request)
    await engine.router.refresh()
    rows = await asyncio.to_thread(repo_ai.list_endpoints)
    live = engine.router.status()
    for row in rows:
        state = (
            live.get("capabilities", {})
            .get(row["capability"], {})
            .get("endpoints", [])
        )
        match = next((s for s in state if s["id"] == row["id"]), None)
        row["circuit"] = match["circuit"] if match else "closed"
        row["consecutive_failures"] = match["consecutive_failures"] if match else 0
        row["last_error"] = match["last_error"] if match else None
        row["retry_in_seconds"] = match["retry_in_seconds"] if match else 0.0
        row["runtime_calls"] = match["calls"] if match else 0
        row["runtime_error_rate"] = match["error_rate"] if match else 0.0
        row["avg_latency_ms"] = match["avg_latency_ms"] if match else 0
    stats = await asyncio.to_thread(repo_ai.endpoint_stats, 1)
    for row in rows:
        row["stats"] = stats.get(str(row["id"]), {})
    return {
        "items": rows,
        "capabilities": list(repo_ai.CAPABILITIES),
        "pool": live,
    }


@router.post("/app/admin/ai/endpoints", status_code=201)
async def admin_create_ai_endpoint(
    body: EndpointBody, request: Request, user: CurrentUser = Depends(require_role("admin"))
):
    endpoint_id = await asyncio.to_thread(
        repo_ai.create_endpoint,
        capability=body.capability,
        name=body.name,
        kind=body.kind,
        base_url=body.base_url,
        api_key=body.api_key or "",
        model=body.model,
        params=body.params,
        priority=body.priority,
        enabled=body.enabled,
        supports_stream=body.supports_stream,
        timeout_seconds=body.timeout_seconds,
    )
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request),
        "ai.endpoint.create", f"{body.capability}:{body.name}", 201,
    )
    await _engine(request).reload()
    return {"ok": True, "id": endpoint_id}


@router.patch("/app/admin/ai/endpoints/{endpoint_id}")
async def admin_update_ai_endpoint(
    endpoint_id: int, body: EndpointPatch, request: Request,
    user: CurrentUser = Depends(require_role("admin")),
):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    # An explicit empty string clears the stored key; omitting it keeps it.
    if body.api_key == "":
        fields["api_key"] = ""
    elif body.api_key:
        fields["api_key"] = body.api_key
    else:
        fields.pop("api_key", None)
    if not fields:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有要修改的字段")
    changed = await asyncio.to_thread(repo_ai.update_endpoint, endpoint_id, **fields)
    if not changed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "endpoint not found")
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request),
        "ai.endpoint.update", f"{endpoint_id}:{','.join(sorted(fields))}", 200,
    )
    await _engine(request).reload()
    return {"ok": True}


@router.delete("/app/admin/ai/endpoints/{endpoint_id}")
async def admin_delete_ai_endpoint(
    endpoint_id: int, request: Request, user: CurrentUser = Depends(require_role("admin"))
):
    removed = await asyncio.to_thread(repo_ai.delete_endpoint, endpoint_id)
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "endpoint not found")
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request),
        "ai.endpoint.delete", str(endpoint_id), 200,
    )
    await _engine(request).reload()
    return {"ok": True}


@router.post("/app/admin/ai/endpoints/{endpoint_id}/reset")
async def admin_reset_ai_endpoint(
    endpoint_id: int, request: Request, user: CurrentUser = Depends(require_role("admin"))
):
    """Close the circuit by hand instead of waiting out the cooldown."""
    _pool(request).reset_circuit(endpoint_id)
    return {"ok": True}


@router.post("/app/admin/ai/endpoints/{endpoint_id}/test")
async def admin_test_ai_endpoint(
    endpoint_id: int, request: Request, user: CurrentUser = Depends(require_role("admin"))
):
    """Probe one endpoint on its own channel and report what came back.

    This bypasses the pool and the circuit breaker on purpose: the point is to
    find out whether a freshly entered endpoint works at all.
    """
    row = await asyncio.to_thread(repo_ai.get_endpoint, endpoint_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "endpoint not found")
    endpoint = Endpoint.from_dict(row)
    provider = build_endpoint_provider(endpoint)
    started = time.monotonic()
    result: dict = {"endpoint": endpoint_id, "capability": endpoint.capability, "name": endpoint.name}
    try:
        if endpoint.capability == "tts":
            audio = await provider.synthesize("连接测试")
            result["ok"] = bool(audio)
            result["detail"] = f"{len(audio or b'')} bytes"
        elif endpoint.capability == "asr":
            text = await provider.transcribe(_silent_wav(), language=None)
            result["ok"] = True
            result["detail"] = (text or "")[:80]
        else:
            reply = await provider.chat([ChatMessage("user", "连接测试，请回复 ok")], max_tokens=16)
            result["ok"] = bool((reply or "").strip())
            result["detail"] = (reply or "").strip()[:80]
        result["error"] = None
    except Exception as exc:  # noqa: BLE001 - report anything the vendor threw
        result["ok"] = False
        result["error"] = str(exc)[:400]
    finally:
        try:
            await provider.close()
        except Exception:
            pass
    result["latency_ms"] = int((time.monotonic() - started) * 1000)
    await asyncio.to_thread(
        repo_ai.log_call,
        capability=endpoint.capability, endpoint_id=endpoint.id, endpoint_name=endpoint.name,
        ok=bool(result["ok"]), latency_ms=result["latency_ms"], error=result["error"],
        call_id=None,
    )
    return result


@router.post("/app/admin/ai/endpoints/seed")
async def admin_seed_ai_endpoints(request: Request, user: CurrentUser = Depends(require_role("admin"))):
    """One-click migration from the flat ai.* settings into the pool.

    Deliberately explicit rather than automatic: creating endpoints silently
    would start routing live calls through whatever base_url happens to be in
    the settings.
    """
    created = await asyncio.to_thread(
        repo_ai.seed_from_legacy_settings, repo_admin.get_setting, repo_admin.get_secret
    )
    await _engine(request).reload()
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request),
        "ai.endpoint.seed", str(created), 200,
    )
    return {"ok": True, "created": created}


@router.get("/app/admin/ai/stats")
async def admin_ai_stats(
    request: Request, hours: int = 1, limit: int = 100,
    user: CurrentUser = Depends(require_role("admin")),
):
    hours = max(1, min(int(hours), 168))
    stats = await asyncio.to_thread(repo_ai.endpoint_stats, hours)
    calls = await asyncio.to_thread(repo_ai.recent_calls, limit)
    return {"window_hours": hours, "endpoints": list(stats.values()), "recent_calls": calls}


@router.post("/app/ai/translate")
async def ai_translate(
    body: TranslateBody, request: Request, user: CurrentUser = Depends(get_current_user)
):
    """Translate one line. Used by the console test box and by the app."""
    started = time.monotonic()
    try:
        text = await _engine(request).translate(
            body.text, source=body.source, target=body.target, glossary=body.glossary
        )
    except AIError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, {"error": {"code": "ai_error", "message": str(e)}})
    return {
        "text": text,
        "target": body.target,
        "provider": _engine(request).kind,
        "endpoint": _engine(request).router.last_used("translate"),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
