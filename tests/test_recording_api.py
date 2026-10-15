"""Recording control over HTTP, plus the call detail route.

Two things are covered here that unit tests cannot reach. First, the recording
switch: whether the configured policy is honoured, whether the far end is told,
and whether an operator action lands in the audit trail. Second, the route a
client uses when it missed the call.ended websocket frame — without it a call
the far end released while still ringing has no representation left in
/app/voice/state, which is what left the app showing 对方振铃中 after hangup.

Capture is stubbed so no test opens the machine's microphone, and the record
directory is a temporary path so nothing lands in /var/lib.
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.voice.recorder import CallRecorder
from app.voice.recording_policy import NOTICE_MID_CALL
from tests.conftest import clear_call, wait_for_call_state


class StubPlayer:
    """Stands in for AudioPlayer so recording needs no sound card."""

    def __init__(self):
        self.started: list[Path] = []

    async def start_recording(self, path, device=None):
        handle_path = Path(path)
        handle_path.parent.mkdir(parents=True, exist_ok=True)
        self.started.append(handle_path)
        return SimpleNamespace(path=handle_path, device=device or "stub", process=None)

    async def stop_recording(self, handle):
        handle.path.write_bytes(b"RIFFstubwavpayload")
        return handle.path


@pytest.fixture()
def stub_recorder(client, monkeypatch, tmp_path):
    player = StubPlayer()
    spoken: list[str] = []

    async def speak(device_id: int, text: str) -> None:
        spoken.append(text)

    recorder = CallRecorder(
        player,
        enabled=lambda: True,
        record_dir=tmp_path,
        speak_into_call=speak,
    )
    monkeypatch.setattr(client.app.state, "recorder", recorder, raising=False)
    return SimpleNamespace(recorder=recorder, player=player, spoken=spoken, dir=tmp_path)


def _set_scenario(client, headers, device_id, scenario: str) -> None:
    response = client.post(
        f"/app/voice/mock/{device_id}/scenario", headers=headers, json={"scenario": scenario}
    )
    assert response.status_code == 200, response.text


def _dial_active(client, headers, device_id, number: str = "13800138000") -> str:
    _set_scenario(client, headers, device_id, "answer")
    response = client.post(
        "/app/voice/dial", headers=headers, json={"number": number, "device_id": device_id}
    )
    assert response.status_code == 200, response.text
    call_id = response.json()["call_id"]
    wait_for_call_state(client, headers, device_id, "active")
    return call_id


def test_recording_routes_need_a_session(client):
    assert client.get("/app/voice/calls/abc123/recording/state").status_code == 401
    assert client.post("/app/voice/calls/abc123/recording", json={"enabled": True}).status_code == 401
    assert client.post("/app/voice/calls/abc123/recording/announce").status_code == 401
    assert client.get("/app/voice/calls/abc123").status_code == 401


def test_an_unknown_call_id_is_not_found(client, admin_headers):
    assert client.get("/app/voice/calls/deadbeefdeadbeef", headers=admin_headers).status_code == 404
    state = client.get("/app/voice/calls/deadbeefdeadbeef/recording/state", headers=admin_headers)
    assert state.status_code == 404


def test_recording_can_be_started_and_stopped_mid_call(
    client, admin_headers, online_device_id, stub_recorder
):
    device_id = online_device_id
    try:
        call_id = _dial_active(client, admin_headers, device_id)

        before = client.get(f"/app/voice/calls/{call_id}/recording/state", headers=admin_headers)
        assert before.status_code == 200, before.text
        payload = before.json()
        assert payload["live"] is True
        assert payload["recording"] is False
        assert payload["policy"] == settings.recording_policy
        assert payload["decision"]["can_change"] is True

        started = client.post(
            f"/app/voice/calls/{call_id}/recording",
            headers=admin_headers,
            json={"enabled": True, "announce": True},
        )
        assert started.status_code == 200, started.text
        body = started.json()
        assert body["ok"] is True
        assert body["recording"] is True
        assert body["status"]["capture"]["notice"]["ok"] is True
        # The far end hears one sentence, and it says only that recording is on.
        assert stub_recorder.spoken == [NOTICE_MID_CALL]
        assert "证词" not in stub_recorder.spoken[0]

        state = client.get("/app/voice/state", headers=admin_headers).json()
        live = next(s for s in state["active"] if s["call_id"] == call_id)
        assert live["recording"] is True
        assert live["recording_detail"]["notice"]["text"] == NOTICE_MID_CALL
        assert state["recording_call_ids"] == [call_id]
        assert state["recording_policy"] == settings.recording_policy
        assert [c["call_id"] for c in state["recording"]] == [call_id]

        stopped = client.post(
            f"/app/voice/calls/{call_id}/recording", headers=admin_headers, json={"enabled": False}
        )
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["recording"] is False
        assert stub_recorder.recorder.is_recording(call_id) is False
        assert stub_recorder.player.started == [stub_recorder.dir / (call_id + ".wav")]
    finally:
        clear_call(client, admin_headers, device_id)


def test_a_silent_capture_does_not_speak_to_the_far_end(
    client, admin_headers, online_device_id, stub_recorder
):
    device_id = online_device_id
    try:
        call_id = _dial_active(client, admin_headers, device_id)
        response = client.post(
            f"/app/voice/calls/{call_id}/recording",
            headers=admin_headers,
            json={"enabled": True, "announce": False},
        )
        assert response.status_code == 200, response.text
        assert response.json()["recording"] is True
        assert stub_recorder.spoken == []
        status = client.get(
            f"/app/voice/calls/{call_id}/recording/state", headers=admin_headers
        ).json()
        assert status["recording"] is True
        assert status["capture"]["notice"] is None
    finally:
        clear_call(client, admin_headers, device_id)


def test_the_notice_can_be_played_later_and_reports_what_happened(
    client, admin_headers, online_device_id, stub_recorder
):
    device_id = online_device_id
    try:
        call_id = _dial_active(client, admin_headers, device_id)
        client.post(
            f"/app/voice/calls/{call_id}/recording",
            headers=admin_headers,
            json={"enabled": True, "announce": False},
        )
        response = client.post(
            f"/app/voice/calls/{call_id}/recording/announce", headers=admin_headers
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True
        assert body["notice"]["text"] == NOTICE_MID_CALL
        assert body["notice"]["mid_call"] is True
        assert body["status"]["capture"]["notice"]["ok"] is True
        assert stub_recorder.spoken == [NOTICE_MID_CALL]
    finally:
        clear_call(client, admin_headers, device_id)


def test_a_forced_policy_cannot_be_switched_off(
    client, admin_headers, online_device_id, stub_recorder, monkeypatch
):
    monkeypatch.setattr(settings, "recording_policy", "forced")
    device_id = online_device_id
    try:
        call_id = _dial_active(client, admin_headers, device_id)
        response = client.post(
            f"/app/voice/calls/{call_id}/recording", headers=admin_headers, json={"enabled": False}
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["error"]["code"] == "recording_policy"
        status = client.get(
            f"/app/voice/calls/{call_id}/recording/state", headers=admin_headers
        ).json()
        assert status["policy"] == "forced"
        assert status["decision"]["can_change"] is False
        assert status["decision"]["record"] is True
    finally:
        clear_call(client, admin_headers, device_id)


def test_a_disabled_policy_cannot_be_switched_on(
    client, admin_headers, online_device_id, stub_recorder, monkeypatch
):
    monkeypatch.setattr(settings, "recording_policy", "disabled")
    device_id = online_device_id
    try:
        call_id = _dial_active(client, admin_headers, device_id)
        response = client.post(
            f"/app/voice/calls/{call_id}/recording", headers=admin_headers, json={"enabled": True}
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["error"]["code"] == "recording_policy"
        assert stub_recorder.recorder.is_recording(call_id) is False
        assert stub_recorder.player.started == []
    finally:
        clear_call(client, admin_headers, device_id)


def test_recording_cannot_be_changed_once_the_call_ended(
    client, admin_headers, online_device_id, stub_recorder
):
    device_id = online_device_id
    call_id = _dial_active(client, admin_headers, device_id)
    clear_call(client, admin_headers, device_id)

    response = client.post(
        f"/app/voice/calls/{call_id}/recording", headers=admin_headers, json={"enabled": True}
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"]["code"] == "call_not_live"


def test_switching_recording_is_written_to_the_audit_trail(
    client, admin_headers, online_device_id, stub_recorder
):
    device_id = online_device_id
    try:
        call_id = _dial_active(client, admin_headers, device_id)
        for request in (
            client.post(
                f"/app/voice/calls/{call_id}/recording",
                headers=admin_headers,
                json={"enabled": True, "announce": True},
            ),
            client.post(f"/app/voice/calls/{call_id}/recording/announce", headers=admin_headers),
            client.post(
                f"/app/voice/calls/{call_id}/recording",
                headers=admin_headers,
                json={"enabled": False},
            ),
        ):
            assert request.status_code == 200, request.text

        audit = client.get("/app/admin/audit", headers=admin_headers, params={"limit": 50})
        assert audit.status_code == 200, audit.text
        rows = [r for r in audit.json()["items"] if r.get("target") == call_id]
        actions = {r["action"] for r in rows}
        assert {"call.recording.start", "call.recording.stop", "call.recording.announce"} <= actions
        # Every switch of the recording is attributable to the operator who did it.
        assert all(r["actor_label"] for r in rows), rows
    finally:
        clear_call(client, admin_headers, device_id)


def test_announcing_a_call_that_is_not_being_recorded_says_so(
    client, admin_headers, online_device_id, stub_recorder
):
    device_id = online_device_id
    try:
        call_id = _dial_active(client, admin_headers, device_id)
        response = client.post(
            f"/app/voice/calls/{call_id}/recording/announce", headers=admin_headers
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["error"]["code"] == "not_recording"
        assert stub_recorder.spoken == []
    finally:
        clear_call(client, admin_headers, device_id)


def test_call_detail_explains_a_call_the_live_list_already_dropped(client, admin_headers, online_device_id):
    """A missed call.ended frame must not leave a client guessing.

    The far end releases the call while our phone is still ringing. The gateway
    ends the session and drops it from /app/voice/state, so a client that missed
    the websocket frame has nothing left to compare against and keeps showing
    the ringing state. Asking about the call id it is displaying answers with
    the ended session and the reason.
    """
    device_id = online_device_id
    _set_scenario(client, admin_headers, device_id, "no_answer")
    try:
        response = client.post(
            "/app/voice/dial",
            headers=admin_headers,
            json={"number": "13700137000", "device_id": device_id},
        )
        assert response.status_code == 200, response.text
        call_id = response.json()["call_id"]

        alerting = wait_for_call_state(client, admin_headers, device_id, "alerting")
        assert alerting["state"] == "alerting"
        # While the call is live the same route reports it as live.
        live = client.get(f"/app/voice/calls/{call_id}", headers=admin_headers).json()
        assert live["live"] is True
        assert live["call"]["state"] == "alerting"

        deadline = time.time() + 12
        while time.time() < deadline:
            state = client.get("/app/voice/state", headers=admin_headers).json()
            if not any(s["call_id"] == call_id for s in state["active"]):
                break
            time.sleep(0.2)
        else:
            pytest.fail("the unanswered call never left the live list")

        detail = client.get(f"/app/voice/calls/{call_id}", headers=admin_headers)
        assert detail.status_code == 200, detail.text
        payload = detail.json()
        assert payload["live"] is False
        assert payload["call"]["state"] == "ended"
        assert payload["call"]["end_reason"] == "no_answer"
        assert payload["call"]["answered_at"] is None
    finally:
        clear_call(client, admin_headers, device_id)
        _set_scenario(client, admin_headers, device_id, "answer")
