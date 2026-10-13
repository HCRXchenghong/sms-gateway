"""Multi-device modem discovery and lifecycle management."""
from __future__ import annotations

import asyncio
import glob
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional

from serial.tools import list_ports

from . import repo
from .config import settings
from .modem import DeviceIdentity, Modem, ModemError, VoiceUrcHandler
from .modem_mock import MockModem
from .pdu import IncomingSms

log = logging.getLogger(__name__)

SmsHandler = Callable[[int, IncomingSms], Awaitable[None]]


@dataclass(frozen=True)
class PortCandidate:
    port: str
    stable_key: str
    usb_path: str


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return None


def _tty_sort_key(port: str) -> tuple[str, int]:
    name = Path(port).name
    digits = "".join(ch for ch in name if ch.isdigit())
    return (name.rstrip(digits), int(digits or 0))


def _port_preference(cand: PortCandidate) -> tuple[int, str]:
    text = f"{cand.port} {cand.usb_path}".lower()
    if " at " in f" {text} " or "at port" in text:
        return (0, cand.port)
    if "modem" in text:
        return (1, cand.port)
    if "diag" in text or "diagnostic" in text:
        return (3, cand.port)
    return (2, cand.port)


def _windows_com_candidates() -> list[PortCandidate]:
    candidates: list[PortCandidate] = []
    for info in list_ports.comports():
        if not info.device:
            continue
        vendor = f"{info.vid:04X}" if info.vid is not None else "unknown"
        product = f"{info.pid:04X}" if info.pid is not None else "unknown"
        serial = info.serial_number or ""
        location = info.location or ""
        stable = f"usb:{vendor}:{product}:{serial or location or info.hwid}"
        label = " | ".join(
            part
            for part in [info.description, info.manufacturer, info.product, info.interface, info.hwid]
            if part
        )
        candidates.append(PortCandidate(port=info.device, stable_key=stable, usb_path=label))
    return candidates


def _tty_usb_info(port: str) -> PortCandidate:
    """Build a stable grouping key for a ttyUSB port.

    A single A7670G exposes multiple ttyUSB interfaces. We climb sysfs to the
    parent USB device and group by that physical device, then the manager picks
    one AT-responsive port from the group.
    """
    name = Path(port).name
    sys_tty = Path("/sys/class/tty") / name / "device"
    try:
        real = sys_tty.resolve()
    except OSError:
        return PortCandidate(port=port, stable_key=f"port:{port}", usb_path="")

    usb_device = real
    while usb_device != usb_device.parent:
        if (usb_device / "idVendor").exists() and (usb_device / "idProduct").exists():
            break
        usb_device = usb_device.parent

    vendor = _read_text(usb_device / "idVendor") or "unknown"
    product = _read_text(usb_device / "idProduct") or "unknown"
    serial = _read_text(usb_device / "serial")
    devpath = _read_text(usb_device / "devpath") or usb_device.name
    stable = f"usb:{vendor}:{product}:{serial or devpath}"
    return PortCandidate(port=port, stable_key=stable, usb_path=str(usb_device))


async def discover_at_ports(skip_stable_keys: Optional[set[str]] = None) -> list[PortCandidate]:
    skip_stable_keys = skip_stable_keys or set()
    if settings.modem_at_port:
        port = str(settings.modem_at_port)
        if await Modem.probe(port):
            return [PortCandidate(port=port, stable_key=f"configured:{port}", usb_path="configured")]
        return []

    by_device: Dict[str, list[PortCandidate]] = {}
    if os.name == "nt":
        for info in _windows_com_candidates():
            by_device.setdefault(info.stable_key, []).append(info)
    else:
        ports = sorted(glob.glob("/dev/ttyUSB*"), key=_tty_sort_key)
        for port in ports:
            info = _tty_usb_info(port)
            by_device.setdefault(info.stable_key, []).append(info)

    selected: list[PortCandidate] = []
    for stable_key, candidates in by_device.items():
        if stable_key in skip_stable_keys:
            continue
        ordered = (
            sorted(candidates, key=_port_preference)
            if os.name == "nt"
            else sorted(candidates, key=lambda x: _tty_sort_key(x.port), reverse=True)
        )
        for cand in ordered:
            if await Modem.probe(cand.port):
                log.info("AT port detected for %s: %s", stable_key, cand.port)
                selected.append(cand)
                break
    return selected


