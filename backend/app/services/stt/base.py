"""Speech-to-text provider interface."""

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class AudioInput:
    """Audio handed to an STT provider."""

    data: bytes = field(repr=False)
    content_type: str = "audio/wav"
    sample_rate_hz: int | None = None


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    text: str
    language: str | None = None
    confidence: float | None = None


@runtime_checkable
class SpeechToTextProvider(Protocol):
    """Anything with a ``name`` and an async ``transcribe`` can be an STT provider."""

    name: str

    async def transcribe(self, audio: AudioInput) -> TranscriptionResult: ...
