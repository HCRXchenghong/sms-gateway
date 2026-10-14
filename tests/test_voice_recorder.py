"""CallRecorder decides when a call is captured and where the file lands.

The player is stubbed: what matters here is the lifecycle (active starts the
capture, ended stops and persists it) and that a disabled or empty capture
never produces a CDR entry pointing at nothing.
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.voice.recorder import CallRecorder


class StubPlayer:
    def __init__(self):
        self.started = []
        self.stopped = []
        self.write_bytes = b"RIFFfakewav"

    async def start_recording(self, path, device=None):
        self.started.append(Path(path))
        return SimpleNamespace(path=Path(path), device=device, process=None)

    async def stop_recording(self, handle):
        self.stopped.append(handle.path)
        if self.write_bytes:
            handle.path.write_bytes(self.write_bytes)
        return handle.path


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def make_recorder(tmp_path, enabled=True, player=None):
    player = player or StubPlayer()
    events = []
    persisted = []

    async def publish(name, payload):
        events.append((name, payload))

    recorder = CallRecorder(
        player,
        enabled=lambda: enabled,
        publish=publish,
        persist=lambda call_id, path: persisted.append((call_id, path)),
        record_dir=tmp_path,
    )
    return recorder, player, events, persisted


def test_active_state_starts_capture_and_ended_persists_it(tmp_path):
    recorder, player, events, persisted = make_recorder(tmp_path)

    started = run(recorder.on_state("active", {"call_id": "c1", "device_id": 3}))
    assert started == tmp_path / "c1.wav"
    assert recorder.is_recording("c1")

    # A duplicate active transition must not open a second capture.
    assert run(recorder.on_state("active", {"call_id": "c1"})) is None
    assert len(player.started) == 1

    saved = run(recorder.on_state("ended", {"call_id": "c1"}))
    assert saved == tmp_path / "c1.wav"
    assert not recorder.is_recording("c1")
    assert persisted == [("c1", str(tmp_path / "c1.wav"))]
    assert [e[0] for e in events] == ["call.recording", "call.recording"]
    assert events[0][1]["phase"] == "started"
    assert events[1][1]["phase"] == "stopped"
    assert events[1][1]["size"] == len(b"RIFFfakewav")


def test_disabled_gate_never_opens_a_capture(tmp_path):
    recorder, player, events, persisted = make_recorder(tmp_path, enabled=False)

    assert run(recorder.on_state("active", {"call_id": "c1"})) is None
    assert player.started == []
    assert run(recorder.on_state("ended", {"call_id": "c1"})) is None
    assert persisted == []
    # No capture is opened and nothing is persisted, but the console is told
    # that recording was wanted and could not happen. Silently skipping it is
    # how a "forced recording" policy ends up capturing nothing unnoticed.
    assert [e[1]["phase"] for e in events] == ["unavailable"]
    assert events[0][1]["failed_reason"] == "audio_capture_disabled"
    assert events[0][1]["recording"] is False


def test_empty_capture_is_dropped_instead_of_persisted(tmp_path):
    recorder, player, events, persisted = make_recorder(tmp_path)
    player.write_bytes = b""

    run(recorder.on_state("active", {"call_id": "c2"}))
    saved = run(recorder.on_state("ended", {"call_id": "c2"}))

    assert saved is None
    assert persisted == []
    assert events[-1][1]["phase"] == "empty"


def test_stop_all_flushes_captures_left_open_by_a_crash(tmp_path):
    recorder, player, events, persisted = make_recorder(tmp_path)
    run(recorder.on_state("active", {"call_id": "c3"}))
    run(recorder.on_state("active", {"call_id": "c4"}))

    run(recorder.stop_all())

    assert recorder.active_call_ids == []
    assert sorted(call_id for call_id, _ in persisted) == ["c3", "c4"]
    assert len(player.stopped) == 2


def test_call_lifecycle_wires_capture_without_an_active_prompt(
    client, admin_headers, online_device_id, monkeypatch, tmp_path
):
    """Exercise real call transitions and recording download, stubbing only PCM capture.

    Active calls have no generic prompt. A previous early return in the prompt
    hook skipped recorder.start entirely; unit tests of CallRecorder missed it.
    """
    import wave
    from app.config import settings
    from tests.conftest import wait_for_call_state, clear_call

    audio = client.app.state.audio
    recorder = client.app.state.recorder
    monkeypatch.setattr(recorder, '_enabled', lambda: True)
    monkeypatch.setattr(recorder, '_dir', tmp_path)
    monkeypatch.setattr(settings, 'audio_record_dir', tmp_path)

    async def start_capture(path, device=None):
        with wave.open(str(path), 'wb') as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(8000)
            f.writeframes(b'\x00\x00' * 800)
        return SimpleNamespace(path=Path(path), device=device)

    async def stop_capture(handle):
        return handle.path

    monkeypatch.setattr(audio, 'start_recording', start_capture)
    monkeypatch.setattr(audio, 'stop_recording', stop_capture)
    try:
        response = client.post('/app/voice/dial', headers=admin_headers,
                               json={'device_id': online_device_id, 'number': '13800000999'})
        assert response.status_code == 200, response.text
        active = wait_for_call_state(client, admin_headers, online_device_id, 'active')
        call_id = active['call_id']
        assert recorder.is_recording(call_id)
        clear_call(client, admin_headers, online_device_id)
        assert not recorder.is_recording(call_id)
        url = f'/app/voice/calls/{call_id}/recording'
        response = client.get(url, headers=admin_headers)
        assert response.status_code == 200, response.text
        assert response.content.startswith(b'RIFF')
        assert len(response.content) > 44
        assert client.get(url).status_code == 401
        # Stored paths outside the configured directory must never be served.
        from app import repo_calls
        outside = tmp_path.parent / 'outside-recording.wav'
        outside.write_bytes(b'private')
        repo_calls.update_call(call_id, recording_path=str(outside))
        assert client.get(url, headers=admin_headers).status_code == 404
    finally:
        clear_call(client, admin_headers, online_device_id)


# ---------------------------------------------------------------------------
# Recording policy: capture and announcement are two independent switches
# ---------------------------------------------------------------------------


def make_policy_recorder(tmp_path, *, policy, default_enabled=True, announce_default=False,
                         speak=None, capture_enabled=True):
    player = StubPlayer()
    events = []
    meta = []
    spoken = []

    async def publish(name, payload):
        events.append((name, payload))

    async def speak_into_call(device_id, text):
        spoken.append((device_id, text))
        if speak == "fail":
            raise RuntimeError("module rejected the TTS command")

    recorder = CallRecorder(
        player,
        enabled=lambda: capture_enabled,
        publish=publish,
        persist=lambda call_id, path: None,
        record_dir=tmp_path,
        policy=lambda: policy,
        default_enabled=lambda: default_enabled,
        announce_default=lambda: announce_default,
        speak_into_call=speak_into_call,
        persist_meta=lambda call_id, payload: meta.append((call_id, payload)),
    )
    return recorder, player, events, meta, spoken


def test_forced_policy_overrides_an_operator_asking_not_to_record(tmp_path):
    recorder, player, _, _, _ = make_policy_recorder(tmp_path, policy="forced")
    run(recorder.on_state("active", {"call_id": "f1", "device_id": 2, "record": False}))
    assert recorder.is_recording("f1")
    assert len(player.started) == 1
    decision = recorder.decide(requested=False)
    assert decision.record is True and decision.can_change is False


def test_disabled_policy_refuses_to_record_even_when_asked(tmp_path):
    recorder, player, events, _, _ = make_policy_recorder(tmp_path, policy="disabled")
    assert run(recorder.on_state("active", {"call_id": "d1", "device_id": 2, "record": True})) is None
    assert player.started == []
    assert not recorder.is_recording("d1")
    assert [e[1]["phase"] for e in events] == ["skipped"]
    assert recorder.decide(requested=True).record is False


def test_optional_policy_follows_the_request_and_the_default(tmp_path):
    recorder, player, _, _, _ = make_policy_recorder(tmp_path, policy="optional", default_enabled=False)
    assert recorder.decide().record is False
    assert recorder.decide(requested=True).record is True
    run(recorder.on_state("active", {"call_id": "o1", "device_id": 2, "record": True}))
    assert recorder.is_recording("o1")
    run(recorder.stop("o1"))
    run(recorder.on_state("active", {"call_id": "o2", "device_id": 2}))
    assert not recorder.is_recording("o2")


def test_announced_recording_plays_a_plain_notice_into_the_call(tmp_path):
    recorder, _, events, meta, spoken = make_policy_recorder(
        tmp_path, policy="forced", announce_default=True
    )
    run(recorder.on_state("active", {"call_id": "a1", "device_id": 7}))
    assert spoken == [(7, "本次通话已开启录音")]
    # The wording states the fact of recording and claims nothing about it.
    text = spoken[0][1]
    for banned in ("证词", "证据", "法律", "法庭", "有效"):
        assert banned not in text
    assert [e[1]["phase"] for e in events][:3] == ["started", "announced"]
    stored = dict(meta[-1][1])
    assert stored["recording_announced"] is True
    assert stored["recording_notice_ok"] is True
    assert stored["recording_notice"] == text


def test_mid_call_notice_uses_its_own_wording(tmp_path):
    recorder, _, _, _, spoken = make_policy_recorder(
        tmp_path, policy="optional", announce_default=True
    )
    run(recorder.on_state("active", {"call_id": "m1", "device_id": 4}))
    run(recorder.stop("m1"))
    spoken.clear()
    run(recorder.set_recording("m1", True, device_id=4))
    assert spoken == [(4, "录音已开启，接下来的通话将被记录")]


def test_a_notice_that_failed_to_play_is_not_recorded_as_announced(tmp_path):
    recorder, _, events, meta, spoken = make_policy_recorder(
        tmp_path, policy="forced", announce_default=True, speak="fail"
    )
    run(recorder.on_state("active", {"call_id": "n1", "device_id": 9}))
    assert spoken and recorder.is_recording("n1")
    assert "announced" not in [e[1]["phase"] for e in events]
    assert "announce_failed" in [e[1]["phase"] for e in events]
    stored = dict(meta[-1][1])
    assert stored["recording_notice_ok"] is False
    assert "module rejected" in stored["recording_notice_error"]


def test_silent_recording_says_nothing_but_still_shows_in_the_console(tmp_path):
    recorder, _, events, _, spoken = make_policy_recorder(
        tmp_path, policy="optional", announce_default=False
    )
    run(recorder.on_state("active", {"call_id": "s1", "device_id": 5}))
    assert spoken == []
    assert recorder.is_recording("s1")
    assert [e[1]["phase"] for e in events] == ["started"]


def test_operator_toggle_is_refused_under_a_forced_policy(tmp_path):
    recorder, _, _, _, _ = make_policy_recorder(tmp_path, policy="forced")
    run(recorder.on_state("active", {"call_id": "t1", "device_id": 1}))
    result = run(recorder.set_recording("t1", False, device_id=1))
    assert result["ok"] is False
    assert result["recording"] is True
    assert recorder.is_recording("t1")


def test_forced_recording_without_a_capture_device_is_reported_loudly(tmp_path):
    recorder, _, events, _, _ = make_policy_recorder(
        tmp_path, policy="forced", capture_enabled=False
    )
    run(recorder.on_state("active", {"call_id": "x1", "device_id": 1}))
    assert [e[1]["phase"] for e in events] == ["unavailable"]
    assert events[0][1]["reason"] == "policy_forced"
    assert events[0][1]["failed_reason"] == "audio_capture_disabled"