class ModemManager:
    """Owns all live modem workers and prevents ttyUSB conflicts."""

    def __init__(self, on_sms: SmsHandler, on_voice_urc: Optional[VoiceUrcHandler] = None):
        self._on_sms = on_sms
        self._on_voice_urc = on_voice_urc
        self._stop = asyncio.Event()
        self._modems: Dict[int, Modem | MockModem] = {}
        self._tasks: Dict[int, asyncio.Task] = {}
        self._stable_to_device: Dict[str, int] = {}
        self._lock = asyncio.Lock()

    def set_voice_handler(self, handler: Optional[VoiceUrcHandler]) -> None:
        """Attach the voice URC handler after construction.

        CallManager needs ModemManager to look modems up, and ModemManager
        needs CallManager to receive URCs. Wiring it in two steps keeps both
        constructors simple and avoids an import cycle.
        """
        self._on_voice_urc = handler

    def get_modem(self, device_id: int) -> Optional[Modem | MockModem]:
        return self._modems.get(int(device_id))

    def online_device_ids(self) -> list[int]:
        return [int(d) for d, m in self._modems.items() if m.connected]

    async def pick_call_device(self, device_id: Optional[int] = None) -> int:
        """Resolve which online module should place or hold a call."""
        async with self._lock:
            if device_id is not None:
                modem = self._modems.get(int(device_id))
                if not modem or not modem.connected:
                    raise ModemError(f"device {device_id} is not online")
                return int(device_id)
            for row in await asyncio.to_thread(repo.list_devices):
                candidate = int(row["id"])
                modem = self._modems.get(candidate)
                if modem and modem.connected:
                    return candidate
        raise ModemError("no online device available")

    @property
    def connected(self) -> bool:
        return any(m.connected for m in self._modems.values())

    @property
    def port(self) -> str:
        ports = [m.port for m in self._modems.values() if m.connected]
        return ",".join(str(p) for p in ports if p) or ""

    async def run(self) -> None:
        if settings.mock_modem:
            await self._run_mock()
            return

        while not self._stop.is_set():
            try:
                await self.scan_once()
            except Exception as e:
                log.warning("modem scan failed: %s", e)
            await self._reap_done()
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=max(5, settings.modem_probe_interval_seconds),
                )
            except asyncio.TimeoutError:
                pass

    async def _run_mock(self) -> None:
        count = max(1, settings.mock_modem_count)
        for idx in range(count):
            stable_key = f"mock:{idx + 1:03d}"
            row = await asyncio.to_thread(
                repo.upsert_device,
                stable_key,
                f"MOCK{idx + 1}",
                "mock",
                "online",
            )
            await asyncio.to_thread(
                repo.update_device_identity,
                row["id"],
                sim_number_auto=f"+10000000{idx + 1:03d}",
                iccid=f"MOCKICCID{idx + 1:03d}",
                imsi=f"MOCKIMSI{idx + 1:03d}",
                imei=f"MOCKIMEI{idx + 1:03d}",
                model="A7670G Mock",
                manufacturer="SIMCom",
            )
            modem = MockModem(
                row["id"],
                self._on_sms,
                self._on_status,
                port=f"MOCK{idx + 1}",
                on_voice_urc=self._on_voice_urc,
                sim_number=f"+10000000{idx + 1:03d}",
            )
            task = asyncio.create_task(modem.run(), name=f"mock-modem-{idx + 1}")
            self._modems[row["id"]] = modem
            self._tasks[row["id"]] = task
        await self._stop.wait()

    async def scan_once(self) -> None:
        async with self._lock:
            for modem in list(self._modems.values()):
                if modem.connected and hasattr(modem, "refresh_identity"):
                    try:
                        await modem.refresh_identity()
                    except ModemError as e:
                        log.warning("identity refresh failed for device %s: %s", modem.device_id, e)
            connected_stable_keys = {
                stable_key
                for stable_key, device_id in self._stable_to_device.items()
                if device_id in self._modems and self._modems[device_id].connected
            }
            candidates = await discover_at_ports(connected_stable_keys)
            for cand in candidates:
                existing_id = self._stable_to_device.get(cand.stable_key)
                if existing_id and existing_id in self._modems and self._modems[existing_id].connected:
                    continue
                row = await asyncio.to_thread(
                    repo.upsert_device,
                    cand.stable_key,
                    cand.port,
                    cand.usb_path,
                    "connecting",
                )
                device_id = int(row["id"])
                if device_id in self._tasks and not self._tasks[device_id].done():
                    continue
                modem = Modem(
                    device_id=device_id,
                    at_port=cand.port,
                    on_sms=self._on_sms,
                    on_identity=self._on_identity,
                    on_status=self._on_status,
                    on_voice_urc=self._on_voice_urc,
                )
                task = asyncio.create_task(modem.run(), name=f"modem-{device_id}")
                self._modems[device_id] = modem
                self._tasks[device_id] = task
                self._stable_to_device[cand.stable_key] = device_id

    async def _reap_done(self) -> None:
        async with self._lock:
            done = [device_id for device_id, task in self._tasks.items() if task.done()]
            for device_id in done:
                self._tasks.pop(device_id, None)
                self._modems.pop(device_id, None)
                await asyncio.to_thread(repo.update_device_status, device_id, "offline", "worker stopped")

    async def _on_identity(self, device_id: int, identity: DeviceIdentity) -> None:
        await asyncio.to_thread(
            repo.update_device_identity,
            device_id,
            sim_number_auto=identity.sim_number_auto,
            iccid=identity.iccid,
            imsi=identity.imsi,
            imei=identity.imei,
            model=identity.model,
            manufacturer=identity.manufacturer,
        )

    async def _on_status(self, device_id: int, status: str, error: Optional[str]) -> None:
        modem = self._modems.get(device_id)
        await asyncio.to_thread(
            repo.update_device_status,
            device_id,
            status,
            error,
            modem.port if modem else None,
        )

    async def send_sms(self, recipient: str, content: str, device_id: Optional[int] = None) -> tuple[int, int]:
        """Send SMS with a selected or automatically chosen online device.

        Returns (device_id, segment_count).
        """
        async with self._lock:
            selected_id = device_id
            if selected_id is None:
                for row in await asyncio.to_thread(repo.list_devices):
                    candidate_id = int(row["id"])
                    modem = self._modems.get(candidate_id)
                    if modem and modem.connected:
                        selected_id = candidate_id
                        break
            if selected_id is None:
                raise ModemError("no online device available")
            modem = self._modems.get(selected_id)
            if not modem or not modem.connected:
                raise ModemError(f"device {selected_id} is not online")
        segments = await modem.send_sms(recipient, content)
        return selected_id, segments

    def stop(self) -> None:
        self._stop.set()
        for modem in self._modems.values():
            modem.stop()
        for task in self._tasks.values():
            task.cancel()
