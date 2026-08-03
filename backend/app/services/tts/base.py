"""Text-to-speech provider interface."""

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class AudioResult:
    """Synthesised audio returned by a TTS provider."""

    data: bytes = field(repr=False)
    content_type: str
    sample_rate_hz: int | None = None
    duration_ms: int | None = None


@runtime_checkable
class TextToSpeechProvider(Protocol):
    """Anything with a ``name`` and an async ``synthesize`` can be a TTS provider."""

    name: str

    async def synthesize(self, text: str) -> AudioResult: ...
