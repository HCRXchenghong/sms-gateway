"""Live two-way audio for one call.

The module's audio port is wired to a sound card on this machine. Capture reads
that card, segments it into utterances, and hands each one to recognition;
playback writes rendered speech back out through the same card so the far end
hears it.

The echo gate is the part that matters. Anything we play into the call comes
back out of the capture a moment later. Without gating it, the system would
recognise its own synthesised voice, translate it back into the other language,
and play that into the call, in a loop that gets louder each time. So capture
callbacks are suppressed for the duration of playback plus a short tail, and
the segmenter is reset afterwards to drop the partial frame it was holding.

Capture runs as a subprocess streaming raw PCM to stdout, which keeps the audio
off the event loop's critical path. For tests, feed frames directly with
push_frames() instead of starting a capture.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, List, Optional

from ..config import settings
from .capture import capture_argv
from .vad import UtteranceSegmenter, VadConfig

log = logging.getLogger(__name__)

__all__ = ["CallAudioBridge", "BridgeStats", "capture_argv"]

OnUtterance = Callable[[bytes, float], Awaitable[None]]


@dataclass
class BridgeStats:
    call_id: str
    device_id: Optional[int] = None
    running: bool = False
    started_at: Optional[float] = None
    frames: int = 0
    bytes_captured: int = 0
    utterances: int = 0
    gated_frames: int = 0
    last_rms: float = 0.0
    capture_error: Optional[str] = None
    playback_error: Optional[str] = None

    def to_dict(self) -> dict:
        out = {
            "call_id": self.call_id,
            "device_id": self.device_id,
            "running": self.running,
            "frames": self.frames,
            "bytes_captured": self.bytes_captured,
            "utterances": self.utterances,
            "gated_frames": self.gated_frames,
            "capture_error": self.capture_error,
            "playback_error": self.playback_error,
        }
        if self.started_at:
            out["seconds"] = round(time.time() - self.started_at, 1)
        return out


@dataclass
class CallAudioBridge:
    call_id: str
    device_id: Optional[int] = None
    on_utterance: Optional[OnUtterance] = None
    vad: VadConfig = field(default_factory=VadConfig)
    input_device: Optional[str] = None
    player: Any = None
    # Extra quiet time after playback ends before capture is trusted again.
    echo_tail_seconds: float = 0.25
    # Shared capture hub. When present the bridge subscribes to the one
    # arecord process that also feeds the call recording instead of opening a
    # second reader on a card that only answers one at a time.
    capture: Any = None

    def __post_init__(self) -> None:
        self._segmenter = UtteranceSegmenter(self.vad)
        self._stats = BridgeStats(call_id=self.call_id, device_id=self.device_id)
        self._process: Optional[asyncio.subprocess.Process] = None
        self._reader: Optional[asyncio.Task] = None
        self._gate_until: float = 0.0
        self._lock = asyncio.Lock()
        self._stopped = False
        self._subscriber: Optional[str] = None

    @property
    def stats(self) -> BridgeStats:
        return self._stats

    @property
    def running(self) -> bool:
        return self._stats.running

    def gated(self) -> bool:
        return time.monotonic() < self._gate_until

    def reset_capture(self) -> None:
        """Drop the partial utterance the segmenter is holding.

        Called after we finish speaking into the call. The gate keeps our own
        audio out of the recogniser, but the frames that arrived right at the
        edge are already in the buffer, and emitting them would hand the
        recogniser a fragment of our own voice.
        """
        self._segmenter.reset()

    # -- capture --------------------------------------------------------

    async def start(self) -> bool:
        """Open the capture stream. False means no audio path is available."""
        if self._stats.running:
            return True
        rate = int(self.vad.sample_rate or settings.audio_sample_rate)
        if self.capture is not None:
            name = f"bridge:{self.call_id}"
            ok = await self.capture.subscribe(
                name,
                on_frames=self.push_frames,
                on_stop=self._capture_stopped,
                device=self.input_device,
                rate=rate,
            )
            if not ok:
                self._stats.capture_error = "没有可用的采集设备"
                log.warning("shared capture unavailable for call %s", self.call_id)
                return False
            self._subscriber = name
            self._stopped = False
            self._stats.running = True
            self._stats.started_at = time.time()
            log.info("audio bridge joined the shared capture for call %s", self.call_id)
            return True
        argv = capture_argv(rate, self.input_device)
        if not argv:
            self._stats.capture_error = "没有可用的采集工具（arecord/parecord）"
            log.warning("no capture tool for call %s", self.call_id)
            return False
        try:
            self._process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (FileNotFoundError, OSError) as exc:
            self._stats.capture_error = str(exc)[:300]
            log.warning("capture failed to start for call %s: %s", self.call_id, exc)
            return False
        self._stopped = False
        self._stats.running = True
        self._stats.started_at = time.time()
        self._reader = asyncio.create_task(self._read_loop(), name=f"audio-bridge-{self.call_id}")
        log.info("audio bridge capture started for call %s (%s)", self.call_id, " ".join(argv))
        return True

    def _capture_stopped(self, error: Optional[str]) -> None:
        """The shared capture died or was closed by its last other consumer."""
        self._stats.running = False
        self._subscriber = None
        if error:
            self._stats.capture_error = str(error)[:300]
            log.warning("shared capture stopped for call %s: %s", self.call_id, error)

    async def _read_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        chunk = max(640, self._segmenter.config.frame_bytes * 5)
        try:
            while not self._stopped:
                data = await self._process.stdout.read(chunk)
                if not data:
                    break
                self.push_frames(data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - capture must not kill the call
            self._stats.capture_error = str(exc)[:300]
            log.warning("capture read failed for call %s: %s", self.call_id, exc)
        finally:
            self._stats.running = False

    def push_frames(self, pcm: bytes) -> List[bytes]:
        """Feed captured PCM. Returns the utterances that completed.

        Public so tests and alternate capture paths can drive the segmenter
        without a sound card.
        """
        self._stats.frames += 1
        self._stats.bytes_captured += len(pcm or b"")
        if self.gated():
            self._stats.gated_frames += 1
            return []
        completed = self._segmenter.feed(pcm or b"")
        if completed:
            self._stats.utterances += len(completed)
            for utterance in completed:
                self._dispatch(utterance)
        return completed

    def _dispatch(self, utterance: bytes) -> None:
        if self.on_utterance is None:
            return
        seconds = len(utterance) / float(
            max(1, self.vad.sample_rate * self.vad.sample_width)
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._safe_dispatch(utterance, seconds))

    async def _safe_dispatch(self, utterance: bytes, seconds: float) -> None:
        try:
            await self.on_utterance(utterance, seconds)  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001
            log.warning("utterance handler failed for call %s: %s", self.call_id, exc)

    # -- playback -------------------------------------------------------

    async def gate_for(self, seconds: float) -> None:
        """Suppress capture callbacks for a while, e.g. during playback."""
        self._gate_until = max(self._gate_until, time.monotonic() + max(0.0, seconds))

    async def play_into_call(self, wav_bytes: bytes, *, tag: str = "call_audio") -> bool:
        """Render speech out through the card wired to the module.

        Capture is gated for the length of the audio plus a tail, and the
        segmenter is reset afterwards so a half-heard frame of our own voice is
        not emitted as an utterance.
        """
        if self.player is None:
            self._stats.playback_error = "no audio player bound to this bridge"
            return False
        seconds = _wav_seconds(wav_bytes)
        await self.gate_for(seconds + self.echo_tail_seconds)
        try:
            result = await self.player.play_bytes(wav_bytes, wait=True,
                                                  timeout=max(5.0, seconds + 10.0), tag=tag)
        except AttributeError:
            self._stats.playback_error = "player does not support play_bytes"
            return False
        except Exception as exc:  # noqa: BLE001
            self._stats.playback_error = str(exc)[:300]
            log.warning("playback into call %s failed: %s", self.call_id, exc)
            return False
        ok = bool(getattr(result, "ok", False))
        if not ok:
            self._stats.playback_error = str(getattr(result, "error", "") or "")[:300]
        self._segmenter.reset()
        return ok

    # -- lifecycle ------------------------------------------------------

    async def stop(self) -> None:
        self._stopped = True
        if self.capture is not None and self._subscriber:
            name, self._subscriber = self._subscriber, None
            await self.capture.release(name)
            self._stats.running = False
            tail = self._segmenter.flush()
            if tail:
                self._stats.utterances += 1
                await self._safe_dispatch(tail, len(tail) / float(
                    max(1, self.vad.sample_rate * self.vad.sample_width)))
            return
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
            self._reader = None
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.communicate(), timeout=3)
            except (asyncio.TimeoutError, Exception):
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass
        self._process = None
        self._stats.running = False
        tail = self._segmenter.flush()
        if tail:
            self._stats.utterances += 1
            await self._safe_dispatch(tail, len(tail) / float(
                max(1, self.vad.sample_rate * self.vad.sample_width)))


def _wav_seconds(data: bytes) -> float:
    import io
    import wave

    try:
        with wave.open(io.BytesIO(data)) as handle:
            rate = handle.getframerate() or 16000
            return handle.getnframes() / float(rate)
    except (wave.Error, EOFError):
        return 0.0
