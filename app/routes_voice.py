"""Voice call control, call history and audio device management."""
from __future__ import annotations

import asyncio
import re
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from . import repo, repo_calls
from .audio import prompts as prompt_registry
from .auth import (
    CurrentApiKey,
    CurrentUser,
    get_current_user,
    require_role,
    require_scope_voice_dial,
    require_scope_voice_read,
)
from .config import settings
from .modem import ModemError
from .voice.manager import VoiceError

router = APIRouter(tags=["voice"])

PHONE_RE = re.compile(r"^\+?[0-9*#,]{2,24}$")


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _calls(request: Request):
    calls = getattr(request.app.state, "calls", None)
    if calls is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "voice subsystem unavailable")
    return calls


def _audio(request: Request):
    audio = getattr(request.app.state, "audio", None)
    if audio is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "audio subsystem unavailable")
    return audio


def _modems(request: Request):
    return getattr(request.app.state, "modems", None) or getattr(request.app.state, "modem", None)


def _http_voice_error(exc: VoiceError) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, {"error": {"code": "call_rejected", "message": str(exc)}})


class DialBody(BaseModel):
    number: str = Field(..., min_length=2, max_length=24)
    device_id: Optional[int] = None

    @field_validator("number")
    @classmethod
    def check_number(cls, v: str) -> str:
        v = v.strip()
        if not PHONE_RE.match(v):
            raise ValueError("number must contain digits and optional + * # ,")
        return v


class DtmfBody(BaseModel):
    digits: str = Field(..., min_length=1, max_length=32)


class TtsBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=512)
    mode: Optional[int] = Field(default=None, ge=1, le=2)


class AudioDeviceBody(BaseModel):
    device: str = ""


class VolumeBody(BaseModel):
    volume: float = Field(..., ge=0.0, le=1.0)


class RawAtBody(BaseModel):
    command: str = Field(..., min_length=2, max_length=128)
    timeout: float = Field(default=5.0, ge=0.5, le=30.0)


class MockIncomingBody(BaseModel):
    number: str = Field(..., min_length=2, max_length=24)
    rings: int = Field(default=4, ge=0, le=20)
    interval: float = Field(default=3.0, ge=0.2, le=30.0)


class MockScenarioBody(BaseModel):
    scenario: str = Field(default="answer")


class MockSmsBody(BaseModel):
    sender: str = Field(..., min_length=2, max_length=32)
    content: str = Field(..., min_length=1, max_length=1024)


@router.post("/app/sms/mock/{device_id}/incoming")
async def mock_incoming_sms(
    device_id: int,
    body: MockSmsBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
):
    """Inject an inbound SMS so receive-path features can be exercised."""
    _require_mock()
    modems = _modems(request)
    modem = modems.get_modem(device_id) if modems else None
    if modem is None or not hasattr(modem, "inject_sms"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"device {device_id} is not a simulator")
    await modem.inject_sms(body.sender, body.content)
    return {"ok": True, "device_id": device_id, "sender": body.sender}


def _require_mock() -> None:
    if not settings.mock_modem:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"error": {"code": "not_available", "message": "simulator controls require mock mode"}},
        )


def _mock_modem(request: Request, device_id: int):
    _require_mock()
    modems = _modems(request)
    modem = modems.get_modem(device_id) if modems else None
    if modem is None or not hasattr(modem, "simulate_incoming"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"device {device_id} is not a simulator")
    return modem


@router.post("/app/voice/mock/{device_id}/incoming")
async def mock_incoming(
    device_id: int,
    body: MockIncomingBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
):
    """Ring the simulator so the whole inbound path can be exercised."""
    modem = _mock_modem(request, device_id)
    asyncio.create_task(modem.simulate_incoming(body.number, rings=body.rings, interval=body.interval))
    return {"ok": True, "device_id": device_id, "number": body.number}


@router.post("/app/voice/mock/{device_id}/scenario")
async def mock_scenario(
    device_id: int,
    body: MockScenarioBody,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
):
    """Pick how the simulator resolves the next outbound call."""
    modem = _mock_modem(request, device_id)
    try:
        modem.set_scenario(body.scenario)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return {"ok": True, "scenario": body.scenario}


@router.get("/app/voice/mock/{device_id}/debug")
async def mock_debug(device_id: int, request: Request, user: CurrentUser = Depends(get_current_user)):
    modem = _mock_modem(request, device_id)
    return modem.debug_state()


# ---------------------------------------------------------------------------
# Session-authenticated endpoints used by the web console and the Flutter app
# ---------------------------------------------------------------------------


@router.get("/app/voice/state")
async def voice_state(request: Request, user: CurrentUser = Depends(get_current_user)):
    calls = _calls(request)
    modems = _modems(request)
    active = [s.to_dict() for s in calls.active_sessions()]
    online = modems.online_device_ids() if modems else []
    return {
        "voice_enabled": settings.voice_enabled,
        "max_concurrent": settings.voice_max_concurrent_calls,
        "online_device_ids": online,
        "active": active,
        "count": len(active),
    }


