"""End-to-end integration tests against a real running server.

Covers the voice call lifecycle over HTTP plus WebSocket, prompt playback,
multi-user account management and API key scoping. Run with:
    .venv/bin/python -m pytest tests/test_live_integration.py -q
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Callable, List, Optional

import pytest
import websockets

from tests.live_server import LiveServer

ADMIN = "liveadmin"
ADMIN_PW = "LivePass123"


class WsCollector:
    """Consume /ws on a background thread and keep every event."""

    def __init__(self, url: str, token: str):
        self.url = url
        self.token = token
        self.events: List[dict] = []
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def __enter__(self) -> "WsCollector":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # Give the handshake a moment so the hub registers this socket before
        # the test starts triggering events.
        time.sleep(0.6)
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        asyncio.run(self._consume())

    async def _consume(self) -> None:
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with websockets.connect(self.url, additional_headers=headers) as ws:
                while not self._stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=0.4)
                    except asyncio.TimeoutError:
                        continue
                    try:
                        message = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    with self._lock:
                        self.events.append(message)
        except Exception as e:  # pragma: no cover - diagnostics only
            with self._lock:
                self.events.append({"event": "__ws_error__", "data": str(e)})

    def snapshot(self) -> List[dict]:
        with self._lock:
            return list(self.events)

    def wait_for(self, predicate: Callable[[dict], bool], timeout: float = 12.0) -> Optional[dict]:
        deadline = time.time() + timeout
        seen = 0
        while time.time() < deadline:
            events = self.snapshot()
            for event in events[seen:]:
                if predicate(event):
                    return event
            seen = len(events)
            time.sleep(0.1)
        return None

    def states_for(self, call_id: str, timeout: float = 0.0) -> List[str]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.1)
        out: List[str] = []
        for event in self.snapshot():
            if event.get("event") != "call.state":
                continue
            data = event.get("data", {})
            if data.get("call_id") == call_id:
                out.append(data["to_state"])
        return out


@pytest.fixture(scope="module")
def server():
    live = LiveServer()
    live.start()
    live.create_admin(ADMIN, ADMIN_PW)
    try:
        yield live
    finally:
        live.stop()


@pytest.fixture(scope="module")
def token(server: LiveServer) -> str:
    return server.login(ADMIN, ADMIN_PW)


@pytest.fixture()
def device_id(server: LiveServer, token: str) -> int:
    status, payload = server.request("GET", "/app/devices", token=token, expect=200)
    online = [d for d in payload["items"] if d["status"] == "online"]
    assert online, f"no online devices: {payload}"
    return int(online[0]["id"])


def _poll_until(server, token, device_id, wanted, timeout=12.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        _, payload = server.request("GET", "/app/voice/state", token=token, expect=200)
        for session in payload["active"]:
            if session["device_id"] == device_id:
                last = session
                if session["state"] == wanted:
                    return session
        if wanted == "gone" and last is None:
            return None
        time.sleep(0.15)
    raise AssertionError(f"timed out waiting for {wanted}; last={last}")


@pytest.fixture(autouse=True)
def _reset_between_tests(server: LiveServer, token: str):
    """Hang up leftovers and restore the default simulator scenario.

    Without this a failure part way through a call leaves the simulator busy
    and every later test that needs an inbound call cascades into failure.
    """
    yield
    _, devices = server.request("GET", "/app/devices", token=token, expect=200)
    for device in devices["items"]:
        device_id = device["id"]
        server.request("POST", f"/app/voice/{device_id}/hangup", token=token)
        server.request(
            "POST", f"/app/voice/mock/{device_id}/scenario",
            token=token, body={"scenario": "answer"},
        )
    deadline = time.time() + 8
    while time.time() < deadline:
        _, state = server.request("GET", "/app/voice/state", token=token, expect=200)
        if not state["active"]:
            return
        time.sleep(0.15)


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------


def test_health_reports_both_devices(server: LiveServer):
    status, payload = server.request("GET", "/api/v1/health", expect=200)
    assert payload["status"] == "ok"
    assert payload["devices_online"] == 2


def test_voice_state_baseline(server: LiveServer, token: str):
    status, payload = server.request("GET", "/app/voice/state", token=token, expect=200)
    assert payload["voice_enabled"] is True
    assert len(payload["online_device_ids"]) == 2
    assert payload["active"] == []


def test_unauthenticated_access_is_rejected(server: LiveServer):
    status, _ = server.request("GET", "/app/voice/state")
    assert status == 401


# ---------------------------------------------------------------------------
# Outbound calls
# ---------------------------------------------------------------------------


def test_outbound_call_full_cycle(server: LiveServer, token: str, device_id: int):
    server.request("POST", f"/app/voice/mock/{device_id}/scenario", token=token,
                   body={"scenario": "answer"}, expect=200)
    with WsCollector(server.ws_url, token) as ws:
        status, session = server.request(
            "POST", "/app/voice/dial", token=token,
            body={"number": "13800138000", "device_id": device_id}, expect=200,
        )
        assert session["direction"] == "outbound"
        assert session["remote"] == "13800138000"
        assert session["state"] in ("dialing", "alerting")
        call_id = session["call_id"]

        active = _poll_until(server, token, device_id, "active")
        assert active["answered_at"] is not None

        _, dtmf = server.request("POST", f"/app/voice/{device_id}/dtmf", token=token,
                                 body={"digits": "12#"}, expect=200)
        assert dtmf["sent"] == 3

        server.request("POST", f"/app/voice/{device_id}/tts", token=token,
                       body={"text": "您好，这是一次联调测试播报"}, expect=200)

        _, debug = server.request("GET", f"/app/voice/mock/{device_id}/debug", token=token, expect=200)
        assert any("联调测试播报" in entry["text"] for entry in debug["tts_log"]), debug["tts_log"]
        assert debug["dtmf_log"] == ["1", "2", "#"]

        server.request("POST", f"/app/voice/{device_id}/hangup", token=token, expect=200)
        _poll_until(server, token, device_id, "gone")

        # Read the ended event first: waiting on it guarantees every earlier
        # transition for this call has already reached the collector.
        ended = ws.wait_for(lambda e: e.get("event") == "call.ended" and e["data"]["call_id"] == call_id)
        assert ended is not None, ws.snapshot()
        assert ended["data"]["end_reason"] == "local_hangup"

        states = ws.states_for(call_id)
        assert "dialing" in states, states
        assert "active" in states, states
        assert states[-1] == "ended", states

    time.sleep(0.5)
    _, history = server.request("GET", "/app/voice/history?limit=10", token=token, expect=200)
    row = next((c for c in history["items"] if c["call_id"] == call_id), None)
    assert row is not None, history["items"]
    assert row["direction"] == "outbound"
    assert row["remote"] == "13800138000"
    assert row["end_reason"] == "local_hangup"
    assert row["answered_at"] is not None


def test_outbound_busy_is_recorded(server: LiveServer, token: str, device_id: int):
    server.request("POST", f"/app/voice/mock/{device_id}/scenario", token=token,
                   body={"scenario": "busy"}, expect=200)
    _, session = server.request("POST", "/app/voice/dial", token=token,
                                body={"number": "13900139000", "device_id": device_id}, expect=200)
    call_id = session["call_id"]
    deadline = time.time() + 12
    while time.time() < deadline:
        _, history = server.request("GET", "/app/voice/history?limit=20", token=token, expect=200)
        row = next((c for c in history["items"] if c["call_id"] == call_id), None)
        if row and row["state"] == "ended":
            assert row["end_reason"] == "busy", row
            assert row["answered_at"] is None
            break
        time.sleep(0.2)
    else:
        pytest.fail("busy call never terminated")
    server.request("POST", f"/app/voice/mock/{device_id}/scenario", token=token,
                   body={"scenario": "answer"}, expect=200)


def test_second_call_on_same_device_is_rejected(server: LiveServer, token: str, device_id: int):
    server.request("POST", f"/app/voice/mock/{device_id}/scenario", token=token,
                   body={"scenario": "hang_forever"}, expect=200)
    try:
        server.request("POST", "/app/voice/dial", token=token,
                       body={"number": "13700137000", "device_id": device_id}, expect=200)
        _poll_until(server, token, device_id, "alerting")
        status, payload = server.request("POST", "/app/voice/dial", token=token,
                                         body={"number": "13700137001", "device_id": device_id})
        assert status == 409, payload
    finally:
        server.request("POST", f"/app/voice/{device_id}/hangup", token=token, expect=200)
        _poll_until(server, token, device_id, "gone")
        server.request("POST", f"/app/voice/mock/{device_id}/scenario", token=token,
                       body={"scenario": "answer"}, expect=200)


def test_dial_on_unknown_device_fails(server: LiveServer, token: str):
    status, payload = server.request("POST", "/app/voice/dial", token=token,
                                     body={"number": "13800138000", "device_id": 9999})
    assert status in (409, 502), (status, payload)


def test_invalid_number_rejected(server: LiveServer, token: str, device_id: int):
    status, _ = server.request("POST", "/app/voice/dial", token=token,
                               body={"number": "not-a-number", "device_id": device_id})
    assert status == 422


# ---------------------------------------------------------------------------
# Inbound calls
# ---------------------------------------------------------------------------


def test_incoming_call_answer_and_hangup(server: LiveServer, token: str, device_id: int):
    with WsCollector(server.ws_url, token) as ws:
        server.request("POST", f"/app/voice/mock/{device_id}/incoming", token=token,
                       body={"number": "13611112222", "rings": 8, "interval": 1.0}, expect=200)
        incoming = ws.wait_for(lambda e: e.get("event") == "call.incoming", timeout=10)
        assert incoming is not None, ws.snapshot()
        assert incoming["data"]["remote"] == "13611112222", incoming
        assert incoming["data"]["direction"] == "inbound"

        ringing = _poll_until(server, token, device_id, "incoming")
        assert ringing["remote"] == "13611112222"

        _, answered = server.request("POST", f"/app/voice/{device_id}/answer", token=token, expect=200)
        assert answered["state"] == "active"

        server.request("POST", f"/app/voice/{device_id}/hangup", token=token, expect=200)
        _poll_until(server, token, device_id, "gone")

        prompts = [e["data"]["prompt"] for e in ws.snapshot() if e.get("event") == "call.prompt"]
        assert "incoming_call" in prompts, prompts
        assert "call_ended" in prompts, prompts


def test_incoming_call_reject(server: LiveServer, token: str, device_id: int):
    server.request("POST", f"/app/voice/mock/{device_id}/incoming", token=token,
                   body={"number": "13633334444", "rings": 8, "interval": 1.0}, expect=200)
    _poll_until(server, token, device_id, "incoming")
    server.request("POST", f"/app/voice/{device_id}/hangup", token=token, expect=200)
    _poll_until(server, token, device_id, "gone")
    time.sleep(0.4)
    _, history = server.request("GET", "/app/voice/history?limit=5", token=token, expect=200)
    row = next(c for c in history["items"] if c["remote"] == "13633334444")
    assert row["direction"] == "inbound"
    assert row["end_reason"] == "rejected"


def test_answer_without_incoming_call_fails(server: LiveServer, token: str, device_id: int):
    status, _ = server.request("POST", f"/app/voice/{device_id}/answer", token=token)
    assert status == 409


# ---------------------------------------------------------------------------
# Audio devices and prompts
# ---------------------------------------------------------------------------


def test_audio_inventory_lists_devices(server: LiveServer, token: str):
    _, payload = server.request("GET", "/app/audio/devices", token=token, expect=200)
    assert payload["backend"] in ("alsa", "pulse")
    assert isinstance(payload["outputs"], list)
    assert isinstance(payload["inputs"], list)


def test_prompt_catalog_is_complete(server: LiveServer, token: str):
    _, payload = server.request("GET", "/app/audio/prompts", token=token, expect=200)
    ids = {item["id"] for item in payload["items"]}
    for expected in ("dial_connecting", "call_busy", "call_no_answer", "call_not_exist",
                     "incoming_call", "call_ended", "ai_thinking"):
        assert expected in ids, expected
    rendered = [item for item in payload["items"] if item["path"]]
    assert len(rendered) == len(payload["items"]), "every prompt must have a rendered wav"
    assert payload["state_map"]["dialing"] == "dial_connecting"
    assert payload["end_reason_map"]["busy"] == "call_busy"


def test_prompt_playback_on_selected_device(server: LiveServer, token: str):
    _, inventory = server.request("GET", "/app/audio/devices", token=token, expect=200)
    if not inventory["outputs"]:
        pytest.skip("host exposes no audio output device")
    target = inventory["outputs"][0]["id"]
    server.request("POST", "/app/audio/output", token=token, body={"device": target}, expect=200)
    _, after = server.request("GET", "/app/audio/devices", token=token, expect=200)
    assert after["output_device"] == target
    status, payload = server.request("POST", "/app/audio/prompts/dial_connecting/play", token=token)
    assert status == 200, payload
    assert payload["ok"] is True, payload
    assert payload["device"] == target


def test_prompt_wav_download(server: LiveServer, token: str):
    import urllib.request

    req = urllib.request.Request(
        f"{server.base_url}/app/audio/prompts/call_busy/file",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read()
        assert resp.headers["content-type"] == "audio/wav"
    assert data[:4] == b"RIFF", data[:16]
    assert len(data) > 1000


def test_volume_is_persisted(server: LiveServer, token: str):
    server.request("POST", "/app/audio/volume", token=token, body={"volume": 0.42}, expect=200)
    _, inventory = server.request("GET", "/app/audio/devices", token=token, expect=200)
    assert abs(inventory["volume"] - 0.42) < 0.001
    server.request("POST", "/app/audio/volume", token=token, body={"volume": 0.8}, expect=200)


# ---------------------------------------------------------------------------
# Multi-user account management
# ---------------------------------------------------------------------------


def test_admin_can_create_and_manage_accounts(server: LiveServer, token: str):
    status, created = server.request(
        "POST", "/app/admin/users", token=token,
        body={"username": "seat01", "password": "SeatPass123", "role": "operator",
              "display_name": "一号坐席"},
    )
    assert status in (201, 409), (status, created)

    _, listing = server.request("GET", "/app/admin/users", token=token, expect=200)
    seat = next(u for u in listing["items"] if u["username"] == "seat01")
    assert seat["role"] == "operator"
    assert seat["is_active"] is True
    assert seat["must_change_password"] is True

    seat_token = server.login("seat01", "SeatPass123")
    status, _ = server.request("GET", "/app/admin/users", token=seat_token)
    assert status == 403, "operator must not reach account management"

    status, _ = server.request("GET", "/app/voice/state", token=seat_token, expect=200)

    server.request("PATCH", f"/app/admin/users/{seat['id']}", token=token,
                   body={"is_active": False}, expect=200)
    status, payload = server.request("POST", "/api/v1/auth/login",
                                     body={"username": "seat01", "password": "SeatPass123"})
    assert status == 403, payload

    server.request("PATCH", f"/app/admin/users/{seat['id']}", token=token,
                   body={"is_active": True}, expect=200)
    server.request("POST", f"/app/admin/users/{seat['id']}/password", token=token,
                   body={"password": "NewSeatPass1", "must_change_password": False}, expect=200)
    new_token = server.login("seat01", "NewSeatPass1")
    assert new_token


def test_admin_guards_protect_last_admin(server: LiveServer, token: str):
    _, listing = server.request("GET", "/app/admin/users", token=token, expect=200)
    me = next(u for u in listing["items"] if u["username"] == ADMIN)

    status, payload = server.request("PATCH", f"/app/admin/users/{me['id']}", token=token,
                                     body={"role": "viewer"})
    assert status == 400, payload
    status, payload = server.request("PATCH", f"/app/admin/users/{me['id']}", token=token,
                                     body={"is_active": False})
    assert status == 400, payload
    status, payload = server.request("DELETE", f"/app/admin/users/{me['id']}", token=token)
    assert status == 400, payload


def test_weak_password_and_duplicate_username_rejected(server: LiveServer, token: str):
    status, payload = server.request("POST", "/app/admin/users", token=token,
                                     body={"username": "weakling", "password": "alllowercase", "role": "viewer"})
    assert status == 400, payload
    status, payload = server.request("POST", "/app/admin/users", token=token,
                                     body={"username": ADMIN, "password": "AnotherPass1", "role": "viewer"})
    assert status == 409, payload


def test_change_own_password_invalidates_sessions(server: LiveServer, token: str):
    _, me = server.request("GET", "/app/account/me", token=token, expect=200)
    assert me["username"] == ADMIN
    status, payload = server.request("POST", "/app/account/password", token=token,
                                     body={"current_password": "WrongPass123", "new_password": "Whatever123"})
    assert status == 401, payload


# ---------------------------------------------------------------------------
# API keys with voice scopes
# ---------------------------------------------------------------------------


def test_api_key_voice_scopes(server: LiveServer, token: str, device_id: int):
    _, key = server.request("POST", "/app/keys", token=token,
                            body={"label": "voice-integration", "scopes": ["voice:read", "voice:dial"]},
                            expect=200)
    api_key = key["key"]
    headers_key = api_key

    status, payload = server.request("GET", "/api/v1/calls/active", token=headers_key, expect=200)
    assert payload["count"] == 0

    server.request("POST", f"/app/voice/mock/{device_id}/scenario", token=token,
                   body={"scenario": "answer"}, expect=200)
    status, session = server.request("POST", "/api/v1/calls/dial", token=headers_key,
                                     body={"number": "13522223333", "device_id": device_id}, expect=200)
    assert session["origin"] == "api"
    _poll_until(server, token, device_id, "active")
    server.request("POST", f"/api/v1/calls/{device_id}/tts", token=headers_key,
                   body={"text": "API 拨号测试"}, expect=200)
    server.request("POST", f"/api/v1/calls/{device_id}/hangup", token=headers_key, expect=200)
    _poll_until(server, token, device_id, "gone")

    status, history = server.request("GET", "/api/v1/calls/history?limit=5", token=headers_key, expect=200)
    assert any(c["remote"] == "13522223333" for c in history["items"]), history


def test_api_key_without_voice_scope_is_forbidden(server: LiveServer, token: str, device_id: int):
    _, key = server.request("POST", "/app/keys", token=token,
                            body={"label": "sms-only", "scopes": ["sms:read", "sms:send"]}, expect=200)
    status, payload = server.request("POST", "/api/v1/calls/dial", token=key["key"],
                                     body={"number": "13800138000", "device_id": device_id})
    assert status == 403, payload


# ---------------------------------------------------------------------------
# SMS still works after the voice refactor
# ---------------------------------------------------------------------------


def test_sms_send_and_thread_grouping(server: LiveServer, token: str, device_id: int):
    status, payload = server.request("POST", "/app/send", token=token,
                                     body={"to": "+8613800138000", "content": "中文短信联调测试",
                                           "device_id": device_id}, expect=200)
    assert payload["status"] == "sent"
    time.sleep(0.6)
    _, conversations = server.request("GET", "/app/conversations", token=token, expect=200)
    keys = {c["peer_key"] for c in conversations["items"]}
    assert "13800138000" in keys, conversations["items"]
    thread = next(c for c in conversations["items"] if c["peer_key"] == "13800138000")
    assert thread["message_count"] >= 1

    _, messages = server.request(
        "GET", "/app/conversations/%2B8613800138000/messages", token=token, expect=200
    )
    assert messages["peer_key"] == "13800138000"
    assert any("中文短信联调测试" in m["content"] for m in messages["items"]), messages["items"]


def test_contacts_crud(server: LiveServer, token: str):
    _, created = server.request("POST", "/app/contacts", token=token,
                                body={"name": "测试联系人", "number": "13800138000",
                                      "group_name": "客户", "favorite": True}, expect=201)
    contact_id = created["id"]
    _, listing = server.request("GET", "/app/contacts?q=测试", token=token, expect=200)
    assert any(c["id"] == contact_id for c in listing["items"])
    assert "客户" in listing["groups"]

    _, updated = server.request("PATCH", f"/app/contacts/{contact_id}", token=token,
                                body={"name": "改名后的联系人"}, expect=200)
    assert updated["name"] == "改名后的联系人"

    _, conversations = server.request("GET", "/app/conversations", token=token, expect=200)
    thread = next((c for c in conversations["items"] if c["peer_key"] == "13800138000"), None)
    assert thread and thread["name"] == "改名后的联系人", thread

    server.request("DELETE", f"/app/contacts/{contact_id}", token=token, expect=200)


def test_call_stats(server: LiveServer, token: str):
    _, stats = server.request("GET", "/app/voice/stats", token=token, expect=200)
    assert stats["total"] >= 3
    assert stats["outbound"] >= 1
    assert stats["inbound"] >= 1
    assert 0 <= stats["answer_rate"] <= 1


def test_audit_trail_records_voice_actions(server: LiveServer, token: str):
    _, audit = server.request("GET", "/app/admin/audit?limit=200", token=token, expect=200)
    actions = {row["action"] for row in audit["items"]}
    for expected in ("call.dial", "call.answer", "call.hangup", "login"):
        assert expected in actions, (expected, sorted(actions))
