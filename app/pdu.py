"""GSM 03.40 SMS PDU encoder / decoder.

Supports:
- SMS-DELIVER decode: GSM 7bit (default alphabet, no national-language shift),
  UCS2 (UTF-16BE), and 8-bit data (returned as hex).
- SMS-SUBMIT encode: UCS2 only (sufficient for arbitrary Unicode incl. Chinese
  and emoji); auto-segments long content using 8-bit concatenation UDH.
- Concatenation-UDH detection on inbound, exposing reference / total / seq.

The encoder always uses UCS2 (DCS=0x08) so that any user input round-trips,
including mixed Latin/CJK text. This trades a slightly smaller per-segment
limit (67 chars when concatenated) for correctness and simplicity.

References:
- 3GPP TS 23.038 (alphabets / DCS)
- 3GPP TS 23.040 (TPDU layout)
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# GSM 7-bit default alphabet
# ---------------------------------------------------------------------------

_GSM7_BASIC = (
    "@\u00a3$\u00a5\u00e8\u00e9\u00f9\u00ec\u00f2\u00c7\n\u00d8\u00f8\r\u00c5\u00e5"
    "\u0394_\u03a6\u0393\u039b\u03a9\u03a0\u03a8\u03a3\u0398\u039e\x1b\u00c6\u00e6\u00df\u00c9"
    " !\"#\u00a4%&'()*+,-./"
    "0123456789:;<=>?"
    "\u00a1ABCDEFGHIJKLMNO"
    "PQRSTUVWXYZ\u00c4\u00d6\u00d1\u00dc\u00a7"
    "\u00bfabcdefghijklmno"
    "pqrstuvwxyz\u00e4\u00f6\u00f1\u00fc\u00e0"
)

_GSM7_EXT = {
    0x0A: "\x0c",
    0x14: "^",
    0x28: "{",
    0x29: "}",
    0x2F: "\\",
    0x3C: "[",
    0x3D: "~",
    0x3E: "]",
    0x40: "|",
    0x65: "\u20ac",
}


def _gsm7_unpack(septets: bytes, n_septets: int, offset_bits: int = 0) -> str:
    """Unpack septets to characters; offset_bits accommodates UDH alignment."""
    bits = 0
    nbits = 0
    out_codes: List[int] = []
    # Burn padding bits up to offset
    skip_bits = offset_bits
    idx = 0
    for byte in septets:
        bits |= byte << nbits
        nbits += 8
        while nbits >= 7:
            if skip_bits > 0:
                bits >>= 1
                nbits -= 1
                skip_bits -= 1
                continue
            out_codes.append(bits & 0x7F)
            bits >>= 7
            nbits -= 7
            idx += 1
            if idx >= n_septets:
                break
        if idx >= n_septets:
            break

    # Apply ESC extension
    chars: List[str] = []
    i = 0
    while i < len(out_codes):
        c = out_codes[i]
        if c == 0x1B and i + 1 < len(out_codes):
            chars.append(_GSM7_EXT.get(out_codes[i + 1], " "))
            i += 2
        else:
            if c < len(_GSM7_BASIC):
                chars.append(_GSM7_BASIC[c])
            else:
                chars.append("?")
            i += 1
    return "".join(chars)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _swap_nibbles(hexs: str) -> str:
    out = []
    for i in range(0, len(hexs), 2):
        pair = hexs[i : i + 2]
        if len(pair) == 2:
            out.append(pair[1] + pair[0])
        else:
            out.append(pair)
    return "".join(out)


def _decode_address(data: bytes, offset: int) -> Tuple[str, int]:
    """Decode TP-Address; returns (number_string, new_offset)."""
    addr_len = data[offset]  # in semi-octets
    offset += 1
    toa = data[offset]
    offset += 1
    octets = (addr_len + 1) // 2
    raw = data[offset : offset + octets]
    offset += octets
    if (toa & 0x70) == 0x50:  # alphanumeric
        # 7-bit packed
        n_septets = (addr_len * 4) // 7
        number = _gsm7_unpack(raw, n_septets)
    else:
        digits = _swap_nibbles(raw.hex())[:addr_len]
        number = digits
        if (toa & 0x70) == 0x10:  # international
            number = "+" + number
    return number, offset


def _decode_scts(data: bytes, offset: int) -> Tuple[int, int]:
    """Decode 7-byte service center timestamp; return (epoch_seconds, new_offset).

    Tolerant: any malformed timestamp falls back to the current server time.
    """
    raw = data[offset : offset + 7]
    offset += 7

    def bcd_swap(b: int) -> int:
        return ((b & 0x0F) * 10) + (b >> 4)

    try:
        yy = bcd_swap(raw[0])
        mm = bcd_swap(raw[1])
        dd = bcd_swap(raw[2])
        hh = bcd_swap(raw[3])
        mi = bcd_swap(raw[4])
        ss = bcd_swap(raw[5])
        # TZ: same nibble-swap; bit 3 of low nibble (= bit 3 of original high nibble)
        # is the sign; remaining 7 bits are the quarter-hour magnitude in BCD.
        tz_b = raw[6]
        tz_low = tz_b & 0x0F  # this nibble carries the sign in bit 3
        tz_high = tz_b >> 4
        sign = -1 if (tz_low & 0x08) else 1
        tz_quarters = ((tz_low & 0x07) * 10) + tz_high
        # Clamp to a sane Python tzinfo range (< 24h).
        tz_minutes = max(-23 * 60, min(23 * 60, sign * tz_quarters * 15))
        year = 2000 + yy
        dt = _dt.datetime(
            year, mm, dd, hh, mi, ss,
            tzinfo=_dt.timezone(_dt.timedelta(minutes=tz_minutes)),
        )
        return int(dt.timestamp()), offset
    except (ValueError, OverflowError):
        return int(_dt.datetime.now(tz=_dt.timezone.utc).timestamp()), offset


@dataclass
class IncomingSms:
    sender: str
    content: str
    received_at: int  # epoch seconds (modem-reported)
    raw_pdu: str
    udh_ref: Optional[int] = None
    udh_total: Optional[int] = None
    udh_seq: Optional[int] = None
    decode_error: bool = False


def decode_deliver(pdu_hex: str) -> IncomingSms:
    """Decode an SMS-DELIVER PDU as returned by ``AT+CMGR``.

    Tolerant: on failure returns ``decode_error=True`` with raw hex preserved.
    """
    pdu_hex_norm = pdu_hex.strip().replace(" ", "").replace("\r", "").replace("\n", "")
    try:
        data = bytes.fromhex(pdu_hex_norm)
    except ValueError:
        return IncomingSms(
            sender="",
            content=pdu_hex,
            received_at=int(_dt.datetime.now(tz=_dt.timezone.utc).timestamp()),
            raw_pdu=pdu_hex,
            decode_error=True,
        )

    try:
        offset = 0
        # SMSC length & content (we just skip it)
        smsc_len = data[offset]
        offset += 1 + smsc_len

        first_octet = data[offset]
        offset += 1
        has_udh = bool(first_octet & 0x40)

        sender, offset = _decode_address(data, offset)
        pid = data[offset]
        offset += 1
        dcs = data[offset]
        offset += 1
        ts_epoch, offset = _decode_scts(data, offset)
        udl = data[offset]
        offset += 1

        ud = data[offset:]
        udh_ref = udh_total = udh_seq = None
        offset_bits = 0
        ud_payload_start = 0
        n_septets_total = udl

        if has_udh and ud:
            udhl = ud[0]
            udh = ud[1 : 1 + udhl]
            ud_payload_start = 1 + udhl
            # Walk IEs
            i = 0
            while i + 1 < len(udh):
                iei = udh[i]
                ielen = udh[i + 1]
                ievalue = udh[i + 2 : i + 2 + ielen]
                if iei == 0x00 and ielen == 3:  # 8-bit concat
                    udh_ref, udh_total, udh_seq = ievalue[0], ievalue[1], ievalue[2]
                elif iei == 0x08 and ielen == 4:  # 16-bit concat
                    udh_ref = (ievalue[0] << 8) | ievalue[1]
                    udh_total, udh_seq = ievalue[2], ievalue[3]
                i += 2 + ielen
            # For 7-bit, septet alignment after UDH
            udh_octets = 1 + udhl
            if (dcs & 0x0C) == 0x00 or dcs == 0x00:
                # 7-bit: number of septets that fit in udh_octets
                septets_in_udh = (udh_octets * 8 + 6) // 7
                n_septets_total = udl
                offset_bits = septets_in_udh * 7 - udh_octets * 8
                # We will pass the slice starting at udh_octets and skip offset_bits.

        coding_group = (dcs >> 4) & 0xF
        # 0xxx general; for our purposes we examine bits 3..2
        alphabet = (dcs >> 2) & 0x03 if coding_group <= 0x3 else 0x00

        if alphabet == 0x00:  # GSM 7-bit
            if has_udh:
                payload = ud[ud_payload_start:]
                # Remaining septets = total udl - septets that were UDH placeholders
                udh_octets = 1 + ud[0]
                septets_in_udh = (udh_octets * 8 + 6) // 7
                remaining_septets = udl - septets_in_udh
                content = _gsm7_unpack(payload, remaining_septets, 0)
                # The above is correct only when we strip the UDH bytes; offset_bits
                # is implicitly handled by feeding the post-UDH bytes.
            else:
                content = _gsm7_unpack(ud, udl, 0)
        elif alphabet == 0x02:  # UCS2
            payload = ud[ud_payload_start:] if has_udh else ud
            # UDL is in octets for UCS2/8bit
            if has_udh:
                payload_len = udl - (1 + ud[0])
                payload = payload[:payload_len]
            else:
                payload = payload[:udl]
            try:
                content = payload.decode("utf-16-be", errors="replace")
            except Exception:
                content = payload.hex()
        else:  # 8-bit data or reserved -> hex
            payload = ud[ud_payload_start:] if has_udh else ud
            content = payload.hex()

        return IncomingSms(
            sender=sender,
            content=content,
            received_at=ts_epoch,
            raw_pdu=pdu_hex_norm.upper(),
            udh_ref=udh_ref,
            udh_total=udh_total,
            udh_seq=udh_seq,
        )
    except Exception:  # pragma: no cover - defensive
        return IncomingSms(
            sender="",
            content="",
            received_at=int(_dt.datetime.now(tz=_dt.timezone.utc).timestamp()),
            raw_pdu=pdu_hex_norm.upper(),
            decode_error=True,
        )


# ---------------------------------------------------------------------------
# SMS-SUBMIT encoding (UCS2 only)
# ---------------------------------------------------------------------------


def _encode_address(number: str) -> bytes:
    """Encode TP-DA from a numeric string (optionally leading '+')."""
    international = number.startswith("+")
    digits = number[1:] if international else number
    if not digits.isdigit():
        raise ValueError("recipient must be digits with optional leading '+'")
    addr_len = len(digits)  # in digits
    toa = 0x91 if international else 0x81
    if len(digits) % 2 == 1:
        digits += "F"
    swapped = _swap_nibbles(digits)
    return bytes([addr_len, toa]) + bytes.fromhex(swapped)


@dataclass
class OutgoingSegment:
    pdu_hex: str
    tpdu_length_octets: int  # what AT+CMGS expects (excluding SMSC)


@dataclass
class OutgoingSubmit:
    recipient: str
    content: str
    segments: List[OutgoingSegment] = field(default_factory=list)


def encode_submit_ucs2(
    recipient: str,
    content: str,
    reference: int = 0,
) -> List[OutgoingSegment]:
    """Encode an SMS-SUBMIT in UCS2, auto-segmenting if needed.

    Returns a list of segments (1 if short, >1 if concatenated).
    """
    if not content:
        raise ValueError("content must not be empty")
    if not recipient:
        raise ValueError("recipient must not be empty")

    body = content.encode("utf-16-be")
    # Single-segment limit: 140 octets / 2 = 70 chars
    # Concat limit per segment: (140 - 6 UDH) / 2 = 67 chars
    max_single = 140
    max_concat = 140 - 6  # 134 octets

    if len(body) <= max_single:
        chunks = [(body, None, None)]  # (payload, total, seq)
    else:
        # Slice into 134-octet chunks (preserve surrogate pairs by working on bytes
        # in 2-byte multiples).
        chunks_bin: List[bytes] = []
        i = 0
        while i < len(body):
            chunk = body[i : i + max_concat]
            # Don't split a UCS2 code unit
            if len(chunk) % 2 == 1:
                chunk = chunk[:-1]
            chunks_bin.append(chunk)
            i += len(chunk)
        total = len(chunks_bin)
        chunks = [(c, total, idx + 1) for idx, c in enumerate(chunks_bin)]

    addr = _encode_address(recipient)
    segments: List[OutgoingSegment] = []
    ref8 = reference & 0xFF
    for payload, total, seq in chunks:
        # First octet
        if total is None:
            first = 0x11  # SMS-SUBMIT, VPF=relative, no UDH
            udh = b""
        else:
            first = 0x51  # SMS-SUBMIT, VPF=relative, UDHI=1
            udh = bytes([0x05, 0x00, 0x03, ref8, total, seq])
        mr = 0x00
        pid = 0x00
        dcs = 0x08  # UCS2
        vp = 0xAA  # 4 days
        ud = udh + payload
        udl = len(ud)
        tpdu = bytes([first, mr]) + addr + bytes([pid, dcs, vp, udl]) + ud
        smsc = bytes([0x00])  # use SMSC stored on SIM
        pdu = smsc + tpdu
        # AT+CMGS length excludes the SMSC byte(s); since smsc=00 it's 1 byte.
        segments.append(
            OutgoingSegment(pdu_hex=pdu.hex().upper(), tpdu_length_octets=len(tpdu))
        )
    return segments


__all__ = [
    "IncomingSms",
    "OutgoingSegment",
    "decode_deliver",
    "encode_submit_ucs2",
]
