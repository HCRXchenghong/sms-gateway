"""The AI call routes over HTTP.

The call flow logic has its own unit tests. What is covered here is the part
only a real request can prove: authentication, validation, the app state
wiring, and that a task actually reaches the simulator and comes back with a
call id.

Capture is stubbed. Without the stub these tests would open the machine's
microphone, which is a side effect no test needs.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.voice import callflow as callflow_module
from tests.conftest import clear_call, wait_for_call_state


class StubBridge:
    """Stands in for CallAudioBridge so no sound card is touched."""

    instances: list = []

    def __init__(self, *, call_id, device_id=None, on_utterance=None, vad=None,
                 player=None, capture=None, **kwargs):
        self.call_id = call_id
        self.device_id = device_id
        self.on_utterance = on_utterance
        self.started = False
        self.stopped = False
        self.gates: list[float] = []
        self.stats = SimpleNamespace(to_dict=lambda: {"call_id": call_id, "running": self.started})
        StubBridge.instances.append(self)

    async def start(self):
        self.started = True
        return True

    async def stop(self):
        self.stopped = True

    def gated(self):
        return False

    async def gate_for(self, seconds):
        self.gates.append(seconds)

    def reset_capture(self):
        return None


@pytest.fixture(autouse=True)
def stub_bridge(monkeypatch):
    StubBridge.instances = []
    monkeypatch.setattr(callflow_module, "CallAudioBridge", StubBridge)
    yield StubBridge


def _mock_answer(client, headers, device_id, scenario="answer"):
    response = client.post(f"/app/voice/mock/{device_id}/scenario", headers=headers,
                           json={"scenario": scenario})
    assert response.status_code == 200, response.text


def test_flow_state_needs_a_session(client):
    assert client.get("/app/voice/flow").status_code == 401


def test_flow_state_reports_inbound_and_translation_settings(client, admin_headers):
    response = client.get("/app/voice/flow", headers=admin_headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["inbound_mode"] in ("manual", "auto_answer", "auto_answer_ai")
    assert set(payload["translation"]) == {"mode", "cadence", "my_language", "peer_language"}
    assert isinstance(payload["calls"], list)


def test_an_outbound_task_needs_a_number(client, admin_headers):
    response = client.post("/app/ai-call/tasks", headers=admin_headers,
                           json={"purpose": "确认发货时间"})
    assert response.status_code == 400, response.text
    # FastAPI wraps the dict detail, matching the rest of the voice routes.
    assert response.json()["detail"]["error"]["code"] == "invalid_task"


def test_a_purpose_is_required(client, admin_headers):
    response = client.post("/app/ai-call/tasks", headers=admin_headers,
                           json={"number": "+8613800000000", "purpose": ""})
    assert response.status_code == 422, response.text


def test_task_places_a_call_and_can_be_cancelled(client, admin_headers, online_device_id):
    _mock_answer(client, admin_headers, online_device_id)
    response = client.post("/app/ai-call/tasks", headers=admin_headers, json={
        "purpose": "确认这批货今天能不能发出",
        "number": "+8613800001111",
        "device_id": online_device_id,
        "max_seconds": 30,
    })
    assert response.status_code == 201, response.text
    task = response.json()["task"]
    task_id = task["id"]
    assert task["status"] == "queued"
    try:
        deadline = time.time() + 20
        row = task
        while time.time() < deadline:
            row = client.get(f"/app/ai-call/tasks/{task_id}", headers=admin_headers).json()["task"]
            if row["status"] in ("in_call", "finished", "failed", "no_answer"):
                break
            time.sleep(0.2)
        assert row["status"] == "in_call", row
        assert row["call_id"], "the task must be tied to the call it placed"
        detail = client.get(f"/app/ai-call/tasks/{task_id}", headers=admin_headers).json()
        assert detail["flow"]["purpose"] == "确认这批货今天能不能发出"
        if StubBridge.instances:
            assert StubBridge.instances[0].started is True
    finally:
        cancelled = client.post(f"/app/ai-call/tasks/{task_id}/cancel", headers=admin_headers)
        assert cancelled.status_code == 200, cancelled.text
        clear_call(client, admin_headers, online_device_id)
        _mock_answer(client, admin_headers, online_device_id)
    row = client.get(f"/app/ai-call/tasks/{task_id}", headers=admin_headers).json()["task"]
    assert row["status"] in ("cancelled", "finished"), row


def test_a_repeated_reference_returns_the_same_task(
    client, admin_headers, online_device_id
):
    body = {
        "purpose": "回访上次的工单",
        "number": "+8613800002222",
        "direction": "outbound",
        "device_id": online_device_id,
        "reference": f"itest-{int(time.time() * 1000)}",
    }
    first = client.post("/app/ai-call/tasks", headers=admin_headers, json=body)
    assert first.status_code == 201, first.text
    second = client.post("/app/ai-call/tasks", headers=admin_headers, json=body)
    assert second.status_code == 201, second.text
    assert second.json()["created"] is False
    assert second.json()["task"]["id"] == first.json()["task"]["id"]
    client.post(f"/app/ai-call/tasks/{first.json()['task']['id']}/cancel", headers=admin_headers)
    # A cancel has to leave the device free, or the next call cannot be placed.
    clear_call(client, admin_headers, online_device_id)


def test_translation_can_be_turned_on_and_off_mid_call(
    client, admin_headers, online_device_id
):
    _mock_answer(client, admin_headers, online_device_id)
    dial = client.post("/app/voice/dial", headers=admin_headers,
                       json={"number": "+8613800003333", "device_id": online_device_id})
    assert dial.status_code == 200, dial.text
    call_id = dial.json()["call_id"]
    try:
        wait_for_call_state(client, admin_headers, online_device_id, "active")
        on = client.post(f"/app/voice/calls/{call_id}/translate", headers=admin_headers,
                         json={"mode": "subtitle", "peer_language": "en"})
        assert on.status_code == 200, on.text
        assert on.json()["flow"]["translation"]["mode"] == "subtitle"
        assert on.json()["flow"]["translation"]["peer_language"] == "en"
        state = client.get("/app/voice/flow", headers=admin_headers).json()
        assert any(call["call_id"] == call_id for call in state["calls"])
        off = client.delete(f"/app/voice/calls/{call_id}/translate", headers=admin_headers)
        assert off.status_code == 200, off.text
        assert off.json()["flow"]["translation"] is None
        transcript = client.get(f"/app/voice/calls/{call_id}/transcript", headers=admin_headers)
        assert transcript.status_code == 200, transcript.text
    finally:
        clear_call(client, admin_headers, online_device_id)
        _mock_answer(client, admin_headers, online_device_id)


def test_translation_on_an_unknown_call_is_a_conflict(client, admin_headers):
    response = client.post("/app/voice/calls/deadbeef/translate", headers=admin_headers,
                           json={"mode": "subtitle"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"]["code"] == "call_flow"


def test_api_key_with_dial_scope_can_drive_tasks(client, admin_headers):
    created = client.post("/app/keys", headers=admin_headers,
                          json={"label": "ai-call-integration",
                                "scopes": ["voice:read", "voice:dial"]})
    assert created.status_code == 200, created.text
    api_key = created.json()["key"]
    key_headers = {"X-API-Key": api_key}

    response = client.post("/api/v1/ai-call-tasks", headers=key_headers, json={
        "purpose": "确认对账邮件是否收到",
        "number": "+8613800004444",
        "direction": "inbound",
        "reference": f"api-{int(time.time() * 1000)}",
    })
    assert response.status_code == 201, response.text
    task = response.json()["task"]
    assert task["direction"] == "inbound"

    listing = client.get("/api/v1/ai-call-tasks", headers=key_headers)
    assert listing.status_code == 200, listing.text
    assert any(row["id"] == task["id"] for row in listing.json()["items"])

    detail = client.get(f"/api/v1/ai-call-tasks/{task['id']}", headers=key_headers)
    assert detail.status_code == 200, detail.text

    cancelled = client.post(f"/api/v1/ai-call-tasks/{task['id']}/cancel", headers=key_headers)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["task"]["status"] == "cancelled"


def test_api_key_without_dial_scope_is_refused(client, admin_headers):
    created = client.post("/app/keys", headers=admin_headers,
                          json={"label": "read-only-integration", "scopes": ["voice:read"]})
    assert created.status_code == 200, created.text
    key_headers = {"X-API-Key": created.json()["key"]}
    response = client.post("/api/v1/ai-call-tasks", headers=key_headers,
                           json={"purpose": "确认收货", "number": "+8613800005555"})
    assert response.status_code == 403, response.text
    assert client.get("/api/v1/ai-call-tasks", headers=key_headers).status_code == 200
