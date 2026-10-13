"""AI integration: chat, speech recognition and speech synthesis."""
from __future__ import annotations

from .engine import AIEngine, AIError, ai_engine
from .providers import ChatMessage, MockProvider, OpenAICompatibleProvider

__all__ = [
    "AIEngine",
    "AIError",
    "ChatMessage",
    "MockProvider",
    "OpenAICompatibleProvider",
    "ai_engine",
]
