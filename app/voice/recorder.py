"""Per-call capture orchestration.

The audio player knows how to run arecord/parecord; this module knows whether a
call should be captured at all, and whether the far end has to be told. It
starts a capture when a call reaches the active state, stops it when the call
ends, persists the file path and the announcement outcome onto the CDR row, and
publishes realtime events so consoles can show a live REC marker.

Recording and announcing are decided by the configured policy, never by whether
a sound card happens to be present. A call that should have been recorded but
could not be is reported as a failure rather than silently skipped, because
"forced" recording that quietly captures nothing is worse than an error.
"""
from __future__ import annotations

import inspect
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from ..audio.player import RecordingHandle
from ..config import settings
from .recording_policy import RecordingDecision, decide_recording

log = logging.getLogger(__name__)

__all__ = ["CallRecorder"]

Publish = Callable[[str, dict], Awaitable[None]]
Persist = Callable[[str, str], Any]
MetaPersist = Callable[[str, dict], Any]
Speak = Callable[[int, str], Awaitable[None]]
BoolSetting = Callable[[], bool]
TextSetting = Callable[[], str]


@dataclass
class _Capture:
    """Everything we know about one call's recording."""

    call_id: str
    device_id: Optional[int]
    decision: RecordingDecision
    handle: Optional[RecordingHandle] = None
    path: Optional[Path] = None
    notice: Dict[str, Any] = field(default_factory=dict)
    failed_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "call_id": self.call_id,
            "device_id": self.device_id,
            "recording": self.handle is not None,
            "record": self.decision.record,
            "announce": self.decision.announce,
            "can_change": self.decision.can_change,
            "reason": self.decision.reason,
            "path": str(self.path) if self.path else None,
            "notice": self.notice or None,
            "failed_reason": self.failed_reason,
        }


