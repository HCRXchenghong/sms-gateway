"""Everything that runs on top of a connected call.

Recording only needs the state transitions, so it has its own module. The
features here need the audio: live translation, the purpose-driven call agent,
and the transcript that feeds the summary. They share one capture stream, one
transcript and one path into the call, so they are orchestrated together.

Two rules keep this from fighting the call manager.

Nothing here runs inside the call manager's lock. on_state() only schedules
work: the hook is invoked while CallManager holds its own lock, so a coroutine
that reached back into answer(), dial() or hangup() from there would wait on a
lock that is never released.

Speaking into the call gates the capture. Whatever we synthesise comes back out
of the line a moment later, and an ungated recogniser would translate our own
voice back at the far end in a loop that gets louder each time.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .. import repo_calls, repo_tasks
from ..ai.agent import (
    OUTCOME_CANCELLED,
    OUTCOME_ERROR,
    OUTCOME_NO_ANSWER,
    CallAgent,
)
from ..audio.bridge import CallAudioBridge
from ..audio.capture import pcm_to_wav
from ..audio.vad import VadConfig
from ..config import settings
from .state import CallState
from .translate import MODE_OFF, MODE_SUBTITLE, MODE_VOICE, CallTranslator

log = logging.getLogger(__name__)

__all__ = [
    "CallFlow",
    "INBOUND_MANUAL",
    "INBOUND_AUTO",
    "INBOUND_AI",
    "INBOUND_MODES",
]

INBOUND_MANUAL = "manual"
INBOUND_AUTO = "auto_answer"
INBOUND_AI = "auto_answer_ai"
INBOUND_MODES = (INBOUND_MANUAL, INBOUND_AUTO, INBOUND_AI)

PHONE_RE = re.compile(r"^\+?[0-9*#,]{2,24}$")

# A task in one of these states is over; a late write must not revive it.
TERMINAL_STATUSES = frozenset((
    "finished", "failed", "cancelled", "no_answer", "rejected", "busy",
))

Publish = Callable[[str, dict], Awaitable[None]]
SpeakIntoCall = Callable[[int, str], Awaitable[None]]
SettingReader = Callable[[str, Any], Any]

DEFAULT_INBOUND_PURPOSE = (
    "接听来电，礼貌确认对方身份和来电目的，把对方要办的事情问清楚，"
    "并说明操作者稍后会回复。不要承诺具体时间，也不要编造信息。"
)


def coerce_setting(raw: Any, default: Any) -> Any:
    """Turn a stored setting string into the type of its default."""
    if raw is None or raw == "":
        return default
    if isinstance(default, bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default
    return str(raw)


def _db_setting(key: str, default: Any) -> Any:
    from .. import repo_admin

    try:
        raw = repo_admin.get_setting(key)
    except Exception:  # noqa: BLE001 - settings must never break a call
        return default
    return coerce_setting(raw, default)


@dataclass
class _Runtime:
    """One live call and everything attached to it."""

    call_id: str
    device_id: int
    direction: str = "outbound"
    remote: Optional[str] = None
    task_id: Optional[int] = None
    purpose: Optional[str] = None
    agent: Optional[CallAgent] = None
    agent_request: Optional[dict] = None
    translator: Optional[CallTranslator] = None
    bridge: Optional[CallAudioBridge] = None
    transcript: List[dict] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    attached_at: Optional[float] = None
    ended_at: Optional[float] = None
    end_reason: Optional[str] = None
    capture_error: Optional[str] = None
    translate_override: Optional[str] = None
    translate_note: Optional[str] = None
    summary: Optional[dict] = None
    summary_error: Optional[str] = None
    outcome: Optional[dict] = None
    handover: bool = False
    watchdog: Optional[asyncio.Task] = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def to_dict(self, *, transcript_limit: int = 200) -> dict:
        agent = None
        if self.agent is not None:
            agent = self.agent.state()
        elif self.agent_request is not None:
            agent = {"pending": True, "purpose": self.agent_request.get("purpose", "")}
        return {
            "call_id": self.call_id,
            "device_id": self.device_id,
            "direction": self.direction,
            "remote": self.remote,
            "task_id": self.task_id,
            "purpose": self.purpose,
            "ended": self.ended_at is not None,
            "attached": self.attached_at is not None,
            "started_at": int(self.started_at),
            "ended_at": int(self.ended_at) if self.ended_at else None,
            "end_reason": self.end_reason,
            "capture_error": self.capture_error,
            "capture": self.bridge.stats.to_dict() if self.bridge else None,
            "translation": self.translator.state() if self.translator else None,
            "translation_note": self.translate_note,
            "translation_history": self.translator.history(60) if self.translator else [],
            "agent": agent,
            "handover": self.handover,
            "outcome": self.outcome,
            "summary": self.summary,
            "summary_error": self.summary_error,
            "transcript": list(self.transcript[-max(1, int(transcript_limit)):]),
            "transcript_lines": len(self.transcript),
        }


class CallFlowError(RuntimeError):
    """A user-visible refusal from the call flow."""


class CallFlow:
    """Attaches translation, the call agent and summaries to live calls."""

    def __init__(
        self,
        *,
        calls,
        engine,
        publish: Optional[Publish] = None,
        speak_into_call: Optional[SpeakIntoCall] = None,
        player: Any = None,
        capture: Any = None,
        modems: Any = None,
        setting: Optional[SettingReader] = None,
        tasks: Any = None,
    ) -> None:
        self._calls = calls
        self._engine = engine
        self._publish = publish
        self._speak = speak_into_call
        self._player = player
        self._capture = capture
        self._modems = modems
        self._setting_reader = setting or _db_setting
        self._tasks = tasks if tasks is not None else repo_tasks
        self._runtimes: Dict[str, _Runtime] = {}
        self._finished: Dict[str, _Runtime] = {}
        self._order: List[str] = []
        self._pending_answer: Dict[int, asyncio.Task] = {}
        self._armed: Dict[int, dict] = {}
        self._running: Dict[int, asyncio.Task] = {}
        self._wake = asyncio.Event()
        self._worker_task: Optional[asyncio.Task] = None
        self._stopping = False
        self._poll_seconds = 2.0
        self._finished_kept = 20

    # -- settings -------------------------------------------------------

    def _setting(self, key: str, default: Any) -> Any:
        try:
            value = self._setting_reader(key, default)
        except Exception:  # noqa: BLE001 - a broken setting must not break a call
            return default
        return default if value is None else value

    def inbound_mode(self) -> str:
        mode = str(self._setting("inbound.mode", settings.inbound_mode) or INBOUND_MANUAL).strip().lower()
        if mode not in INBOUND_MODES:
            mode = INBOUND_MANUAL
        if mode == INBOUND_MANUAL and self._setting("ai.auto_answer_call", settings.ai_auto_answer_call):
            # The older single toggle still means "pick the phone up for me".
            mode = INBOUND_AUTO
        return mode

    def inbound_purpose(self) -> str:
        value = str(self._setting("inbound.ai_purpose", settings.inbound_ai_purpose) or "").strip()
        return value or DEFAULT_INBOUND_PURPOSE

    def translation_defaults(self) -> dict:
        return {
            "mode": str(self._setting("translate.mode", settings.translate_mode) or MODE_OFF),
            "cadence": str(self._setting("translate.cadence", settings.translate_cadence) or "sentence"),
            "my_language": str(self._setting("translate.my_language", settings.translate_my_language) or "zh"),
            "peer_language": str(self._setting("translate.peer_language", settings.translate_peer_language) or "en"),
        }

    # -- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        self._stopping = False
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker(), name="ai-call-worker")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        for task in list(self._pending_answer.values()):
            task.cancel()
        self._pending_answer.clear()
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass
            self._worker_task = None
        for call_id in list(self._runtimes):
            try:
                await self._detach(call_id, end_reason="shutdown")
            except Exception as e:  # noqa: BLE001 - shutdown must not raise
                log.warning("detaching call %s failed: %s", call_id, e)
        for runner in list(self._running.values()):
            runner.cancel()
        self._running.clear()

    async def on_state(self, device_id: int, state: str, session: dict) -> None:
        """Call-transition hook. Schedules work; never awaits call control.

        This runs while CallManager holds its lock, so reaching into answer(),
        dial() or hangup() from here would deadlock. Everything real happens in
        a task that starts after the transition is applied.
        """
        session = session or {}
        call_id = session.get("call_id")
        if not call_id:
            return
        if state == CallState.INCOMING.value:
            self._schedule_auto_answer(device_id, session)
            return
        if state == CallState.ACTIVE.value:
            asyncio.create_task(self._attach(session))
            return
        if state == CallState.ENDED.value:
            asyncio.create_task(
                self._detach(call_id, end_reason=session.get("end_reason"), session=session)
            )
            return
        if state in (CallState.DIALING.value, CallState.ALERTING.value):
            # Create the record early so the console shows the call while it
            # is still ringing out.
            self._runtime(session)

    def _runtime(self, session: dict) -> _Runtime:
        call_id = session.get("call_id")
        if not call_id:
            raise CallFlowError("通话缺少标识")
        existing = self._runtimes.get(call_id)
        if existing is not None:
            return existing
        rt = _Runtime(
            call_id=call_id,
            device_id=int(session.get("device_id") or 0),
            direction=str(session.get("direction") or "outbound"),
            remote=session.get("remote"),
        )
        self._runtimes[call_id] = rt
        return rt

    def _runtime_for_device(self, device_id: int) -> Optional[_Runtime]:
        for rt in self._runtimes.values():
            if rt.device_id == int(device_id) and rt.ended_at is None:
                return rt
        return None

    def _runtime_for_task(self, task_id: int) -> Optional[_Runtime]:
        for rt in self._runtimes.values():
            if rt.task_id == int(task_id):
                return rt
        return None

    # -- inbound --------------------------------------------------------

    def _schedule_auto_answer(self, device_id: int, session: dict) -> None:
        call_id = session.get("call_id")
        rt = self._runtime(session)
        armed = self._match_armed_task(session.get("remote"))
        if armed is not None:
            # An explicit inbound task outranks the manual setting: someone
            # asked for the AI to take this call.
            rt.task_id = int(armed["id"])
            rt.purpose = armed.get("purpose") or self.inbound_purpose()
            rt.agent_request = self._agent_spec(
                purpose=rt.purpose,
                context=armed.get("context"),
                language=armed.get("language"),
                peer_language=armed.get("peer_language"),
                max_seconds=armed.get("max_seconds") or settings.inbound_ai_max_seconds,
            )
            self._armed.pop(int(armed["id"]), None)
            asyncio.create_task(self._mark_task_running(int(armed["id"]), call_id))
        mode = self.inbound_mode()
        if mode == INBOUND_MANUAL and rt.agent_request is None:
            return
        if mode == INBOUND_MANUAL:
            mode = INBOUND_AI
        if device_id in self._pending_answer:
            return
        delay = float(self._setting(
            "inbound.auto_answer_delay_seconds", settings.inbound_auto_answer_delay_seconds
        ))
        self._pending_answer[device_id] = asyncio.create_task(
            self._auto_answer(device_id, call_id, mode, delay),
            name=f"auto-answer-{device_id}",
        )

    def _match_armed_task(self, remote: Optional[str]) -> Optional[dict]:
        if not self._armed:
            return None
        number = (remote or "").strip()
        for task in self._armed.values():
            wanted = (task.get("number") or "").strip()
            if not wanted or wanted == number:
                return task
        return None

    async def _mark_task_running(self, task_id: int, call_id: str) -> None:
        await self._write_task(
            task_id, status="in_call", call_id=call_id, started_at=int(time.time())
        )

    async def _auto_answer(self, device_id: int, call_id: str, mode: str, delay: float) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            session = self._calls.session_by_id(call_id)
            if session is None or session.state != CallState.INCOMING:
                # The operator picked up first, or the caller hung up.
                return
            await self._calls.answer(device_id)
            await self._emit("call.auto_answer", {
                "call_id": call_id, "device_id": device_id, "mode": mode,
            })
            if mode != INBOUND_AI:
                return
            rt = self._runtimes.get(call_id)
            if rt is None or rt.agent_request is None:
                await self.attach_agent(
                    call_id,
                    purpose=self.inbound_purpose(),
                    opener=str(self._setting("inbound.ai_opener", settings.inbound_ai_opener) or "") or None,
                    max_seconds=int(self._setting("inbound.ai_max_seconds", settings.inbound_ai_max_seconds)),
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - the call must go on
            log.warning("auto-answer failed for call %s: %s", call_id, e)
            await self._emit("call.auto_answer_failed", {
                "call_id": call_id, "device_id": device_id, "error": str(e)[:300],
            })
        finally:
            self._pending_answer.pop(device_id, None)

    # -- attach ---------------------------------------------------------

    def _effective_mode(self, rt: _Runtime) -> str:
        mode = rt.translate_override
        if mode is None:
            mode = str(self._setting("translate.mode", settings.translate_mode) or MODE_OFF)
        mode = mode.strip().lower()
        return mode if mode in (MODE_OFF, MODE_SUBTITLE, MODE_VOICE) else MODE_OFF

    async def _attach(self, session: dict) -> None:
        call_id = session.get("call_id")
        rt = self._runtime(session)
        if rt.attached_at is not None:
            return
        # Claimed before the first await so a second _attach for the same call
        # (state hook plus the task runner) cannot build two agents.
        rt.attached_at = time.time()
        rt.remote = session.get("remote") or rt.remote
        mode = self._effective_mode(rt)
        if mode != MODE_OFF:
            self._build_translator(rt, mode)
        if rt.bridge is None and (rt.translator is not None or rt.agent_request is not None):
            await self._start_capture(rt)
        await self._emit("call.flow", rt.to_dict(transcript_limit=0))
        if rt.agent_request is not None:
            await self._build_agent(rt)

    def _build_translator(self, rt: _Runtime, mode: str) -> None:
        note = None
        if mode == MODE_VOICE and (rt.agent is not None or rt.agent_request is not None):
            # An agent call already has one voice talking into the line. Two
            # would talk over each other, so the translation stays on screen.
            mode = MODE_SUBTITLE
            note = "AI 正在通话中，翻译只显示字幕"
        defaults = self.translation_defaults()
        rt.translator = CallTranslator(
            call_id=rt.call_id,
            device_id=rt.device_id,
            engine=self._engine,
            mode=mode,
            cadence=defaults["cadence"],
            my_language=defaults["my_language"],
            peer_language=defaults["peer_language"],
            publish=self._publish,
            speak_into_call=self._speak_gated,
            play_to_operator=self._play_to_operator,
            transcribe=self._transcribe_pcm,
        )
        rt.translate_note = note
        log.info("translation for call %s: mode=%s cadence=%s %s<-%s", rt.call_id, mode,
                 defaults["cadence"], defaults["my_language"], defaults["peer_language"])

    async def _start_capture(self, rt: _Runtime) -> None:
        if rt.bridge is not None:
            return
        bridge = CallAudioBridge(
            call_id=rt.call_id,
            device_id=rt.device_id,
            on_utterance=lambda audio, seconds: self._on_utterance(rt, audio, seconds),
            vad=VadConfig(sample_rate=int(settings.audio_sample_rate or 16000)),
            player=self._player,
            capture=self._capture,
        )
        rt.bridge = bridge
        if not await bridge.start():
            rt.capture_error = bridge.stats.capture_error or "无法采集通话音频"
            log.warning("no call audio for %s: %s", rt.call_id, rt.capture_error)
            await self._emit("call.flow.capture_failed", {
                "call_id": rt.call_id, "error": rt.capture_error,
            })

    async def _on_utterance(self, rt: _Runtime, audio: bytes, seconds: float) -> None:
        """One thing the far end said: recognise it, subtitle it, answer it."""
        async with rt.lock:
            if rt.ended_at is not None and rt.agent is None:
                return
            language = None
            if rt.translator is not None and rt.translator.active:
                language = rt.translator.peer_language
            text = await self._transcribe_pcm(audio, language)
            if not text:
                return
            await self._record(rt, {"role": "peer", "content": text, "at": int(time.time())})
            if rt.translator is not None and rt.translator.active:
                await rt.translator.peer_text(text)
            if rt.agent is not None and not rt.agent.finished:
                await rt.agent.peer_said(text)

    async def _transcribe_pcm(self, audio: bytes, language: Optional[str]) -> str:
        if not audio:
            return ""
        wav = pcm_to_wav(audio, int(settings.audio_sample_rate or 16000))
        try:
            text = await self._engine.transcribe(wav, filename="utterance.wav", language=language)
        except Exception as e:  # noqa: BLE001 - recognition failure is not fatal
            log.warning("call asr failed: %s", e)
            await self._emit("call.flow.asr_failed", {"detail": str(e)[:200]})
            return ""
        return (text or "").strip()

    async def _record(self, rt: _Runtime, entry: dict) -> None:
        rt.transcript.append(entry)
        session = self._calls.session_by_id(rt.call_id)
        if session is not None and self._setting("transcript.save", settings.transcript_save):
            session.transcript.append(entry)
        await self._emit("call.transcript", {
            "call_id": rt.call_id,
            "device_id": rt.device_id,
            "entry": entry,
            "lines": len(rt.transcript),
        })

    # -- audio paths ----------------------------------------------------

    async def _speak_gated(self, device_id: int, text: str) -> None:
        """Speak into the call with capture muted for the length of the audio.

        Without the gate the recogniser hears our own synthesised voice, treats
        it as the far end, and answers it.
        """
        text = (text or "").strip()
        if not text:
            return
        rt = self._runtime_for_device(device_id)
        bridge = rt.bridge if rt is not None else None
        if bridge is not None:
            await bridge.gate_for(0.4 * max(1, len(text)) + 1.0)
        try:
            if self._speak is not None:
                await self._speak(int(device_id), text)
            else:
                await self._calls.speak(int(device_id), text)
        finally:
            if bridge is not None:
                await bridge.gate_for(0.4)
                bridge.reset_capture()

    async def _play_to_operator(self, audio: bytes, tag: str) -> bool:
        """The far end's words, translated, in the operator's own ear."""
        if self._player is None or not audio:
            return False
        try:
            result = await self._player.play_bytes(audio, wait=True, timeout=30.0, tag=tag)
        except Exception as e:  # noqa: BLE001
            log.warning("operator playback failed: %s", e)
            return False
        return bool(getattr(result, "ok", False))

    async def _hangup(self, device_id: int) -> None:
        await self._calls.hangup(int(device_id))

    # -- detach ---------------------------------------------------------

    async def _detach(
        self, call_id: str, *, end_reason: Optional[str] = None, session: Optional[dict] = None
    ) -> None:
        rt = self._runtimes.pop(call_id, None)
        if rt is None:
            return
        rt.ended_at = time.time()
        rt.end_reason = end_reason or (session or {}).get("end_reason")
        pending = self._pending_answer.pop(rt.device_id, None)
        if pending is not None:
            pending.cancel()
        if rt.watchdog is not None:
            rt.watchdog.cancel()
            rt.watchdog = None
        if rt.bridge is not None:
            bridge, rt.bridge = rt.bridge, None
            try:
                # stop() flushes the last utterance, so the tail of the call is
                # still transcribed before the summary is written.
                await bridge.stop()
            except Exception as e:  # noqa: BLE001
                log.warning("stopping capture for %s failed: %s", call_id, e)
        if rt.agent is not None and not rt.agent.finished:
            try:
                await rt.agent.cancel()
            except Exception as e:  # noqa: BLE001
                log.warning("cancelling the agent for %s failed: %s", call_id, e)
        if rt.agent is not None and rt.agent.outcome is not None:
            rt.outcome = rt.agent.outcome.to_dict()
        keep = bool(self._setting("transcript.save", settings.transcript_save))
        if keep and rt.transcript:
            await asyncio.to_thread(repo_calls.update_call, call_id, transcript=rt.transcript)
        if keep and rt.transcript and self._setting("summary.auto_generate", settings.summary_auto_generate) \
                and self._setting("summary.save", settings.summary_save):
            await self.summarize(call_id, rt=rt)
        if rt.task_id is not None:
            await self._finish_task(rt)
        self._remember(rt)
        await self._emit("call.flow.ended", rt.to_dict(transcript_limit=20))
        rt.done.set()

    def _remember(self, rt: _Runtime) -> None:
        """Keep finished calls queryable so the app can still read the summary."""
        self._finished[rt.call_id] = rt
        self._order.append(rt.call_id)
        while len(self._order) > self._finished_kept:
            self._finished.pop(self._order.pop(0), None)

    # -- translation controls -------------------------------------------

    async def set_translation(
        self,
        call_id: str,
        *,
        mode: Optional[str] = None,
        cadence: Optional[str] = None,
        my_language: Optional[str] = None,
        peer_language: Optional[str] = None,
    ) -> dict:
        rt = self._runtimes.get(call_id)
        if rt is None:
            raise CallFlowError("通话不存在或已经结束")
        if mode is not None:
            normalized = str(mode).strip().lower()
            if normalized not in (MODE_OFF, MODE_SUBTITLE, MODE_VOICE):
                raise CallFlowError("翻译模式只能是 off、subtitle 或 voice")
            rt.translate_override = normalized
            if normalized == MODE_OFF:
                rt.translator = None
                rt.translate_note = None
            elif rt.translator is None:
                session = self._calls.session_by_id(call_id)
                if session is not None and session.state == CallState.ACTIVE:
                    rt.attached_at = rt.attached_at or time.time()
                self._build_translator(rt, normalized)
                if rt.bridge is None:
                    await self._start_capture(rt)
            else:
                rt.translator.set_mode(normalized)
        if rt.translator is not None:
            if cadence:
                rt.translator.set_cadence(cadence)
            if my_language:
                rt.translator.my_language = str(my_language).strip()
            if peer_language:
                rt.translator.peer_language = str(peer_language).strip()
        await self._emit("call.flow", rt.to_dict(transcript_limit=0))
        return {"ok": True, "flow": rt.to_dict()}

    async def stop_translation(self, call_id: str) -> dict:
        return await self.set_translation(call_id, mode=MODE_OFF)

    async def operator_text(self, call_id: str, text: str) -> dict:
        """The operator's own words, translated if needed, into the call."""
        rt = self._runtimes.get(call_id)
        if rt is None:
            raise CallFlowError("通话不存在或已经结束")
        text = (text or "").strip()
        if not text:
            raise CallFlowError("内容为空")
        if rt.agent is not None and not rt.agent.finished and not rt.handover:
            raise CallFlowError("AI 正在通话中，请先接管再说话")
        await self._record(rt, {"role": "me", "content": text, "at": int(time.time())})
        if rt.translator is not None and rt.translator.active:
            result = await rt.translator.say_to_peer(text)
            if result is not None:
                return {
                    "ok": True, "text": text, "spoken": result.target_text,
                    "translated": True, "target_language": result.target_language,
                    "played": result.played, "play_error": result.play_error,
                }
        await self._speak_gated(rt.device_id, text)
        return {"ok": True, "text": text, "spoken": text, "translated": False, "played": True}

    async def operator_audio(
        self, call_id: str, audio: bytes, *, language: Optional[str] = None,
        filename: str = "operator.wav",
    ) -> dict:
        """The operator speaks on the phone; we recognise it and say it across."""
        rt = self._runtimes.get(call_id)
        if rt is None:
            raise CallFlowError("通话不存在或已经结束")
        if not audio:
            raise CallFlowError("音频为空")
        hint = language
        if not hint and rt.translator is not None:
            hint = rt.translator.my_language
        if not hint:
            hint = str(self._setting("translate.my_language", settings.translate_my_language) or "") or None
        try:
            heard = await self._engine.transcribe(audio, filename=filename, language=hint or None)
        except Exception as e:  # noqa: BLE001
            raise CallFlowError(f"没有听清：{e}") from e
        heard = (heard or "").strip()
        if not heard:
            raise CallFlowError("没有听清内容，请再说一次")
        result = await self.operator_text(call_id, heard)
        result["heard"] = heard
        return result

    # -- agent controls -------------------------------------------------

    def _agent_spec(self, **kwargs) -> dict:
        return {k: v for k, v in kwargs.items() if v is not None}

    async def attach_agent(
        self,
        call_id: str,
        *,
        purpose: str,
        context: Optional[str] = None,
        language: Optional[str] = None,
        peer_language: Optional[str] = None,
        max_seconds: Optional[int] = None,
        opener: Optional[str] = None,
        extra_rules: Optional[str] = None,
        task_id: Optional[int] = None,
    ) -> dict:
        rt = self._runtimes.get(call_id)
        session = self._calls.session_by_id(call_id)
        if rt is None and session is None:
            raise CallFlowError("通话不存在")
        if rt is None and session is not None:
            rt = self._runtime(session.to_dict())
        assert rt is not None
        if rt.agent is not None and not rt.agent.finished:
            raise CallFlowError("已经有 AI 在这通电话里")
        purpose = (purpose or "").strip()
        if not purpose:
            raise CallFlowError("请填写这通电话要达到的目的")
        rt.purpose = purpose
        rt.handover = False
        if task_id is not None and rt.task_id is None:
            rt.task_id = int(task_id)
        rt.agent_request = self._agent_spec(
            purpose=purpose, context=context, language=language or "zh",
            peer_language=peer_language, max_seconds=max_seconds,
            opener=opener, extra_rules=extra_rules,
        )
        if session is not None and session.state == CallState.ACTIVE:
            if rt.attached_at is None:
                await self._attach(session.to_dict())
            else:
                await self._build_agent(rt)
        return {"ok": True, "flow": rt.to_dict()}

    async def _build_agent(self, rt: _Runtime) -> None:
        spec = rt.agent_request or {}
        rt.agent_request = None
        agent = CallAgent(
            call_id=rt.call_id,
            engine=self._engine,
            speak=lambda text: self._speak_gated(rt.device_id, text),
            hangup=lambda: self._hangup(rt.device_id),
            purpose=spec.get("purpose") or rt.purpose or "",
            task_id=rt.task_id,
            context=spec.get("context"),
            language=spec.get("language") or "zh",
            peer_language=spec.get("peer_language"),
            extra_rules=spec.get("extra_rules"),
            max_seconds=spec.get("max_seconds"),
            publish=self._agent_publish,
            opener=spec.get("opener"),
        )
        rt.agent = agent
        if rt.translator is not None and rt.translator.mode == MODE_VOICE:
            self._build_translator(rt, MODE_VOICE)
        if rt.bridge is None:
            await self._start_capture(rt)
        await self._emit("call.agent.started", {
            "call_id": rt.call_id, "task_id": rt.task_id, "purpose": agent.purpose,
        })
        rt.watchdog = asyncio.create_task(self._agent_watchdog(rt, agent))
        await agent.start()

    async def _agent_watchdog(self, rt: _Runtime, agent: CallAgent) -> None:
        """End a call the far end is holding open past its time limit."""
        limit = max(5, int(agent.max_seconds or settings.ai_call_max_seconds or 600))
        try:
            await asyncio.sleep(limit + 5)
        except asyncio.CancelledError:
            return
        if agent.finished or rt.ended_at is not None:
            return
        log.info("ai call %s reached its %ss limit", rt.call_id, limit)
        try:
            await agent.time_out()
        except Exception as e:  # noqa: BLE001
            log.warning("timing out the agent for %s failed: %s", rt.call_id, e)

    async def _agent_publish(self, event: str, payload: dict) -> None:
        call_id = payload.get("call_id")
        rt = self._runtimes.get(call_id) if call_id else None
        if rt is not None and event == "agent.finished":
            rt.outcome = dict(payload)
        await self._emit(event, payload)

    async def handover(self, call_id: str) -> dict:
        """A human takes the call: the agent stops talking and stops deciding."""
        rt = self._runtimes.get(call_id)
        if rt is None:
            raise CallFlowError("通话不存在或已经结束")
        rt.handover = True
        if rt.agent is not None and not rt.agent.finished:
            await rt.agent.request_handover()
        await self._emit("call.flow.handover", {"call_id": call_id, "device_id": rt.device_id})
        return {"ok": True, "flow": rt.to_dict()}

    # -- summaries ------------------------------------------------------

    async def summarize(
        self, call_id: str, *, rt: Optional[_Runtime] = None, force: bool = False
    ) -> Optional[dict]:
        rt = rt or self._runtimes.get(call_id) or self._finished.get(call_id)
        if rt is None:
            raise CallFlowError("通话不存在")
        if rt.summary and not force:
            return rt.summary
        if not rt.transcript:
            rt.summary_error = "没有可整理的通话内容"
            return None
        template = str(self._setting("summary.template", "") or "").strip() or None
        try:
            minutes = await self._engine.summarize(
                rt.transcript, purpose=rt.purpose, template=template, call_id=call_id
            )
        except Exception as e:  # noqa: BLE001 - a summary failure is not a call failure
            rt.summary_error = str(e)[:300]
            log.warning("summary failed for call %s: %s", call_id, e)
            await self._emit("call.summary.failed", {"call_id": call_id, "error": rt.summary_error})
            return None
        rt.summary = minutes
        rt.summary_error = None
        if self._setting("summary.save", settings.summary_save):
            await asyncio.to_thread(
                repo_calls.update_call, call_id, summary=minutes, summary_at=int(time.time())
            )
            if rt.task_id is not None:
                await self._write_task(rt.task_id, summary=minutes, force=True)
        await self._emit("call.summary", {
            "call_id": call_id, "task_id": rt.task_id, "summary": minutes,
        })
        return minutes

    # -- ai call tasks --------------------------------------------------

    async def submit_task(
        self,
        *,
        purpose: str,
        number: Optional[str] = None,
        direction: str = "outbound",
        device_id: Optional[int] = None,
        context: Optional[str] = None,
        language: Optional[str] = None,
        peer_language: Optional[str] = None,
        template: Optional[str] = None,
        max_seconds: Optional[int] = None,
        reference: Optional[str] = None,
        created_by: Optional[str] = None,
        owner_user_id: Optional[int] = None,
    ) -> dict:
        purpose = (purpose or "").strip()
        if not purpose:
            raise CallFlowError("请填写这通电话要达到的目的")
        direction = (direction or "outbound").strip().lower()
        if direction not in ("outbound", "inbound"):
            raise CallFlowError("direction 只能是 outbound 或 inbound")
        number = (number or "").strip() or None
        if direction == "outbound" and not number:
            raise CallFlowError("外呼任务需要填写号码")
        if number is not None and not PHONE_RE.match(number):
            raise CallFlowError("号码格式不正确")
        if max_seconds is not None:
            max_seconds = max(10, min(int(max_seconds), 3600))
        if reference:
            existing = await asyncio.to_thread(self._tasks.find_by_reference, str(reference))
            if existing is not None:
                # A retry of the same request must not place a second call.
                return {"task": existing, "created": False, "idempotent": True}
        task_id = await asyncio.to_thread(
            self._tasks.create_task,
            purpose=purpose, number=number, direction=direction, device_id=device_id,
            context=context, language=language, peer_language=peer_language,
            template=template, max_seconds=max_seconds, reference=reference,
            created_by=created_by, owner_user_id=owner_user_id,
        )
        task = await asyncio.to_thread(self._tasks.get_task, task_id) or {}
        if direction == "inbound":
            self._armed[task_id] = task
        self._wake.set()
        await self._emit("ai_call.task_queued", {"task": task})
        return {"task": task, "created": True, "idempotent": False}

    async def cancel_task(self, task_id: int) -> dict:
        task = await asyncio.to_thread(self._tasks.get_task, int(task_id))
        if task is None:
            raise CallFlowError("任务不存在")
        if task.get("status") in TERMINAL_STATUSES:
            return {"ok": False, "task": task, "error": "任务已经结束"}
        runner = self._running.pop(int(task_id), None)
        self._armed.pop(int(task_id), None)
        rt = self._runtime_for_task(int(task_id))
        if rt is not None:
            hung_up = False
            if rt.agent is not None and not rt.agent.finished:
                # The agent's own teardown hangs the call up; doing it here as
                # well would send a second hangup for one cancellation.
                await rt.agent.cancel()
                hung_up = True
            if rt.ended_at is None and not hung_up:
                try:
                    await self._hangup(rt.device_id)
                except Exception as e:  # noqa: BLE001
                    log.warning("hanging up for cancelled task %s failed: %s", task_id, e)
        await self._write_task(
            int(task_id), status="cancelled", result="任务被取消",
            finished_at=int(time.time()), force=True,
        )
        if runner is not None:
            # The runner is not cancelled outright: it may be inside dial(),
            # and killing it there would leave the call up with nobody holding
            # it. It checks the row again after the dial and hangs up itself.
            try:
                await asyncio.wait_for(asyncio.shield(runner), timeout=10.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                log.info("cancel of task %s did not finish its teardown in time", task_id)
        late = self._runtime_for_task(int(task_id))
        if late is not None and late.ended_at is None:
            # Whatever the runner was doing, the line must not stay up after a
            # cancel has been acknowledged. The call manager is asked rather
            # than the runtime, because the agent's own teardown may have hung
            # up a moment ago without the transition reaching us yet.
            session = self._calls.session_by_id(late.call_id)
            still_up = session is not None and session.state not in (
                CallState.IDLE, CallState.ENDED
            )
            if still_up:
                try:
                    await self._hangup(late.device_id)
                except Exception as e:  # noqa: BLE001
                    log.warning("final hangup for cancelled task %s failed: %s", task_id, e)
                await self._detach(late.call_id, end_reason="cancelled")
        task = await asyncio.to_thread(self._tasks.get_task, int(task_id)) or task
        await self._emit("ai_call.task_cancelled", {"task": task})
        return {"ok": True, "task": task}

    async def _worker(self) -> None:
        while not self._stopping:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_seconds)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if self._stopping:
                break
            try:
                await self._drain_queue()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - the queue must keep running
                log.warning("ai call task queue failed: %s", e)

    async def _drain_queue(self) -> None:
        pending = await asyncio.to_thread(self._tasks.list_pending, 25)
        armed: Dict[int, dict] = {}
        for task in pending:
            if task.get("direction") == "inbound":
                armed[int(task["id"])] = task
        self._armed = armed
        capacity = max(1, int(settings.voice_max_concurrent_calls or 1))
        for task in pending:
            if task.get("direction") == "inbound":
                continue
            task_id = int(task["id"])
            if task_id in self._running:
                continue
            if len(self._running) >= capacity:
                break
            self._launch(task)

    def _launch(self, task: dict) -> None:
        task_id = int(task["id"])
        runner = asyncio.create_task(self._run_task(task), name=f"ai-call-{task_id}")
        self._running[task_id] = runner
        runner.add_done_callback(lambda _done, tid=task_id: self._running.pop(tid, None))

    async def _run_task(self, task: dict) -> None:
        task_id = int(task["id"])
        number = task.get("number")
        purpose = task.get("purpose") or ""
        # Not forced: a cancel that landed while this task was still queued has
        # already written a terminal status, and dialling anyway would place a
        # call nobody asked for.
        await self._write_task(task_id, status="dialing")
        if await self._cancelled(task_id):
            return
        try:
            device_id = await self._pick_device(task.get("device_id"))
        except Exception as e:  # noqa: BLE001
            await self._write_task(task_id, status="failed", error=f"没有可用的模块：{e}"[:300],
                                   finished_at=int(time.time()), force=True)
            return
        try:
            session = await self._calls.dial(
                device_id, number, owner_user_id=task.get("owner_user_id"), origin="ai_task"
            )
        except Exception as e:  # noqa: BLE001
            await self._write_task(task_id, status="failed", error=f"拨号失败：{e}"[:300],
                                   finished_at=int(time.time()), force=True)
            return
        call_id = session.call_id
        if await self._cancelled(task_id):
            # Cancelled while the dial was in flight. The call exists now, so it
            # has to be taken down again rather than left ringing out.
            rt = self._runtime(session.to_dict())
            rt.task_id = task_id
            try:
                await self._hangup(device_id)
            except Exception as e:  # noqa: BLE001
                log.warning("hanging up cancelled task %s failed: %s", task_id, e)
            await self._detach(call_id, end_reason="cancelled")
            return
        rt = self._runtime(session.to_dict())
        rt.task_id = task_id
        rt.purpose = purpose
        rt.agent_request = self._agent_spec(
            purpose=purpose,
            context=task.get("context"),
            language=task.get("language") or "zh",
            peer_language=task.get("peer_language"),
            max_seconds=task.get("max_seconds"),
        )
        await asyncio.to_thread(
            repo_calls.update_call, call_id, task_id=task_id, task_purpose=purpose[:300]
        )
        await self._write_task(task_id, status="in_call", call_id=call_id,
                               started_at=int(time.time()))
        await self._emit("ai_call.task_started", {
            "task_id": task_id, "call_id": call_id, "number": number, "purpose": purpose,
        })
        connected = await self._wait_for_active(call_id)
        if not connected:
            reason = rt.end_reason or "no_answer"
            status = {"no_answer": "no_answer", "busy": "busy", "rejected": "rejected"}.get(
                reason, "failed"
            )
            await self._write_task(task_id, status=status, call_id=call_id,
                                   result=f"通话未接通（{reason}）"[:300],
                                   finished_at=int(time.time()))
            return
        limit = int(task.get("max_seconds") or settings.ai_call_max_seconds or 600)
        try:
            await asyncio.wait_for(rt.done.wait(), timeout=limit + 60)
        except asyncio.TimeoutError:
            log.warning("ai call task %s overran its limit, hanging up", task_id)
            if rt.agent is not None and not rt.agent.finished:
                await rt.agent.time_out()
            await asyncio.sleep(2)
            if not rt.done.is_set():
                try:
                    await self._hangup(device_id)
                except Exception as e:  # noqa: BLE001
                    log.warning("hanging up task %s failed: %s", task_id, e)

    async def _wait_for_active(self, call_id: str) -> bool:
        deadline = time.monotonic() + float(settings.voice_dial_timeout_answer_seconds or 90)
        while time.monotonic() < deadline:
            session = self._calls.session_by_id(call_id)
            if session is None:
                return False
            if session.state == CallState.ACTIVE:
                return True
            if session.state == CallState.ENDED:
                rt = self._runtimes.get(call_id)
                if rt is not None and session.end_reason is not None:
                    rt.end_reason = session.end_reason.value
                return False
            await asyncio.sleep(0.2)
        return False

    async def _pick_device(self, device_id: Optional[int]) -> int:
        if self._modems is None:
            raise CallFlowError("没有可用的模块管理器")
        return int(await self._modems.pick_call_device(device_id))

    async def _cancelled(self, task_id: int) -> bool:
        row = await asyncio.to_thread(self._tasks.get_task, int(task_id))
        return bool(row and row.get("status") == "cancelled")

    async def _finish_task(self, rt: _Runtime) -> None:
        outcome = rt.outcome or {}
        kind = str(outcome.get("outcome") or "")
        result = str(outcome.get("result") or "")
        achieved = outcome.get("achieved")
        if kind == OUTCOME_ERROR:
            status = "failed"
            result = result or str(outcome.get("error") or "AI 通话出错")
        elif kind == OUTCOME_CANCELLED:
            status = "cancelled"
            result = result or "任务被取消"
        elif kind == OUTCOME_NO_ANSWER:
            status = "no_answer"
            result = result or "对方未接听"
        elif kind:
            status = "finished"
            result = result or "通话结束"
        else:
            reason = rt.end_reason or "unknown"
            status = {"no_answer": "no_answer", "busy": "busy", "rejected": "rejected"}.get(
                reason, "finished"
            )
            result = result or f"通话结束（{reason}）"
        await self._write_task(
            rt.task_id, status=status, call_id=rt.call_id, result=result[:300],
            achieved=achieved, summary=rt.summary, finished_at=int(time.time()),
        )
        await self._emit("ai_call.task_finished", {
            "task_id": rt.task_id, "call_id": rt.call_id, "status": status,
            "outcome": kind or None, "result": result[:300], "achieved": achieved,
        })

    async def _write_task(self, task_id: Optional[int], *, force: bool = False, **fields) -> bool:
        if task_id is None:
            return False
        if not force:
            row = await asyncio.to_thread(self._tasks.get_task, int(task_id))
            if row is not None and row.get("status") in TERMINAL_STATUSES:
                return False
        fields = {k: v for k, v in fields.items() if v is not None}
        if not fields:
            return False
        try:
            return bool(await asyncio.to_thread(self._tasks.update_task, int(task_id), **fields))
        except Exception as e:  # noqa: BLE001
            log.warning("updating task %s failed: %s", task_id, e)
            return False

    async def _fail_task(self, task_id: int, detail: str) -> None:
        await self._write_task(task_id, status="failed", error=detail[:300],
                               finished_at=int(time.time()), force=True)

    # -- introspection --------------------------------------------------

    def snapshot(self) -> List[dict]:
        return [rt.to_dict(transcript_limit=0) for rt in self._runtimes.values()]

    def flow(self, call_id: str) -> Optional[dict]:
        rt = self._runtimes.get(call_id) or self._finished.get(call_id)
        return rt.to_dict() if rt else None

    def transcript(self, call_id: str, limit: int = 400) -> List[dict]:
        rt = self._runtimes.get(call_id) or self._finished.get(call_id)
        if rt is None:
            return []
        return list(rt.transcript[-max(1, int(limit)):])

    def state(self) -> dict:
        return {
            "inbound_mode": self.inbound_mode(),
            "inbound_purpose": self.inbound_purpose(),
            "translation": self.translation_defaults(),
            "capture": self._capture.stats() if self._capture is not None else [],
            "armed_inbound_tasks": sorted(self._armed),
            "running_tasks": sorted(self._running),
            "calls": self.snapshot(),
        }

    async def _emit(self, event: str, payload: dict) -> None:
        if self._publish is None:
            return
        try:
            await self._publish(event, payload)
        except Exception as e:  # noqa: BLE001
            log.debug("publishing %s failed: %s", event, e)
