"""Live translation pipeline.

The engine and both playback paths are faked, so these cover the parts that are
easy to get wrong: which ear each direction goes to, the echo gate that stops
the system translating its own voice, and the stream cadence never re-sending a
prefix it already translated.
"""
from __future__ import annotations

import asyncio

import pytest

from app.voice.translate import (
    CADENCE_SENTENCE,
    CADENCE_STREAM,
    MODE_OFF,
    MODE_SUBTITLE,
    MODE_VOICE,
    CallTranslator,
    StreamAccumulator,
    TranslationManager,
)


class FakeEngine:
    def __init__(self, *, asr_text="hello there", tts_fail=False, asr_fail=False):
        self.asr_text = asr_text
        self.asr_fail = asr_fail
        self.tts_fail = tts_fail
        self.transcribed: list[tuple] = []
        self.translated: list[dict] = []
        self.synthesized: list[str] = []

    async def transcribe(self, audio, *, language=None, call_id=None, **kwargs):
        self.transcribed.append((len(audio or b""), language))
        if self.asr_fail:
            raise RuntimeError("asr down")
        return self.asr_text

    async def translate(self, text, *, source=None, target, glossary=None, call_id=None):
        self.translated.append({"text": text, "source": source, "target": target})
        return f"[{target}]{text}"

    async def synthesize(self, text, *, call_id=None, **kwargs):
        self.synthesized.append(text)
        if self.tts_fail:
            raise RuntimeError("tts down")
        return b"RIFF" + text.encode("utf-8", "replace")


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def make(mode=MODE_SUBTITLE, cadence=CADENCE_SENTENCE, engine=None, **kwargs):
    engine = engine or FakeEngine()
    events: list[tuple[str, dict]] = []
    spoken: list[tuple[int, str]] = []
    played: list[tuple[bytes, str]] = []

    async def publish(name, payload):
        events.append((name, payload))

    async def speak_into_call(device_id, text):
        spoken.append((device_id, text))

    async def play_to_operator(audio, tag):
        played.append((audio, tag))
        return True

    translator = CallTranslator(
        call_id="call-1",
        device_id=4,
        engine=engine,
        mode=mode,
        cadence=cadence,
        my_language="zh",
        peer_language="en",
        publish=publish,
        speak_into_call=speak_into_call,
        play_to_operator=play_to_operator,
        **kwargs,
    )
    return translator, engine, events, spoken, played


def test_off_mode_does_nothing():
    translator, engine, events, _, _ = make(mode=MODE_OFF)
    assert run(translator.peer_text("hello")) is None
    assert engine.translated == []
    assert events == []
    assert translator.active is False


def test_subtitle_mode_publishes_both_texts_and_speaks_nothing():
    translator, engine, events, spoken, played = make(mode=MODE_SUBTITLE)
    result = run(translator.peer_text("Are you open today"))
    assert result is not None
    assert result.source_text == "Are you open today"
    assert result.target_text == "[zh]Are you open today"
    assert result.source_language == "en"
    assert result.target_language == "zh"
    assert spoken == [] and played == []
    assert [name for name, _ in events] == ["call.translation"]
    assert engine.synthesized == []


def test_far_end_translation_goes_to_the_operator_not_back_into_the_call():
    translator, _, _, spoken, played = make(mode=MODE_VOICE)
    result = run(translator.peer_text("hello"))
    assert result.played is True
    assert len(played) == 1
    assert spoken == [], "the far end must not hear their own words translated back"


def test_our_translation_goes_into_the_call():
    translator, _, _, spoken, played = make(mode=MODE_VOICE)
    result = run(translator.my_text("我们九点开门"))
    assert result.played is True
    assert spoken == [(4, "[en]我们九点开门")]
    assert played == []
    assert result.target_language == "en"


def test_speaking_into_the_call_gates_the_far_end_capture():
    """Without this gate the system hears its own voice and translates it back."""
    translator, engine, _, _, _ = make(mode=MODE_VOICE)
    translator._speaking_into_call = True
    assert run(translator.peer_audio(b"\x00\x01")) is None
    assert engine.transcribed == []
    translator._speaking_into_call = False
    assert run(translator.peer_audio(b"\x00\x01")) is not None
    assert engine.transcribed == [(2, "en")]


def test_audio_paths_recognise_with_the_right_language_hint():
    translator, engine, _, _, _ = make(mode=MODE_SUBTITLE)
    run(translator.peer_audio(b"\x00" * 10))
    run(translator.my_audio(b"\x00" * 10))
    assert [lang for _, lang in engine.transcribed] == ["en", "zh"]


def test_sentence_cadence_ignores_growing_partials():
    translator, engine, _, _, _ = make(cadence=CADENCE_SENTENCE)
    assert run(translator.peer_partial("are you")) is None
    assert engine.translated == []
    assert run(translator.peer_text("are you open", partial=False)) is not None
    assert len(engine.translated) == 1


