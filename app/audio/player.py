"""Prompt playback, capture, and audio device persistence."""
from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from typing import Callable

from .. import repo_admin
from ..config import settings
from . import prompts as prompt_registry
from .capture import CaptureHub, WavWriter
from .devices import (
    AudioDeviceInfo,
    detect_backend,
    list_input_devices,
    list_output_devices,
    set_default_input,
    set_default_output,
)

log = logging.getLogger(__name__)

__all__ = ["AudioPlayer", "PlayResult", "RecordingHandle", "audio_player"]

SETTING_OUTPUT = "audio.output_device"
SETTING_INPUT = "audio.input_device"
SETTING_VOLUME = "audio.prompt_volume"
SETTING_BACKEND = "audio.backend"

_ALSA_PREFIXES = ("plughw:", "hw:", "sysdefault", "default", "plugsurround")


def _looks_like_alsa(device: Optional[str]) -> bool:
    return bool(device) and device.strip().startswith(_ALSA_PREFIXES)


@dataclass
class PlayResult:
    ok: bool
    path: Optional[str] = None
    backend: str = "none"
    device: Optional[str] = None
    argv: List[str] = field(default_factory=list)
    returncode: Optional[int] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "path": self.path,
            "backend": self.backend,
            "device": self.device,
            "returncode": self.returncode,
            "error": self.error,
        }


@dataclass
class RecordingHandle:
    path: Path
    argv: List[str]
    # None when the capture comes from the shared hub instead of a process this
    # player owns.
    process: Optional[asyncio.subprocess.Process] = None
    device: Optional[str] = None
    started_at: float = 0.0
    capture_name: Optional[str] = None
    writer: Optional[WavWriter] = None
    # Set when the shared capture died underneath us, so a REC marker cannot
    # keep claiming a recording that stopped collecting bytes mid-call.
    capture_error: Optional[str] = None


