"""Shared test helpers and fake providers."""

from collections.abc import Sequence

from app.core.config import Settings
from app.services.llm.base import LLMResponse, Message
from app.services.llm.tools import ToolDefinition
from app.services.stt.base import AudioInput, TranscriptionResult
from app.services.tts.base import AudioResult
from app.services.wav import generate_tone_wav


def make_settings(**overrides) -> Settings:
    """Settings that ignore any local .env file."""
    values = {"app_env": "testing", "log_level": "INFO"}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def make_wav(duration_ms: int = 200) -> bytes:
    return generate_tone_wav(duration_ms)


class FakeSTT:
    name = "fake"

    def __init__(self, text: str = "fake transcript") -> None:
        self._text = text

    async def transcribe(self, audio: AudioInput) -> TranscriptionResult:
        return TranscriptionResult(text=self._text)


class FailingSTT:
    name = "failing-stt"

    def __init__(self, message: str = "boom") -> None:
        self._message = message

    async def transcribe(self, audio: AudioInput) -> TranscriptionResult:
        raise RuntimeError(self._message)


class FakeLLM:
    name = "fake-llm"

    def __init__(self, text: str = "fake reply") -> None:
        self._text = text

    async def generate(
        self, messages: Sequence[Message], tools: Sequence[ToolDefinition] | None = None
    ) -> LLMResponse:
        return LLMResponse(text=self._text)


class FailingLLM:
    name = "failing-llm"

    def __init__(self, message: str = "boom") -> None:
        self._message = message

    async def generate(
        self, messages: Sequence[Message], tools: Sequence[ToolDefinition] | None = None
    ) -> LLMResponse:
        raise RuntimeError(self._message)


class FakeTTS:
    name = "fake-tts"

    async def synthesize(self, text: str) -> AudioResult:
        return AudioResult(data=b"RIFFfake", content_type="audio/wav")