def test_stream_cadence_emits_at_clause_boundaries_without_repeating():
    translator, engine, _, _, _ = make(cadence=CADENCE_STREAM)
    assert run(translator.peer_partial("are you")) is None
    first = run(translator.peer_partial("are you open,"))
    assert first is not None and first.partial is True
    assert first.source_text == "are you open,"
    # The same prefix arrives again with more text; only the new part is sent.
    second = run(translator.peer_partial("are you open, and when do you close?"))
    assert second is not None
    assert second.source_text.startswith("and when")
    sent = "".join(call["text"] for call in engine.translated)
    assert sent.count("are you open,") == 1


def test_flush_emits_the_tail_of_a_stream_utterance():
    translator, engine, _, _, _ = make(cadence=CADENCE_STREAM)
    run(translator.peer_partial("are you open today"))
    tail = run(translator.flush("peer"))
    assert tail is not None
    assert "are you open today" in tail.source_text


def test_accumulator_respects_a_length_limit():
    acc = StreamAccumulator(min_chars=4, max_chars=10)
    assert acc.observe("abcdefghij") is not None
    assert acc.pending() == ""


def test_accumulator_survives_a_recogniser_restart():
    acc = StreamAccumulator(min_chars=2, max_chars=50)
    assert acc.observe("hello world") is None
    assert acc.observe("hello world, how are you") is None
    # A shorter transcript that is not a prefix means the recogniser started
    # over. The pending words are emitted rather than dropped.
    out = acc.observe("goodbye")
    assert out == "hello world, how are you"
    # The new hypothesis is buffered and waits for its own boundary.
    assert acc.pending() == "goodbye"
    assert acc.observe("goodbye now.") == "goodbye now."
    assert acc.pending() == ""


def test_accumulator_keeps_ground_on_a_shorter_prefix():
    acc = StreamAccumulator(min_chars=2, max_chars=50)
    acc.observe("hello world, how are you")
    # Same hypothesis, briefly reported shorter: nothing to emit, nothing lost.
    assert acc.observe("hello world") is None
    assert acc.pending() == "hello world, how are you"


def test_empty_translation_is_not_published():
    class Quiet(FakeEngine):
        async def translate(self, text, **kwargs):
            return "   "

    translator, _, events, _, _ = make(engine=Quiet())
    assert run(translator.peer_text("umm")) is None
    assert events == []


def test_recognition_failure_publishes_an_error_and_keeps_the_call_going():
    translator, _, events, _, _ = make(engine=FakeEngine(asr_fail=True))
    assert run(translator.peer_audio(b"\x00")) is None
    assert [name for name, _ in events] == ["call.translation.error"]
    assert events[0][1]["stage"] == "asr"


def test_speech_failure_degrades_to_a_subtitle_instead_of_losing_the_line():
    translator, _, events, spoken, played = make(mode=MODE_VOICE, engine=FakeEngine(tts_fail=True))
    result = run(translator.peer_text("hello"))
    assert result is not None
    assert result.target_text == "[zh]hello", "the subtitle must survive a broken voice path"
    assert result.played is False
    assert "tts" in (result.play_error or "")
    assert played == [] and spoken == []
    assert [name for name, _ in events] == ["call.translation", "call.translation.error"] or \
           [name for name, _ in events] == ["call.translation.error", "call.translation"]


def test_mode_can_be_switched_mid_call():
    translator, _, _, spoken, played = make(mode=MODE_SUBTITLE)
    run(translator.peer_text("one"))
    assert played == []
    assert translator.set_mode(MODE_VOICE) == MODE_VOICE
    run(translator.peer_text("two"))
    assert len(played) == 1
    assert translator.set_mode("nonsense") == MODE_OFF
    assert run(translator.peer_text("three")) is None


def test_cadence_can_be_switched_mid_call():
    translator, _, _, _, _ = make(cadence=CADENCE_SENTENCE)
    assert translator.set_cadence(CADENCE_STREAM) == CADENCE_STREAM
    assert run(translator.peer_partial("are you open,")) is not None


def test_transcript_entry_keeps_the_original_and_the_translation():
    translator, _, _, _, _ = make()
    result = run(translator.peer_text("hello"))
    entry = result.transcript_entry()
    assert entry["original"] == "hello"
    assert entry["content"] == "[zh]hello"
    assert entry["role"] == "peer"
    assert entry["source_language"] == "en"


def test_manager_tracks_one_translator_per_call():
    manager = TranslationManager(FakeEngine())
    first = manager.start(call_id="a", device_id=1, mode=MODE_SUBTITLE,
                          my_language="zh", peer_language="en")
    manager.start(call_id="b", device_id=2, mode=MODE_VOICE,
                  my_language="zh", peer_language="ja")
    assert manager.get("a") is first
    assert sorted(manager.active_call_ids()) == ["a", "b"]
    assert {s["call_id"]: s["peer_language"] for s in manager.snapshot()} == {"a": "en", "b": "ja"}
    manager.stop("a")
    assert manager.get("a") is None
    assert manager.active_call_ids() == ["b"]


def test_unknown_mode_and_cadence_fall_back_to_a_safe_default():
    translator = CallTranslator(call_id="c", device_id=1, engine=FakeEngine(),
                                mode="banana", cadence="also-banana")
    assert translator.mode == MODE_OFF
    assert translator.cadence == CADENCE_SENTENCE
