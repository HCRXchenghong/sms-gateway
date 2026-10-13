"""AI channel integration tests against a live server.

These run against the offline mock provider so they pass without network
access or an API key, while still exercising the real HTTP surface, the
settings plumbing and the in-call AI path.
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

import pytest

from tests.live_server import LiveServer
from tests.test_live_integration import ADMIN, ADMIN_PW, WsCollector, _poll_until


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
    _, payload = server.request("GET", "/app/devices", token=token, expect=200)
    online = [d for d in payload["items"] if d["status"] == "online"]
    assert online
    return int(online[0]["id"])


@pytest.fixture(autouse=True)
def _reset(server: LiveServer, token: str):
    yield
    _, devices = server.request("GET", "/app/devices", token=token, expect=200)
    for device in devices["items"]:
        server.request("POST", f"/app/voice/{device['id']}/hangup", token=token)
        server.request("POST", f"/app/voice/mock/{device['id']}/scenario",
                       token=token, body={"scenario": "answer"})
    server.request("PUT", "/app/admin/settings", token=token,
                   body={"settings": {"ai.auto_reply_sms": False}})
    deadline = time.time() + 8
    while time.time() < deadline:
        _, state = server.request("GET", "/app/voice/state", token=token, expect=200)
        if not state["active"]:
            return
        time.sleep(0.15)


def _post_raw(server: LiveServer, path: str, token: str, body: dict):
    req = urllib.request.Request(
        f"{server.base_url}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=40) as resp:
        return resp.headers, resp.read()


def _upload_wav(server: LiveServer, token: str, wav: bytes) -> dict:
    boundary = "----smsgwtestboundary"
    head = (
        "--" + boundary + "\r\n"
        + 'Content-Disposition: form-data; name="file"; filename="sample.wav"\r\n'
        + "Content-Type: audio/wav\r\n\r\n"
    )
    payload = head.encode("utf-8") + wav + ("\r\n--" + boundary + "--\r\n").encode("utf-8")
    req = urllib.request.Request(
        f"{server.base_url}/app/ai/asr",
        data=payload,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_ai_status_reports_offline_provider(server: LiveServer, token: str):
    _, payload = server.request("GET", "/app/ai/status", token=token, expect=200)
    assert payload["provider"] in ("mock", "openai")
    assert payload["loaded"] is True
    for value in payload["config"].values():
        assert "sk-" not in str(value)


def test_ai_chat_echoes_and_reports_latency(server: LiveServer, token: str):
    _, payload = server.request(
        "POST", "/app/ai/chat", token=token,
        body={"message": "请确认网关在线", "history": [{"role": "user", "content": "你好"}]},
        expect=200,
    )
    assert payload["reply"]
    assert "请确认网关在线" in payload["reply"]
    assert payload["elapsed_ms"] >= 0
    assert payload["provider"] in ("mock", "openai")


def test_ai_tts_returns_wav_bytes(server: LiveServer, token: str):
    headers, body = _post_raw(server, "/app/ai/tts", token, {"text": "正在接通，请稍候"})
    assert headers["content-type"].startswith("audio/")
    assert body[:4] == b"RIFF", body[:16]
    assert len(body) > 2000


def test_ai_asr_roundtrip(server: LiveServer, token: str):
    """Synthesise audio then feed it back through ASR."""
    _, wav = _post_raw(server, "/app/ai/tts", token, {"text": "转写回环测试"})
    result = _upload_wav(server, token, wav)
    assert result["text"]
    assert result["bytes"] == len(wav)


def test_ai_health_is_admin_only(server: LiveServer, token: str):
    _, health = server.request("GET", "/app/ai/health", token=token, expect=200)
    assert health["provider"] in ("mock", "openai")
    for channel in ("chat", "tts"):
        value = health[channel]
        assert value in ("ok", "empty") or value.startswith("error"), health

    server.request("POST", "/app/admin/users", token=token,
                   body={"username": "aioperator", "password": "AiOperPass1", "role": "operator"})
    op_token = server.login("aioperator", "AiOperPass1")
    status, _ = server.request("GET", "/app/ai/health", token=op_token)
    assert status == 403
    server.request("POST", "/app/ai/chat", token=op_token, body={"message": "hi"}, expect=200)


def test_render_spoken_prompt_replaces_tone(server: LiveServer, token: str):
    spoken = Path(server.root) / "prompts" / "dial_connecting.tts.wav"
    if spoken.exists():
        spoken.unlink()

    _, payload = server.request("POST", "/app/ai/tts/prompt/dial_connecting", token=token, expect=200)
    assert payload["prompt"] == "dial_connecting"
    assert payload["bytes"] > 1000
    assert spoken.exists()

    _, catalog = server.request("GET", "/app/audio/prompts", token=token, expect=200)
    entry = next(i for i in catalog["items"] if i["id"] == "dial_connecting")
    assert entry["has_spoken_version"] is True
    assert entry["path"].endswith(".tts.wav")

    _, played = server.request("POST", "/app/audio/prompts/dial_connecting/play", token=token)
    assert played["ok"] is True, played
    assert played["path"].endswith(".tts.wav")

    # key_press has no spoken text, so rendering it must be refused cleanly.
    status, detail = server.request("POST", "/app/ai/tts/prompt/key_press", token=token)
    assert status == 400, (status, detail)


def test_ai_speak_into_active_call(server: LiveServer, token: str, device_id: int):
    server.request("POST", f"/app/voice/mock/{device_id}/scenario", token=token,
                   body={"scenario": "answer"}, expect=200)
    _, session = server.request("POST", "/app/voice/dial", token=token,
                                body={"number": "13800001111", "device_id": device_id}, expect=200)
    call_id = session["call_id"]
    _poll_until(server, token, device_id, "active")

    _, spoken = server.request("POST", f"/app/voice/{device_id}/ai/speak", token=token,
                               body={"text": "对方问什么时候到货", "engine": "module"}, expect=200)
    assert spoken["channel"] == "module_tts"
    assert spoken["reply"]

    server.request("POST", f"/app/voice/{device_id}/hangup", token=token, expect=200)
    _poll_until(server, token, device_id, "gone")
    time.sleep(0.5)

    _, history = server.request("GET", "/app/voice/history?limit=20", token=token, expect=200)
    row = next(c for c in history["items"] if c["call_id"] == call_id)
    transcript = row["transcript"]
    assert any(t["role"] == "user" and "什么时候到货" in t["content"] for t in transcript), transcript
    assert any(t["role"] == "assistant" and t["content"] for t in transcript), transcript


def test_switching_provider_at_runtime(server: LiveServer, token: str):
    server.request("PUT", "/app/admin/settings", token=token,
                   body={"settings": {"ai.provider": "mock", "ai.chat.model": "qwen-plus",
                                      "ai.tts.voice": "longxiaochun"}}, expect=200)
    _, status_payload = server.request("GET", "/app/ai/status", token=token, expect=200)
    assert status_payload["provider"] == "mock"
    assert status_payload["config"]["ai.chat.model"] == "qwen-plus"
    assert status_payload["config"]["ai.tts.voice"] == "longxiaochun"

    server.request("PUT", "/app/admin/settings", token=token,
                   body={"settings": {"ai.chat.api_key": "sk-live-fake-key-for-test"}}, expect=200)
    _, after = server.request("GET", "/app/ai/status", token=token, expect=200)
    assert "sk-live" not in str(after["config"].get("ai.chat.api_key", ""))

    # An explicit provider choice wins over auto-detection, so mock stays mock.
    _, still_mock = server.request("GET", "/app/ai/status", token=token, expect=200)
    assert still_mock["provider"] == "mock"

    # Point the real client at a closed port so the failure is deterministic
    # and does not depend on outbound network access.
    server.request("PUT", "/app/admin/settings", token=token,
                   body={"settings": {"ai.provider": "auto",
                                      "ai.chat.base_url": "http://127.0.0.1:9/v1",
                                      "ai.asr.base_url": "http://127.0.0.1:9/v1",
                                      "ai.tts.base_url": "http://127.0.0.1:9/v1"}}, expect=200)
    _, openai_mode = server.request("GET", "/app/ai/status", token=token, expect=200)
    assert openai_mode["provider"] == "openai"
    assert openai_mode["config"]["ai.chat.api_key"], "key presence must be reported"
    assert "sk-live-fake-key-for-test" not in str(openai_mode["config"]["ai.chat.api_key"])

    _, health = server.request("GET", "/app/ai/health", token=token, expect=200)
    assert health["provider"] == "openai"
    assert health["chat"].startswith("error"), health
    assert health["tts"].startswith("error"), health

    server.request("PUT", "/app/admin/settings", token=token,
                   body={"settings": {"ai.chat.api_key": "", "ai.provider": "mock",
                                      "ai.chat.base_url": "https://api.openai.com/v1",
                                      "ai.asr.base_url": "https://api.openai.com/v1",
                                      "ai.tts.base_url": "https://api.openai.com/v1"}}, expect=200)
    _, restored = server.request("GET", "/app/ai/status", token=token, expect=200)
    assert restored["provider"] == "mock"
    server.request("POST", "/app/ai/chat", token=token, body={"message": "恢复检查"}, expect=200)


def test_api_key_ai_scope(server: LiveServer, token: str):
    _, key = server.request("POST", "/app/keys", token=token,
                            body={"label": "ai-client", "scopes": ["ai:use"]}, expect=200)
    _, payload = server.request("POST", "/api/v1/ai/chat", token=key["key"],
                                body={"message": "第三方系统调用"}, expect=200)
    assert "第三方系统调用" in payload["reply"]

    _, sms_key = server.request("POST", "/app/keys", token=token,
                                body={"label": "sms-client", "scopes": ["sms:send"]}, expect=200)
    status, _ = server.request("POST", "/api/v1/ai/chat", token=sms_key["key"],
                               body={"message": "should be refused"})
    assert status == 403


def test_sms_auto_reply(server: LiveServer, token: str, device_id: int):
    server.request("PUT", "/app/admin/settings", token=token,
                   body={"settings": {"ai.auto_reply_sms": True}}, expect=200)
    _, status_payload = server.request("GET", "/app/admin/settings", token=token, expect=200)
    assert status_payload["settings"]["ai.auto_reply_sms"] is True

    with WsCollector(server.ws_url, token) as ws:
        server.request("POST", f"/app/sms/mock/{device_id}/incoming", token=token,
                       body={"sender": "13900001234", "content": "你们的设备还在保修期内吗"}, expect=200)
        event = ws.wait_for(lambda e: e.get("event") == "sms.auto_reply", timeout=25)
        assert event is not None, ws.snapshot()
        assert event["data"]["recipient"] == "13900001234"
        assert event["data"]["content"]

    time.sleep(0.5)
    _, outbox = server.request("GET", f"/app/outbox?device_id={device_id}", token=token, expect=200)
    replies = [m for m in outbox["items"] if m["recipient"] == "13900001234"]
    assert replies, outbox["items"]
    assert len(replies) <= 4, replies


def test_auto_reply_can_be_disabled(server: LiveServer, token: str, device_id: int):
    server.request("PUT", "/app/admin/settings", token=token,
                   body={"settings": {"ai.auto_reply_sms": False}}, expect=200)
    server.request("POST", f"/app/sms/mock/{device_id}/incoming", token=token,
                   body={"sender": "13977778888", "content": "关闭自动回复后的测试"}, expect=200)
    time.sleep(1.5)
    _, outbox = server.request("GET", f"/app/outbox?device_id={device_id}", token=token, expect=200)
    assert not [m for m in outbox["items"] if m["recipient"] == "13977778888"], outbox["items"]
    _, inbox = server.request("GET", f"/app/inbox?device_id={device_id}", token=token, expect=200)
    assert any(m["sender"] == "13977778888" for m in inbox["items"]), inbox["items"]
