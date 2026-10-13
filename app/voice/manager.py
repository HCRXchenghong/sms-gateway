"""Call lifecycle management across all attached modules."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Awaitable, Callable, Dict, List, Optional, Protocol

from ..config import settings
from .parser import VoiceUrc
from .state import CallDirection, CallSession, CallState, CallStateMachine, EndReason, Transition

log = logging.getLogger(__name__)


class VoiceCapableModem(Protocol):
    """The slice of a modem object that the call manager depends on."""

    device_id: int
    connected: bool

    async def dial(self, number: str) -> List[str]: ...
    async def answer(self) -> List[str]: ...
    async def hangup(self) -> None: ...
    async def send_dtmf(self, digits: str) -> int: ...
    async def tts_speak(self, text: str, mode: int = 1) -> None: ...
    async def tts_stop(self) -> None: ...
    async def list_calls(self) -> List[dict]: ...


EventPublisher = Callable[[str, dict], Awaitable[None]]
# phase is "start" when the call is created and "end" when it terminates.
CdrWriter = Callable[[CallSession, str], Awaitable[None]]
PromptHook = Callable[[int, str, dict], Awaitable[None]]


class VoiceError(RuntimeError):
    """Raised for user-visible call control failures."""


class CallManager:
    """One call session per device, driven by URCs plus AT+CLCC polling."""

    def __init__(
        self,
        get_modem: Callable[[int], Optional[VoiceCapableModem]],
        publish: Optional[EventPublisher] = None,
        write_cdr: Optional[CdrWriter] = None,
        on_prompt: Optional[PromptHook] = None,
    ):
        self._get_modem = get_modem
        self._publish = publish
        self._write_cdr = write_cdr
        self._on_prompt = on_prompt
        self._sessions: Dict[int, CallStateMachine] = {}
        self._by_call_id: Dict[str, CallStateMachine] = {}
        self._poll_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._urc_loop: Optional[asyncio.AbstractEventLoop] = None
        self._tts_queue: Dict[int, asyncio.Queue] = {}
        # RING arrives before +CLIP, so an incoming call is announced only
        # once the caller number shows up or a short grace period expires.
        self._announced: set[str] = set()
        self._pending_announce: Dict[int, asyncio.Task] = {}
        self._announce_grace = 0.5

    # -- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        self._urc_loop = asyncio.get_running_loop()
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(self._poll_loop(), name="call-clcc-poll")

    async def stop(self) -> None:
        for task in self._pending_announce.values():
            task.cancel()
        self._pending_announce.clear()
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None
        for machine in list(self._sessions.values()):
            if machine.session.state != CallState.ENDED:
                await self._apply(machine.handle_local_hangup())

    # -- queries --------------------------------------------------------

    def session_for_device(self, device_id: int) -> Optional[CallSession]:
        machine = self._sessions.get(int(device_id))
        if not machine or machine.session.state in (CallState.IDLE, CallState.ENDED):
            return None
        return machine.session

    def session_by_id(self, call_id: str) -> Optional[CallSession]:
        machine = self._by_call_id.get(call_id)
        return machine.session if machine else None

    def active_sessions(self) -> List[CallSession]:
        return [
            m.session
            for m in self._sessions.values()
            if m.session.state not in (CallState.IDLE, CallState.ENDED)
        ]

    def snapshot(self) -> List[dict]:
        return [m.session.to_dict() for m in self._sessions.values()]

    # -- URC entry point ------------------------------------------------

    def on_voice_urc(self, device_id: int, urc: VoiceUrc) -> None:
        """Synchronous hook called from the modem reader on the event loop."""
        loop = self._urc_loop or asyncio.get_event_loop_policy().get_event_loop()
        try:
            loop.create_task(self._handle_urc(device_id, urc))
        except RuntimeError:
            log.warning("no running loop to dispatch voice urc %s", urc.kind)

    async def _handle_urc(self, device_id: int, urc: VoiceUrc) -> None:
        log.info("voice urc device=%s kind=%s number=%s", device_id, urc.kind, urc.number)
        async with self._lock:
            machine = self._sessions.get(device_id)
            if machine is None or machine.session.state in (CallState.IDLE, CallState.ENDED):
                if urc.kind not in ("ring", "clip"):
                    return
                machine = self._new_session(device_id, CallDirection.INBOUND)

            if urc.kind == "ring":
                transitions = machine.handle_urc("ring", urc.number)
                await self._apply(transitions)
                if machine.session.state == CallState.INCOMING:
                    self._schedule_announce(device_id, machine)
                else:
                    # Repeated RING on a call that is already established.
                    await self._emit("call.ring", machine.session.to_dict())
                return

            if urc.kind == "clip":
                machine.handle_urc("clip", urc.number)
                if machine.session.state == CallState.INCOMING and urc.number:
                    await self._announce(device_id, machine)
                return

            transitions = machine.handle_urc(urc.kind, urc.number)
            await self._apply(transitions)

    def _schedule_announce(self, device_id: int, machine: CallStateMachine) -> None:
        """Announce the incoming call even if the module never sends +CLIP."""
        if machine.session.call_id in self._announced:
            return
        pending = self._pending_announce.get(device_id)
        if pending and not pending.done():
            return
        self._pending_announce[device_id] = asyncio.create_task(
            self._announce_after_grace(device_id, machine)
        )

    async def _announce_after_grace(self, device_id: int, machine: CallStateMachine) -> None:
        try:
            await asyncio.sleep(self._announce_grace)
        except asyncio.CancelledError:
            return
        await self._announce(device_id, machine)

    async def _announce(self, device_id: int, machine: CallStateMachine) -> None:
        pending = self._pending_announce.pop(device_id, None)
        if pending and not pending.done():
            pending.cancel()
        if machine.session.call_id in self._announced:
            return
        if machine.session.state != CallState.INCOMING:
            return
        self._announced.add(machine.session.call_id)
        await self._emit("call.incoming", machine.session.to_dict())

    def _new_session(
        self,
        device_id: int,
        direction: CallDirection,
        remote: Optional[str] = None,
        owner_user_id: Optional[int] = None,
        origin: Optional[str] = None,
    ) -> CallStateMachine:
        call_id = uuid.uuid4().hex[:16]
        session = CallSession(
            call_id=call_id,
            device_id=int(device_id),
            direction=direction,
            remote=remote,
            owner_user_id=owner_user_id,
            origin=origin,
        )
        machine = CallStateMachine(session)
        self._sessions[int(device_id)] = machine
        self._by_call_id[call_id] = machine
        return machine

    # -- control --------------------------------------------------------

    async def dial(
        self,
        device_id: int,
        number: str,
        *,
        owner_user_id: Optional[int] = None,
        origin: Optional[str] = None,
    ) -> CallSession:
        if not settings.voice_enabled:
            raise VoiceError("voice calls are disabled on this server")
        modem = self._get_modem(int(device_id))
        if modem is None or not modem.connected:
            raise VoiceError(f"device {device_id} is not online")
        async with self._lock:
            existing = self.session_for_device(device_id)
            if existing:
                raise VoiceError(f"device {device_id} is already in a call")
            if len(self.active_sessions()) >= settings.voice_max_concurrent_calls:
                raise VoiceError("too many concurrent calls on this server")
            machine = self._new_session(
                device_id, CallDirection.OUTBOUND, number, owner_user_id, origin or "api"
            )
            await self._apply(machine.start_outbound(number))
        try:
            await modem.dial(number)
        except Exception as e:
            async with self._lock:
                await self._apply(machine.handle_error(str(e)))
            raise VoiceError(str(e))
        return machine.session

    async def answer(self, device_id: int) -> CallSession:
        modem = self._get_modem(int(device_id))
        if modem is None or not modem.connected:
            raise VoiceError(f"device {device_id} is not online")
        async with self._lock:
            machine = self._sessions.get(int(device_id))
            if not machine or machine.session.state != CallState.INCOMING:
                raise VoiceError(f"no incoming call on device {device_id}")
        try:
            await modem.answer()
        except Exception as e:
            async with self._lock:
                await self._apply(machine.handle_error(str(e)))
            raise VoiceError(str(e))
        async with self._lock:
            await self._apply(machine.handle_answer())
        return machine.session

    async def hangup(self, device_id: int) -> None:
        modem = self._get_modem(int(device_id))
        async with self._lock:
            machine = self._sessions.get(int(device_id))
            if not machine or machine.session.state in (CallState.IDLE, CallState.ENDED):
                return
        if modem is not None and modem.connected:
            try:
                await modem.hangup()
            except Exception as e:
                log.warning("hangup on device %s failed: %s", device_id, e)
        async with self._lock:
            await self._apply(machine.handle_local_hangup())

    async def send_dtmf(self, device_id: int, digits: str) -> int:
        modem = self._get_modem(int(device_id))
        if modem is None or not modem.connected:
            raise VoiceError(f"device {device_id} is not online")
        sent = await modem.send_dtmf(digits)
        await self._emit(
            "call.dtmf",
            {"device_id": int(device_id), "digits": digits, "sent": sent},
        )
        return sent

    async def speak(self, device_id: int, text: str, mode: Optional[int] = None) -> None:
        """Play TTS into the call through the module synthesiser."""
        modem = self._get_modem(int(device_id))
        if modem is None or not modem.connected:
            raise VoiceError(f"device {device_id} is not online")
        engine_mode = int(mode or settings.voice_tts_mode)
        await modem.tts_speak(text, engine_mode)
        async with self._lock:
            machine = self._sessions.get(int(device_id))
            if machine:
                machine.session.tts_log.append({"text": text, "mode": engine_mode, "at": int(time.time())})
        await self._emit("call.tts", {"device_id": int(device_id), "text": text, "mode": engine_mode})

    async def stop_speak(self, device_id: int) -> None:
        modem = self._get_modem(int(device_id))
        if modem is None or not modem.connected:
            raise VoiceError(f"device {device_id} is not online")
        await modem.tts_stop()

    # -- polling --------------------------------------------------------

    async def _poll_loop(self) -> None:
        interval = max(0.3, float(settings.voice_clcc_poll_interval_seconds))
        while True:
            await asyncio.sleep(interval)
            targets = [
                device_id
                for device_id, m in list(self._sessions.items())
                if m.session.state not in (CallState.IDLE, CallState.ENDED)
            ]
            for device_id in targets:
                try:
                    await self._poll_device(device_id)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.debug("clcc poll failed for %s: %s", device_id, e)

    async def _poll_device(self, device_id: int) -> None:
        modem = self._get_modem(device_id)
        async with self._lock:
            machine = self._sessions.get(device_id)
        if machine is None or machine.session.state in (CallState.IDLE, CallState.ENDED):
            return
        if modem is None or not modem.connected:
            async with self._lock:
                await self._apply(machine.handle_timeout())
            return
        rows = await modem.list_calls()
        async with self._lock:
            if not rows:
                await self._apply(machine.handle_clcc_absent())
                return
            for row in rows:
                await self._apply(machine.handle_clcc(row.get("stat", ""), row.get("number") or None))

    # -- transitions ----------------------------------------------------

    async def _apply(self, transitions: List[Transition]) -> None:
        for transition in transitions:
            machine = self._by_call_id.get(transition.call_id)
            session = machine.session if machine else None
            payload = transition.to_dict()
            if session:
                payload["session"] = session.to_dict()
            await self._emit("call.state", payload)
            # Open the CDR row as soon as the call leaves IDLE so in-progress
            # calls show up in history and survive an unclean shutdown.
            if self._write_cdr and session and transition.from_state == CallState.IDLE:
                try:
                    await self._write_cdr(session, "start")
                except Exception as e:
                    log.warning("cdr start write failed: %s", e)
            if self._on_prompt and session:
                try:
                    await self._on_prompt(session.device_id, transition.to_state.value, session.to_dict())
                except Exception as e:
                    log.warning("prompt hook failed: %s", e)
            if transition.to_state == CallState.ENDED and session:
                await self._emit("call.ended", session.to_dict())
                self._announced.discard(session.call_id)
                pending = self._pending_announce.pop(session.device_id, None)
                if pending and not pending.done():
                    pending.cancel()
                if self._write_cdr:
                    try:
                        await self._write_cdr(session, "end")
                    except Exception as e:
                        log.warning("cdr write failed: %s", e)

    async def _emit(self, event: str, payload: dict) -> None:
        if not self._publish:
            return
        try:
            await self._publish(event, payload)
        except Exception as e:
            log.debug("publish %s failed: %s", event, e)
