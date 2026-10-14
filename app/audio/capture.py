"""One capture process per sound card, shared by every consumer.

Recording a call and recognising what the far end says both want the same
bytes from the same card. A plain ALSA device answers only one reader at a
time, so opening a second arecord for the recogniser makes the recording fail
with "device busy" and the call loses its recording while the operator still
sees a REC marker. One process with many subscribers keeps both features
working together, and keeps the number of open capture devices predictable no
matter how many calls are live.

Subscribers register a plain callback that receives raw mono S16LE PCM. The
callback runs on the event loop, so it must not block; both consumers here
(buffering into a WAV file, feeding the utterance segmenter) just copy bytes.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ..config import settings

log = logging.getLogger(__name__)

__all__ = ["capture_argv", "CaptureHub", "capture_hub", "WavWriter", "pcm_to_wav"]

FrameSink = Callable[[bytes], None]
StopSink = Callable[[Optional[str]], None]


def capture_argv(rate: int, device: Optional[str], backend: str = "alsa") -> List[str]:
    """The command that streams raw mono S16LE PCM to stdout."""
    if shutil.which("arecord"):
        argv = ["arecord", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(rate), "-c", "1"]
        if device:
            argv += ["-D", device]
        argv.append("/dev/stdout")
        return argv
    if shutil.which("parecord"):
        argv = ["parecord", "--format=s16le", "--rate=" + str(rate), "--channels=1"]
        if device:
            argv += ["-d", device]
        argv.append("/dev/stdout")
        return argv
    if shutil.which("pw-record"):
        argv = ["pw-record", "--rate", str(rate), "--channels", "1", "--format", "S16LE"]
        if device:
            argv += ["--target=" + str(device)]
        argv.append("-")
        return argv
    return []


def pcm_to_wav(pcm: bytes, rate: int = 16000, *, channels: int = 1, width: int = 2) -> bytes:
    """Wrap captured PCM in a WAV header.

    The capture stream is headerless raw audio, and every recognition endpoint
    we talk to wants a real audio file. Wrapping per utterance keeps the bytes
    we recognised identical to the bytes we recorded.
    """
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(pcm or b"")
    return buffer.getvalue()


class WavWriter:
    """Turns a raw PCM stream into a playable WAV file.

    The header is rewritten on close so the length is right even though the
    stream had no known end when it started.
    """

    def __init__(self, path, *, rate: int = 16000, channels: int = 1, width: int = 2) -> None:
        import wave

        self.path = str(path)
        self._wave = wave.open(self.path, "wb")
        self._wave.setnchannels(channels)
        self._wave.setsampwidth(width)
        self._wave.setframerate(rate)
        self.frames = 0
        self.closed = False

    def write(self, data: bytes) -> None:
        if self.closed or not data:
            return
        self._wave.writeframes(data)
        self.frames += len(data)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self._wave.close()
        except Exception as exc:  # noqa: BLE001 - closing must not raise
            log.warning("closing the capture wav failed: %s", exc)


@dataclass
class _Stream:
    key: str
    argv: List[str]
    rate: int
    device: Optional[str]
    sinks: Dict[str, FrameSink] = field(default_factory=dict)
    stops: Dict[str, StopSink] = field(default_factory=dict)
    process: Optional[asyncio.subprocess.Process] = None
    reader: Optional[asyncio.Task] = None
    bytes_read: int = 0
    chunks: int = 0
    error: Optional[str] = None
    started_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "device": self.device,
            "rate": self.rate,
            "argv": self.argv,
            "running": self.process is not None and self.process.returncode is None,
            "subscribers": sorted(self.sinks),
            "bytes_read": self.bytes_read,
            "chunks": self.chunks,
            "seconds": round(time.time() - self.started_at, 1),
            "error": self.error,
        }


class CaptureHub:
    """Reference-counted capture streams, keyed by device and sample rate."""

    def __init__(self, *, argv_builder: Optional[Callable[..., List[str]]] = None) -> None:
        self._argv_builder = argv_builder or capture_argv
        self._streams: Dict[str, _Stream] = {}
        self._owner: Dict[str, str] = {}
        self._lock = asyncio.Lock()

    def default_device(self) -> Optional[str]:
        device = (settings.audio_input_device or "").strip()
        return device or None

    def default_rate(self) -> int:
        return int(settings.audio_sample_rate or 16000)

    async def subscribe(
        self,
        name: str,
        *,
        on_frames: FrameSink,
        on_stop: Optional[StopSink] = None,
        device: Optional[str] = None,
        rate: Optional[int] = None,
    ) -> bool:
        """Attach to a capture stream, starting one if nothing is capturing yet."""
        use_device = device if device is not None else self.default_device()
        use_rate = int(rate or self.default_rate())
        key = (use_device or "default") + "@" + str(use_rate)
        async with self._lock:
            await self._release_locked(name)
            stream = self._streams.get(key)
            if stream is None:
                argv = self._argv_builder(use_rate, use_device)
                if not argv:
                    log.warning("no capture tool available for %s", key)
                    return False
                stream = _Stream(key=key, argv=list(argv), rate=use_rate, device=use_device)
                try:
                    stream.process = await asyncio.create_subprocess_exec(
                        *argv,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                except (FileNotFoundError, OSError) as exc:
                    stream.error = str(exc)[:300]
                    log.warning("capture failed to start for %s: %s", key, exc)
                    return False
                self._streams[key] = stream
                stream.reader = asyncio.create_task(self._read_loop(stream), name="capture-" + key)
                log.info("capture started for %s: %s", key, " ".join(argv))
            stream.sinks[name] = on_frames
            if on_stop is not None:
                stream.stops[name] = on_stop
            self._owner[name] = key
            return True

    async def release(self, name: str) -> None:
        async with self._lock:
            await self._release_locked(name)

    async def _release_locked(self, name: str) -> None:
        key = self._owner.pop(name, None)
        if key is None:
            return
        stream = self._streams.get(key)
        if stream is None:
            return
        stream.sinks.pop(name, None)
        stream.stops.pop(name, None)
        if stream.sinks:
            return
        await self._stop_stream_locked(stream)

    async def _stop_stream_locked(self, stream: _Stream) -> None:
        self._streams.pop(stream.key, None)
        reader = stream.reader
        stream.reader = None
        # Never cancel and await ourselves: this runs on the reader task when
        # the capture device disappears on its own.
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            try:
                await reader
            except (asyncio.CancelledError, Exception):
                pass
        proc = stream.process
        stream.process = None
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.communicate(), timeout=3)
            except (asyncio.TimeoutError, Exception):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        log.info("capture stopped for %s (%d bytes)", stream.key, stream.bytes_read)

    async def _read_loop(self, stream: _Stream) -> None:
        assert stream.process is not None and stream.process.stdout is not None
        chunk = max(1024, stream.rate * 2 // 10)  # ~100 ms of mono S16LE
        try:
            while True:
                data = await stream.process.stdout.read(chunk)
                if not data:
                    break
                stream.bytes_read += len(data)
                stream.chunks += 1
                for sink in list(stream.sinks.values()):
                    try:
                        sink(data)
                    except Exception as exc:  # noqa: BLE001 - one bad consumer
                        log.warning("capture subscriber failed: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            stream.error = str(exc)[:300]
            log.warning("capture read failed for %s: %s", stream.key, exc)
        finally:
            # Two ways to get here. Somebody released the last subscriber, in
            # which case the stream is already unregistered and that caller
            # holds the lock; taking it again would deadlock, because it is
            # waiting for this task to finish. Otherwise the capture ended by
            # itself, the device went away or arecord exited, and the cleanup
            # is ours to do.
            died_alone = self._streams.get(stream.key) is stream
            error = stream.error
            if died_alone and error is None and stream.bytes_read == 0:
                error = "capture produced no audio"
            stops = list(stream.stops.values())
            if died_alone:
                async with self._lock:
                    for name in [n for n, k in self._owner.items() if k == stream.key]:
                        stream.sinks.pop(name, None)
                        stream.stops.pop(name, None)
                        self._owner.pop(name, None)
                    if self._streams.get(stream.key) is stream:
                        await self._stop_stream_locked(stream)
            for stop in stops:
                try:
                    stop(error)
                except Exception:  # noqa: BLE001
                    pass

    def stats(self) -> List[dict]:
        return [s.to_dict() for s in self._streams.values()]

    def subscribers(self) -> List[str]:
        return sorted(self._owner)

    async def stop_all(self) -> None:
        async with self._lock:
            for stream in list(self._streams.values()):
                await self._stop_stream_locked(stream)
            self._owner.clear()


capture_hub = CaptureHub()
