"""Async AT-command client for SIMCom A7670G.

Responsibilities
----------------
- Auto-discover the AT serial port among /dev/ttyUSB* by sending plain ``AT``.
- Configure modem for PDU mode + new-message URC reporting.
- Watch for ``+CMTI`` URC, fetch via ``AT+CMGR``, decode (handling concatenated
  long SMS), persist via callback.
- Serialize sending: only one ``AT+CMGS`` runs at a time; URC handling pauses
  while we expect ``>`` prompt.
- Auto-reconnect when the device disappears (USB unplugged) and reappears.
"""
from __future__ import annotations

import asyncio
import glob
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

import serial_asyncio

from .config import settings
from .pdu import IncomingSms, OutgoingSegment, decode_deliver, encode_submit_ucs2

log = logging.getLogger(__name__)


SmsHandler = Callable[[IncomingSms], Awaitable[None]]


@dataclass
class _PendingConcat:
    total: int
    parts: Dict[int, IncomingSms] = field(default_factory=dict)
    first_at: float = field(default_factory=time.time)


class ModemError(RuntimeError):
    pass


class Modem:
    """Owns the serial connection and provides a simple ``send_sms`` API."""

    def __init__(self, on_sms: SmsHandler):
        self._on_sms = on_sms
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._port: Optional[str] = None
        self._lock = asyncio.Lock()  # serialize AT commands
        self._reading_task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._connected = asyncio.Event()
        self._urc_queue: asyncio.Queue[str] = asyncio.Queue()
        self._concat_buffer: Dict[Tuple[str, int], _PendingConcat] = {}
        self._submit_ref = 0

    # ------------------------------------------------------------------ public

    async def run(self) -> None:
        """Run forever: connect, configure, read; reconnect on failure."""
        backoff = 2
        while not self._stop.is_set():
            try:
                port = await self._find_at_port()
                self._port = port
                log.info("opening AT port %s", port)
                self._reader, self._writer = await serial_asyncio.open_serial_connection(
                    url=port, baudrate=settings.modem_baudrate
                )
                self._connected.set()
                self._reading_task = asyncio.create_task(self._read_loop(), name="modem-read")
                await self._configure()
                # Drain any messages that arrived while we were offline
                await self._scan_inbox()
                backoff = 2
                # Stay until reader dies
                await self._reading_task
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("modem connection error: %s", e)
            finally:
                self._connected.clear()
                if self._writer:
                    try:
                        self._writer.close()
                    except Exception:
                        pass
                self._reader = self._writer = None
                self._reading_task = None
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    def stop(self) -> None:
        self._stop.set()
        if self._reading_task:
            self._reading_task.cancel()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def port(self) -> Optional[str]:
        return self._port

    async def send_sms(self, recipient: str, content: str) -> int:
        """Send SMS, returning the number of segments transmitted."""
        if not self.connected:
            raise ModemError("modem not connected")
        self._submit_ref = (self._submit_ref + 1) & 0xFF
        segments = encode_submit_ucs2(recipient, content, reference=self._submit_ref)
        async with self._lock:
            for seg in segments:
                await self._send_one_segment(seg)
        return len(segments)

    # ---------------------------------------------------------------- internals

    async def _find_at_port(self) -> str:
        """Return a configured port if specified, else probe ttyUSB*."""
        if settings.modem_at_port:
            return str(settings.modem_at_port)
        candidates = sorted(glob.glob("/dev/ttyUSB*"))
        if not candidates:
            raise ModemError("no /dev/ttyUSB* devices present")
        # Try last-first; A7670G typically exposes AT on ttyUSB2 or higher.
        for port in reversed(candidates):
            if await self._probe(port):
                log.info("AT port detected: %s", port)
                return port
        raise ModemError(f"no AT-responsive port among: {candidates}")

    async def _probe(self, port: str) -> bool:
        try:
            r, w = await serial_asyncio.open_serial_connection(
                url=port, baudrate=settings.modem_baudrate
            )
        except Exception as e:
            log.debug("probe %s open failed: %s", port, e)
            return False
        try:
            w.write(b"AT\r\n")
            await w.drain()
            try:
                buf = await asyncio.wait_for(r.readuntil(b"OK\r\n"), timeout=2.0)
                return b"OK" in buf
            except asyncio.TimeoutError:
                return False
        finally:
            w.close()

    async def _configure(self) -> None:
        await self._cmd("ATE0", timeout=2)            # echo off
        await self._cmd("AT+CMEE=2", timeout=2)       # verbose errors
        await self._cmd("AT+CMGF=0", timeout=2)       # PDU mode
        await self._cmd("AT+CPMS=\"SM\",\"SM\",\"SM\"", timeout=3)
        # Buffer URCs to TE: deliver "+CMTI" indications, forward CBM/STATUS to ME
        await self._cmd("AT+CNMI=2,1,0,0,0", timeout=3)
        log.info("modem configured (PDU mode, +CMTI URC enabled)")

    async def _cmd(self, cmd: str, timeout: float = 10.0) -> List[str]:
        """Send an AT command; return list of response lines up to OK/ERROR."""
        async with self._lock:
            return await self._cmd_unlocked(cmd, timeout)

    async def _cmd_unlocked(self, cmd: str, timeout: float = 10.0) -> List[str]:
        if not self._writer or not self._reader:
            raise ModemError("modem not open")
        log.debug(">> %s", cmd)
        self._writer.write((cmd + "\r\n").encode("ascii"))
        await self._writer.drain()
        return await self._read_response(timeout)

    async def _read_response(self, timeout: float) -> List[str]:
        """Collect response lines until OK / ERROR / +CMS ERROR."""
        lines: List[str] = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ModemError("AT timeout")
            try:
                raw = await asyncio.wait_for(self._reader.readline(), timeout=remaining)  # type: ignore[union-attr]
            except asyncio.TimeoutError:
                raise ModemError("AT timeout")
            if not raw:
                raise ModemError("modem closed")
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                continue
            log.debug("<< %s", line)
            if line.startswith("+CMTI:"):
                # URC: queue and continue waiting for command result
                await self._urc_queue.put(line)
                continue
            lines.append(line)
            if line == "OK":
                return lines
            if line == "ERROR" or line.startswith("+CMS ERROR") or line.startswith("+CME ERROR"):
                raise ModemError(line)

    async def _read_loop(self) -> None:
        """Background reader that dispatches URCs when no command is in flight."""
        try:
            while True:
                raw = await self._reader.readline()  # type: ignore[union-attr]
                if not raw:
                    raise ModemError("modem stream closed")
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    continue
                log.debug("[bg] << %s", line)
                if line.startswith("+CMTI:"):
                    await self._urc_queue.put(line)
                    asyncio.create_task(self._handle_cmti_async())
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.info("read_loop ended: %s", e)

    async def _handle_cmti_async(self) -> None:
        try:
            line = await self._urc_queue.get()
        except asyncio.CancelledError:
            return
        # +CMTI: "SM",<index>
        try:
            _, rest = line.split(":", 1)
            _, idx_s = rest.rsplit(",", 1)
            idx = int(idx_s.strip())
        except Exception:
            log.warning("bad +CMTI line: %r", line)
            return
        await self._fetch_index(idx)

    async def _scan_inbox(self) -> None:
        """List and consume messages already on SIM."""
        try:
            lines = await self._cmd("AT+CMGL=4", timeout=15)
        except ModemError as e:
            log.warning("CMGL failed: %s", e)
            return
        # +CMGL: <index>,<stat>,...,<length>
        # <pdu_hex>
        i = 0
        indices: List[int] = []
        while i < len(lines):
            line = lines[i]
            if line.startswith("+CMGL:"):
                try:
                    idx = int(line.split(":", 1)[1].split(",")[0].strip())
                    indices.append(idx)
                    if i + 1 < len(lines):
                        await self._ingest_pdu(lines[i + 1], idx)
                    i += 2
                    continue
                except Exception:
                    pass
            i += 1

    async def _fetch_index(self, idx: int) -> None:
        try:
            lines = await self._cmd(f"AT+CMGR={idx}", timeout=10)
        except ModemError as e:
            log.warning("CMGR %d failed: %s", idx, e)
            return
        for j, line in enumerate(lines):
            if line.startswith("+CMGR:") and j + 1 < len(lines):
                await self._ingest_pdu(lines[j + 1], idx)
                break
        # Always free the slot to avoid SIM-full
        try:
            await self._cmd(f"AT+CMGD={idx}", timeout=5)
        except ModemError as e:
            log.warning("CMGD %d failed: %s", idx, e)

    async def _ingest_pdu(self, pdu_hex: str, _idx: int) -> None:
        sms = decode_deliver(pdu_hex)
        if sms.udh_ref is not None and sms.udh_total and sms.udh_total > 1:
            key = (sms.sender or "?", sms.udh_ref)
            slot = self._concat_buffer.setdefault(key, _PendingConcat(total=sms.udh_total))
            slot.parts[sms.udh_seq or 0] = sms
            if len(slot.parts) >= slot.total:
                ordered = [slot.parts[i].content for i in sorted(slot.parts.keys())]
                merged = IncomingSms(
                    sender=sms.sender,
                    content="".join(ordered),
                    received_at=min(p.received_at for p in slot.parts.values()),
                    raw_pdu=" | ".join(p.raw_pdu for p in slot.parts.values()),
                    decode_error=any(p.decode_error for p in slot.parts.values()),
                )
                self._concat_buffer.pop(key, None)
                await self._on_sms(merged)
            else:
                # GC stale concat groups after 5 minutes
                self._gc_concat()
        else:
            await self._on_sms(sms)

    def _gc_concat(self) -> None:
        now = time.time()
        stale = [k for k, v in self._concat_buffer.items() if now - v.first_at > 300]
        for k in stale:
            log.warning("dropping incomplete concat group %s", k)
            self._concat_buffer.pop(k, None)

    async def _send_one_segment(self, seg: OutgoingSegment) -> None:
        """Send one PDU using AT+CMGS=<len>\\r<pdu>\\x1A."""
        if not self._writer or not self._reader:
            raise ModemError("modem not open")
        cmd = f"AT+CMGS={seg.tpdu_length_octets}\r"
        log.debug(">> %s", cmd.strip())
        self._writer.write(cmd.encode("ascii"))
        await self._writer.drain()
        # Wait for "> " prompt
        deadline = time.monotonic() + 5
        prompt_buf = b""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ModemError("CMGS prompt timeout")
            chunk = await asyncio.wait_for(self._reader.read(64), timeout=remaining)
            if not chunk:
                raise ModemError("modem closed during CMGS")
            prompt_buf += chunk
            if b">" in prompt_buf:
                break
            if b"ERROR" in prompt_buf:
                raise ModemError(prompt_buf.decode("ascii", errors="replace").strip())
        self._writer.write(seg.pdu_hex.encode("ascii") + b"\x1a")
        await self._writer.drain()
        # Now expect +CMGS: <mr> then OK
        await self._read_response(settings.modem_send_timeout_seconds)
