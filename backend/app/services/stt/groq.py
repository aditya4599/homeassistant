"""Groq Whisper speech-to-text provider.

Uses Groq's OpenAI-compatible audio transcription endpoint. Audio is always sent as
WAV so the provider receives a concrete container even when the ESP32 sends raw PCM.
The language is optional: leaving it blank lets Whisper auto-detect Hindi, English,
or mixed Hindi-English (Hinglish).
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.core.config import Settings
from app.core.exceptions import (
    ConfigurationError,
    InvalidAudioError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.services.stt.base import AudioInput, TranscriptionResult
from app.services.voice.audio import RAW_CONTENT_TYPES, WAV_CONTENT_TYPES
from app.services.wav import pcm_to_wav

PROVIDER_NAME = "groq"
GROQ_TRANSCRIPT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
RAW_PCM_SAMPLE_RATE_HZ = 16_000


class GroqSTTProvider:
    name = PROVIDER_NAME

    def __init__(
        self,
        settings: Settings,
        *,
        model: str,
        language: str = "",
        prompt: str = "",
        timeout_seconds: float = 60.0,
    ) -> None:
        key = settings.groq_api_key.get_secret_value().strip()
        if not key:
            raise ConfigurationError("STT_PROVIDER=groq requires GROQ_API_KEY.")
        self._api_key = key
        self._model = model
        self._language = language.strip()
        self._prompt = prompt.strip()
        self._timeout = timeout_seconds

    @property
    def model(self) -> str:
        return self._model

    @staticmethod
    def _to_wav(audio: AudioInput) -> bytes:
        if not audio.data:
            raise InvalidAudioError("The audio payload is empty.")
        if audio.content_type in WAV_CONTENT_TYPES:
            return audio.data
        if audio.content_type in RAW_CONTENT_TYPES:
            try:
                return pcm_to_wav(
                    audio.data,
                    sample_rate_hz=audio.sample_rate_hz or RAW_PCM_SAMPLE_RATE_HZ,
                    channels=1,
                    sample_width_bytes=2,
                )
            except ValueError as exc:
                raise InvalidAudioError("The raw PCM audio is invalid.", detail=str(exc)) from exc
        raise InvalidAudioError(
            "Unsupported audio format for Groq transcription.",
            detail=f"content_type={audio.content_type!r}",
        )

    async def transcribe(self, audio: AudioInput) -> TranscriptionResult:
        wav = self._to_wav(audio)
        data: dict[str, Any] = {
            "model": self._model,
            "response_format": "verbose_json",
            "temperature": 0,
        }
        if self._language:
            data["language"] = self._language
        if self._prompt:
            data["prompt"] = self._prompt

        headers = {"Authorization": f"Bearer {self._api_key}"}
        files = {"file": ("audio.wav", wav, "audio/wav")}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    GROQ_TRANSCRIPT_URL,
                    headers=headers,
                    data=data,
                    files=files,
                )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                provider=self.name, stage="stt", detail="Groq transcription timed out"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                provider=self.name, stage="stt", detail=f"Groq network error: {type(exc).__name__}"
            ) from exc

        if response.status_code in (401, 403):
            raise ProviderAuthError(
                provider=self.name, stage="stt", detail=_error_text(response)
            )
        if response.status_code == 429:
            raise ProviderRateLimitError(
                provider=self.name, stage="stt", detail=_error_text(response)
            )
        if response.status_code >= 500:
            raise ProviderUnavailableError(
                provider=self.name, stage="stt", detail=_error_text(response)
            )
        if response.status_code >= 400:
            raise ProviderError(provider=self.name, stage="stt", detail=_error_text(response))

        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(
                provider=self.name, stage="stt", detail="Groq returned invalid JSON"
            ) from exc

        text = str(payload.get("text") or "").strip()
        if not text:
            raise ProviderError(provider=self.name, stage="stt", detail="Groq returned an empty transcript")
        language = payload.get("language")
        return TranscriptionResult(text=text, language=str(language) if language else None)


def _error_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                return str(error.get("message") or error)
            return str(error or payload)
    except (ValueError, json.JSONDecodeError):
        pass
    return response.text[:500] or f"HTTP {response.status_code}"
