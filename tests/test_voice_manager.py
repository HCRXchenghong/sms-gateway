"""CallManager lifecycle tests driven by a fake module.

These replay the sequences captured from a real A7670X so the two hangup bugs
stay fixed: an inbound call that vanished the moment it was answered, and a
call the far end released that never left the screen.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.modem import ModemError
from app.voice.manager import CallManager
from app.voice.parser import VoiceUrc, classify_voice_line
from app.voice.state import CallState, EndReason


class FakeModem:
    device_id = 1
    connected = True

    def __init__(self):
        self.clcc_rows: list[dict] = []
        self.clcc_error: str | None = None
        self.dialed: list[str] = []
        self.hangups = 0
        self.on_hangup = None

    async def dial(self, number: str):
        self.dialed.append(number)
        return ["OK"]

    async def answer(self):
        return ["OK"]

    async def hangup(self) -> None:
        self.hangups += 1
        if self.on_hangup:
            await self.on_hangup()

    async def send_dtmf(self, digits: str) -> int:
        return len(digits)

    async def tts_speak(self, text: str, mode: int = 1) -> None:
        return None

    async def tts_stop(self) -> None:
        return None

    async def list_calls(self):
        if self.clcc_error:
            raise ModemError(self.clcc_error)
        return list(self.clcc_rows)


def _manager(modem: FakeModem, *, grace=0.0, threshold=1):
    events: list[tuple[str, dict]] = []

    async def publish(event, payload):
        events.append((event, payload))

    manager = CallManager(get_modem=lambda device_id: modem, publish=publish)
    return manager, events


def _urc(manager, line: str):
    parsed = classify_voice_line(line)
    assert parsed is not None, line
    return manager._handle_urc(FakeModem.device_id, parsed)


@pytest.fixture(autouse=True)
def _fast_debounce(monkeypatch):
    monkeypatch.setattr(settings, "voice_clcc_absent_grace_seconds", 0.0, raising=False)
    monkeypatch.setattr(settings, "voice_clcc_absent_threshold", 1, raising=False)


@pytest.mark.asyncio
async def test_inbound_answer_survives_the_empty_clcc_window():
    """ATA makes the module report zero rows for a moment. That used to end the
    call in the same second it was answered."""
    modem = FakeModem()
    manager, events = _manager(modem)

    await _urc(manager, "RING")
    await _urc(manager, '+CLIP: "15331986287",129')
    assert manager.session_for_device(1).state == CallState.INCOMING

    await manager.answer(1)
    session = manager.session_for_device(1)
    assert session is not None and session.state == CallState.ACTIVE
    assert session.answered_at is not None

    # Poll repeatedly while the module still returns nothing.
    for _ in range(5):
        await manager._poll_device(1)
    session = manager.session_for_device(1)
    assert session is not None, "call must still be live right after answering"
    assert session.state == CallState.ACTIVE

    ended = [e for e in events if e[0] == "call.ended"]
    assert ended == []


@pytest.mark.asyncio
async def test_far_end_cend_reaches_the_app_immediately():
    modem = FakeModem()
    modem.clcc_rows = [{"stat": "0", "number": "15331986287"}]
    manager, events = _manager(modem)

    await _urc(manager, "RING")
    await _urc(manager, '+CLIP: "15331986287",129')
    await manager.answer(1)

    await _urc(manager, "VOICE CALL: END: 000015")
    assert manager.session_for_device(1) is None

    ended = [payload for name, payload in events if name == "call.ended"]
    assert len(ended) == 1
    assert ended[0]["end_reason"] == EndReason.REMOTE_HANGUP.value
    assert ended[0]["duration"] >= 0


@pytest.mark.asyncio
async def test_cend_plus_urc_ends_an_outbound_call():
    modem = FakeModem()
    manager, events = _manager(modem)

    session = await manager.dial(1, "15331986287")
    await _urc(manager, '+CLCC: 1,0,0,0,0,"15331986287",129')
    assert manager.session_by_id(session.call_id).state == CallState.ACTIVE

    await _urc(manager, '+CEND: 15,"15331986287","00",0,16')
    assert manager.session_for_device(1) is None
    ended = [payload for name, payload in events if name == "call.ended"]
    assert ended[-1]["end_reason"] == EndReason.REMOTE_HANGUP.value

    # NO CARRIER trails +CEND and must not create a second end event.
    await _urc(manager, "NO CARRIER")
    assert len([p for n, p in events if n == "call.ended"]) == 1


@pytest.mark.asyncio
async def test_own_hangup_is_not_booked_as_the_far_end():
    modem = FakeModem()
    manager, events = _manager(modem)

    await _urc(manager, "RING")
    await _urc(manager, '+CLIP: "15331986287",129')
    await manager.answer(1)

    # The module pushes +CEND while AT+CHUP is still in flight.
    async def module_releases():
        await _urc(manager, "VOICE CALL: END: 000004")

    modem.on_hangup = module_releases
    await manager.hangup(1)

    ended = [payload for name, payload in events if name == "call.ended"]
    assert len(ended) == 1
    assert ended[0]["end_reason"] == EndReason.LOCAL_HANGUP.value


@pytest.mark.asyncio
async def test_failed_clcc_poll_does_not_end_the_call():
    modem = FakeModem()
    modem.clcc_error = "AT+CLCC timed out"
    manager, events = _manager(modem)

    session = await manager.dial(1, "15331986287")
    modem.clcc_error = None
    modem.clcc_rows = [{"stat": "0"}]
    await manager._poll_device(1)
    assert manager.session_by_id(session.call_id).state == CallState.ACTIVE

    modem.clcc_error = "AT+CLCC timed out"
    for _ in range(5):
        await manager._poll_device(1)
    assert manager.session_by_id(session.call_id).state == CallState.ACTIVE
    assert [e for e in events if e[0] == "call.ended"] == []


@pytest.mark.asyncio
async def test_sustained_empty_clcc_is_the_fallback_end():
    modem = FakeModem()
    manager, events = _manager(modem)

    await _urc(manager, "RING")
    await _urc(manager, '+CLIP: "15331986287",129')
    await manager.answer(1)

    # The module confirms the call, which lifts the ATA hold.
    modem.clcc_rows = [{"stat": "0", "number": "15331986287"}]
    await manager._poll_device(1)
    assert manager.session_for_device(1) is not None

    modem.clcc_rows = []
    await manager._poll_device(1)
    assert manager.session_for_device(1) is None
    ended = [payload for name, payload in events if name == "call.ended"]
    assert ended[-1]["end_reason"] == EndReason.REMOTE_HANGUP.value


@pytest.mark.asyncio
async def test_production_debounce_needs_a_streak_of_empty_polls(monkeypatch):
    """With the shipped settings one or two empty replies are not enough."""
    monkeypatch.setattr(settings, "voice_clcc_absent_grace_seconds", 0.0, raising=False)
    monkeypatch.setattr(settings, "voice_clcc_absent_threshold", 3, raising=False)
    modem = FakeModem()
    manager, events = _manager(modem)

    await _urc(manager, "RING")
    await manager.answer(1)

    # A confirmed row lifts the ATA hold so the debounce is what is under test.
    modem.clcc_rows = [{"stat": "0"}]
    await manager._poll_device(1)

    modem.clcc_rows = []
    await manager._poll_device(1)
    await manager._poll_device(1)
    assert manager.session_for_device(1) is not None
    await manager._poll_device(1)
    assert manager.session_for_device(1) is None
