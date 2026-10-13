"""Telephony tone synthesis using only the standard library.

Frequencies and cadences follow the usual PSTN conventions so the generated
prompts sound like real network tones rather than arbitrary beeps. Everything
is written as 16-bit mono PCM WAV, which every player in the chain accepts.
"""
from __future__ import annotations

import math
import struct
import wave
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

__all__ = [
    "DTMF_PAIRS",
    "TONE_RECIPES",
    "write_tone",
    "write_dtmf",
    "write_silence",
    "mix",
]

SAMPLE_RATE = 16000

# Standard DTMF matrix, row frequency plus column frequency.
DTMF_PAIRS: dict[str, Tuple[int, int]] = {
    "1": (697, 1209), "2": (697, 1336), "3": (697, 1477), "A": (697, 1633),
    "4": (770, 1209), "5": (770, 1336), "6": (770, 1477), "B": (770, 1633),
    "7": (852, 1209), "8": (852, 1336), "9": (852, 1477), "C": (852, 1633),
    "*": (941, 1209), "0": (941, 1336), "#": (941, 1477), "D": (941, 1633),
}

# name -> (frequencies, cadence) where cadence is a list of (on, off) seconds.
TONE_RECIPES: dict[str, Tuple[Tuple[int, ...], Sequence[Tuple[float, float]]]] = {
    "dialtone": ((350, 440), [(1.0, 0.0)]),
    "ringback_cn": ((450,), [(1.0, 4.0)]),
    "ringback_us": ((440, 480), [(2.0, 4.0)]),
    "busy_cn": ((450,), [(0.35, 0.35)]),
    "busy_us": ((480, 620), [(0.5, 0.5)]),
    "congestion": ((480, 620), [(0.25, 0.25)]),
    "ring": ((440, 480), [(1.0, 2.0)]),
    "beep": ((880,), [(0.15, 0.0)]),
    "double_beep": ((880,), [(0.12, 0.08), (0.12, 0.0)]),
    "click": ((1200,), [(0.03, 0.0)]),
    "success": ((660, 880), [(0.12, 0.06), (0.18, 0.0)]),
    "error": ((300,), [(0.18, 0.06), (0.18, 0.0)]),
    "hold": ((600,), [(0.3, 0.2)]),
}


def _sine(freq: float, seconds: float, rate: int = SAMPLE_RATE, amplitude: float = 0.32) -> List[float]:
    count = int(rate * seconds)
    if count <= 0:
        return []
    step = 2.0 * math.pi * freq / rate
    # Short fade in and out avoids the click a raw sine burst produces.
    fade = min(count // 8, int(rate * 0.005))
    out: List[float] = []
    for i in range(count):
        gain = amplitude
        if fade and i < fade:
            gain *= i / fade
        elif fade and i > count - fade:
            gain *= (count - i) / fade
        out.append(gain * math.sin(step * i))
    return out


def mix(*layers: Iterable[float]) -> List[float]:
    """Sum sample streams, clipping to the 16-bit range."""
    streams = [list(layer) for layer in layers]
    length = max((len(s) for s in streams), default=0)
    out: List[float] = [0.0] * length
    for stream in streams:
        for i, value in enumerate(stream):
            out[i] += value
    return [max(-1.0, min(1.0, v)) for v in out]


def _to_pcm(samples: Iterable[float]) -> bytes:
    packed = bytearray()
    for value in samples:
        packed += struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32767))
    return bytes(packed)


def write_wav(path: Path, samples: Iterable[float], rate: int = SAMPLE_RATE) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(_to_pcm(samples))
    return path


def build_cadence(
    freqs: Sequence[float],
    cadence: Sequence[Tuple[float, float]],
    loops: int = 1,
    amplitude: float = 0.32,
) -> List[float]:
    samples: List[float] = []
    for _ in range(max(1, loops)):
        for on, off in cadence:
            if on > 0:
                samples.extend(mix(*[_sine(f, on, amplitude=amplitude) for f in freqs]))
            if off > 0:
                samples.extend([0.0] * int(SAMPLE_RATE * off))
    return samples


def write_tone(name: str, path: Path, loops: int = 1, amplitude: float = 0.32) -> Path:
    """Render one of TONE_RECIPES to a WAV file."""
    if name not in TONE_RECIPES:
        raise KeyError(f"unknown tone recipe {name}")
    freqs, cadence = TONE_RECIPES[name]
    return write_wav(path, build_cadence(freqs, cadence, loops=loops, amplitude=amplitude))


def write_dtmf(digits: str, path: Path, tone_seconds: float = 0.08, gap_seconds: float = 0.05) -> Path:
    """Render a DTMF digit string, useful for testing keypads."""
    samples: List[float] = []
    for ch in digits.upper():
        pair = DTMF_PAIRS.get(ch)
        if not pair:
            continue
        samples.extend(mix(*[_sine(f, tone_seconds) for f in pair]))
        if gap_seconds > 0:
            samples.extend([0.0] * int(SAMPLE_RATE * gap_seconds))
    return write_wav(path, samples)


def write_silence(path: Path, seconds: float = 1.0) -> Path:
    return write_wav(path, [0.0] * int(SAMPLE_RATE * seconds))
