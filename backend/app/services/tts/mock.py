"""Mock TTS: returns a valid WAV file containing a short tone."""

from app.core.exceptions import ProviderError
from app.services.tts.base import AudioResult
from app.services.wav import generate_tone_wav

_MS_PER_CHAR = 60
_MIN_MS = 300
_MAX_MS = 3000


class MockTTSProvider:
    name = "mock"

    def __init__(self, sample_rate_hz: int = 16_000) -> None:
        self._sample_rate_hz = sample_rate_hz

    async def synthesize(self, text: str) -> AudioResult:
        if not text.strip():
            raise ProviderError(provider=self.name, stage="tts", detail="text is empty")
        duration_ms = max(_MIN_MS, min(_MAX_MS, len(text) * _MS_PER_CHAR))
        data = generate_tone_wav(duration_ms, sample_rate_hz=self._sample_rate_hz)
        return AudioResult(
            data=data,
            content_type="audio/wav",
            sample_rate_hz=self._sample_rate_hz,
            duration_ms=duration_ms,
        )
