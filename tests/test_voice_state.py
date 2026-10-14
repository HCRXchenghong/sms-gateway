"""State machine tests. No I/O, so these run anywhere."""
from __future__ import annotations

from app.voice.state import (
    CallDirection,
    CallSession,
    CallState,
    CallStateMachine,
    EndReason,
)


def _machine(direction=CallDirection.OUTBOUND) -> CallStateMachine:
    return CallStateMachine(
        CallSession(call_id="c1", device_id=1, direction=direction)
    )


def test_outbound_happy_path():
    machine = _machine()
    assert machine.start_outbound("13800138000")[0].to_state == CallState.DIALING
    assert machine.session.remote == "13800138000"
    assert machine.handle_clcc("3")[0].to_state == CallState.ALERTING
    transitions = machine.handle_clcc("0")
    assert transitions[0].to_state == CallState.ACTIVE
    assert machine.session.answered_at is not None


def test_outbound_busy_terminates():
    machine = _machine()
    machine.start_outbound("138")
    transitions = machine.handle_urc("busy")
    assert transitions[0].to_state == CallState.ENDED
    assert machine.session.end_reason == EndReason.BUSY
    assert machine.session.duration == 0


def test_no_answer_and_no_carrier():
    for kind, reason in (("no_answer", EndReason.NO_ANSWER), ("no_carrier", EndReason.NO_CARRIER)):
        machine = _machine()
        machine.start_outbound("138")
        machine.handle_urc(kind)
        assert machine.session.end_reason == reason, kind


def test_active_never_regresses_to_dialing():
    machine = _machine()
    machine.start_outbound("138")
    machine.handle_clcc("0")
    assert machine.session.state == CallState.ACTIVE
    # A late CLCC poll reporting the old state must not walk us backwards.
    assert machine.handle_clcc("2") == []
    assert machine.session.state == CallState.ACTIVE


def test_incoming_ring_then_answer():
    machine = _machine(CallDirection.INBOUND)
    assert machine.handle_urc("ring")[0].to_state == CallState.INCOMING
    machine.handle_urc("clip", "13800138000")
    assert machine.session.remote == "13800138000"
    assert machine.handle_answer()[0].to_state == CallState.ACTIVE


def test_incoming_reject_reports_rejected():
    machine = _machine(CallDirection.INBOUND)
    machine.handle_urc("ring")
    machine.handle_local_hangup()
    assert machine.session.end_reason == EndReason.REJECTED


def test_remote_hangup_after_answer():
    machine = _machine()
    machine.start_outbound("138")
    machine.handle_clcc("0")
    machine.handle_clcc_absent()
    assert machine.session.end_reason == EndReason.REMOTE_HANGUP


def test_clcc_absent_before_answer_is_no_carrier():
    machine = _machine()
    machine.start_outbound("138")
    machine.handle_clcc_absent()
    assert machine.session.end_reason == EndReason.NO_CARRIER


def test_ended_is_terminal():
    machine = _machine()
    machine.start_outbound("138")
    machine.handle_urc("busy")
    assert machine.session.state == CallState.ENDED
    assert machine.handle_urc("no_carrier") == []
    assert machine.handle_local_hangup() == []
    assert machine.session.end_reason == EndReason.BUSY


def test_duration_counts_from_answer():
    machine = _machine()
    machine.start_outbound("138")
    machine.handle_clcc("0")
    machine.session.answered_at -= 42
    machine.handle_local_hangup()
    assert machine.session.duration >= 42


def test_timeout_from_alerting():
    machine = _machine()
    machine.start_outbound("138")
    machine.handle_clcc("3")
    machine.handle_timeout()
    assert machine.session.end_reason == EndReason.TIMEOUT


def test_session_serialisation_shape():
    machine = _machine()
    machine.start_outbound("13800138000")
    payload = machine.session.to_dict()
    for key in ("call_id", "device_id", "direction", "remote", "state", "duration"):
        assert key in payload
    assert payload["direction"] == "outbound"
    assert payload["state"] == "dialing"


def test_outbound_alerting_stamps_ringing_at():
    """ringing_at means ringing started, so an outbound call stamps it at ALERTING.

    It used to be written only on INCOMING, which left every outbound record with
    ringing_at=None even though the far end demonstrably rang, so ring duration
    could not be measured for placed calls.
    """
    machine = _machine()
    machine.start_outbound("15331986287")
    assert machine.session.ringing_at is None
    machine.handle_clcc("3")
    assert machine.session.state == CallState.ALERTING
    assert machine.session.ringing_at is not None


def test_real_a7670_outbound_session_replay():
    """Replay the exact URC sequence captured from an A7670G-LLSE voice call.

    Recorded while dialling a real mobile over LTE (CSQ 29, operator 46000).
    The firmware never sends CONNECT; it announces the answer with its own
    VOICE CALL: BEGIN line and reports the talk time on VOICE CALL: END.
    """
    machine = _machine()
    assert machine.start_outbound("15331986287")[0].to_state == CallState.DIALING

    # +CLCC: 1,0,2 -> still dialing
    machine.handle_clcc("2", "15331986287")
    assert machine.session.state == CallState.DIALING

    # +CLCC: 1,0,3 -> far end rings
    machine.handle_clcc("3", "15331986287")
    assert machine.session.state == CallState.ALERTING
    assert machine.session.ringing_at is not None

    # VOICE CALL: BEGIN parses to kind "connect"
    assert machine.handle_urc("connect")[0].to_state == CallState.ACTIVE
    assert machine.session.answered_at is not None

    # +CLCC: 1,0,0 arrives right after; must not disturb an active call
    assert machine.handle_clcc("0", "15331986287") == []
    assert machine.session.state == CallState.ACTIVE

    # +COLP confirms the connected number
    machine.handle_urc("colp", "15331986287")
    assert machine.session.remote == "15331986287"

    # +CLCC: 1,0,6 -> released by the far end
    machine.handle_clcc("6", "15331986287")
    assert machine.session.state == CallState.ENDED
    assert machine.session.end_reason == EndReason.REMOTE_HANGUP

    # VOICE CALL: END and NO CARRIER follow; the session is already terminal
    assert machine.handle_urc("cend") == []
    assert machine.handle_urc("no_carrier") == []
    assert machine.session.end_reason == EndReason.REMOTE_HANGUP
    assert machine.session.ended_at is not None
