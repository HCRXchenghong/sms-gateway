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
