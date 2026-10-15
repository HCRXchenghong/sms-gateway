"""Utterance segmentation and the live audio bridge.

Both are driven with synthesised PCM rather than a sound card, because the card
is the part that cannot be exercised in CI and the logic above it is the part
that is easy to get subtly wrong.
"""
from __future__ import annotations

import asyncio
import math
import struct

import pytest

from app.audio.bridge import BridgeStats, CallAudioBridge, capture_argv
from app.audio.vad import UtteranceSegmenter, VadConfig

RATE = 8000
CFG = VadConfig(sample_rate=RATE, frame_ms=20, min_speech_ms=100,
                max_speech_ms=1000, silence_end_ms=100, preroll_ms=40)
FRAME = CFG.frame_bytes


def tone(ms: int, amplitude: int = 3000, rate: int = RATE) -> bytes:
    samples = int(rate * ms / 1000)
    out = bytearray()
    for i in range(samples):
        value = int(amplitude * math.sin(2 * math.pi * 300 * i / rate))
        out += struct.pack("<h", max(-32768, min(32767, value)))
    return bytes(out)


SILENCE = b"\x00\x00" * int(RATE * 0.02)
LOUD = tone(20)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# -- segmenter ---------------------------------------------------------------


def test_silence_produces_nothing():
    seg = UtteranceSegmenter(CFG)
    assert seg.feed(SILENCE * 50) == []
    assert seg.emitted == 0


def test_speech_then_quiet_yields_one_utterance():
    seg = UtteranceSegmenter(CFG)
    assert seg.feed(LOUD * 20) == []          # still speaking
    out = seg.feed(SILENCE * 20)              # quiet long enough to close it
    assert len(out) == 1
    assert seg.emitted == 1
    # The utterance carries the speech and a little of the trailing quiet.
    assert len(out[0]) >= FRAME * 20


def test_preroll_keeps_the_start_of_the_word():
    seg = UtteranceSegmenter(VadConfig(sample_rate=RATE, frame_ms=20, preroll_ms=200,
                                       min_speech_ms=40, silence_end_ms=100,
                                       max_speech_ms=1000))
    quiet = SILENCE * 20
    seg.feed(quiet)
    before = seg.emitted
    out = seg.feed(LOUD * 10) + seg.feed(SILENCE * 20)
    assert out and seg.emitted == before + 1
    # Two hundred milliseconds of preroll at 8k mono s16 is 3200 bytes.
    assert len(out[0]) >= 3200


def test_a_short_blip_is_dropped_instead_of_recognised():
    seg = UtteranceSegmenter(VadConfig(sample_rate=RATE, frame_ms=20, min_speech_ms=300,
                                       silence_end_ms=100, max_speech_ms=1000))
    assert seg.feed(LOUD * 2) == []
    assert seg.feed(SILENCE * 20) == []
    assert seg.dropped_short == 1
    assert seg.emitted == 0


def test_a_long_monologue_is_cut_at_the_limit():
    seg = UtteranceSegmenter(CFG)   # max_speech_ms = 1000
    out = seg.feed(LOUD * 200)      # 4 seconds of continuous speech
    assert len(out) >= 3, "continuous speech must be chunked, not held to the end"
    assert all(len(chunk) > 0 for chunk in out)


def test_flush_emits_an_utterance_still_open_at_end_of_call():
    seg = UtteranceSegmenter(CFG)
    seg.feed(LOUD * 20)
    tail = seg.flush()
    assert tail is not None and len(tail) > 0
    assert seg.flush() is None


def test_reset_drops_a_partially_captured_frame():
    seg = UtteranceSegmenter(CFG)
    seg.feed(LOUD * 10)
    seg.reset()
    assert seg.feed(SILENCE * 40) == []
    assert seg.emitted == 0


def test_two_utterances_separated_by_a_pause():
    seg = UtteranceSegmenter(CFG)
    out = seg.feed(LOUD * 15) + seg.feed(SILENCE * 20) + seg.feed(LOUD * 15) + seg.feed(SILENCE * 20)
    assert len(out) == 2
    assert seg.emitted == 2


# -- bridge ------------------------------------------------------------------


class FakePlayer:
    def __init__(self, ok=True, error=None):
        self.ok = ok
        self.error = error
        self.played: list[tuple[int, str]] = []

    async def play_bytes(self, data, *, tag="clip", wait=True, timeout=60.0, **kwargs):
        self.played.append((len(data), tag))
        return type("R", (), {"ok": self.ok, "error": self.error})()


def make_bridge(**kwargs):
    got: list[bytes] = []

    async def on_utterance(pcm, seconds):
        got.append(pcm)

    bridge = CallAudioBridge(call_id="c1", device_id=2, on_utterance=on_utterance,
                             vad=CFG, **kwargs)
    return bridge, got


def test_frames_are_segmented_and_dispatched():
    bridge, got = make_bridge()

    async def drive():
        bridge.push_frames(LOUD * 20)
        bridge.push_frames(SILENCE * 20)
        await asyncio.sleep(0.05)

    run(drive())
    assert len(got) == 1
    assert bridge.stats.utterances == 1
    assert bridge.stats.bytes_captured == len(LOUD * 40)


