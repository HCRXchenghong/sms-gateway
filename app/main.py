"""FastAPI application entrypoint."""
from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import db, repo, repo_admin, repo_calls, repo_threads
from .ai.engine import AIError, ai_engine
from .audio import prompts as prompt_registry
from .audio.player import audio_player
from .config import settings
from .hub import hub
from .modem_manager import ModemManager
from .modem import ModemError
from .pdu import IncomingSms
from .rate_limit import WindowLimiter
from .routes_api import router as api_router
from .routes_app import router as app_router
from .routes_admin import router as admin_router
from .routes_ai import router as ai_router
from .routes_voice import router as voice_router
from .routes_web import router as web_router
from .voice.manager import CallManager, VoiceError
from .voice.recorder import CallRecorder
from .voice.state import CallSession
from typing import Awaitable, Callable

log = logging.getLogger("smsgw")

# Hooks run after an incoming SMS is persisted and broadcast. The lifespan
# registers the AI auto-reply hook here once its dependencies exist.
SmsHook = Callable[[int, IncomingSms, int], Awaitable[None]]
_sms_hooks: list[SmsHook] = []

# One auto-reply per peer per window keeps a chatty number from looping.
_auto_reply_limiter = WindowLimiter(limit=3, window_seconds=600)

def _configure_logging() -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    try:
        fh = TimedRotatingFileHandler(
            settings.log_path, when="midnight", backupCount=14, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except (OSError, PermissionError) as e:
        log.warning("file logging disabled: %s", e)


async def _on_sms(device_id: int, sms: IncomingSms) -> None:
    """Persist an incoming SMS and broadcast over websocket."""
    msg_id = await asyncio.to_thread(repo.insert_inbox, device_id, sms)
    device = await asyncio.to_thread(repo.get_device, device_id)
    device_payload = repo.serialize_device(device) if device else None
    await hub.publish(
        "sms.received",
        {
            "id": msg_id,
            "device_id": device_id,
            "device": device_payload,
            "sender": sms.sender,
            "content": sms.content,
            "received_at": sms.received_at,
            "decode_error": sms.decode_error,
        },
    )
    await asyncio.to_thread(
        repo.audit, "system", device_id, "modem", None, "sms.received", str(msg_id), 200
    )
    log.info(
        "sms received id=%d device=%s from=%s len=%d",
        msg_id,
        device_id,
        sms.sender,
        len(sms.content),
    )
    for hook in list(_sms_hooks):
        try:
            await hook(device_id, sms, msg_id)
        except Exception as e:
            log.warning("sms hook failed: %s", e)


def _flag_from_settings(key: str, fallback: bool) -> bool:
    """Read a boolean toggle. Called from async code, so keep it non-blocking."""
    raw = repo_admin.get_setting(key)  # noqa: ASYNC - short sqlite read
    if raw is None or raw == "":
        return fallback
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


async def _retention_loop() -> None:
    while True:
        try:
            result = await asyncio.to_thread(repo.cleanup_expired_messages, settings.message_retention_days)
            deleted = result["inbox_deleted"] + result["outbox_deleted"]
            if deleted:
                log.info("message retention cleanup deleted %d rows", deleted)
            calls_deleted = await asyncio.to_thread(
                repo_calls.cleanup_expired_calls, max(settings.message_retention_days, 30)
            )
            if calls_deleted:
                log.info("call retention cleanup deleted %d rows", calls_deleted)
        except Exception as e:
            log.warning("message retention cleanup failed: %s", e)
        await asyncio.sleep(3600)


async def _write_cdr(session: CallSession, phase: str) -> None:
    """Persist a call row when it opens and again when it closes."""
    await asyncio.to_thread(repo_calls.persist_session, session)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    db.init_db()
    await asyncio.to_thread(repo.cleanup_expired_messages, settings.message_retention_days)

    async def on_call_prompt(device_id: int, state: str, session_dict: dict) -> None:
        prompt_id = prompt_registry.state_prompt(state, session_dict.get("end_reason"))
        await hub.publish(
            "call.prompt",
            {"device_id": device_id, "prompt": prompt_id, "state": state, "session": session_dict},
        )
        # Capture follows the call: starts when both sides are connected and
        # stops on hangup, so the CDR row carries the recording path.
        rec_path = await recorder.on_state(state, session_dict)
        if rec_path is not None:
            if state == "active" and settings.audio_local_prompts:
                asyncio.create_task(audio_player.play_prompt("recording_notice"))
            if state == "ended":
                await asyncio.to_thread(
                    repo.audit,
                    "system",
                    session_dict.get("device_id"),
                    "call",
                    session_dict.get("call_id"),
                    "call.recording.saved",
                    str(rec_path),
                    200,
                )

        if not prompt_id:
            return
        if settings.audio_local_prompts:
            asyncio.create_task(audio_player.play_prompt(prompt_id))
        if settings.voice_prompt_into_call and state == "active":
            spec = prompt_registry.resolve_prompt(prompt_id)
            if spec and spec.text:
                try:
                    await calls.speak(device_id, spec.text)
                except Exception as e:
                    log.warning("in-call prompt failed: %s", e)

    modems = ModemManager(on_sms=_on_sms)

    async def auto_reply_sms(device_id: int, sms: IncomingSms, msg_id: int) -> None:
        """Let the model answer an incoming text message.

        Rate limited per sender so two gateways talking to each other, or a
        chatty number, cannot turn into an SMS loop.
        """
        if not _flag_from_settings("ai.auto_reply_sms", settings.ai_auto_reply_sms):
            return
        if sms.decode_error:
            return
        inbound = (sms.content or "").strip()
        if not inbound or not sms.sender:
            return
        allowed, _ = _auto_reply_limiter.check(f"sms:{sms.sender}")
        if not allowed:
            log.info("auto-reply suppressed for %s (rate limit)", sms.sender)
            return
        try:
            reply = await ai_engine.chat(inbound)
        except AIError as e:
            log.warning("auto-reply generation failed: %s", e)
            return
        reply = (reply or "").strip()
        if not reply:
            return
        out_id = await asyncio.to_thread(
            repo.queue_outbox, device_id, sms.sender, reply, None, None
        )
        try:
            used_device, segments = await modems.send_sms(sms.sender, reply, device_id)
        except ModemError as e:
            await asyncio.to_thread(repo.mark_outbox, out_id, "failed", 1, str(e))
            log.warning("auto-reply send failed: %s", e)
            return
        await asyncio.to_thread(repo.set_outbox_device, out_id, used_device)
        await asyncio.to_thread(repo.mark_outbox, out_id, "sent", segments, None)
        await asyncio.to_thread(repo_threads.touch_thread, sms.sender, used_device, sms.received_at)
        await hub.publish(
            "sms.auto_reply",
            {
                "id": out_id,
                "device_id": used_device,
                "recipient": sms.sender,
                "content": reply,
                "in_reply_to": msg_id,
                "provider": ai_engine.kind,
            },
        )
        await asyncio.to_thread(
            repo.audit, "system", device_id, "ai", None, "sms.auto_reply", str(out_id), 200
        )

    _sms_hooks.append(auto_reply_sms)

    calls = CallManager(
        get_modem=modems.get_modem,
        publish=hub.publish,
        write_cdr=_write_cdr,
        on_prompt=on_call_prompt,
    )
    modems.set_voice_handler(calls.on_voice_urc)

    async def _persist_recording_path(call_id: str, path: str) -> None:
        await asyncio.to_thread(repo_calls.update_call, call_id, recording_path=path)

    async def _persist_recording_meta(call_id: str, payload: dict) -> None:
        await asyncio.to_thread(repo_calls.update_call, call_id, **payload)

    async def _speak_into_call(device_id: int, text: str) -> None:
        """Say something to the far end.

        The module synthesiser is tried first because it needs no sound card.
        Firmwares without the TTS voice bank reject AT+CTTS, and then the line
        is rendered by the configured speech provider and played through the
        card bound to that module, which reaches the call only when the card is
        wired to the module audio port.
        """
        try:
            await calls.speak(int(device_id), text)
            return
        except VoiceError as e:
            log.info("module TTS unavailable for the recording notice: %s", e)
        data = await ai_engine.synthesize(text)
        target = Path(audio_player.prompt_dir) / "call_notice.wav"
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, data)
        result = await audio_player.play_file(target, wait=True, timeout=30, tag="call_notice")
        if not result.ok:
            raise RuntimeError(result.error or "播放录音提示失败")

    recorder = CallRecorder(
        audio_player,
        enabled=lambda: _flag_from_settings("audio.record_enabled", settings.audio_record_enabled),
        publish=hub.publish,
        persist=_persist_recording_path,
        policy=lambda: str(repo_admin.get_setting("recording.policy") or settings.recording_policy),
        default_enabled=lambda: _flag_from_settings(
            "recording.default_enabled", settings.recording_default_enabled
        ),
        announce_default=lambda: _flag_from_settings(
            "recording.announce", settings.recording_announce
        ),
        speak_into_call=_speak_into_call,
        persist_meta=_persist_recording_meta,
    )
    # Let the console toggle win over the config default at runtime.
    audio_player.record_enabled = lambda: _flag_from_settings(
        "audio.record_enabled", settings.audio_record_enabled
    )

    try:
        await audio_player.load()
    except Exception as e:
        log.warning("audio subsystem degraded: %s", e)

    try:
        await ai_engine.load()
    except Exception as e:
        log.warning("ai engine degraded: %s", e)

    app.state.modem = modems
    app.state.modems = modems
    app.state.calls = calls
    app.state.recorder = recorder
    app.state.audio = audio_player
    app.state.ai = ai_engine
    task = asyncio.create_task(modems.run(), name="modem-manager")
    await calls.start()
    retention_task = asyncio.create_task(_retention_loop(), name="message-retention")
    log.info(
        "gateway started (mock=%s voice=%s audio=%s)",
        settings.mock_modem,
        settings.voice_enabled,
        audio_player.backend,
    )
    try:
        yield
    finally:
        await calls.stop()
        await recorder.stop_all()
        await audio_player.stop_all()
        _sms_hooks.clear()
        modems.stop()
        task.cancel()
        retention_task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await retention_task
        except (asyncio.CancelledError, Exception):
            pass


app = FastAPI(title="SMS Gateway", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "X-API-Key", "Content-Type", "Accept"],
)

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

app.include_router(web_router)
app.include_router(api_router)
app.include_router(voice_router)
app.include_router(admin_router)
app.include_router(app_router)
app.include_router(ai_router)