@router.post("/app/voice/dial")
async def voice_dial(body: DialBody, request: Request, user: CurrentUser = Depends(get_current_user)):
    calls = _calls(request)
    modems = _modems(request)
    try:
        device_id = await modems.pick_call_device(body.device_id)
    except ModemError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, {"error": {"code": "no_device", "message": str(e)}})
    try:
        session = await calls.dial(device_id, body.number, owner_user_id=user.id, origin="web")
    except VoiceError as e:
        raise _http_voice_error(e)
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request), "call.dial", body.number, 200
    )
    return session.to_dict()


@router.post("/app/voice/{device_id}/answer")
async def voice_answer(device_id: int, request: Request, user: CurrentUser = Depends(get_current_user)):
    try:
        session = await _calls(request).answer(device_id)
    except VoiceError as e:
        raise _http_voice_error(e)
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request), "call.answer", str(device_id), 200
    )
    return session.to_dict()


@router.post("/app/voice/{device_id}/hangup")
async def voice_hangup(device_id: int, request: Request, user: CurrentUser = Depends(get_current_user)):
    await _calls(request).hangup(device_id)
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request), "call.hangup", str(device_id), 200
    )
    return {"ok": True}


@router.post("/app/voice/{device_id}/dtmf")
async def voice_dtmf(device_id: int, body: DtmfBody, request: Request, user: CurrentUser = Depends(get_current_user)):
    try:
        sent = await _calls(request).send_dtmf(device_id, body.digits)
    except VoiceError as e:
        raise _http_voice_error(e)
    return {"ok": True, "sent": sent}


@router.post("/app/voice/{device_id}/tts")
async def voice_tts(device_id: int, body: TtsBody, request: Request, user: CurrentUser = Depends(get_current_user)):
    try:
        await _calls(request).speak(device_id, body.text, body.mode)
    except VoiceError as e:
        raise _http_voice_error(e)
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request), "call.tts", str(device_id), 200
    )
    return {"ok": True}


@router.post("/app/voice/{device_id}/tts/stop")
async def voice_tts_stop(device_id: int, request: Request, user: CurrentUser = Depends(get_current_user)):
    try:
        await _calls(request).stop_speak(device_id)
    except VoiceError as e:
        raise _http_voice_error(e)
    return {"ok": True}


@router.get("/app/voice/history")
async def voice_history(
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    limit: int = 50,
    device_id: Optional[int] = None,
    remote: Optional[str] = None,
    direction: Optional[str] = None,
    since: Optional[int] = None,
):
    if limit < 1 or limit > 500:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "limit must be 1..500")
    rows = await asyncio.to_thread(repo_calls.list_calls, limit, device_id, remote, direction, since)
    return {"items": [repo_calls.serialize_call(r) for r in rows], "count": len(rows)}


@router.get("/app/voice/stats")
async def voice_stats(request: Request, user: CurrentUser = Depends(get_current_user), days: int = 7):
    return await asyncio.to_thread(repo_calls.call_stats, max(1, min(days, 365)))


# ---------------------------------------------------------------------------
# Audio devices and prompts
# ---------------------------------------------------------------------------


@router.get("/app/audio/devices")
async def audio_devices(request: Request, user: CurrentUser = Depends(get_current_user)):
    return await _audio(request).inventory()


@router.post("/app/audio/output")
async def audio_set_output(
    body: AudioDeviceBody,
    request: Request,
    user: CurrentUser = Depends(require_role("admin")),
):
    applied = await _audio(request).set_output_device(body.device)
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request), "audio.output", body.device, 200
    )
    return {"ok": True, "device": body.device, "system_default_applied": applied}


@router.post("/app/audio/input")
async def audio_set_input(
    body: AudioDeviceBody,
    request: Request,
    user: CurrentUser = Depends(require_role("admin")),
):
    applied = await _audio(request).set_input_device(body.device)
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request), "audio.input", body.device, 200
    )
    return {"ok": True, "device": body.device, "system_default_applied": applied}


@router.post("/app/audio/volume")
async def audio_set_volume(
    body: VolumeBody, request: Request, user: CurrentUser = Depends(get_current_user)
):
    volume = await _audio(request).set_volume(body.volume)
    return {"ok": True, "volume": volume}


@router.get("/app/audio/prompts")
async def audio_prompts(request: Request, user: CurrentUser = Depends(get_current_user)):
    player = _audio(request)
    return {
        "items": player.prompt_catalog(),
        "state_map": prompt_registry.STATE_PROMPTS,
        "end_reason_map": prompt_registry.END_REASON_PROMPTS,
    }


@router.post("/app/audio/prompts/{prompt_id}/play")
async def audio_play_prompt(
    prompt_id: str, request: Request, user: CurrentUser = Depends(get_current_user)
):
    result = await _audio(request).play_prompt(prompt_id, wait=True, timeout=30)
    if not result.ok:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, {"error": {"code": "playback_failed", "message": result.error or "unknown"}}
        )
    return result.to_dict()