def test_capture_is_gated_while_we_are_speaking_into_the_call():
    """Without the gate the bridge would hear its own synthesised voice."""
    bridge, got = make_bridge(player=FakePlayer())

    async def drive():
        await bridge.gate_for(0.3)
        assert bridge.gated() is True
        bridge.push_frames(LOUD * 20)
        bridge.push_frames(SILENCE * 20)
        await asyncio.sleep(0.02)
        assert got == []
        assert bridge.stats.gated_frames == 2

    run(drive())


def test_gate_expires_and_capture_resumes():
    bridge, got = make_bridge()

    async def drive():
        await bridge.gate_for(0.01)
        await asyncio.sleep(0.03)
        assert bridge.gated() is False
        bridge.push_frames(LOUD * 20)
        bridge.push_frames(SILENCE * 20)
        await asyncio.sleep(0.05)
        assert len(got) == 1

    run(drive())


def test_play_into_call_gates_for_the_clip_length_and_reports_failure():
    player = FakePlayer(ok=False, error="no output device")
    bridge, _ = make_bridge(player=player)

    async def drive():
        import io
        import wave
        buf = io.BytesIO()
        with wave.open(buf, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(RATE)
            handle.writeframes(b"\x00\x00" * RATE)   # one second
        ok = await bridge.play_into_call(buf.getvalue(), tag="translate:c1")
        assert ok is False
        assert bridge.stats.playback_error == "no output device"
        assert bridge.gated() is True, "a failed clip must still gate its own echo"
        assert player.played and player.played[0][1] == "translate:c1"

    run(drive())


def test_play_without_a_player_is_reported_not_swallowed():
    bridge, _ = make_bridge()
    assert run(bridge.play_into_call(b"RIFFxxxx")) is False
    assert "no audio player" in (bridge.stats.playback_error or "")


def test_stop_flushes_the_open_utterance():
    bridge, got = make_bridge()

    async def drive():
        bridge.push_frames(LOUD * 20)
        await bridge.stop()
        await asyncio.sleep(0.05)

    run(drive())
    assert len(got) == 1
    assert bridge.stats.running is False


def test_capture_argv_streams_raw_pcm_to_stdout():
    argv = capture_argv(8000, "plughw:1,0")
    assert argv, "a capture command must be available on this host"
    assert "-t" in argv and "raw" in argv or "--format=s16le" in argv
    assert "-D" not in argv or "plughw:1,0" in argv
    # /dev/stdout gets reopened through /proc/self/fd/1, and the kernel answers
    # ENXIO for an anonymous pipe, so the capture used to die before reading a
    # single frame. The tools write to stdout without being told a filename.
    assert "/dev/stdout" not in argv


def test_stats_are_serialisable():
    stats = BridgeStats(call_id="c1", device_id=2)
    payload = stats.to_dict()
    assert payload["call_id"] == "c1" and payload["running"] is False
    assert "seconds" not in payload


# -- shared capture ----------------------------------------------------------


def test_two_consumers_share_one_capture_process():
    """Recording and recognition must not fight over the card.

    A real subprocess stands in for arecord so the fan-out, the reference
    counting and the teardown are all exercised rather than assumed.
    """
    from app.audio.capture import CaptureHub

    started: list[int] = []

    def builder(rate, device):
        started.append(rate)
        return ["/bin/sh", "-c", "while :; do head -c 640 /dev/zero; sleep 0.02; done"]

    hub = CaptureHub(argv_builder=builder)
    got = {"recorder": 0, "bridge": 0}

    async def scenario():
        assert await hub.subscribe("recorder", rate=16000,
                                   on_frames=lambda d: got.__setitem__("recorder", got["recorder"] + len(d)))
        assert await hub.subscribe("bridge", rate=16000,
                                   on_frames=lambda d: got.__setitem__("bridge", got["bridge"] + len(d)))
        await wait_for_capture(lambda: got["recorder"] >= 640 and got["bridge"] >= 640)
        assert started == [16000], "one card, one capture process"
        stats = hub.stats()
        assert len(stats) == 1
        assert stats[0]["subscribers"] == ["bridge", "recorder"]

        await hub.release("recorder")
        stats = hub.stats()
        assert stats and stats[0]["subscribers"] == ["bridge"], "the stream stays up for the other consumer"
        await hub.release("bridge")
        assert hub.stats() == [], "the last consumer closing stops the capture"

    run(scenario())


def test_a_capture_that_dies_tells_its_subscribers():
    from app.audio.capture import CaptureHub

    stops: list = []
    hub = CaptureHub(argv_builder=lambda rate, device: ["/bin/sh", "-c", "head -c 320 /dev/zero"])

    async def scenario():
        assert await hub.subscribe("bridge", rate=8000, on_frames=lambda d: None,
                                   on_stop=lambda error: stops.append(error))
        await wait_for_capture(lambda: bool(stops))
        assert hub.stats() == []

    run(scenario())
    assert stops and stops[0] is None, "a clean end of stream is not an error"


async def wait_for_capture(predicate, timeout=5.0):
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("capture never delivered the expected frames")
