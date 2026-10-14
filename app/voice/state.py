"""Call state machine.

The machine is deliberately free of I/O. It receives events and returns the
transitions that happened, so CallManager can decide how to persist, publish
and play prompts. That keeps the whole thing testable without a modem.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

__all__ = [
    "CallState",
    "CallDirection",
    "EndReason",
    "CallSession",
    "Transition",
    "CallStateMachine",
    "CLCC_STAT_TO_STATE",
]


class CallState(str, Enum):
    IDLE = "idle"
    DIALING = "dialing"
    ALERTING = "alerting"
    INCOMING = "incoming"
    ACTIVE = "active"
    HELD = "held"
    ENDED = "ended"


class CallDirection(str, Enum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"


class EndReason(str, Enum):
    LOCAL_HANGUP = "local_hangup"
    REMOTE_HANGUP = "remote_hangup"
    REJECTED = "rejected"
    BUSY = "busy"
    NO_ANSWER = "no_answer"
    NO_CARRIER = "no_carrier"
    NO_DIALTONE = "no_dialtone"
    TIMEOUT = "timeout"
    ERROR = "error"
    UNKNOWN = "unknown"


# AT+CLCC <stat> field, 3GPP TS 27.007 section 7.11.
CLCC_STAT_TO_STATE = {
    "0": CallState.ACTIVE,
    "1": CallState.HELD,
    "2": CallState.DIALING,
    "3": CallState.ALERTING,
    "4": CallState.INCOMING,
    "5": CallState.INCOMING,
    "6": CallState.ENDED,
}

_TERMINAL_EVENTS = {
    "busy": EndReason.BUSY,
    "no_answer": EndReason.NO_ANSWER,
    "no_carrier": EndReason.NO_CARRIER,
    "no_dialtone": EndReason.NO_DIALTONE,
}


@dataclass
class CallSession:
    call_id: str
    device_id: int
    direction: CallDirection
    remote: Optional[str] = None
    state: CallState = CallState.IDLE
    created_at: int = field(default_factory=lambda: int(time.time()))
    ringing_at: Optional[int] = None
    answered_at: Optional[int] = None
    ended_at: Optional[int] = None
    end_reason: Optional[EndReason] = None
    tts_log: List[dict] = field(default_factory=list)
    transcript: List[dict] = field(default_factory=list)
    recording_path: Optional[str] = None
    owner_user_id: Optional[int] = None
    origin: Optional[str] = None

    @property
    def duration(self) -> int:
        if not self.answered_at:
            return 0
        end = self.ended_at or int(time.time())
        return max(0, end - self.answered_at)

    def to_dict(self) -> dict:
        return {
            "call_id": self.call_id,
            "device_id": self.device_id,
            "direction": self.direction.value,
            "remote": self.remote,
            "state": self.state.value,
            "created_at": self.created_at,
            "ringing_at": self.ringing_at,
            "answered_at": self.answered_at,
            "ended_at": self.ended_at,
            "end_reason": self.end_reason.value if self.end_reason else None,
            "duration": self.duration,
            "tts_log": self.tts_log,
            "recording_path": self.recording_path,
            "owner_user_id": self.owner_user_id,
            "origin": self.origin,
        }


@dataclass
class Transition:
    call_id: str
    from_state: CallState
    to_state: CallState
    trigger: str
    reason: Optional[EndReason] = None

    def to_dict(self) -> dict:
        return {
            "call_id": self.call_id,
            "from_state": self.from_state.value,
            "to_state": self.to_state.value,
            "trigger": self.trigger,
            "reason": self.reason.value if self.reason else None,
        }


class CallStateMachine:
    """Owns one CallSession and applies events to it."""

    def __init__(self, session: CallSession):
        self.session = session

    def _go(self, to_state: CallState, trigger: str, reason: Optional[EndReason] = None) -> List[Transition]:
        if self.session.state == to_state:
            return []
        now = int(time.time())
        transition = Transition(self.session.call_id, self.session.state, to_state, trigger, reason)
        self.session.state = to_state
        if to_state == CallState.ACTIVE and not self.session.answered_at:
            self.session.answered_at = now
        # ringing_at means "ringing started", which is INCOMING for a call we
        # received and ALERTING for one we placed. Only stamping INCOMING left
        # every outbound record with ringing_at=None even though the far end
        # did ring, so ring duration could not be measured.
        if to_state in (CallState.INCOMING, CallState.ALERTING) and not self.session.ringing_at:
            self.session.ringing_at = now
        if to_state == CallState.ENDED:
            self.session.ended_at = now
            self.session.end_reason = reason or EndReason.UNKNOWN
        return [transition]

    def start_outbound(self, remote: str) -> List[Transition]:
        self.session.direction = CallDirection.OUTBOUND
        self.session.remote = remote
        return self._go(CallState.DIALING, "dial")

    def start_inbound(self, remote: Optional[str] = None) -> List[Transition]:
        self.session.direction = CallDirection.INBOUND
        if remote:
            self.session.remote = remote
        return self._go(CallState.INCOMING, "ring")

    def handle_urc(self, kind: str, number: Optional[str] = None) -> List[Transition]:
        """Apply a URC parsed by voice.parser.classify_voice_line."""
        if kind == "ring":
            if self.session.state in (CallState.IDLE, CallState.ENDED):
                return self.start_inbound(number)
            return []
        if kind == "clip":
            if number:
                self.session.remote = number
            return []
        if kind == "colp":
            # Connected line identification confirms the reached number.
            if number:
                self.session.remote = number
            return []
        if kind == "connect":
            return self._go(CallState.ACTIVE, "connect")
        if kind in _TERMINAL_EVENTS:
            if self.session.state == CallState.ENDED:
                return []
            return self._go(CallState.ENDED, kind, _TERMINAL_EVENTS[kind])
        return []

    def handle_answer(self) -> List[Transition]:
        """We issued ATA and the module accepted it."""
        if self.session.state in (CallState.INCOMING, CallState.ALERTING):
            return self._go(CallState.ACTIVE, "answer")
        return []

    def handle_clcc(self, stat: str, number: Optional[str] = None) -> List[Transition]:
        """Apply one AT+CLCC row. Polling keeps us honest across firmwares."""
        target = CLCC_STAT_TO_STATE.get(str(stat).strip())
        if target is None:
            return []
        if number and not self.session.remote:
            self.session.remote = number
        if self.session.state == CallState.ENDED:
            return []
        if target == CallState.ENDED:
            return self._go(CallState.ENDED, "clcc_release", EndReason.REMOTE_HANGUP)
        # Never walk backwards out of ACTIVE; a CLCC poll can lag a URC.
        if self.session.state == CallState.ACTIVE and target in (CallState.DIALING, CallState.ALERTING):
            return []
        return self._go(target, "clcc")

    def handle_clcc_absent(self) -> List[Transition]:
        """CLCC reported no rows while we believed a call existed."""
        if self.session.state in (CallState.IDLE, CallState.ENDED):
            return []
        reason = EndReason.REMOTE_HANGUP if self.session.answered_at else EndReason.NO_CARRIER
        return self._go(CallState.ENDED, "clcc_empty", reason)

    def handle_local_hangup(self) -> List[Transition]:
        if self.session.state == CallState.ENDED:
            return []
        reason = EndReason.REJECTED if self.session.state == CallState.INCOMING else EndReason.LOCAL_HANGUP
        return self._go(CallState.ENDED, "local_hangup", reason)

    def handle_timeout(self) -> List[Transition]:
        if self.session.state == CallState.ENDED:
            return []
        return self._go(CallState.ENDED, "timeout", EndReason.TIMEOUT)

    def handle_error(self, detail: str) -> List[Transition]:
        if self.session.state == CallState.ENDED:
            return []
        self.session.tts_log.append({"error": detail})
        return self._go(CallState.ENDED, "error", EndReason.ERROR)