class CallRecorder:
    """Tracks one capture per live call and the notice that went with it."""

    def __init__(
        self,
        player,
        enabled: Optional[BoolSetting] = None,
        publish: Optional[Publish] = None,
        persist: Optional[Persist] = None,
        record_dir: Optional[Path] = None,
        *,
        policy: Optional[TextSetting] = None,
        default_enabled: Optional[BoolSetting] = None,
        announce_default: Optional[BoolSetting] = None,
        speak_into_call: Optional[Speak] = None,
        persist_meta: Optional[MetaPersist] = None,
    ) -> None:
        self._player = player
        self._enabled = enabled
        self._publish = publish
        self._persist = persist
        self._persist_meta = persist_meta
        self._policy = policy or (lambda: settings.recording_policy)
        self._default_enabled = default_enabled or (lambda: settings.recording_default_enabled)
        self._announce_default = announce_default or (lambda: settings.recording_announce)
        self._speak = speak_into_call
        self._dir = Path(record_dir or settings.audio_record_dir)
        self._captures: Dict[str, _Capture] = {}

    # -- queries --------------------------------------------------------

    def is_recording(self, call_id: str) -> bool:
        capture = self._captures.get(call_id)
        return bool(capture and capture.handle is not None)

    @property
    def active_call_ids(self) -> list[str]:
        return [cid for cid, cap in self._captures.items() if cap.handle is not None]

    def path_for(self, call_id: str) -> Path:
        return self._dir / (call_id + ".wav")

    def capture_for(self, call_id: str) -> Optional[dict]:
        capture = self._captures.get(call_id)
        return capture.to_dict() if capture else None

    def snapshot(self) -> list[dict]:
        return [cap.to_dict() for cap in self._captures.values()]

    def policy(self) -> str:
        """The configured policy name, for consoles that label the switch."""
        return str(self._policy())

    def status(self, call_id: str) -> dict:
        """Everything a client needs to render one call's recording control.

        The decision is resolved with no operator override so can_change tells
        the truth about whether the toggle below will be honoured: a forced
        policy cannot be switched off from a phone, and a disabled one cannot
        be switched on.
        """
        capture = self._captures.get(call_id)
        decision = self.decide(mid_call=True)
        return {
            "call_id": call_id,
            "recording": self.is_recording(call_id),
            "capture": capture.to_dict() if capture else None,
            "policy": self.policy(),
            "decision": decision.to_dict(),
            "notice": (capture.notice if capture else None) or None,
        }

    def decide(
        self,
        *,
        requested: Optional[bool] = None,
        announce: Optional[bool] = None,
        mid_call: bool = False,
    ) -> RecordingDecision:
        """Resolve the current policy into a decision. Exposed for the API."""
        return decide_recording(
            policy=self._policy(),
            requested=requested,
            announce=announce,
            default_enabled=self._default_enabled(),
            announce_default=self._announce_default(),
            mid_call=mid_call,
        )

    # -- lifecycle ------------------------------------------------------

    async def on_state(self, state: str, session: dict) -> Optional[Path]:
        """Hook for call state transitions: active starts, ended stops."""
        call_id = session.get("call_id")
        if not call_id:
            return None
        if state == "active":
            return await self.start(
                call_id,
                device_id=session.get("device_id"),
                requested=session.get("record"),
                announce=session.get("announce_recording"),
            )
        if state == "ended":
            return await self.stop(call_id)
        return None

    async def start(
        self,
        call_id: str,
        *,
        device_id: Optional[int] = None,
        requested: Optional[bool] = None,
        announce: Optional[bool] = None,
        mid_call: bool = False,
    ) -> Optional[Path]:
        existing = self._captures.get(call_id)
        if existing is not None:
            if existing.handle is not None:
                # Already capturing: nothing new happened, so report None the
                # way callers expect rather than a path they did not just open.
                return None
            # A previous attempt failed; retrying on an explicit operator
            # request is legitimate, so fall through and try again.
            mid_call = True

        decision = self.decide(requested=requested, announce=announce, mid_call=mid_call)
        capture = _Capture(call_id=call_id, device_id=device_id, decision=decision)
        self._captures[call_id] = capture

        if not decision.record:
            await self._persist_decision(capture)
            await self._emit("skipped", capture)
            return None

        # The legacy audio.record_enabled switch still gates the hardware path.
        if self._enabled is not None and not self._enabled():
            capture.failed_reason = "audio_capture_disabled"
            await self._persist_decision(capture)
            await self._emit("unavailable", capture)
            if decision.reason == "policy_forced":
                log.error(
                    "forced recording is configured but audio capture is disabled; "
                    "call %s is NOT being recorded", call_id,
                )
            return None

        path = self.path_for(call_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # The default record directory lives under /var/lib, which the
            # service user may not be allowed to create. Raising here would
            # escape into the call prompt hook and look like a modem failure,
            # so report it as the capture problem it is.
            capture.failed_reason = "record_dir_unwritable"
            log.error(
                "recording directory %s is not writable (%s); call %s is NOT being recorded",
                path.parent, exc, call_id,
            )
            await self._persist_decision(capture)
            await self._emit("unavailable", capture)
            return None
        handle = await self._player.start_recording(path)
        if handle is None:
            capture.failed_reason = "no_input_device"
            await self._persist_decision(capture)
            await self._emit("unavailable", capture)
            if decision.reason == "policy_forced":
                log.error(
                    "forced recording is configured but no capture device answered; "
                    "call %s is NOT being recorded", call_id,
                )
            else:
                log.info("recording unavailable for call %s", call_id)
            return None

        capture.handle = handle
        capture.path = path
        log.info("recording started for call %s on %s", call_id, handle.device or "default input")
        await self._persist_decision(capture)
        await self._emit("started", capture)

        if decision.announce:
            await self.announce(call_id, mid_call=mid_call)
        return path

    async def announce(self, call_id: str, *, mid_call: bool = False) -> dict:
        """Tell the far end the call is being recorded, and record the outcome.

        A notice that failed to play is stored as failed. It is never reported
        as announced, because the whole point of the log is to show whether the
        other party was actually told.
        """
        capture = self._captures.get(call_id)
        if capture is None:
            return {"ok": False, "error": "no such call"}
        decision = capture.decision
        text = decision.notice_text
        if not text:
            from .recording_policy import NOTICE_AT_START, NOTICE_MID_CALL

            text = NOTICE_MID_CALL if mid_call else NOTICE_AT_START
        notice: Dict[str, Any] = {
            "text": text,
            "at": int(time.time()),
            "ok": False,
            "error": None,
            "mid_call": bool(mid_call),
        }
        if self._speak is None or capture.device_id is None:
            notice["error"] = "no audio path into the call"
        else:
            try:
                await self._speak(int(capture.device_id), text)
                notice["ok"] = True
            except Exception as exc:  # noqa: BLE001 - the call must go on
                notice["error"] = str(exc)[:300]
                log.warning("recording notice failed for call %s: %s", call_id, exc)
        capture.notice = notice
        await self._persist_decision(capture)
        await self._emit("announced" if notice["ok"] else "announce_failed", capture)
        return notice

    async def set_recording(
        self,
        call_id: str,
        enabled: bool,
        *,
        device_id: Optional[int] = None,
        announce: Optional[bool] = None,
    ) -> dict:
        """Operator toggle. Refused when the policy does not allow a change."""
        decision = self.decide(requested=enabled, announce=announce, mid_call=True)
        if not decision.can_change:
            return {
                "ok": False,
                "error": "当前录音策略不允许修改",
                "recording": self.is_recording(call_id),
                "decision": decision.to_dict(),
            }
        if enabled:
            await self.start(call_id, device_id=device_id, requested=True, announce=announce, mid_call=True)
        else:
            await self.stop(call_id)
        return {
            "ok": True,
            "recording": self.is_recording(call_id),
            "decision": decision.to_dict(),
        }

    async def stop(self, call_id: str) -> Optional[Path]:
        capture = self._captures.pop(call_id, None)
        if capture is None or capture.handle is None:
            if capture is not None:
                await self._persist_decision(capture)
            return None
        handle = capture.handle
        path = await self._player.stop_recording(handle)
        size = path.stat().st_size if path and path.exists() else 0
        capture.path = path
        if size <= 0:
            capture.failed_reason = "empty_file"
            log.warning("recording for call %s is empty, dropping", call_id)
            await self._persist_decision(capture)
            await self._emit("empty", capture, size=0)
            self._captures.pop(call_id, None)
            return None
        if self._persist is not None:
            result = self._persist(call_id, str(path))
            if inspect.isawaitable(result):
                await result
        log.info("recording for call %s saved (%d bytes)", call_id, size)
        await self._emit("stopped", capture, size=size)
        return path

    async def stop_all(self) -> None:
        for call_id in list(self._captures):
            try:
                await self.stop(call_id)
            except Exception as e:  # shutdown must not raise
                log.warning("stopping recording for %s failed: %s", call_id, e)

    # -- plumbing -------------------------------------------------------

    async def _persist_decision(self, capture: _Capture) -> None:
        if self._persist_meta is None:
            return
        notice = capture.notice or {}
        payload = {
            "recording_reason": capture.decision.reason,
            "recording_announced": bool(capture.decision.announce),
        }
        if notice:
            payload.update({
                "recording_notice": notice.get("text"),
                "recording_notice_at": notice.get("at"),
                "recording_notice_ok": bool(notice.get("ok")),
                "recording_notice_error": notice.get("error") or "",
            })
        try:
            result = self._persist_meta(capture.call_id, payload)
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            log.warning("persisting recording metadata for %s failed: %s", capture.call_id, e)

    async def _emit(self, phase: str, capture: _Capture, *, size: Optional[int] = None) -> None:
        if self._publish is None:
            return
        payload = capture.to_dict()
        payload["phase"] = phase
        if size is not None:
            payload["size"] = size
        try:
            await self._publish("call.recording", payload)
        except Exception as e:
            log.warning("publishing call.recording failed: %s", e)
