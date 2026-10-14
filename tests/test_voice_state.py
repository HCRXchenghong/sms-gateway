"""State machine tests. No I/O, so these run anywhere."""
from __future__ import annotations

from app.voice.state import (
    CallDirection,
    CallSession,
    CallState,
    CallStateMachine,
    EndReason,
)


def _machine(direction=CallDirection.OUTBOUND, *, trust_first_poll=False) -> CallStateMachine:
    """Build a machine. Empty-CLCC detection is debounced by default, matching
    production; pass trust_first_poll to exercise the end-of-call path directly."""
    kwargs = {}
    if trust_first_poll:
        kwargs = {"clcc_absent_grace": 0.0, "clcc_absent_threshold": 1}
    return CallStateMachine(
        CallSession(call_id="c1", device_id=1, direction=direction), **kwargs
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
    machine = _machine(trust_first_poll=True)
    machine.start_outbound("138")
    machine.handle_clcc("0")
    machine.handle_clcc_absent()
    assert machine.session.end_reason == EndReason.REMOTE_HANGUP


def test_clcc_absent_before_answer_is_no_carrier():
    machine = _machine(trust_first_poll=True)
    machine.start_outbound("138")
    machine.handle_clcc_absent()
    assert machine.session.end_reason == EndReason.NO_CARRIER


def test_single_empty_clcc_does_not_end_an_answered_call():
    """Regression: the A7670 answers AT+CLCC with no rows for a moment while it
    finishes ATA. That one sample used to end the call in the same second it was
    picked up, so the app lost the call screen on inbound calls."""
    machine = _machine(CallDirection.INBOUND)
    machine.handle_urc("ring", "138")
    machine.handle_answer()
    assert machine.session.state == CallState.ACTIVE
    machine._changed_at -= 60  # let the grace window expire
    assert machine.handle_clcc_absent() == []
    assert machine.session.state == CallState.ACTIVE
    assert machine.session.ended_at is None
    # The next real row confirms the call is alive.
    machine.handle_clcc("0", "138")
    assert machine.session.state == CallState.ACTIVE


def test_sustained_empty_clcc_still_ends_the_call():
    machine = _machine()
    machine.clcc_absent_grace = 0.0
    machine.clcc_absent_threshold = 3
    machine.handle_urc("ring", "138")
    machine.handle_answer()
    assert machine.handle_clcc_absent() == []
    assert machine.handle_clcc_absent() == []
    assert machine.session.state == CallState.ACTIVE
    assert machine.handle_clcc_absent()[0].to_state == CallState.ENDED
    assert machine.session.end_reason == EndReason.REMOTE_HANGUP


def test_empty_clcc_inside_grace_window_is_ignored():
    machine = _machine()
    machine.clcc_absent_threshold = 1
    machine.handle_urc("ring", "138")
    machine.handle_answer()
    # Grace has not expired yet, so even a threshold of one must not fire.
    assert machine.handle_clcc_absent() == []
    assert machine.session.state == CallState.ACTIVE


def test_answer_command_holds_absent_detection():
    machine = _machine(CallDirection.INBOUND)
    machine.handle_urc("ring", "138")
    machine.hold_absent_detection()
    machine._changed_at -= 60
    for _ in range(10):
        assert machine.handle_clcc_absent() == []
    assert machine.session.state == CallState.INCOMING
    machine.handle_answer()
    assert machine.session.state == CallState.ACTIVE
    # The hold survives the transition: ATA answering OK does not mean the
    # module is ready to report the call on AT+CLCC yet.
    machine._changed_at -= 60
    machine.clcc_absent_threshold = 1
    assert machine.handle_clcc_absent() == []
    assert machine.session.state == CallState.ACTIVE
    # A real row lifts the hold, and empty replies become a fallback again.
    machine.handle_clcc("0", "138")
    machine._changed_at -= 60
    assert machine.handle_clcc_absent()[0].to_state == CallState.ENDED


def test_absent_detection_hold_expires():
    """A firmware that never reports a CLCC row must not keep a call alive forever."""
    machine = _machine(CallDirection.INBOUND, trust_first_poll=True)
    machine.clcc_absent_hold_timeout = 0.0
    machine.handle_urc("ring", "138")
    machine.handle_answer()
    machine.hold_absent_detection()
    assert machine.handle_clcc_absent()[0].to_state == CallState.ENDED


def test_cend_ends_an_active_call_as_remote_hangup():
    """Regression: +CEND / VOICE CALL: END were parsed but never applied, so a
    call the far end released stayed on screen until NO CARRIER arrived, and
    stayed forever on firmwares that only send +CEND."""
    machine = _machine()
    machine.start_outbound("138")
    machine.handle_clcc("0")
    transitions = machine.handle_urc("cend")
    assert transitions[0].to_state == CallState.ENDED
    assert transitions[0].trigger == "cend"
    assert machine.session.end_reason == EndReason.REMOTE_HANGUP
    # NO CARRIER follows and must not overwrite the attribution.
    assert machine.handle_urc("no_carrier") == []
    assert machine.session.end_reason == EndReason.REMOTE_HANGUP


def test_cend_before_answer_is_a_missed_call():
    machine = _machine(CallDirection.INBOUND)
    machine.handle_urc("ring", "138")
    machine.handle_urc("cend")
    assert machine.session.end_reason == EndReason.NO_ANSWER
    assert machine.session.answered_at is None


def test_local_hangup_intent_wins_over_cend():
    machine = _machine()
    machine.start_outbound("138")
    machine.handle_clcc("0")
    machine.request_local_hangup()
    machine.handle_urc("cend")
    assert machine.session.end_reason == EndReason.LOCAL_HANGUP


def test_local_hangup_intent_applies_to_no_carrier():
    machine = _machine()
    machine.start_outbound("138")
    machine.handle_clcc("0")
    machine.request_local_hangup()
    machine.handle_urc("no_carrier")
    assert machine.session.end_reason == EndReason.LOCAL_HANGUP


def test_dial_failure_reasons_are_untouched_by_local_intent():
    machine = _machine()
    machine.start_outbound("138")
    machine.request_local_hangup()
    machine.handle_urc("busy")
    assert machine.session.end_reason == EndReason.BUSY


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
