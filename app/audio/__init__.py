"""Audio subsystem: device selection, prompt tones, playback and capture."""
from __future__ import annotations

from .devices import AudioDeviceInfo, list_input_devices, list_output_devices
from .player import AudioPlayer
from .prompts import PROMPTS, STATE_PROMPTS, prompt_path, resolve_prompt, state_prompt

__all__ = [
    "AudioDeviceInfo",
    "AudioPlayer",
    "PROMPTS",
    "STATE_PROMPTS",
    "list_input_devices",
    "list_output_devices",
    "prompt_path",
    "resolve_prompt",
    "state_prompt",
]
