"""ElevenLabs text-to-speech provider.

The ESP32 path consumes WAV/PCM, so this provider requests raw 16 kHz PCM from
ElevenLabs and wraps it in a standard mono 16-bit WAV container before returning it.
Leaving ELEVENLABS_TTS_LANGUAGE_CODE empty lets the multilingual model infer the
language from the generated text, which is useful for English, Hindi, and Hinglish.
"""

from __future__ import annotations

import json

import httpx

from app.core.config import Settings
from app.core.exceptions import (
    ConfigurationError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.services.tts.base import AudioResult
from app.services.wav import pcm_to_wav

PROVIDER_NAME = "elevenlabs"
ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"


class ElevenLabsTTSProvider:
    name = PROVIDER_NAME

    def __init__(
        self,
        settings: Settings,
        *,
        voice_id: str,
        model: str,
        language_code: str = "",
        output_format: str = "pcm_16000",
        timeout_seconds: float = 60.0,
    ) -> None:
        key = settings.elevenlabs_api_key.get_secret_value().strip()
        if not key:
            raise ConfigurationError("TTS_PROVIDER=elevenlabs requires ELEVENLABS_API_KEY.")
        if not voice_id.strip():
            raise ConfigurationError("ELEVENLABS_TTS_VOICE_ID must contain an ElevenLabs voice ID.")
        self._api_key = key
        self._voice_id = voice_id.strip()
        self._model = model.strip()
        self._language_code = language_code.strip()
        self._output_format = output_format.strip()
        self._timeout = timeout_seconds

    @property
    def voice_id(self) -> str:
        return self._voice_id

    @property
    def model(self) -> str:
        return self._model

    async def synthesize(self, text: str) -> AudioResult:
        text = text.strip()
        if not text:
            raise ProviderError(provider=self.name, stage="tts", detail="text is empty")

        params = {"output_format": self._output_format}
        body = {"text": text, "model_id": self._model}
        if self._language_code:
            body["language_code"] = self._language_code

        headers = {
            "xi-api-key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/octet-stream",
        }
        url = ELEVENLABS_TTS_URL.format(voice_id=self._voice_id)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, params=params, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                provider=self.name, stage="tts", detail="ElevenLabs synthesis timed out"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                provider=self.name,
                stage="tts",
                detail=f"ElevenLabs network error: {type(exc).__name__}",
            ) from exc

        if response.status_code in (401, 403):
            raise ProviderAuthError(
                provider=self.name, stage="tts", detail=_error_text(response)
            )
        if response.status_code == 429:
            raise ProviderRateLimitError(
                provider=self.name, stage="tts", detail=_error_text(response)
            )
        if response.status_code >= 500:
            raise ProviderUnavailableError(
                provider=self.name, stage="tts", detail=_error_text(response)
            )
        if response.status_code >= 400:
            raise ProviderError(provider=self.name, stage="tts", detail=_error_text(response))

        # pcm_16000 is 16-bit mono PCM. Keep this provider strict so the device
        # pipeline always receives an audio format it knows how to decode.
        if self._output_format != "pcm_16000":
            raise ProviderError(
                provider=self.name,
                stage="tts",
                detail="This integration expects ELEVENLABS_TTS_OUTPUT_FORMAT=pcm_16000",
            )
        pcm = response.content
        if not pcm:
            raise ProviderError(provider=self.name, stage="tts", detail="ElevenLabs returned no audio")
        wav = pcm_to_wav(pcm, sample_rate_hz=16_000, channels=1, sample_width_bytes=2)
        duration_ms = round(len(pcm) / 2 / 16_000 * 1000)
        return AudioResult(
            data=wav,
            content_type="audio/wav",
            sample_rate_hz=16_000,
            duration_ms=duration_ms,
        )


def _error_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            detail = payload.get("detail")
            if isinstance(detail, dict):
                return str(detail.get("message") or detail)
            return str(detail or payload)
    except (ValueError, json.JSONDecodeError):
        pass
    return response.text[:500] or f"HTTP {response.status_code}"
