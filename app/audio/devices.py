"""Sound card discovery and selection.

Two backends are supported. PulseAudio/PipeWire is preferred when a server is
reachable because it allows a global default to be set at runtime; ALSA is the
fallback and the selected card is passed per playback with -D.

Subprocesses are forced to LC_ALL=C so parsing does not depend on the host
locale. Cards are matched on their numeric ids, which never get translated.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

__all__ = [
    "AudioDeviceInfo",
    "detect_backend",
    "list_output_devices",
    "list_input_devices",
    "get_default_output",
    "get_default_input",
    "set_default_output",
    "set_default_input",
]

_ENV = {"LC_ALL": "C", "LANG": "C", "PATH": "/usr/local/bin:/usr/bin:/bin"}

_ALSA_CARD = re.compile(
    r"card\s+(\d+):\s+(\S+)\s*\[(.*?)\],\s*device\s+(\d+):\s*(.*?)\s*\[(.*?)\]"
)
_PULSE_SINK_BLOCK = re.compile(r"^(Sink|Source)\s+#(\d+)", re.M)


@dataclass
class AudioDeviceInfo:
    backend: str          # pulse | alsa
    kind: str             # output | input
    id: str               # value handed to the player (-d for pulse, -D for alsa)
    name: str             # human readable label
    description: str = ""
    state: str = ""
    is_default: bool = False
    is_monitor: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


async def _run(argv: List[str], timeout: float = 6.0) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_ENV,
        )
    except FileNotFoundError:
        return 127, ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, ""
    return proc.returncode or 0, out.decode("utf-8", errors="replace")


def _have(binary: str) -> bool:
    return shutil.which(binary) is not None


async def detect_backend() -> str:
    """Return the audio backend actually usable on this host."""
    if _have("pactl"):
        code, out = await _run(["pactl", "info"])
        if code == 0 and out.strip():
            return "pulse"
    if _have("aplay") or _have("arecord"):
        return "alsa"
    return "none"


async def _pulse_descriptions(kind: str) -> Dict[str, str]:
    """Map pulse object names to their localized Description field."""
    code, out = await _run(["pactl", "list", "sinks" if kind == "output" else "sources"])
    if code != 0:
        return {}
    descriptions: Dict[str, str] = {}
    current: Optional[str] = None
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("Name:"):
            current = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Description:") and current:
            descriptions[current] = stripped.split(":", 1)[1].strip()
    return descriptions


async def _pulse_list(kind: str) -> List[AudioDeviceInfo]:
    target = "sinks" if kind == "output" else "sources"
    code, out = await _run(["pactl", "list", "short", target])
    if code != 0:
        return []
    default = await get_default_output() if kind == "output" else await get_default_input()
    descriptions = await _pulse_descriptions(kind)
    devices: List[AudioDeviceInfo] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name = parts[1].strip()
        if not name:
            continue
        devices.append(
            AudioDeviceInfo(
                backend="pulse",
                kind=kind,
                id=name,
                name=descriptions.get(name) or name,
                description=parts[3].strip() if len(parts) > 3 else "",
                state=parts[4].strip() if len(parts) > 4 else "",
                is_default=(name == default),
                is_monitor=name.endswith(".monitor"),
            )
        )
    return devices


async def _alsa_list(kind: str) -> List[AudioDeviceInfo]:
    binary = "aplay" if kind == "output" else "arecord"
    code, out = await _run([binary, "-l"])
    if code != 0:
        return []
    devices: List[AudioDeviceInfo] = []
    for match in _ALSA_CARD.finditer(out):
        card, card_name, card_label, device, dev_name, dev_label = match.groups()
        dev_id = f"plughw:{card},{device}"
        devices.append(
            AudioDeviceInfo(
                backend="alsa",
                kind=kind,
                id=dev_id,
                name=f"{card_label or card_name} · {dev_label or dev_name}",
                description=f"card {card} device {device}",
                state="available",
                is_default=(card == "0" and device == "0"),
                is_monitor=False,
            )
        )
    return devices


async def list_output_devices(backend: Optional[str] = None) -> List[AudioDeviceInfo]:
    backend = backend or await detect_backend()
    if backend == "pulse":
        devices = await _pulse_list("output")
        if devices:
            return devices
    if backend == "none":
        return []
    return await _alsa_list("output")


async def list_input_devices(backend: Optional[str] = None) -> List[AudioDeviceInfo]:
    backend = backend or await detect_backend()
    if backend == "pulse":
        devices = [d for d in await _pulse_list("input") if not d.is_monitor]
        if devices:
            return devices
    if backend == "none":
        return []
    return await _alsa_list("input")


async def get_default_output() -> Optional[str]:
    if not _have("pactl"):
        return None
    code, out = await _run(["pactl", "get-default-sink"])
    return out.strip() if code == 0 and out.strip() else None


async def get_default_input() -> Optional[str]:
    if not _have("pactl"):
        return None
    code, out = await _run(["pactl", "get-default-source"])
    return out.strip() if code == 0 and out.strip() else None


async def set_default_output(device_id: str) -> bool:
    """Point the system default sink at a device. ALSA-only hosts persist
    the choice in the database and pass it to aplay with -D instead."""
    if not device_id:
        return False
    if _have("pactl"):
        code, _ = await _run(["pactl", "set-default-sink", device_id])
        if code == 0:
            return True
        log.warning("pactl set-default-sink %s failed with %s", device_id, code)
    return False


async def set_default_input(device_id: str) -> bool:
    if not device_id:
        return False
    if _have("pactl"):
        code, _ = await _run(["pactl", "set-default-source", device_id])
        if code == 0:
            return True
        log.warning("pactl set-default-source %s failed with %s", device_id, code)
    return False
