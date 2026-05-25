"""In-memory mock modem for local UI preview without real hardware."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

from .pdu import IncomingSms

log = logging.getLogger(__name__)

SmsHandler = Callable[[int, IncomingSms], Awaitable[None]]
StatusHandler = Callable[[int, str, Optional[str]], Awaitable[None]]


class MockModem:
    """A non-functional modem that pretends to be online."""

    def __init__(
        self,
        device_id: int,
        on_sms: SmsHandler,
        on_status: Optional[StatusHandler] = None,
        port: str = "MOCK",
    ):
        self.device_id = device_id
        self._on_sms = on_sms
        self._on_status = on_status
        self._port = port
        self._stop = asyncio.Event()
        self._connected = True

    async def run(self) -> None:
        log.info("MockModem started (no real serial port). Use it only for UI preview.")
        if self._on_status:
            await self._on_status(self.device_id, "online", None)
        try:
            await self._stop.wait()
        except asyncio.CancelledError:
            return
        finally:
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

    async def send_sms(self, recipient: str, content: str) -> int:
        await asyncio.sleep(0.2)
        echo = IncomingSms(
            sender=recipient,
            content=f"[mock echo] {content}",
            received_at=int(time.time()),
            raw_pdu="MOCK",
        )
        asyncio.create_task(self._on_sms(self.device_id, echo))
        return max(1, (len(content.encode("utf-16-be")) + 133) // 134)
