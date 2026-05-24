"""In-memory mock modem for local UI preview without real hardware.

Activated when ``settings.mock_modem`` is true. Implements the same public
surface as ``Modem``: ``run``, ``stop``, ``connected``, ``port``, ``send_sms``.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from .pdu import IncomingSms

log = logging.getLogger(__name__)


class MockModem:
    """A non-functional modem that pretends to be online."""

    def __init__(self, on_sms: Callable[[IncomingSms], Awaitable[None]]):
        self._on_sms = on_sms
        self._stop = asyncio.Event()
        self._connected = True

    async def run(self) -> None:
        log.info("MockModem started (no real serial port). Use it only for UI preview.")
        try:
            await self._stop.wait()
        except asyncio.CancelledError:
            return

    def stop(self) -> None:
        self._stop.set()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def port(self) -> str:
        return "MOCK"

    async def send_sms(self, recipient: str, content: str) -> int:
        # Simulate the round-trip and immediately echo it back as a "received"
        # message so you can watch the websocket push update the inbox.
        await asyncio.sleep(0.2)
        echo = IncomingSms(
            sender=recipient,
            content=f"[mock echo] {content}",
            received_at=int(time.time()),
            raw_pdu="MOCK",
        )
        asyncio.create_task(self._on_sms(echo))
        # 1 segment for short, more for long content (UCS2 67 chars per concat segment)
        return max(1, (len(content.encode("utf-16-be")) + 133) // 134)
