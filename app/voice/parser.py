"""Pure parsers for voice-related AT response lines and URCs.

Everything in this module works on plain strings and returns plain data, so it
can be unit tested without a serial port. Capture a real AT session log and
feed the lines back in to reproduce hardware behaviour in tests.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Optional

__all__ = [
    "VoiceUrc",
    "classify_voice_line",
    "is_voice_urc",
    "VOICE_URC_PREFIXES",
    "CALL_PROGRESS_LINES",
]

# Lines the module pushes without being asked. These are intercepted and routed
# to the voice callback in addition to the normal command response queue, so a
# pending AT command that treats them as its terminal response still completes.
VOICE_URC_PREFIXES = (
    "RING",
    "NO CARRIER",
    "BUSY",
    "NO ANSWER",
    "NO DIALTONE",
    "CONNECT",
    "+CLIP:",
    "+CTTS:",
    "+CIEV:",
    "+CEND:",
    "+CEER:",
)

# Terminal call-progress lines accepted as the end of an ATD/ATA response.
CALL_PROGRESS_LINES = ("NO CARRIER", "BUSY", "NO ANSWER", "NO DIALTONE", "CONNECT")

_QUOTED = re.compile(r'"([^"]*)"')
_DIGITS = re.compile(r"^\+?[0-9*#]{3,20}$")


@dataclass
class VoiceUrc:
    """A normalised voice event parsed from one AT line."""

    kind: str
    raw: str
    number: Optional[str] = None
    value: Optional[int] = None
    extra: Dict[str, str] = field(default_factory=dict)


def _is_number(token: str) -> bool:
    return bool(_DIGITS.match(token.strip()))


def _split_fields(payload: str) -> list[str]:
    out: list[str] = []
    current = ""
    in_quotes = False
    for ch in payload:
        if ch == '"':
            in_quotes = not in_quotes
            current += ch
        elif ch == "," and not in_quotes:
            out.append(current.strip())
            current = ""
        else:
            current += ch
    out.append(current.strip())
    return out


def _extract_number(line: str) -> Optional[str]:
    """Pull a phone number out of a URC regardless of quoting style."""
    for match in _QUOTED.findall(line):
        if _is_number(match):
            return match.strip()
    for part in _split_fields(line):
        cleaned = part.strip().strip('"')
        if _is_number(cleaned):
            return cleaned
    return None


def _extract_int(payload: str) -> Optional[int]:
    match = re.search(r"-?\d+", payload)
    return int(match.group(0)) if match else None


def is_voice_urc(line: str) -> bool:
    """True when a line should also be delivered to the voice event callback."""
    stripped = line.strip()
    if not stripped:
        return False
    for prefix in VOICE_URC_PREFIXES:
        if stripped == prefix or stripped.startswith(prefix):
            return True
    return False


def classify_voice_line(line: str) -> Optional[VoiceUrc]:
    """Parse one AT line into a VoiceUrc, or None if it is not a voice event."""
    stripped = line.strip()
    if not stripped:
        return None

    if stripped == "RING":
        return VoiceUrc(kind="ring", raw=stripped)

    if stripped.startswith("NO CARRIER"):
        return VoiceUrc(kind="no_carrier", raw=stripped, value=_extract_int(stripped))
    if stripped.startswith("BUSY"):
        return VoiceUrc(kind="busy", raw=stripped)
    if stripped.startswith("NO ANSWER"):
        return VoiceUrc(kind="no_answer", raw=stripped)
    if stripped.startswith("NO DIALTONE"):
        return VoiceUrc(kind="no_dialtone", raw=stripped)
    if stripped.startswith("CONNECT"):
        return VoiceUrc(kind="connect", raw=stripped, value=_extract_int(stripped))

    if stripped.startswith("+CLIP:"):
        return VoiceUrc(kind="clip", raw=stripped, number=_extract_number(stripped[6:]))

    if stripped.startswith("+CTTS:"):
        return VoiceUrc(kind="ctts", raw=stripped, value=_extract_int(stripped[6:]))

    if stripped.startswith("+CIEV:"):
        fields = _split_fields(stripped[6:])
        extra = {"ind": fields[0] if fields else "", "indvalue": fields[1] if len(fields) > 1 else ""}
        return VoiceUrc(kind="ciev", raw=stripped, value=_extract_int(extra["indvalue"]), extra=extra)

    if stripped.startswith("+CEND:"):
        # SIMCom proprietary end-of-call report: duration, number, reason.
        fields = _split_fields(stripped[6:])
        return VoiceUrc(
            kind="cend",
            raw=stripped,
            number=_extract_number(stripped[6:]),
            extra={f"f{i}": f for i, f in enumerate(fields)},
        )

    if stripped.startswith("+CEER:"):
        return VoiceUrc(kind="ceer", raw=stripped, value=_extract_int(stripped[6:]))

    return None


def parse_clcc(line: str) -> Optional[Dict[str, str]]:
    """Parse one +CLCC list-current-calls response line.

    +CLCC: <id>,<dir>,<stat>,<mode>,<mpty>[,<number>,<type>[,<alpha>[,<priority>]]]
    stat: 0 active, 1 held, 2 dialing, 3 alerting, 4 incoming, 5 waiting, 6 released
    """
    stripped = line.strip()
    if not stripped.startswith("+CLCC:"):
        return None
    fields = _split_fields(stripped[6:])
    if len(fields) < 5:
        return None
    number = None
    for part in fields[5:]:
        cleaned = part.strip().strip('"')
        if _is_number(cleaned):
            number = cleaned
            break
    return {
        "id": fields[0],
        "dir": fields[1],
        "stat": fields[2],
        "mode": fields[3],
        "mpty": fields[4],
        "number": number or "",
    }
