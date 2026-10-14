"""Per-call capture orchestration.

The audio player knows how to run arecord/parecord; this module knows when a
call should be captured. It starts a capture when a call reaches the active
state, stops it when the call ends, persists the file path onto the CDR row
and publishes realtime events so consoles can show a live REC marker.
"""
from __future__ import annotations

import inspect
import logging
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional
from typing import Any

from ..audio.player import RecordingHandle
from ..config import settings

log = logging.getLogger(__name__)

__all__ = ["CallRecorder"]

Publish = Callable[[str, dict], Awaitable[None]]
Persist = Callable[[str, str], Any]


class CallRecorder:
    """Tracks one capture handle per live call."""

    def __init__(
        self,
        player,
        enabled: Callable[[], bool],
        publish: Optional[Publish] = None,
        persist: Optional[Persist] = None,
        record_dir: Optional[Path] = None,
    ) -> None:
        self._player = player
        self._enabled = enabled
        self._publish = publish
        self._persist = persist
        self._dir = Path(record_dir or settings.audio_record_dir)
        self._handles: Dict[str, RecordingHandle] = {}

    def is_recording(self, call_id: str) -> bool:
        return call_id in self._handles

    @property
    def active_call_ids(self) -> list[str]:
        return list(self._handles)

    def path_for(self, call_id: str) -> Path:
        return self._dir / (call_id + ".wav")

    async def on_state(self, state: str, session: dict) -> Optional[Path]:
        """Hook for call state transitions: active starts, ended stops."""
        call_id = session.get("call_id")
        if not call_id:
            return None
        if state == "active":
            return await self.start(call_id)
        if state == "ended":
            return await self.stop(call_id)
        return None

    async def start(self, call_id: str) -> Optional[Path]:
        if call_id in self._handles:
            return None
        if not self._enabled():
            return None
        path = self.path_for(call_id)
        handle = await self._player.start_recording(path)
        if handle is None:
            log.info("recording unavailable for call %s", call_id)
            return None
        self._handles[call_id] = handle
        log.info("recording started for call %s on %s", call_id, handle.device or "default input")
        await self._emit("started", call_id, path, None)
        return path

    async def stop(self, call_id: str) -> Optional[Path]:
        handle = self._handles.pop(call_id, None)
        if handle is None:
            return None
        path = await self._player.stop_recording(handle)
        size = path.stat().st_size if path and path.exists() else 0
        if size <= 0:
            log.warning("recording for call %s is empty, dropping", call_id)
            await self._emit("empty", call_id, path, 0)
            return None
        if self._persist is not None:
            result = self._persist(call_id, str(path))
            if inspect.isawaitable(result):
                await result
        log.info("recording for call %s saved (%d bytes)", call_id, size)
        await self._emit("stopped", call_id, path, size)
        return path

    async def stop_all(self) -> None:
        for call_id in list(self._handles):
            try:
                await self.stop(call_id)
            except Exception as e:  # shutdown must not raise
                log.warning("stopping recording for %s failed: %s", call_id, e)

    async def _emit(self, phase: str, call_id: str, path: Optional[Path], size: Optional[int]) -> None:
        if self._publish is None:
            return
        payload = {
            "call_id": call_id,
            "phase": phase,
            "path": str(path) if path else None,
        }
        if size is not None:
            payload["size"] = size
        try:
            await self._publish("call.recording", payload)
        except Exception as e:
            log.warning("publishing call.recording failed: %s", e)
