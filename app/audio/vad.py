"""Energy-based utterance segmenter.

Translation and the call agent both need "one thing the other party said" as a
unit, not an endless byte stream. This turns raw PCM into those units.

It is deliberately simple and stateless about content: speech starts when a
frame is loud enough, and the utterance is emitted after enough quiet frames
follow, or when it runs past a hard length limit so a long monologue still gets
translated in pieces. Working on plain bytes keeps it testable without a sound
card, which matters because the card is the part that is hardest to exercise.
"""
from __future__ import annotations

import audioop
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional

__all__ = ["VadConfig", "UtteranceSegmenter"]


@dataclass
class VadConfig:
    sample_rate: int = 8000
    sample_width: int = 2
    frame_ms: int = 20
    # RMS as a fraction of full scale. Telephone audio is quiet, so these are
    # low; the console can raise them on a noisy line.
    start_threshold: float = 0.02
    end_threshold: float = 0.012
    min_speech_ms: int = 200
    max_speech_ms: int = 12000
    silence_end_ms: int = 700
    # Keep a little audio from before the trigger so the first consonant of a
    # word is not clipped off, which recognisers handle badly.
    preroll_ms: int = 200

    def __post_init__(self) -> None:
        self.frame_ms = max(5, int(self.frame_ms))
        self.start_threshold = max(0.0, float(self.start_threshold))
        self.end_threshold = max(0.0, float(self.end_threshold))
        self.min_speech_ms = max(0, int(self.min_speech_ms))
        self.max_speech_ms = max(self.min_speech_ms + 1, int(self.max_speech_ms))
        self.silence_end_ms = max(self.frame_ms, int(self.silence_end_ms))
        self.preroll_ms = max(0, int(self.preroll_ms))

    @property
    def frame_bytes(self) -> int:
        return int(self.sample_rate * self.sample_width * self.frame_ms / 1000)

    def _frames(self, ms: int) -> int:
        return max(1, int(ms / self.frame_ms))

    @property
    def min_speech_frames(self) -> int:
        return self._frames(self.min_speech_ms)

    @property
    def max_speech_frames(self) -> int:
        return self._frames(self.max_speech_ms)

    @property
    def silence_end_frames(self) -> int:
        return self._frames(self.silence_end_ms)

    @property
    def preroll_frames(self) -> int:
        return int(self.preroll_ms / self.frame_ms)


@dataclass
class UtteranceSegmenter:
    """Feed PCM bytes in, get complete utterances out."""

    config: VadConfig = field(default_factory=VadConfig)
    _buffer: bytes = field(default=b"", init=False, repr=False)
    _preroll: Deque[bytes] = field(default_factory=deque, init=False, repr=False)
    _speech: List[bytes] = field(default_factory=list, init=False, repr=False)
    _speaking: bool = field(default=False, init=False, repr=False)
    _speech_frames: int = field(default=0, init=False, repr=False)
    _quiet_frames: int = field(default=0, init=False, repr=False)
    emitted: int = field(default=0, init=False)
    dropped_short: int = field(default=0, init=False)

    def _rms(self, frame: bytes) -> float:
        if not frame:
            return 0.0
        try:
            peak = float(audioop.rms(frame, self.config.sample_width))
        except audioop.error:
            return 0.0
        # 16 bit full scale is 32767; normalise so thresholds are comparable
        # across sample widths.
        full_scale = float((1 << (8 * self.config.sample_width - 1)) - 1)
        return peak / full_scale if full_scale else 0.0

    def feed(self, pcm: bytes) -> List[bytes]:
        """Add captured audio and return any utterances that completed."""
        if not pcm:
            return []
        self._buffer += pcm
        size = self.config.frame_bytes
        out: List[bytes] = []
        while len(self._buffer) >= size:
            frame, self._buffer = self._buffer[:size], self._buffer[size:]
            completed = self._frame(frame)
            if completed:
                out.append(completed)
        return out

    def _frame(self, frame: bytes) -> Optional[bytes]:
        cfg = self.config
        loud = self._rms(frame) >= (cfg.end_threshold if self._speaking else cfg.start_threshold)

        if not self._speaking:
            self._preroll.append(frame)
            while len(self._preroll) > max(0, cfg.preroll_frames):
                self._preroll.popleft()
            if loud:
                self._speaking = True
                self._speech_frames = 1
                self._quiet_frames = 0
                self._speech = list(self._preroll)
                self._preroll.clear()
            return None

        self._speech.append(frame)
        self._speech_frames += 1
        if loud:
            self._quiet_frames = 0
        else:
            self._quiet_frames += 1

        if self._quiet_frames >= cfg.silence_end_frames:
            return self._close()
        if self._speech_frames >= cfg.max_speech_frames:
            # Long monologue: cut it here so translation is not held until the
            # speaker finally stops. The cut is mid-speech, so the quiet frames
            # are not part of what we emit.
            return self._close(trim_quiet=True)
        return None

    def _close(self, *, trim_quiet: bool = False) -> Optional[bytes]:
        cfg = self.config
        frames = self._speech
        if trim_quiet and self._quiet_frames:
            frames = frames[: max(0, len(frames) - self._quiet_frames)]
        self._speech = []
        self._speaking = False
        self._speech_frames = 0
        self._quiet_frames = 0
        spoken = len(frames)
        if spoken < cfg.min_speech_frames:
            # A cough or a click. Dropping it keeps the recogniser from being
            # fed noise, which is how hallucinated transcripts start.
            self.dropped_short += 1
            return None
        self.emitted += 1
        return b"".join(frames)

    def flush(self) -> Optional[bytes]:
        """End of call: emit whatever speech is still open."""
        if not self._speaking or not self._speech:
            self._speaking = False
            self._speech = []
            self._speech_frames = 0
            self._quiet_frames = 0
            return None
        return self._close(trim_quiet=True)

    def reset(self) -> None:
        self._buffer = b""
        self._preroll.clear()
        self._speech = []
        self._speaking = False
        self._speech_frames = 0
        self._quiet_frames = 0
