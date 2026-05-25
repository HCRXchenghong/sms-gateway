"""Async AT-command client for SIMCom A7670G."""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

import serial

from .config import settings
from .pdu import IncomingSms, OutgoingSegment, decode_deliver, encode_submit_ucs2

log = logging.getLogger(__name__)

SmsHandler = Callable[[int, IncomingSms], Awaitable[None]]
IdentityHandler = Callable[[int, "DeviceIdentity"], Awaitable[None]]
StatusHandler = Callable[[int, str, Optional[str]], Awaitable[None]]


@dataclass
class _PendingConcat:
    total: int
    parts: Dict[int, IncomingSms] = field(default_factory=dict)
    first_at: float = field(default_factory=time.time)


@dataclass
class DeviceIdentity:
    sim_number_auto: Optional[str] = None
    iccid: Optional[str] = None
    imsi: Optional[str] = None
    imei: Optional[str] = None
    model: Optional[str] = None
    manufacturer: Optional[str] = None


class ModemError(RuntimeError):
    pass


class Modem:
    """Owns the serial connection and provides a simple send_sms API."""

    def __init__(
        self,
        device_id: int,
        at_port: str,
        on_sms: SmsHandler,
        on_identity: Optional[IdentityHandler] = None,
        on_status: Optional[StatusHandler] = None,
    ):
        self.device_id = device_id
        self._on_sms = on_sms
        self._on_identity = on_identity
        self._on_status = on_status
        self._serial: Optional[serial.Serial] = None
        self._port: Optional[str] = at_port
        self._lock = asyncio.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._reader_done: Optional[asyncio.Future[None]] = None
        self._stop = asyncio.Event()
        self._connected = asyncio.Event()
        self._response_queue: asyncio.Queue[str] = asyncio.Queue()
        self._urc_queue: asyncio.Queue[str] = asyncio.Queue()
        self._concat_buffer: Dict[Tuple[str, int], _PendingConcat] = {}
        self._submit_ref = 0

    async def run(self) -> None:
        """Own one serial port until it disconnects.

        Re-discovery is handled by ModemManager so a replugged device can be
        matched to its new ttyUSB port instead of retrying an old path forever.
        """
        try:
            port = self._port
            if not port:
                raise ModemError("missing AT port")
            log.info("opening AT port %s", port)
            await self._emit_status("connecting", None)
            self._loop = asyncio.get_running_loop()
            self._reader_done = self._loop.create_future()
            self._serial = await asyncio.to_thread(
                serial.Serial,
                port=port,
                baudrate=settings.modem_baudrate,
                timeout=0.2,
                write_timeout=5,
            )
            await asyncio.to_thread(self._serial.reset_input_buffer)
            await asyncio.to_thread(self._serial.reset_output_buffer)
            self._reader_thread = threading.Thread(
                target=self._reader_thread_main,
                name=f"modem-reader-{self.device_id}",
                daemon=True,
            )
            self._reader_thread.start()
            await self._configure()
            await self._identify()
            await self._scan_inbox()
            self._connected.set()
            await self._emit_status("online", None)
            if self._reader_done:
                await self._reader_done
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("modem connection error: %s", e)
            await self._emit_status("offline", str(e))
        finally:
            self._connected.clear()
            if self._serial:
                try:
                    self._serial.close()
                except Exception:
                    pass
            if self._reader_thread and self._reader_thread.is_alive():
                await asyncio.to_thread(self._reader_thread.join, 1.0)
            self._serial = None
            self._reader_thread = None
            self._reader_done = None

    def stop(self) -> None:
        self._stop.set()
        if self._serial:
            try:
                self._serial.close()
            except Exception:
                pass

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

    async def _emit_status(self, status: str, error: Optional[str]) -> None:
        if self._on_status:
            await self._on_status(self.device_id, status, error)

    @staticmethod
    async def probe(port: str) -> bool:
        return await asyncio.to_thread(Modem._probe_sync, port)

    @staticmethod
    def _probe_sync(port: str) -> bool:
        try:
            with serial.Serial(
                port=port,
                baudrate=settings.modem_baudrate,
                timeout=0.2,
                write_timeout=2,
            ) as ser:
                ser.reset_input_buffer()
                ser.reset_output_buffer()
                ser.write(b"AT\r\n")
                ser.flush()
                deadline = time.monotonic() + 2.0
                data = b""
                while time.monotonic() < deadline:
                    chunk = ser.read(128)
                    if chunk:
                        data += chunk
                        if b"OK" in data:
                            return True
                    else:
                        time.sleep(0.05)
                return False
        except Exception as e:
            log.debug("probe %s failed: %s", port, e)
            return False

    def _reader_thread_main(self) -> None:
        buf = bytearray()
        error: Optional[BaseException] = None
        try:
            while not self._stop.is_set():
                ser = self._serial
                if not ser or not ser.is_open:
                    break
                raw = ser.read(1)
                if not raw:
                    continue
                ch = raw[0]
                if ch == ord(">"):
                    if buf.strip():
                        self._post_serial_token(buf.decode("utf-8", errors="replace").strip())
                    buf.clear()
                    self._post_serial_token(">")
                    continue
                buf.append(ch)
                if ch == 0x0A:
                    line = buf.decode("utf-8", errors="replace").strip()
                    buf.clear()
                    if line:
                        self._post_serial_token(line)
        except BaseException as e:
            if not self._stop.is_set():
                error = e
                self._post_serial_token(f"__MODEM_CLOSED__:{e}")
        finally:
            loop = self._loop
            if loop:
                loop.call_soon_threadsafe(self._finish_reader_done, error)

    def _finish_reader_done(self, error: Optional[BaseException]) -> None:
        done = self._reader_done
        if not done or done.done():
            return
        if error:
            done.set_exception(ModemError(str(error)))
        else:
            done.set_result(None)

    def _post_serial_token(self, token: str) -> None:
        loop = self._loop
        if not loop or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._handle_serial_token, token)

    def _handle_serial_token(self, token: str) -> None:
        log.debug("<< %s", token)
        if token.startswith("+CMTI:"):
            self._urc_queue.put_nowait(token)
            asyncio.create_task(self._handle_cmti_async())
            return
        self._response_queue.put_nowait(token)

    async def _configure(self) -> None:
        await self._cmd("ATE0", timeout=2)
        await self._cmd("AT+CMEE=2", timeout=2)
        await self._cmd("AT+CMGF=0", timeout=2)
        await self._cmd('AT+CPMS="SM","SM","SM"', timeout=3)
        await self._cmd("AT+CNMI=2,1,0,0,0", timeout=3)
        log.info("modem configured (PDU mode, +CMTI URC enabled)")

    async def _identify(self) -> None:
        """Best-effort module/SIM identity collection."""
        identity = DeviceIdentity(
            manufacturer=await self._query_plain("AT+CGMI"),
            model=await self._query_plain("AT+CGMM"),
            imei=await self._query_plain("AT+CGSN"),
            imsi=await self._query_plain("AT+CIMI"),
            iccid=await self._query_iccid(),
            sim_number_auto=await self._query_cnum(),
        )
        if self._on_identity:
            await self._on_identity(self.device_id, identity)

    async def refresh_identity(self) -> None:
        """Re-read current module/SIM identity, used after a SIM swap."""
        if not self.connected:
            raise ModemError("modem not connected")
        await self._identify()

    async def _query_plain(self, cmd: str) -> Optional[str]:
        try:
            lines = await self._cmd(cmd, timeout=3)
        except ModemError:
            return None
        for line in lines:
            line = line.strip()
            if line and line != "OK" and not line.startswith("+"):
                return line
        return None

    async def _query_prefixed(self, cmd: str, prefix: str) -> Optional[str]:
        try:
            lines = await self._cmd(cmd, timeout=3)
        except ModemError:
            return None
        for line in lines:
            if line.startswith(prefix):
                value = line[len(prefix) :].strip().strip('"')
                return value or None
        return None

    async def _query_cnum(self) -> Optional[str]:
        try:
            lines = await self._cmd("AT+CNUM", timeout=3)
        except ModemError:
            return None
        for line in lines:
            if not line.startswith("+CNUM:"):
                continue
            values = re.findall(r'"([^"]*)"', line)
            for value in values:
                value = value.strip()
                if value and any(ch.isdigit() for ch in value):
                    return value
            parts = [p.strip().strip('"') for p in line.split(",")]
            for part in parts:
                digits = part[1:] if part.startswith("+") else part
                if digits.isdigit() and len(digits) >= 5:
                    return part
        return None

    async def _query_iccid(self) -> Optional[str]:
        for cmd, prefix in (
            ("AT+CCID", "+CCID:"),
            ("AT+CICCID", "+ICCID:"),
            ("AT+QCCID", "+ICCID:"),
        ):
            value = await self._query_prefixed(cmd, prefix)
            if value:
                return value
        return None

    async def _cmd(self, cmd: str, timeout: float = 10.0) -> List[str]:
        """Send an AT command; return response lines up to OK/ERROR."""
        async with self._lock:
            return await self._cmd_unlocked(cmd, timeout)

    async def _cmd_unlocked(self, cmd: str, timeout: float = 10.0) -> List[str]:
        if not self._serial or not self._reader_thread:
            raise ModemError("modem not open")
        log.debug(">> %s", cmd)
        self._drain_response_queue()
        await self._serial_write((cmd + "\r\n").encode("ascii"))
        return await self._read_response(timeout)

    async def _serial_write(self, data: bytes) -> None:
        ser = self._serial
        if not ser or not ser.is_open:
            raise ModemError("modem not open")

        def write() -> None:
            ser.write(data)
            ser.flush()

        await asyncio.to_thread(write)

    def _drain_response_queue(self) -> None:
        while True:
            try:
                self._response_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _read_response(self, timeout: float, expect_prompt: bool = False) -> List[str]:
        """Collect response lines until OK / ERROR / +CMS ERROR."""
        lines: List[str] = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ModemError("AT timeout")
            try:
                line = await asyncio.wait_for(self._response_queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                raise ModemError("AT timeout")
            if line.startswith("__MODEM_CLOSED__:"):
                raise ModemError(line.split(":", 1)[1] or "modem closed")
            if line == ">":
                if expect_prompt:
                    return lines
                continue
            lines.append(line)
            if line == "OK":
                return lines
            if line == "ERROR" or line.startswith("+CMS ERROR") or line.startswith("+CME ERROR"):
                raise ModemError(line)

    async def _handle_cmti_async(self) -> None:
        try:
            line = await self._urc_queue.get()
        except asyncio.CancelledError:
            return
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
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith("+CMGL:"):
                try:
                    idx = int(line.split(":", 1)[1].split(",")[0].strip())
                    if i + 1 < len(lines):
                        await self._ingest_pdu(lines[i + 1], idx)
                        await self._delete_index(idx)
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
        await self._delete_index(idx)

    async def _delete_index(self, idx: int) -> None:
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
                await self._on_sms(self.device_id, merged)
            else:
                self._gc_concat()
        else:
            await self._on_sms(self.device_id, sms)

    def _gc_concat(self) -> None:
        now = time.time()
        stale = [k for k, v in self._concat_buffer.items() if now - v.first_at > 300]
        for k in stale:
            log.warning("dropping incomplete concat group %s", k)
            self._concat_buffer.pop(k, None)

    async def _send_one_segment(self, seg: OutgoingSegment) -> None:
        """Send one PDU using AT+CMGS=<len> then PDU and Ctrl-Z."""
        if not self._serial or not self._reader_thread:
            raise ModemError("modem not open")
        cmd = f"AT+CMGS={seg.tpdu_length_octets}\r"
        log.debug(">> %s", cmd.strip())
        self._drain_response_queue()
        await self._serial_write(cmd.encode("ascii"))
        await self._read_response(5, expect_prompt=True)
        await self._serial_write(seg.pdu_hex.encode("ascii") + b"\x1a")
        await self._read_response(settings.modem_send_timeout_seconds)
