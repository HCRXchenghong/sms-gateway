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

# Events that only say "the call is over" without saying who ended it.
_UNATTRIBUTED_END_EVENTS = frozenset({"cend", "no_carrier"})


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

    def __init__(
        self,
        session: CallSession,
        *,
        clcc_absent_grace: float = 2.0,
        clcc_absent_threshold: int = 2,
        clcc_absent_hold_timeout: float = 15.0,
    ):
        self.session = session
        # Polling AT+CLCC is a fallback behind the URCs. The A7670 answers with
        # zero rows for a moment while it is still processing ATA/ATD, so one
        # empty reply cannot mean the call is gone: that single sample used to
        # end every answered inbound call in the same second it was picked up.
        self.clcc_absent_grace = float(clcc_absent_grace)
        self.clcc_absent_threshold = max(1, int(clcc_absent_threshold))
        # Upper bound on the hold, so a firmware that never reports a CLCC row
        # cannot keep a dead call alive forever.
        self.clcc_absent_hold_timeout = float(clcc_absent_hold_timeout)
        self._absent_streak = 0
        # True while a slow module command (ATD/ATA) is in flight. The module
        # has no CLCC rows to report yet, and that is expected, not a hangup.
        self._absent_suppressed = False
        self._held_at: Optional[float] = None
        self._changed_at = time.monotonic()
        # Set before we ask the module to hang up, so the +CEND / NO CARRIER
        # that follows is booked as our hangup instead of the far end's.
        self._local_hangup_requested = False

    def request_local_hangup(self) -> None:
        """Record that we are about to tell the module to release the call."""
        self._local_hangup_requested = True

    def hold_absent_detection(self) -> None:
        """Pause empty-CLCC detection while a blocking AT command is running."""
        self._absent_suppressed = True
        self._absent_streak = 0
        self._held_at = time.monotonic()

    def release_absent_detection(self) -> None:
        """Resume empty-CLCC detection and restart the grace window."""
        self._absent_suppressed = False
        self._absent_streak = 0
        self._held_at = None
        self._changed_at = time.monotonic()

    def _hold_expired(self) -> bool:
        if self._held_at is None:
            return True
        return (time.monotonic() - self._held_at) >= self.clcc_absent_hold_timeout

    def _go(self, to_state: CallState, trigger: str, reason: Optional[EndReason] = None) -> List[Transition]:
        if self.session.state == to_state:
            return []
        now = int(time.time())
        transition = Transition(self.session.call_id, self.session.state, to_state, trigger, reason)
        self.session.state = to_state
        self._changed_at = time.monotonic()
        self._absent_streak = 0
        if to_state == CallState.ENDED:
            self.release_absent_detection()
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
        if kind == "cend":
            # +CEND and SIMCom's "VOICE CALL: END" are the primary hangup
            # signal on this firmware and arrive before NO CARRIER, sometimes
            # without it. Ignoring them left the app sitting on a live call
            # screen after the far end had already gone.
            if self.session.state == CallState.ENDED:
                return []
            return self._go(CallState.ENDED, "cend", self._end_reason_for(kind))
        if kind in _TERMINAL_EVENTS:
            if self.session.state == CallState.ENDED:
                return []
            reason = self._end_reason_for(kind)
            return self._go(CallState.ENDED, kind, reason)
        return []

    def _end_reason_for(self, kind: str) -> EndReason:
        """Attribute an end event to the party that actually released the call."""
        default = _TERMINAL_EVENTS.get(kind, EndReason.UNKNOWN)
        if kind not in _UNATTRIBUTED_END_EVENTS:
            return default
        if self._local_hangup_requested:
            return EndReason.LOCAL_HANGUP
        if self.session.state == CallState.INCOMING:
            # The caller gave up while our phone was still ringing.
            return EndReason.NO_ANSWER
        if self.session.answered_at:
            return EndReason.REMOTE_HANGUP
        return default

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
        # A real row proves the call is alive; forget any empty replies and lift
        # the hold that covers ATD/ATA, since the module now agrees with us.
        self.release_absent_detection()
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
        """CLCC reported no rows while we believed a call existed.

        Debounced: only a sustained run of empty replies, started after the
        grace window that follows every state change, ends the session.
        """
        if self.session.state in (CallState.IDLE, CallState.ENDED):
            return []
        if self._absent_suppressed and not self._hold_expired():
            return []
        if self._absent_suppressed:
            self.release_absent_detection()
        self._absent_streak += 1
        since_change = time.monotonic() - self._changed_at
        if since_change < self.clcc_absent_grace:
            return []
        if self._absent_streak < self.clcc_absent_threshold:
            return []
        reason = self._end_reason_for("no_carrier")
        if reason == EndReason.NO_CARRIER and self.session.answered_at:
            reason = EndReason.REMOTE_HANGUP
        return self._go(CallState.ENDED, "clcc_empty", reason)

    def handle_local_hangup(self) -> List[Transition]:
        if self.session.state == CallState.ENDED:
            return []
        self._local_hangup_requested = True
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