@router.get("/app/audio/prompts/{prompt_id}/file")
async def audio_prompt_file(prompt_id: str, request: Request, user: CurrentUser = Depends(get_current_user)):
    path = prompt_registry.prompt_path(prompt_id, _audio(request).prompt_dir)
    if path is None or not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "prompt audio not found")
    return FileResponse(str(path), media_type="audio/wav", filename=path.name)


# ---------------------------------------------------------------------------
# Hardware bring-up helpers
# ---------------------------------------------------------------------------


@router.get("/app/devices/{device_id}/signal")
async def device_signal(device_id: int, request: Request, user: CurrentUser = Depends(get_current_user)):
    modems = _modems(request)
    modem = modems.get_modem(device_id) if modems else None
    if modem is None or not modem.connected:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device offline")
    csq = await modem.signal_quality() if hasattr(modem, "signal_quality") else None
    reg = await modem.registration_status() if hasattr(modem, "registration_status") else {}
    rssi = csq[0] if csq else None
    return {
        "device_id": device_id,
        "csq": rssi,
        "ber": csq[1] if csq else None,
        # CSQ 0..31 maps roughly to -113..-51 dBm in 2 dB steps.
        "dbm": (-113 + 2 * rssi) if isinstance(rssi, int) and 0 <= rssi <= 31 else None,
        "registration": reg,
        "voice_supported": await modem.tts_supported() if hasattr(modem, "tts_supported") else False,
    }


@router.post("/app/devices/{device_id}/at")
async def device_raw_at(
    device_id: int,
    body: RawAtBody,
    request: Request,
    user: CurrentUser = Depends(require_role("admin")),
):
    modems = _modems(request)
    modem = modems.get_modem(device_id) if modems else None
    if modem is None or not modem.connected:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device offline")
    if not hasattr(modem, "raw_at"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "raw AT console unavailable on this device")
    try:
        lines = await modem.raw_at(body.command, body.timeout)
    except ModemError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, {"error": {"code": "at_error", "message": str(e)}})
    await asyncio.to_thread(
        repo.audit, "user", user.id, user.username, _client_ip(request), "device.at", body.command, 200
    )
    return {"command": body.command, "lines": lines}


# ---------------------------------------------------------------------------
# API-key endpoints for third-party integrations
# ---------------------------------------------------------------------------


@router.post("/api/v1/calls/dial")
async def api_call_dial(
    body: DialBody, request: Request, key: CurrentApiKey = Depends(require_scope_voice_dial)
):
    calls = _calls(request)
    modems = _modems(request)
    device_id = body.device_id
    if device_id is not None and key.allowed_device_ids is not None and device_id not in key.allowed_device_ids:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "device not allowed for this API key")
    try:
        device_id = await modems.pick_call_device(device_id)
    except ModemError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, {"error": {"code": "no_device", "message": str(e)}})
    if key.allowed_device_ids is not None and device_id not in key.allowed_device_ids:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "device not allowed for this API key")
    try:
        session = await calls.dial(device_id, body.number, origin="api")
    except VoiceError as e:
        raise _http_voice_error(e)
    await asyncio.to_thread(
        repo.audit, "api_key", key.id, key.prefix, _client_ip(request), "call.dial", body.number, 200
    )
    return session.to_dict()


@router.post("/api/v1/calls/{device_id}/hangup")
async def api_call_hangup(
    device_id: int, request: Request, key: CurrentApiKey = Depends(require_scope_voice_dial)
):
    await _calls(request).hangup(device_id)
    return {"ok": True}


@router.post("/api/v1/calls/{device_id}/tts")
async def api_call_tts(
    device_id: int, body: TtsBody, request: Request, key: CurrentApiKey = Depends(require_scope_voice_dial)
):
    try:
        await _calls(request).speak(device_id, body.text, body.mode)
    except VoiceError as e:
        raise _http_voice_error(e)
    return {"ok": True}


@router.get("/api/v1/calls/active")
async def api_calls_active(request: Request, key: CurrentApiKey = Depends(require_scope_voice_read)):
    sessions = _calls(request).active_sessions()
    items = [s.to_dict() for s in sessions if key.allowed_device_ids is None or s.device_id in key.allowed_device_ids]
    return {"items": items, "count": len(items)}


@router.get("/api/v1/calls/history")
async def api_calls_history(
    request: Request,
    limit: int = 50,
    device_id: Optional[int] = None,
    key: CurrentApiKey = Depends(require_scope_voice_read),
):
    if limit < 1 or limit > 500:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "limit must be 1..500")
    rows = await asyncio.to_thread(repo_calls.list_calls, limit, device_id, None, None, None)
    items = [
        repo_calls.serialize_call(r)
        for r in rows
        if key.allowed_device_ids is None or r["device_id"] in key.allowed_device_ids
    ]
    return {"items": items, "count": len(items)}
