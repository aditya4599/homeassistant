"""Mock STT: returns a fixed transcript without inspecting the audio."""

from app.services.stt.base import AudioInput, TranscriptionResult


class MockSTTProvider:
    name = "mock"

    def __init__(self, transcript: str = "Hello") -> None:
        self._transcript = transcript

    async def transcribe(self, audio: AudioInput) -> TranscriptionResult:
        if not audio.data:
            raise ValueError("audio data is empty")
        return TranscriptionResult(text=self._transcript, language="en", confidence=1.0)