class AudioPlayer:
    """Owns the selected sound cards and runs playback and capture."""

    def __init__(self, prompt_dir: Optional[Path] = None):
        self._prompt_dir = Path(prompt_dir or settings.audio_prompt_dir)
        self._backend: str = "none"
        self._output_device: str = ""
        self._input_device: str = ""
        self._volume: float = float(settings.audio_prompt_volume)
        self._playing: Dict[str, asyncio.subprocess.Process] = {}
        self._recordings: List[RecordingHandle] = []
        self._loaded = False
        # Attached by main.py so capture is shared with live recognition
        # instead of opening a second reader on the same card.
        self._capture: Optional[CaptureHub] = None
        # Runtime gate for capture. main.py swaps in the admin setting so the
        # console toggle takes effect without restarting the gateway.
        self.record_enabled: Callable[[], bool] = lambda: bool(settings.audio_record_enabled)

    def attach_capture(self, hub: Optional[CaptureHub]) -> None:
        self._capture = hub

    @property
    def capture(self) -> Optional[CaptureHub]:
        return self._capture

    # -- setup ----------------------------------------------------------

    async def load(self) -> None:
        """Detect the backend, restore saved device choices, render prompts."""
        self._backend = await detect_backend()
        stored_backend = await asyncio.to_thread(repo_admin.get_setting, SETTING_BACKEND)
        if stored_backend in ("pulse", "alsa") and stored_backend != "auto":
            self._backend = stored_backend
        self._output_device = await asyncio.to_thread(repo_admin.get_setting, SETTING_OUTPUT, "") or ""
        self._input_device = await asyncio.to_thread(repo_admin.get_setting, SETTING_INPUT, "") or ""
        raw_volume = await asyncio.to_thread(repo_admin.get_setting, SETTING_VOLUME)
        if raw_volume:
            try:
                self._volume = max(0.0, min(1.0, float(raw_volume)))
            except ValueError:
                pass
        await asyncio.to_thread(prompt_registry.ensure_prompt_files, self._prompt_dir)
        self._loaded = True
        log.info(
            "audio ready backend=%s out=%s in=%s prompts=%s",
            self._backend,
            self._output_device or "system-default",
            self._input_device or "system-default",
            self._prompt_dir,
        )

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def prompt_dir(self) -> Path:
        return self._prompt_dir

    @property
    def output_device(self) -> str:
        return self._output_device

    @property
    def input_device(self) -> str:
        return self._input_device

    @property
    def volume(self) -> float:
        return self._volume

    async def inventory(self) -> dict:
        outputs = await list_output_devices(self._backend)
        inputs = await list_input_devices(self._backend)
        return {
            "backend": self._backend,
            "volume": self._volume,
            "output_device": self._output_device,
            "input_device": self._input_device,
            "outputs": [d.to_dict() for d in outputs],
            "inputs": [d.to_dict() for d in inputs],
        }

    async def set_output_device(self, device_id: str) -> bool:
        device_id = (device_id or "").strip()
        await asyncio.to_thread(repo_admin.set_setting, SETTING_OUTPUT, device_id)
        self._output_device = device_id
        applied = False
        if device_id and not _looks_like_alsa(device_id):
            applied = await set_default_output(device_id)
        return applied

    async def set_input_device(self, device_id: str) -> bool:
        device_id = (device_id or "").strip()
        await asyncio.to_thread(repo_admin.set_setting, SETTING_INPUT, device_id)
        self._input_device = device_id
        applied = False
        if device_id and not _looks_like_alsa(device_id):
            applied = await set_default_input(device_id)
        return applied

    async def set_volume(self, volume: float) -> float:
        self._volume = max(0.0, min(1.0, float(volume)))
        await asyncio.to_thread(repo_admin.set_setting, SETTING_VOLUME, f"{self._volume:.3f}")
        return self._volume

    # -- playback -------------------------------------------------------

    def _play_argv(self, path: Path, device: Optional[str], volume: float) -> tuple[List[str], str]:
        if _looks_like_alsa(device):
            argv = ["aplay", "-q", "-D", device, str(path)]
            return argv, "alsa"
        if self._backend == "pulse" and shutil.which("paplay"):
            argv = ["paplay", "--volume", str(int(max(0.0, min(1.0, volume)) * 65536))]
            if device:
                argv += ["-d", device]
            argv.append(str(path))
            return argv, "pulse"
        if shutil.which("aplay"):
            argv = ["aplay", "-q"]
            if device:
                argv += ["-D", device]
            argv.append(str(path))
            return argv, "alsa"
        return [], "none"

    async def play_file(
        self,
        path: Path | str,
        *,
        device: Optional[str] = None,
        volume: Optional[float] = None,
        wait: bool = False,
        timeout: float = 60.0,
        tag: Optional[str] = None,
    ) -> PlayResult:
        target = Path(path)
        if not target.exists():
            return PlayResult(ok=False, path=str(target), backend=self._backend, error="file not found")
        use_device = device if device is not None else self._output_device
        use_volume = self._volume if volume is None else volume
        argv, backend = self._play_argv(target, use_device, use_volume)
        if not argv:
            return PlayResult(
                ok=False, path=str(target), backend="none", device=use_device,
                error="no audio player available on this host",
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            return PlayResult(ok=False, path=str(target), backend=backend, argv=argv, error=str(e))
        key = tag or target.stem
        previous = self._playing.pop(key, None)
        if previous and previous.returncode is None:
            previous.terminate()
        self._playing[key] = proc
        if not wait:
            return PlayResult(ok=True, path=str(target), backend=backend, device=use_device, argv=argv)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return PlayResult(
                ok=False, path=str(target), backend=backend, device=use_device, argv=argv,
                error=f"playback timed out after {timeout}s",
            )
        finally:
            self._playing.pop(key, None)
        code = proc.returncode
        detail = (err or b"").decode("utf-8", errors="replace").strip()
        return PlayResult(
            ok=(code == 0), path=str(target), backend=backend, device=use_device,
            argv=argv, returncode=code, error=detail or None,
        )

    async def play_bytes(
        self,
        data: bytes,
        *,
        tag: str = "audio_bytes",
        device: Optional[str] = None,
        volume: Optional[float] = None,
        wait: bool = True,
        timeout: float = 60.0,
    ) -> PlayResult:
        """Play rendered speech that only exists in memory.

        Cloud TTS returns bytes and the callers that need them (translation, the
        call agent) run per call, so the clip is written to a per-tag file and
        played from there. One file per tag keeps two calls from overwriting each
        other mid-playback.
        """
        if not data:
            return PlayResult(ok=False, path="", backend=self._backend, error="empty audio")
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in tag)[:60] or "clip"
        target = Path(self._prompt_dir) / f"_live_{safe}.wav"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(target.write_bytes, data)
        except OSError as e:
            return PlayResult(ok=False, path=str(target), backend=self._backend, error=str(e))
        return await self.play_file(
            target, device=device, volume=volume, wait=wait, timeout=timeout, tag=tag
        )

    async def play_prompt(
        self,
        prompt_id: str,
        *,
        device: Optional[str] = None,
        volume: Optional[float] = None,
        wait: bool = False,
        timeout: float = 60.0,
    ) -> PlayResult:
        spec = prompt_registry.resolve_prompt(prompt_id)
        if spec is None:
            return PlayResult(ok=False, backend=self._backend, error=f"unknown prompt {prompt_id}")
        path = prompt_registry.prompt_path(prompt_id, self._prompt_dir)
        if path is None:
            rendered = await asyncio.to_thread(
                prompt_registry.ensure_prompt_files, self._prompt_dir
            )
            log.debug("rendered %d prompt files", len(rendered))
            path = prompt_registry.prompt_path(prompt_id, self._prompt_dir)
        if path is None:
            return PlayResult(ok=False, backend=self._backend, error=f"no audio file for {prompt_id}")
        return await self.play_file(path, device=device, volume=volume, wait=wait, timeout=timeout, tag=prompt_id)

    async def stop(self, tag: Optional[str] = None) -> None:
        targets = [self._playing.pop(tag)] if tag else list(self._playing.values())
        for proc in targets:
            if proc and proc.returncode is None:
                proc.terminate()
        if tag:
            self._playing.pop(tag, None)

    async def stop_all(self) -> None:
        await self.stop()
        for handle in list(self._recordings):
            await self.stop_recording(handle)
        if self._capture is not None:
            await self._capture.stop_all()

    # -- capture --------------------------------------------------------

    def _record_argv(self, path: Path, device: Optional[str]) -> List[str]:
        rate = str(settings.audio_sample_rate)
        if _looks_like_alsa(device) or self._backend == "alsa":
            argv = ["arecord", "-q", "-f", "S16_LE", "-r", rate, "-c", "1"]
            if device:
                argv += ["-D", device]
            argv.append(str(path))
            return argv
        if shutil.which("parecord"):
            argv = ["parecord", "--format=s16le", f"--rate={rate}", "--channels=1"]
            if device:
                argv += ["-d", device]
            argv.append(str(path))
            return argv
        if shutil.which("pw-record"):
            argv = ["pw-record", "--rate", rate, "--channels", "1", "--format", "S16LE"]
            if device:
                argv += [f"--target={device}"]
            argv.append(str(path))
            return argv
        if shutil.which("arecord"):
            argv = ["arecord", "-q", "-f", "S16_LE", "-r", rate, "-c", "1"]
            if device:
                argv += ["-D", device]
            argv.append(str(path))
            return argv
        return []

    async def start_recording(self, path: Path | str, *, device: Optional[str] = None) -> Optional[RecordingHandle]:
        if not self.record_enabled():
            log.info("recording disabled by configuration")
            return None
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        use_device = device if device is not None else self._input_device
        if self._capture is not None:
            return await self._start_shared_recording(target, use_device)
        argv = self._record_argv(target, use_device)
        if not argv:
            log.warning("no capture tool available")
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
        except FileNotFoundError as e:
            log.warning("failed to start recording: %s", e)
            return None
        import time

        handle = RecordingHandle(path=target, argv=argv, process=proc, device=use_device, started_at=time.time())
        self._recordings.append(handle)
        return handle

    async def _start_shared_recording(self, target: Path, device: Optional[str]) -> Optional[RecordingHandle]:
        """Record through the shared capture so recognition can listen too."""
        import time

        rate = int(settings.audio_sample_rate)
        try:
            writer = WavWriter(target, rate=rate)
        except OSError as e:
            log.warning("failed to open the recording file %s: %s", target, e)
            return None
        name = "record:" + target.stem
        handle = RecordingHandle(
            path=target, argv=[], process=None, device=device,
            started_at=time.time(), capture_name=name, writer=writer,
        )

        def _capture_stopped(error: Optional[str]) -> None:
            # Only an abnormal end reaches this callback; a plain release pops
            # the sink first. So anything here means the card stopped feeding
            # us while the call was still up.
            if not error:
                return
            handle.capture_error = error
            log.warning("capture for %s stopped early: %s", target.name, error)

        ok = await self._capture.subscribe(  # type: ignore[union-attr]
            name,
            on_frames=writer.write,
            on_stop=_capture_stopped,
            device=device,
            rate=rate,
        )
        if not ok:
            writer.close()
            log.warning("no capture device answered for %s", target)
            return None
        self._recordings.append(handle)
        return handle

    async def stop_recording(self, handle: RecordingHandle) -> Path:
        if handle.process is None:
            if handle.capture_name and self._capture is not None:
                await self._capture.release(handle.capture_name)
            if handle.writer is not None:
                handle.writer.close()
            if handle in self._recordings:
                self._recordings.remove(handle)
            return handle.path
        if handle.process.returncode is None:
            handle.process.terminate()
            try:
                await asyncio.wait_for(handle.process.communicate(), timeout=5)
            except asyncio.TimeoutError:
                handle.process.kill()
        if handle in self._recordings:
            self._recordings.remove(handle)
        return handle.path

    def prompt_catalog(self) -> List[dict]:
        out = []
        for spec in prompt_registry.PROMPTS:
            path = prompt_registry.prompt_path(spec.id, self._prompt_dir)
            entry = spec.to_dict()
            entry["has_spoken_version"] = bool(path and path.name.endswith(".tts.wav"))
            entry["path"] = str(path) if path else None
            out.append(entry)
        return out


audio_player = AudioPlayer()
