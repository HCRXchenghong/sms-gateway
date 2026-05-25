"""In-process pub/sub for pushing SMS events to connected WebSocket clients."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Set

from fastapi import WebSocket

log = logging.getLogger(__name__)


class Hub:
    """Fan out events to all connected websockets."""

    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def register(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.add(ws)

    async def unregister(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def publish(self, event: str, payload: Dict[str, Any]) -> None:
        msg = json.dumps({"event": event, "data": payload}, ensure_ascii=False)
        async with self._lock:
            stale = []
            for ws in self._clients:
                try:
                    await ws.send_text(msg)
                except Exception as e:
                    log.debug("ws send failed, dropping: %s", e)
                    stale.append(ws)
            for ws in stale:
                self._clients.discard(ws)


hub = Hub()
