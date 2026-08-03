"""Gemini text-to-speech provider.

Output is always a WAV file, which is what ``/api/v1/voice`` returns:

* Gemini 3.8 TTS models return a complete WAV (24 kHz, mono, 16-bit) on unary calls.
* Older preview TTS models (e.g. ``gemini-3.1-flash-tts-preview``) return headerless
  16-bit PCM (``audio/L16;rate=24000``), which is wrapped in a WAV header here.
"""

import base64
import re
from typing import Any

from google.genai import types

from app.core.exceptions import ProviderError
from app.services.gemini.client import PROVIDER_NAME, gemini_call, malformed
from app.services.tts.base import AudioResult
from app.services.wav import WavFormatError, is_wav, pcm_to_wav, read_wav_info

DEFAULT_PCM_RATE_HZ = 24_000
_RATE_RE = re.compile(r"rate=(\d+)", re.IGNORECASE)


def _is_legacy_preview_model(model: str) -> bool:
    """Pre-3.8 preview TTS models only take ``prebuilt_voice_config.voice_name``."""
    m = model.lower()
    return m.endswith("-preview-tts") or m.endswith("-tts-preview")


def build_voice_config(model: str, voice: str) -> types.VoiceConfig:
    if _is_legacy_preview_model(model):
        return types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
        )
    # 3.8+: accepts prebuilt names ("Kore") and custom voice IDs ("voice_...").
    return types.VoiceConfig(voice=voice)


def audio_bytes_to_wav(data: bytes, mime_type: str | None) -> tuple[bytes, int]:
    """Normalise Gemini audio output to WAV. Returns ``(wav_bytes, sample_rate_hz)``."""
    if is_wav(data):
        return data, read_wav_info(data).sample_rate_hz
    mime = (mime_type or "").lower()
    if not mime or "l16" in mime or "pcm" in mime:
        match = _RATE_RE.search(mime)
        rate = int(match.group(1)) if match else DEFAULT_PCM_RATE_HZ
        return pcm_to_wav(data, sample_rate_hz=rate), rate
    raise ValueError(f"unsupported audio mime type {mime!r}")


class GeminiTTSProvider:
    """Implements :class:`app.services.tts.base.TextToSpeechProvider` on Gemini."""

    name = PROVIDER_NAME

    def __init__(
        self, client: Any, *, model: str, voice: str, timeout_seconds: float = 30.0
    ) -> None:
        self._client = client
        self._model = model
        self._voice = voice
        self._timeout = timeout_seconds

    @property
    def model(self) -> str:
        return self._model

    def build_config(self) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=build_voice_config(self._model, self._voice)
            ),
        )

    async def synthesize(self, text: str) -> AudioResult:
        text = text.strip()
        if not text:
            raise ProviderError(provider=self.name, stage="tts", detail="text is empty")
        config = self.build_config()
        response = await gemini_call(
            lambda: self._client.aio.models.generate_content(
                model=self._model, contents=text, config=config
            ),
            stage="tts",
            timeout_seconds=self._timeout,
        )
        data, mime_type = self._extract_audio(response)
        try:
            wav, rate = audio_bytes_to_wav(data, mime_type)
            info = read_wav_info(wav)
        except (ValueError, WavFormatError) as exc:
            raise malformed("tts", str(exc)) from exc
        if info.frame_count == 0:
            raise malformed("tts", "audio contains no samples")
        return AudioResult(
            data=wav, content_type="audio/wav", sample_rate_hz=rate, duration_ms=info.duration_ms
        )

    @staticmethod
    def _extract_audio(response: Any) -> tuple[bytes, str | None]:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            raise malformed("tts", "no candidates")
        parts = getattr(getattr(candidates[0], "content", None), "parts", None) or []
        for part in parts:
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None)
            if data:
                if isinstance(data, str):  # defensive: SDK normally decodes base64 for us
                    data = base64.b64decode(data)
                return bytes(data), getattr(inline, "mime_type", None)
        raise malformed("tts", "no inline audio in response")
