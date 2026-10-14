"""Call orchestration on top of a live call.

Everything hardware-bound is faked: the call manager, the AI engine, the sound
card and the task table. What is under test is the wiring between them, because
that is where a call quietly loses its translation, its minutes or its goodbye.
"""
from __future__ import annotations

import asyncio
import math
import struct
import time
import uuid
from types import SimpleNamespace

import pytest

from app import voice as _voice_pkg  # noqa: F401  (package import for monkeypatching)
from app.config import settings
from app.voice import callflow as callflow_module
from app.voice.callflow import CallFlow, CallFlowError
from app.voice.state import CallDirection, CallSession, CallState, EndReason


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def settle(times: int = 30) -> None:
    """Let queued tasks run, including the thread-pool hops the repo writes use."""
    for _ in range(times):
        await asyncio.sleep(0.002)


async def wait_for(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was never met")


def tone(frame_ms: int = 20, *, rate: int | None = None, amplitude: int = 6000) -> bytes:
    rate = int(rate or settings.audio_sample_rate)
    samples = int(rate * frame_ms / 1000)
    out = bytearray()
    for i in range(samples):
        value = int(amplitude * math.sin(2 * math.pi * 440 * i / rate))
        out += struct.pack("<h", max(-32768, min(32767, value)))
    return bytes(out)


SILENT_FRAME = b"\x00\x00" * int(settings.audio_sample_rate * 0.02)


class FakeCalls:
    """Stands in for CallManager: sessions, dialling and the audio into the call."""

    def __init__(self, *, dial_error=None):
        self.sessions: dict[str, CallSession] = {}
        self.dialed: list[tuple] = []
        self.answered: list[int] = []
        self.hangups: list[int] = []
        self.spoken: list[tuple] = []
        self.dial_error = dial_error

    def add(self, call_id, device_id=1, direction="outbound", remote="+8613800000000",
            state=CallState.ACTIVE):
        session = CallSession(
            call_id=call_id, device_id=device_id,
            direction=CallDirection(direction), remote=remote, state=state,
        )
        self.sessions[call_id] = session
        return session

    def session_by_id(self, call_id):
        return self.sessions.get(call_id)

    def session_for_device(self, device_id):
        for session in self.sessions.values():
            if session.device_id == device_id and session.state not in (CallState.IDLE, CallState.ENDED):
                return session
        return None

    async def dial(self, device_id, number, *, owner_user_id=None, origin=None):
        self.dialed.append((device_id, number, origin))
        if self.dial_error is not None:
            raise self.dial_error
        call_id = uuid.uuid4().hex[:8]
        return self.add(call_id, device_id, "outbound", number, CallState.DIALING)

    async def answer(self, device_id):
        self.answered.append(device_id)
        for session in self.sessions.values():
            if session.device_id == device_id and session.state == CallState.INCOMING:
                session.state = CallState.ACTIVE
                session.answered_at = int(time.time())
                return session
        raise RuntimeError("no incoming call")

    async def hangup(self, device_id):
        self.hangups.append(device_id)
        for session in self.sessions.values():
            if session.device_id == device_id and session.state != CallState.ENDED:
                session.state = CallState.ENDED
                session.end_reason = EndReason.LOCAL_HANGUP

    async def speak(self, device_id, text, mode=None):
        self.spoken.append((device_id, text))


class FakeEngine:
    """Scripted answers, so a conversation can be driven deterministically."""

    def __init__(self, *, chat=None, transcripts=None, translations=None, summary_result=None):
        self.chat_replies = list(chat or [])
        self.transcripts = list(transcripts or [])
        self.translations = list(translations or [])
        self.summary_result = summary_result or {
            "summary": "对方确认今天发货，约定下午发出。",
            "key_facts": ["对方说今天下午就发"],
            "agreements": ["今天下午发货"],
            "todos": [],
            "purpose_achieved": True,
            "purpose_evidence": "今天下午就发",
            "unconfirmed": [],
        }
        self.prompts: list[dict] = []
        self.summarized: list[dict] = []

    async def chat(self, text, *, history=None, system=None, temperature=0.4,
                   max_tokens=800, call_id=None):
        self.prompts.append({"text": text, "system": system})
        if self.chat_replies:
            return self.chat_replies.pop(0)
        return ('{"say": "", "done": true, "achieved": null, "evidence": "", '
                '"result": "无更多回复"}')

    async def transcribe(self, audio, *, filename="audio.wav", language=None, call_id=None):
        if self.transcripts:
            return self.transcripts.pop(0)
        return "我听到了"

    async def translate(self, text, *, source=None, target="", glossary=None, call_id=None):
        if self.translations:
            return self.translations.pop(0)
        return text

    async def synthesize(self, text, *, voice=None, call_id=None):
        return b"RIFFfakewavdata"

    async def summarize(self, transcript, *, purpose=None, template=None, call_id=None):
        self.summarized.append({"lines": len(transcript), "purpose": purpose, "template": template})
        return dict(self.summary_result)


class FakePlayer:
    def __init__(self):
        self.played: list[tuple] = []

    async def play_bytes(self, data, *, tag="clip", wait=True, timeout=30.0, **kwargs):
        self.played.append((data, tag))
        return SimpleNamespace(ok=True, error=None)


class FakeCapture:
    """The shared capture hub: one stream, several subscribers."""

    def __init__(self, *, available=True):
        self.available = available
        self.subscribers: list[str] = []
        self.sinks: dict[str, object] = {}
        self.released: list[str] = []

    async def subscribe(self, name, *, on_frames, on_stop=None, device=None, rate=None):
        if not self.available:
            return False
        self.subscribers.append(name)
        self.sinks[name] = on_frames
        return True

    async def release(self, name):
        self.released.append(name)
        self.sinks.pop(name, None)
        if name in self.subscribers:
            self.subscribers.remove(name)

    def push(self, name, pcm):
        self.sinks[name](pcm)

    def stats(self):
        return [{"subscribers": list(self.subscribers)}]


class FakeTasks:
    """The ai_call_tasks table, in memory."""

    def __init__(self):
        self.rows: dict[int, dict] = {}
        self._next = 1

    def create_task(self, **fields):
        task_id = self._next
        self._next += 1
        row = {"id": task_id, "status": "queued", "created_at": int(time.time())}
        row.update({k: v for k, v in fields.items() if v is not None})
        self.rows[task_id] = row
        return task_id

    def get_task(self, task_id):
        row = self.rows.get(int(task_id))
        return dict(row) if row else None

    def get_task_by_call(self, call_id):
        for row in self.rows.values():
            if row.get("call_id") == call_id:
                return dict(row)
        return None

    def find_by_reference(self, reference):
        for row in self.rows.values():
            if row.get("reference") == reference:
                return dict(row)
        return None

    def update_task(self, task_id, **fields):
        row = self.rows.get(int(task_id))
        if row is None:
            return False
        for key, value in fields.items():
            if value is None:
                continue
            row[key] = value
        return True

    def list_pending(self, limit=20):
        return [dict(r) for r in self.rows.values() if r.get("status") == "queued"][:limit]

    def list_tasks(self, limit=50, status=None, owner_user_id=None):
        return [dict(r) for r in self.rows.values()]


class FakeModems:
    def __init__(self, device_id=1):
        self.device_id = device_id
        self.picked: list = []

    async def pick_call_device(self, device_id=None):
        self.picked.append(device_id)
        return int(device_id or self.device_id)


class FakeCallRepo:
    """Records what would have been written to the call row."""

    def __init__(self):
        self.updates: list[tuple] = []

    def update_call(self, call_id, **fields):
        self.updates.append((call_id, fields))


def make_setting(**overrides):
    def setting(key, default):
        return overrides[key] if key in overrides else default
    return setting


def make_flow(*, calls=None, engine=None, capture=None, player=None, tasks=None,
              modems=None, setting=None, repo_calls=None):
    calls = calls if calls is not None else FakeCalls()
    engine = engine if engine is not None else FakeEngine()
    capture = capture if capture is not None else FakeCapture()
    player = player if player is not None else FakePlayer()
    tasks = tasks if tasks is not None else FakeTasks()
    modems = modems if modems is not None else FakeModems()
    repo = repo_calls if repo_calls is not None else FakeCallRepo()
    events: list[tuple] = []

    async def publish(name, payload):
        events.append((name, payload))

    async def speak_into_call(device_id, text):
        calls.spoken.append((device_id, text))

    flow = CallFlow(
        calls=calls, engine=engine, publish=publish, speak_into_call=speak_into_call,
        player=player, capture=capture, modems=modems,
        setting=setting or make_setting(), tasks=tasks,
    )
    flow._poll_seconds = 0.02
    previous_repo = callflow_module.repo_calls
    callflow_module.repo_calls = repo
    return SimpleNamespace(flow=flow, calls=calls, engine=engine, capture=capture,
                           player=player, tasks=tasks, modems=modems, repo=repo,
                           events=events, played=player.played, spoken=calls.spoken,
                           restore=lambda: setattr(callflow_module, "repo_calls", previous_repo))


# -- inbound policy ----------------------------------------------------------


def test_manual_inbound_leaves_the_phone_ringing():
    calls = FakeCalls()
    f = make_flow(calls=calls, setting=make_setting(**{"inbound.mode": "manual"}))
    session = calls.add("c1", state=CallState.INCOMING)
    run(f.flow.on_state(1, "incoming", session.to_dict()))
    run(settle())
    assert calls.answered == []


def test_auto_answer_picks_up_after_the_configured_delay():
    calls = FakeCalls()
    f = make_flow(calls=calls, setting=make_setting(**{
        "inbound.mode": "auto_answer", "inbound.auto_answer_delay_seconds": 0.01}))
    session = calls.add("c1", state=CallState.INCOMING)

    async def scenario():
        await f.flow.on_state(1, "incoming", session.to_dict())
        await asyncio.sleep(0.05)
        await settle()

    run(scenario())
    assert calls.answered == [1]
    assert [name for name, _ in f.events if name == "call.auto_answer"]


def test_auto_answer_ai_speaks_the_purpose_opener():
    calls = FakeCalls()
    f = make_flow(calls=calls, engine=FakeEngine(chat=[
        '{"say": "您好，这里是自动接听。", "done": false, "achieved": null, "evidence": "", "result": ""}',
    ]), setting=make_setting(**{
        "inbound.mode": "auto_answer_ai",
        "inbound.auto_answer_delay_seconds": 0.0,
        "inbound.ai_purpose": "问清对方的来电目的",
    }))
    session = calls.add("c1", state=CallState.INCOMING)

    async def scenario():
        await f.flow.on_state(1, "incoming", session.to_dict())
        await asyncio.sleep(0.05)
        session.state = CallState.ACTIVE
        await f.flow.on_state(1, "active", session.to_dict())
        await settle(40)

    run(scenario())
    assert calls.answered == [1]
    assert calls.spoken == [(1, "您好，这里是自动接听。")]
    runtime = f.flow._runtimes["c1"]
    assert runtime.agent is not None
    assert runtime.agent.purpose == "问清对方的来电目的"


def test_an_operator_who_picks_up_first_cancels_the_auto_answer():
    calls = FakeCalls()
    f = make_flow(calls=calls, setting=make_setting(**{
        "inbound.mode": "auto_answer", "inbound.auto_answer_delay_seconds": 0.05}))
    session = calls.add("c1", state=CallState.INCOMING)

    async def scenario():
        await f.flow.on_state(1, "incoming", session.to_dict())
        # The operator answers on the phone before the delay expires.
        session.state = CallState.ACTIVE
        await asyncio.sleep(0.1)
        await settle()

    run(scenario())
    assert calls.answered == []


def test_an_armed_inbound_task_answers_a_matching_number():
    calls = FakeCalls()
    tasks = FakeTasks()
    f = make_flow(calls=calls, tasks=tasks, engine=FakeEngine(chat=[
        '{"say": "您好，我帮您确认一下。", "done": false, "achieved": null, "evidence": "", "result": ""}',
    ]), setting=make_setting(**{"inbound.auto_answer_delay_seconds": 0.0}))

    async def scenario():
        result = await f.flow.submit_task(
            purpose="确认回访时间", number="+8613800000000", direction="inbound")
        assert result["created"] is True
        other = calls.add("other", remote="+8619999999999", state=CallState.INCOMING)
        await f.flow.on_state(1, "incoming", other.to_dict())
        await settle()
        assert calls.answered == [], "a number the task did not name must ring on"
        wanted = calls.add("c2", remote="+8613800000000", state=CallState.INCOMING)
        await f.flow.on_state(1, "incoming", wanted.to_dict())
        await asyncio.sleep(0.05)
        wanted.state = CallState.ACTIVE
        await f.flow.on_state(1, "active", wanted.to_dict())
        await settle(40)

    run(scenario())
    assert calls.answered == [1]
    assert calls.spoken == [(1, "您好，我帮您确认一下。")]
    assert tasks.rows[1]["status"] == "in_call"
    assert tasks.rows[1]["call_id"] == "c2"


# -- translation -------------------------------------------------------------


def test_subtitle_mode_transcribes_and_translates_the_far_end():
    calls = FakeCalls()
    f = make_flow(calls=calls, engine=FakeEngine(transcripts=["Where is my order?"],
                                                 translations=["我的订单在哪里？"]),
                  setting=make_setting(**{"translate.mode": "subtitle"}))
    session = calls.add("c1", state=CallState.ACTIVE)

    async def scenario():
        await f.flow.on_state(1, "active", session.to_dict())
        await settle()
        await f.flow._on_utterance(f.flow._runtimes["c1"], b"audio", 1.0)
        await settle(30)

    run(scenario())
    names = [name for name, _ in f.events]
    assert "call.transcript" in names
    assert "call.translation" in names
    transcript = [p for n, p in f.events if n == "call.transcript"][0]["entry"]
    assert transcript == {"role": "peer", "content": "Where is my order?",
                          "at": transcript["at"]}
    translation = [p for n, p in f.events if n == "call.translation"][0]
    assert translation["target_text"] == "我的订单在哪里？"
    assert translation["speaker"] == "peer"
    # Subtitle mode is text only. The operator still hears the far end
    # directly, so speaking the translation over it would only confuse.
    assert calls.spoken == []
    assert f.played == []


def test_voice_mode_plays_the_translation_to_the_operator_only():
    calls = FakeCalls()
    f = make_flow(calls=calls, engine=FakeEngine(transcripts=["Hello"], translations=["你好"]),
                  setting=make_setting(**{"translate.mode": "voice"}))
    session = calls.add("c1", state=CallState.ACTIVE)

    async def scenario():
        await f.flow.on_state(1, "active", session.to_dict())
        await settle()
        await f.flow._on_utterance(f.flow._runtimes["c1"], b"audio", 1.0)
        await settle(30)

    run(scenario())
    assert f.played and f.played[0][1] == "translate:c1"
    assert calls.spoken == [], "the far end must not hear its own words translated back"


def test_an_agent_call_keeps_translation_on_screen():
    calls = FakeCalls()
    f = make_flow(calls=calls, engine=FakeEngine(chat=[
        '{"say": "开场白", "done": false, "achieved": null, "evidence": "", "result": ""}']),
        setting=make_setting(**{"translate.mode": "voice"}))
    session = calls.add("c1", state=CallState.ACTIVE)

    async def scenario():
        await f.flow.attach_agent("c1", purpose="确认收货")
        await f.flow.on_state(1, "active", session.to_dict())
        await settle(40)

    run(scenario())
    runtime = f.flow._runtimes["c1"]
    assert runtime.translator is not None
    assert runtime.translator.mode == "subtitle"
    assert runtime.translate_note


def test_speaking_into_the_call_gates_the_capture():
    calls = FakeCalls()
    f = make_flow(calls=calls, setting=make_setting(**{"translate.mode": "subtitle"}))
    session = calls.add("c1", state=CallState.ACTIVE)

    async def scenario():
        await f.flow.on_state(1, "active", session.to_dict())
        await settle()
        runtime = f.flow._runtimes["c1"]
        assert runtime.bridge is not None
        assert runtime.bridge.gated() is False
        await f.flow._speak_gated(1, "这句话要说给对方听")
        assert runtime.bridge.gated() is True
        assert calls.spoken == [(1, "这句话要说给对方听")]

    run(scenario())


def test_operator_text_is_translated_and_spoken_into_the_call():
    calls = FakeCalls()
    f = make_flow(calls=calls, engine=FakeEngine(translations=["Your parcel left today."]),
                  setting=make_setting(**{"translate.mode": "subtitle",
                                          "translate.my_language": "zh",
                                          "translate.peer_language": "en"}))
    session = calls.add("c1", state=CallState.ACTIVE)

    async def scenario():
        await f.flow.on_state(1, "active", session.to_dict())
        await settle()
        result = await f.flow.operator_text("c1", "您的包裹今天发出了")
        await settle(20)
        return result

    result = run(scenario())
    assert result["translated"] is True
    assert result["spoken"] == "Your parcel left today."
    assert calls.spoken == [(1, "Your parcel left today.")]
    roles = [line["role"] for line in f.flow._runtimes["c1"].transcript]
    assert roles == ["me"]


def test_operator_audio_is_recognised_then_relayed():
    calls = FakeCalls()
    f = make_flow(calls=calls, setting=make_setting(**{"translate.mode": "off"}))
    session = calls.add("c1", state=CallState.ACTIVE)

    async def scenario():
        await f.flow.on_state(1, "active", session.to_dict())
        await settle()
        return await f.flow.operator_audio("c1", b"RIFFfake", filename="op.wav")

    result = run(scenario())
    assert result["heard"] == "我听到了"
    assert calls.spoken == [(1, "我听到了")]


def test_translation_can_be_switched_on_mid_call():
    calls = FakeCalls()
    f = make_flow(calls=calls, engine=FakeEngine(transcripts=["Good morning"],
                                                 translations=["早上好"]),
                  setting=make_setting(**{"translate.mode": "off"}))
    session = calls.add("c1", state=CallState.ACTIVE)

    async def scenario():
        await f.flow.on_state(1, "active", session.to_dict())
        await settle()
        assert f.flow._runtimes["c1"].translator is None
        await f.flow.set_translation("c1", mode="subtitle", peer_language="en")
        await settle()
        await f.flow._on_utterance(f.flow._runtimes["c1"], b"audio", 1.0)
        await settle(30)

    run(scenario())
    assert [n for n, _ in f.events if n == "call.translation"]
    assert f.flow._runtimes["c1"].translator.peer_language == "en"


# -- ai call tasks -----------------------------------------------------------


def test_an_outbound_task_reaches_its_purpose_and_hangs_up():
    calls = FakeCalls()
    tasks = FakeTasks()
    repo = FakeCallRepo()
    engine = FakeEngine(chat=[
        '{"say": "您好，我确认一下发货时间。", "done": false, "achieved": null, "evidence": "", "result": ""}',
        '{"say": "好的，谢谢您，再见。", "done": true, "achieved": true, '
        '"evidence": "今天下午就发", "result": "对方确认今天发货"}',
    ])
    f = make_flow(calls=calls, tasks=tasks, engine=engine, repo_calls=repo)

    async def scenario():
        await f.flow.start()
        result = await f.flow.submit_task(
            purpose="确认订单今天能否发货", number="+8613800000000", max_seconds=60)
        task_id = result["task"]["id"]
        await wait_for(lambda: bool(calls.dialed))
        call_id = next(iter(calls.sessions))
        session = calls.session_by_id(call_id)
        session.state = CallState.ACTIVE
        await f.flow.on_state(1, "active", session.to_dict())
        await settle(40)
        assert calls.spoken[0][1] == "您好，我确认一下发货时间。"
        engine.transcripts.append("今天下午就发")
        await f.flow._on_utterance(f.flow._runtimes[call_id], b"audio", 1.0)
        await settle(40)
        assert calls.hangups == [1], "the farewell is played before the hangup"
        session.state = CallState.ENDED
        session.end_reason = EndReason.LOCAL_HANGUP
        await f.flow.on_state(1, "ended", session.to_dict())
        await settle(40)
        return task_id, call_id

    task_id, call_id = run(scenario())
    row = tasks.rows[task_id]
    assert row["status"] == "finished"
    assert row["achieved"] is True
    assert row["result"] == "对方确认今天发货"
    assert row["summary"]["summary"] == "对方确认今天发货，约定下午发出。"
    assert any(call_id == cid and "summary" in fields for cid, fields in repo.updates)
    assert any(name == "ai_call.task_finished" for name, _ in f.events)


def test_a_task_whose_call_never_connects_is_reported_as_no_answer():
    calls = FakeCalls()
    tasks = FakeTasks()
    f = make_flow(calls=calls, tasks=tasks)

    async def scenario():
        await f.flow.start()
        result = await f.flow.submit_task(purpose="催一下货款", number="+8613800000000")
        task_id = result["task"]["id"]
        await wait_for(lambda: bool(calls.dialed))
        call_id = next(iter(calls.sessions))
        session = calls.session_by_id(call_id)
        session.state = CallState.ENDED
        session.end_reason = EndReason.NO_ANSWER
        await f.flow.on_state(1, "ended", session.to_dict())
        await wait_for(lambda: tasks.rows[task_id]["status"] not in ("queued", "dialing", "in_call"))
        await settle(20)
        return task_id

    task_id = run(scenario())
    assert tasks.rows[task_id]["status"] == "no_answer"
    assert calls.spoken == []


def test_a_retried_reference_does_not_place_a_second_call():
    f = make_flow()

    async def scenario():
        first = await f.flow.submit_task(purpose="回访客户", number="+8613800000000",
                                         reference="order-42")
        second = await f.flow.submit_task(purpose="回访客户", number="+8613800000000",
                                          reference="order-42")
        return first, second

    first, second = run(scenario())
    assert first["created"] is True
    assert second["created"] is False
    assert second["idempotent"] is True
    assert second["task"]["id"] == first["task"]["id"]


def test_cancelling_a_task_hangs_up_and_the_later_teardown_does_not_overwrite_it():
    calls = FakeCalls()
    tasks = FakeTasks()
    f = make_flow(calls=calls, tasks=tasks, engine=FakeEngine(chat=[
        '{"say": "您好。", "done": false, "achieved": null, "evidence": "", "result": ""}']))

    async def scenario():
        await f.flow.start()
        result = await f.flow.submit_task(purpose="确认地址", number="+8613800000000")
        task_id = result["task"]["id"]
        await wait_for(lambda: bool(calls.sessions))
        call_id = next(iter(calls.sessions))
        session = calls.session_by_id(call_id)
        session.state = CallState.ACTIVE
        await f.flow.on_state(1, "active", session.to_dict())
        await settle(40)
        cancelled = await f.flow.cancel_task(task_id)
        await settle(20)
        session.state = CallState.ENDED
        session.end_reason = EndReason.LOCAL_HANGUP
        await f.flow.on_state(1, "ended", session.to_dict())
        await settle(40)
        return task_id, cancelled

    task_id, cancelled = run(scenario())
    assert cancelled["ok"] is True
    assert calls.hangups == [1]
    assert tasks.rows[task_id]["status"] == "cancelled"


def test_handover_stops_the_agent_and_lets_the_operator_talk():
    calls = FakeCalls()
    f = make_flow(calls=calls, engine=FakeEngine(chat=[
        '{"say": "您好，我帮您问一下。", "done": false, "achieved": null, "evidence": "", "result": ""}']))
    session = calls.add("c1", state=CallState.ACTIVE)

    async def scenario():
        await f.flow.on_state(1, "active", session.to_dict())
        await f.flow.attach_agent("c1", purpose="问清退货流程")
        await settle(40)
        runtime = f.flow._runtimes["c1"]
        assert runtime.agent is not None and not runtime.agent.finished
        with pytest.raises(CallFlowError):
            await f.flow.operator_text("c1", "我来说")
        await f.flow.handover("c1")
        await settle(20)
        assert runtime.agent.finished is True
        assert calls.hangups == [], "a handover keeps the call up"
        await f.flow.operator_text("c1", "我来说")
        await settle(20)

    run(scenario())
    assert calls.spoken[-1] == (1, "我来说")


def test_turning_the_transcript_switch_off_keeps_words_out_of_the_database():
    calls = FakeCalls()
    repo = FakeCallRepo()
    f = make_flow(calls=calls, engine=FakeEngine(transcripts=["secret"], translations=["秘密"]),
                  repo_calls=repo,
                  setting=make_setting(**{"translate.mode": "subtitle", "transcript.save": False,
                                          "summary.auto_generate": False}))
    session = calls.add("c1", state=CallState.ACTIVE)

    async def scenario():
        await f.flow.on_state(1, "active", session.to_dict())
        await settle()
        await f.flow._on_utterance(f.flow._runtimes["c1"], b"audio", 1.0)
        await settle(30)
        session.state = CallState.ENDED
        await f.flow.on_state(1, "ended", session.to_dict())
        await settle(40)

    run(scenario())
    assert session.transcript == []
    assert all("transcript" not in fields for _, fields in repo.updates)
    assert [n for n, _ in f.events if n == "call.transcript"], "the console still sees it live"


def test_a_call_with_no_task_still_gets_minutes():
    calls = FakeCalls()
    repo = FakeCallRepo()
    f = make_flow(calls=calls, engine=FakeEngine(transcripts=["我们下周三签"], translations=[]),
                  repo_calls=repo,
                  setting=make_setting(**{"translate.mode": "off", "summary.auto_generate": True}))
    session = calls.add("c1", state=CallState.ACTIVE)

    async def scenario():
        await f.flow.on_state(1, "active", session.to_dict())
        await settle()
        await f.flow._on_utterance(f.flow._runtimes["c1"], b"audio", 1.0)
        await settle(30)
        session.state = CallState.ENDED
        await f.flow.on_state(1, "ended", session.to_dict())
        await settle(60)

    run(scenario())
    saved = [fields for cid, fields in repo.updates if cid == "c1" and "summary" in fields]
    assert saved, "the minutes are written onto the call row"
    assert saved[-1]["summary"]["summary"] == "对方确认今天发货，约定下午发出。"
    assert any(name == "call.summary" for name, _ in f.events)
    # The runtime stays readable after the call so the app can fetch the result.
    assert f.flow.flow("c1")["summary"] is not None
