"""In-memory mock modem for local preview and hardware-free integration tests.

The mock implements the same surface as Modem, including voice. It reports
call progress by emitting the very same AT lines a real A7670 would print
(RING, +CLIP, BUSY, NO CARRIER) and runs them through the production parser,
so a test that passes here exercises the real state machine end to end.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, List, Optional

from .config import settings
from .modem import ModemError, VoiceUrcHandler, sanitize_dial_string
from .pdu import IncomingSms
from .voice.parser import classify_voice_line

log = logging.getLogger(__name__)

SmsHandler = Callable[[int, IncomingSms], Awaitable[None]]
StatusHandler = Callable[[int, str, Optional[str]], Awaitable[None]]

# AT+CLCC <stat> values used by the simulator.
STAT_NONE = None
STAT_DIALING = "2"
STAT_ALERTING = "3"
STAT_INCOMING = "4"
STAT_ACTIVE = "0"

# Outbound scenarios: how a dialled call resolves.
SCENARIOS = ("answer", "busy", "no_answer", "no_carrier", "hang_forever")


class MockModem:
    """A simulated A7670 module."""

    def __init__(
        self,
        device_id: int,
        on_sms: SmsHandler,
        on_status: Optional[StatusHandler] = None,
        port: str = "MOCK",
        on_voice_urc: Optional[VoiceUrcHandler] = None,
        sim_number: Optional[str] = None,
    ):
        self.device_id = device_id
        self._on_sms = on_sms
        self._on_status = on_status
        self._on_voice_urc = on_voice_urc
        self._port = port
        self._sim_number = sim_number
        self._stop = asyncio.Event()
        self._connected = True

        # Voice simulation state, mirroring what AT+CLCC would report.
        self._stat: Optional[str] = STAT_NONE
        self._remote: Optional[str] = None
        self._direction = 0  # 0 = MO (outbound), 1 = MT (inbound)
        self._scenario = "answer"
        self._volume = settings.voice_volume_default
        self._muted = False
        self._audio_channel = 0
        self._tts_log: List[dict] = []
        self._dtmf_log: List[str] = []
        self._timers: List[asyncio.Task] = []
        # Timings kept short so integration tests finish quickly.
        self._alert_after = 0.6
        self._connect_after = 1.2

    # -- lifecycle ------------------------------------------------------

    async def run(self) -> None:
        log.info("MockModem started (no real serial port). Use it only for preview and tests.")
        if self._on_status:
            await self._on_status(self.device_id, "online", None)
        try:
            await self._stop.wait()
        except asyncio.CancelledError:
            return
        finally:
            self._connected = False
            for task in self._timers:
                task.cancel()
            if self._on_status:
                await self._on_status(self.device_id, "offline", None)

    def stop(self) -> None:
        self._stop.set()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def port(self) -> str:
        return self._port

    # -- SMS ------------------------------------------------------------

    async def send_sms(self, recipient: str, content: str) -> int:
        await asyncio.sleep(0.05)
        echo = IncomingSms(
            sender=recipient,
            content=f"[mock echo] {content}",
            received_at=int(time.time()),
            raw_pdu="MOCK",
        )
        asyncio.create_task(self._on_sms(self.device_id, echo))
        return max(1, (len(content.encode("utf-16-be")) + 133) // 134)

    async def refresh_identity(self) -> None:
        return None

    async def inject_sms(self, sender: str, content: str) -> int:
        """Deliver an inbound SMS through the same handler a real PDU uses."""
        sms = IncomingSms(
            sender=sender,
            content=content,
            received_at=int(time.time()),
            raw_pdu="MOCK-INJECT",
        )
        await self._on_sms(self.device_id, sms)
        return 1

    # -- voice: helpers -------------------------------------------------

    def _emit_line(self, line: str) -> None:
        """Feed one simulated AT line through the production parser."""
        if not self._on_voice_urc:
            return
        urc = classify_voice_line(line)
        if urc is None:
            return
        try:
            self._on_voice_urc(self.device_id, urc)
        except Exception as e:
            log.warning("mock voice urc dispatch failed: %s", e)

    def _clcc_row(self) -> dict:
        return {
            "id": "1",
            "dir": str(self._direction),
            "stat": self._stat or "6",
            "mode": "0",
            "mpty": "0",
            "number": self._remote or "",
        }

    def _clear_call(self) -> None:
        self._stat = STAT_NONE
        self._remote = None
        self._direction = 0

    # -- voice: test controls -------------------------------------------

    def set_scenario(self, name: str) -> None:
        """Choose how the next outbound call resolves."""
        if name not in SCENARIOS:
            raise ValueError(f"unknown scenario {name}, expected one of {SCENARIOS}")
        self._scenario = name

    def set_timings(self, alert_after: float, connect_after: float) -> None:
        self._alert_after = alert_after
        self._connect_after = connect_after

    async def simulate_incoming(self, number: str, rings: int = 4, interval: float = 3.0) -> None:
        """Simulate an inbound call ringing on this module."""
        if self._stat is not None:
            raise ModemError("mock modem already busy with a call")
        self._direction = 1
        self._remote = number
        self._stat = STAT_INCOMING
        self._emit_line("RING")
        self._emit_line(f'+CLIP: "{number}",129,,0,,0')
        for _ in range(max(0, rings)):
            await asyncio.sleep(interval)
            if self._stat != STAT_INCOMING:
                return
            self._emit_line("RING")
        if self._stat == STAT_INCOMING:
            self._emit_line("NO CARRIER")
            self._clear_call()

    # -- voice: control surface -----------------------------------------

    async def dial(self, number: str) -> List[str]:
        target = sanitize_dial_string(number)
        if self._stat is not None:
            raise ModemError("+CMS ERROR: 511")
        self._direction = 0
        self._remote = target
        self._stat = STAT_DIALING
        task = asyncio.create_task(self._run_dial_scenario(self._scenario))
        self._timers.append(task)
        return ["OK"]

    async def _run_dial_scenario(self, scenario: str) -> None:
        try:
            await asyncio.sleep(self._alert_after)
            if self._stat not in (STAT_DIALING, STAT_ALERTING):
                return
            if scenario == "busy":
                self._emit_line("BUSY")
                self._clear_call()
                return
            if scenario == "no_carrier":
                self._emit_line("NO CARRIER")
                self._clear_call()
                return
            self._stat = STAT_ALERTING
            if scenario == "no_answer":
                await asyncio.sleep(self._connect_after * 3)
                if self._stat == STAT_ALERTING:
                    self._emit_line("NO ANSWER")
                    self._clear_call()
                return
            if scenario == "hang_forever":
                return
            await asyncio.sleep(self._connect_after)
            if self._stat != STAT_ALERTING:
                return
            self._stat = STAT_ACTIVE
            self._emit_line("CONNECT")
        except asyncio.CancelledError:
            return

    async def answer(self) -> List[str]:
        if self._stat != STAT_INCOMING:
            raise ModemError("+CME ERROR: no incoming call")
        self._stat = STAT_ACTIVE
        self._emit_line("CONNECT")
        return ["OK"]

    async def hangup(self) -> None:
        if self._stat is None:
            return
        self._emit_line("NO CARRIER")
        self._clear_call()
        for task in self._timers:
            task.cancel()
        self._timers.clear()

    async def send_dtmf(self, digits: str) -> int:
        sent = 0
        for ch in digits.strip():
            if ch.upper() in "0123456789*#ABCD":
                self._dtmf_log.append(ch.upper())
                sent += 1
        return sent

    async def tts_speak(self, text: str, mode: int = 1) -> None:
        if not text.strip():
            return
        self._tts_log.append({"text": text, "mode": int(mode), "at": int(time.time())})
        await asyncio.sleep(0.02)
        self._emit_line("+CTTS: 1")
        self._emit_line("+CTTS: 0")

    async def tts_stop(self) -> None:
        self._emit_line("+CTTS: 0")

    async def tts_supported(self) -> bool:
        return True

    async def voice_capability(self) -> bool:
        return True

    async def list_calls(self) -> List[dict]:
        if self._stat is None:
            return []
        return [self._clcc_row()]

    async def signal_quality(self):
        return (24, 99)

    async def registration_status(self):
        return {"pin": "READY", "creg": "0,1", "cereg": "0,1"}

    async def set_volume(self, level: int) -> None:
        self._volume = max(0, min(int(level), settings.voice_volume_max))

    async def get_volume(self) -> Optional[int]:
        return self._volume

    async def set_mute(self, muted: bool) -> None:
        self._muted = bool(muted)

    async def set_audio_channel(self, channel: int) -> None:
        self._audio_channel = int(channel)

    async def raw_at(self, command: str, timeout: float = 5.0) -> List[str]:
        return [f"[mock] {command}", "OK"]

    # -- introspection used by tests ------------------------------------

    def debug_state(self) -> dict:
        return {
            "device_id": self.device_id,
            "connected": self._connected,
            "stat": self._stat,
            "remote": self._remote,
            "direction": self._direction,
            "scenario": self._scenario,
            "volume": self._volume,
            "muted": self._muted,
            "audio_channel": self._audio_channel,
            "tts_log": list(self._tts_log),
            "dtmf_log": list(self._dtmf_log),
            "sim_number": self._sim_number,
        }
