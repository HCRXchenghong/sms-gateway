"""Simple in-memory window counter for API rate limiting."""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Dict


class WindowLimiter:
    """Track timestamps for each key and reject if too many in the window."""

    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window = window_seconds
        self._buckets: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds)."""
        now = time.time()
        with self._lock:
            dq = self._buckets.setdefault(key, deque())
            cutoff = now - self.window
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= self.limit:
                retry = max(1, int(dq[0] + self.window - now))
                return False, retry
            dq.append(now)
            return True, 0
