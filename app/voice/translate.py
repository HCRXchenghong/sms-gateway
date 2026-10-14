"""Live call translation.

Two independent settings decide what happens:

mode      off | subtitle | voice
            subtitle shows the far end's words and their translation as text.
            voice additionally speaks each side the other's language.

cadence   sentence | stream
            sentence waits for the speaker to finish a short utterance.
            stream translates while they are still talking, emitting at clause
            boundaries and never re-translating a prefix already sent.

Direction matters for where audio goes. The far end's speech is translated into
the operator's language and played to the operator, never back into the call.
Our speech is translated into the far end's language and played into the call.
Keeping the two paths separate is what stops the system hearing its own
synthesised voice, translating it back, and looping.

This module does no I/O of its own. Recognition, translation and synthesis go
through the AI engine, playback goes through injected callables, so the whole
pipeline is testable without a sound card or a module.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ..config import settings

log = logging.getLogger(__name__)

__all__ = [
    "MODES",
    "CADENCES",
    "MODE_OFF",
    "MODE_SUBTITLE",
    "MODE_VOICE",
    "CADENCE_SENTENCE",
    "CADENCE_STREAM",
    "TranslationResult",
    "StreamAccumulator",
    "CallTranslator",
]

MODE_OFF = "off"
MODE_SUBTITLE = "subtitle"
MODE_VOICE = "voice"
MODES = (MODE_OFF, MODE_SUBTITLE, MODE_VOICE)

CADENCE_SENTENCE = "sentence"
CADENCE_STREAM = "stream"
CADENCES = (CADENCE_SENTENCE, CADENCE_STREAM)

# Clause boundaries that make a reasonable place to cut a stream translation.
_CLAUSE_ENDS = tuple("。！？；，、,.!?;:\\n")

Publish = Callable[[str, dict], Awaitable[None]]
Speak = Callable[[int, str], Awaitable[None]]
PlayLocal = Callable[[bytes, str], Awaitable[bool]]


@dataclass
class TranslationResult:
    call_id: str
    speaker: str                 # "peer" | "me"
    source_text: str
    target_text: str
    source_language: str
    target_language: str
    mode: str
    cadence: str
    partial: bool = False
    played: bool = False
    play_error: Optional[str] = None
    at: int = field(default_factory=lambda: int(time.time()))
    elapsed_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "call_id": self.call_id,
            "speaker": self.speaker,
            "source_text": self.source_text,
            "target_text": self.target_text,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "mode": self.mode,
            "cadence": self.cadence,
            "partial": self.partial,
            "played": self.played,
            "play_error": self.play_error,
            "at": self.at,
            "elapsed_ms": self.elapsed_ms,
        }

    def transcript_entry(self) -> dict:
        """One line for the call transcript, keeping both texts side by side."""
        return {
            "role": "peer" if self.speaker == "peer" else "me",
            "content": self.target_text,
            "original": self.source_text,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "partial": self.partial,
            "at": self.at,
        }


class StreamAccumulator:
    """Decides when a stream translation should emit its next chunk.

    Speech recognition delivers growing partial transcripts. Translating every
    intermediate result wastes calls and makes the subtitles flicker, so this
    emits only when the new text reaches a clause boundary or grows past a
    length limit, and it tracks what has already been sent so a prefix is never
    translated twice.
    """

    def __init__(self, *, min_chars: int = 6, max_chars: int = 60, flush_on_boundary: bool = True):
        self.min_chars = max(1, int(min_chars))
        self.max_chars = max(self.min_chars, int(max_chars))
        self.flush_on_boundary = flush_on_boundary
        self._sent = 0
        self._buffer = ""

    @property
    def sent_chars(self) -> int:
        return self._sent

    def pending(self) -> str:
        return self._buffer[self._sent:]

    def observe(self, text: str) -> Optional[str]:
        """Feed a partial transcript. Return the next chunk to translate."""
        text = (text or "").strip()
        if not text:
            return None
        if self._buffer and len(text) < len(self._buffer) and not self._buffer.startswith(text):
            # The recogniser restarted on a different hypothesis. Emit what was
            # still pending so those words are not lost, then start over.
            leftover = self.flush()
            self._buffer = text
            if leftover:
                return leftover
        if len(text) >= len(self._buffer):
            self._buffer = text
        pending = self.pending()
        if len(pending) < self.min_chars:
            return None
        if len(pending) >= self.max_chars:
            return self._take(len(pending))
        if self.flush_on_boundary and pending[-1] in _CLAUSE_ENDS:
            return self._take(len(pending))
        return None

    def _take(self, count: int) -> str:
        chunk = self.pending()[:count]
        self._sent += len(chunk)
        return chunk

    def flush(self) -> Optional[str]:
        """Emit whatever is left, at the end of an utterance."""
        pending = self.pending()
        self._buffer = ""
        self._sent = 0
        return pending or None


class CallTranslator:
    """Translation state for one live call."""

    def __init__(
        self,
        *,
        call_id: str,
        device_id: Optional[int],
        engine,
        mode: Optional[str] = None,
        cadence: Optional[str] = None,
        my_language: Optional[str] = None,
        peer_language: Optional[str] = None,
        glossary: Optional[List[str]] = None,
        publish: Optional[Publish] = None,
        speak_into_call: Optional[Speak] = None,
        play_to_operator: Optional[PlayLocal] = None,
        transcribe: Optional[Callable[[bytes, Optional[str]], Awaitable[str]]] = None,
    ) -> None:
        self.call_id = call_id
        self.device_id = device_id
        self._engine = engine
        self.mode = self._normalize_mode(mode)
        self.cadence = self._normalize_cadence(cadence)
        self.my_language = (my_language or settings.translate_my_language or "zh").strip()
        self.peer_language = (peer_language or settings.translate_peer_language or "en").strip()
        self.glossary = list(glossary or [])
        self._publish = publish
        self._speak = speak_into_call
        self._play_local = play_to_operator
        self._transcribe = transcribe
        self._accumulators: Dict[str, StreamAccumulator] = {
            "peer": StreamAccumulator(), "me": StreamAccumulator()
        }
        self._history: List[dict] = []
        self._lock = asyncio.Lock()
        # While audio is playing into the call the far end's capture is gated,
        # so our own synthesised voice cannot be recognised and translated back.
        self._speaking_into_call = False
        self.degraded_to_sentence = False

    @staticmethod
    def _normalize_mode(mode: Optional[str]) -> str:
        value = (mode or settings.translate_mode or MODE_OFF).strip().lower()
        return value if value in MODES else MODE_OFF

    @staticmethod
    def _normalize_cadence(cadence: Optional[str]) -> str:
        value = (cadence or settings.translate_cadence or CADENCE_SENTENCE).strip().lower()
        return value if value in CADENCES else CADENCE_SENTENCE

    @property
    def active(self) -> bool:
        return self.mode != MODE_OFF

    def state(self) -> dict:
        return {
            "call_id": self.call_id,
            "mode": self.mode,
            "cadence": self.cadence,
            "my_language": self.my_language,
            "peer_language": self.peer_language,
            "degraded_to_sentence": self.degraded_to_sentence,
            "lines": len(self._history),
        }

    def history(self, limit: int = 200) -> List[dict]:
        return list(self._history[-max(1, int(limit)):])

    def set_mode(self, mode: Optional[str]) -> str:
        """Change mode mid-call. Takes effect on the next utterance."""
        self.mode = self._normalize_mode(mode)
        return self.mode

    def set_cadence(self, cadence: Optional[str]) -> str:
        self.cadence = self._normalize_cadence(cadence)
        return self.cadence

    # -- input ----------------------------------------------------------

    async def peer_audio(self, audio: bytes) -> Optional[TranslationResult]:
        """One captured utterance from the far end."""
        if self._speaking_into_call:
            return None
        text = await self._recognize(audio, self.peer_language)
        if not text:
            return None
        return await self.peer_text(text)

    async def my_audio(self, audio: bytes) -> Optional[TranslationResult]:
        """One captured utterance from the operator."""
        text = await self._recognize(audio, self.my_language)
        if not text:
            return None
        return await self.my_text(text)

    async def peer_text(self, text: str, *, partial: bool = False) -> Optional[TranslationResult]:
        """The far end's words, translated into the operator's language."""
        return await self._handle(
            "peer", text, partial=partial,
            source=self.peer_language, target=self.my_language,
        )

    async def my_text(self, text: str, *, partial: bool = False) -> Optional[TranslationResult]:
        """The operator's words, translated into the far end's language."""
        return await self._handle(
            "me", text, partial=partial,
            source=self.my_language, target=self.peer_language,
        )

    async def peer_partial(self, text: str) -> Optional[TranslationResult]:
        """A growing partial transcript from the far end (stream cadence)."""
        chunk = self._accumulators["peer"].observe(text)
        if not chunk:
            return None
        return await self.peer_text(chunk, partial=True)

    async def my_partial(self, text: str) -> Optional[TranslationResult]:
        chunk = self._accumulators["me"].observe(text)
        if not chunk:
            return None
        return await self.my_text(chunk, partial=True)

    async def say_to_peer(self, text: str) -> Optional[TranslationResult]:
        """Operator text that has to reach the far end whatever the mode is.

        Subtitle mode shows translations without speaking them, but an operator
        who typed or dictated a sentence expects it to be said. This forces the
        voice path for one utterance and leaves the configured mode alone, so a
        call running in subtitle mode stays in subtitle mode afterwards.
        """
        previous = self.mode
        self.mode = MODE_VOICE
        try:
            return await self.my_text(text)
        finally:
            self.mode = previous

    async def flush(self, speaker: str = "peer") -> Optional[TranslationResult]:
        """End of utterance: translate whatever the accumulator still holds."""
        chunk = self._accumulators.get(speaker, StreamAccumulator()).flush()
        if not chunk:
            return None
        if speaker == "me":
            return await self.my_text(chunk, partial=True)
        return await self.peer_text(chunk, partial=True)

    # -- pipeline -------------------------------------------------------

    async def _recognize(self, audio: bytes, language: Optional[str]) -> str:
        if self._transcribe is not None:
            return (await self._transcribe(audio, language) or "").strip()
        try:
            text = await self._engine.transcribe(audio, language=language, call_id=self.call_id)
        except Exception as e:  # noqa: BLE001 - a failed recognition is not fatal
            log.warning("translation asr failed for call %s: %s", self.call_id, e)
            await self._emit_error("asr", str(e))
            return ""
        return (text or "").strip()

    async def _handle(
        self, speaker: str, text: str, *, partial: bool, source: str, target: str
    ) -> Optional[TranslationResult]:
        if not self.active:
            return None
        text = (text or "").strip()
        if not text:
            return None
        if partial and self.cadence != CADENCE_STREAM:
            # Sentence cadence ignores growing partials and waits for the final.
            return None
        started = time.monotonic()
        try:
            translated = await self._engine.translate(
                text, source=source, target=target,
                glossary=self.glossary, call_id=self.call_id,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("translation failed for call %s: %s", self.call_id, e)
            await self._emit_error("translate", str(e))
            return None
        translated = (translated or "").strip()
        if not translated:
            return None
        result = TranslationResult(
            call_id=self.call_id,
            speaker=speaker,
            source_text=text,
            target_text=translated,
            source_language=source,
            target_language=target,
            mode=self.mode,
            cadence=self.cadence,
            partial=partial,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        if self.mode == MODE_VOICE:
            await self._speak_result(result)
        async with self._lock:
            self._history.append(result.to_dict())
            if len(self._history) > 400:
                del self._history[: len(self._history) - 400]
        await self._publish_result(result)
        return result

    async def _speak_result(self, result: TranslationResult) -> None:
        """Render and route the translation to the right ear.

        The far end's words go to the operator. Our words go into the call. A
        failure to speak leaves the subtitle in place, so a broken voice path
        degrades to subtitle mode instead of losing the translation.
        """
        try:
            audio = await self._engine.synthesize(
                result.target_text, call_id=self.call_id
            )
        except Exception as e:  # noqa: BLE001
            result.play_error = f"tts: {e}"
            log.warning("translation tts failed for call %s: %s", self.call_id, e)
            await self._emit_error("tts", str(e))
            return
        try:
            if result.speaker == "me":
                if self._speak is None or self.device_id is None:
                    result.play_error = "no audio path into the call"
                    return
                self._speaking_into_call = True
                try:
                    await self._speak(int(self.device_id), result.target_text)
                finally:
                    self._speaking_into_call = False
                result.played = True
            else:
                if self._play_local is None:
                    result.play_error = "no operator audio output"
                    return
                result.played = bool(await self._play_local(audio, f"translate:{self.call_id}"))
                if not result.played:
                    result.play_error = "operator playback refused"
        except Exception as e:  # noqa: BLE001
            result.play_error = str(e)[:300]
            self._speaking_into_call = False
            log.warning("translation playback failed for call %s: %s", self.call_id, e)

    async def _publish_result(self, result: TranslationResult) -> None:
        if self._publish is None:
            return
        try:
            await self._publish("call.translation", result.to_dict())
        except Exception as e:
            log.debug("publishing call.translation failed: %s", e)

    async def _emit_error(self, stage: str, detail: str) -> None:
        if self._publish is None:
            return
        try:
            await self._publish("call.translation.error", {
                "call_id": self.call_id, "stage": stage, "detail": detail[:300],
                "at": int(time.time()),
            })
        except Exception:
            pass


class TranslationManager:
    """One CallTranslator per live call, created when translation is requested."""

    def __init__(self, engine, *, publish: Optional[Publish] = None,
                 speak_into_call: Optional[Speak] = None,
                 play_to_operator: Optional[PlayLocal] = None) -> None:
        self._engine = engine
        self._publish = publish
        self._speak = speak_into_call
        self._play_local = play_to_operator
        self._translators: Dict[str, CallTranslator] = {}

    def get(self, call_id: str) -> Optional[CallTranslator]:
        return self._translators.get(call_id)

    def active_call_ids(self) -> List[str]:
        return [cid for cid, t in self._translators.items() if t.active]

    def start(
        self,
        *,
        call_id: str,
        device_id: Optional[int],
        mode: Optional[str] = None,
        cadence: Optional[str] = None,
        my_language: Optional[str] = None,
        peer_language: Optional[str] = None,
        glossary: Optional[List[str]] = None,
    ) -> CallTranslator:
        translator = CallTranslator(
            call_id=call_id,
            device_id=device_id,
            engine=self._engine,
            mode=mode,
            cadence=cadence,
            my_language=my_language,
            peer_language=peer_language,
            glossary=glossary,
            publish=self._publish,
            speak_into_call=self._speak,
            play_to_operator=self._play_local,
        )
        self._translators[call_id] = translator
        return translator

    def stop(self, call_id: str) -> Optional[CallTranslator]:
        return self._translators.pop(call_id, None)

    def snapshot(self) -> List[dict]:
        return [t.state() for t in self._translators.values()]
