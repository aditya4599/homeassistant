"""Gemini speech-to-text provider.

Two modes, chosen from the configured model name:

* **Dedicated transcription model** (name contains ``transcribe``, e.g. the default
  ``gemini-3.5-transcribe``): the audio is the only content and options go in
  ``audio_transcription_config``. This model transcribes; it does not answer.
* **General multimodal model** (any other name): a strict transcription-only system
  instruction is used, because a chat model would otherwise try to answer the question.

Audio is sent inline (no Files API upload), which suits short voice commands.
"""

import logging
from typing import Any

from google.genai import types

from app.core.exceptions import InvalidAudioError
from app.services.gemini.client import PROVIDER_NAME, gemini_call, malformed
from app.services.stt.base import AudioInput, TranscriptionResult
from app.services.voice.audio import RAW_CONTENT_TYPES, WAV_CONTENT_TYPES
from app.services.wake import strip_wake_word  # noqa: F401 - re-exported
from app.services.wav import pcm_to_wav

_logger = logging.getLogger("app.stt")

# /voice accepts headerless PCM; we assume the common ESP32 format for it.
RAW_PCM_SAMPLE_RATE_HZ = 16_000
NO_SPEECH_TOKEN = "<NO_SPEECH>"

_GENERIC_TRANSCRIBE_INSTRUCTION = (
    "You are a speech-to-text engine. Transcribe the speech in the audio exactly as "
    "spoken, in the original language and script. Output ONLY the transcript text. "
    "Do not answer, translate, summarise, explain, or add quotes or labels. If the "
    f"audio contains no intelligible speech, output exactly {NO_SPEECH_TOKEN}."
)


def to_gemini_audio(audio: AudioInput) -> tuple[bytes, str]:
    """Return ``(bytes, mime_type)`` that Gemini accepts for this upload."""
    if not audio.data:
        raise InvalidAudioError("The audio payload is empty.")
    ctype = audio.content_type
    if ctype in WAV_CONTENT_TYPES:
        return audio.data, "audio/wav"
    if ctype in RAW_CONTENT_TYPES:
        rate = audio.sample_rate_hz or RAW_PCM_SAMPLE_RATE_HZ
        try:
            return pcm_to_wav(audio.data, sample_rate_hz=rate), "audio/wav"
        except ValueError as exc:
            raise InvalidAudioError("The raw PCM audio is invalid.", detail=str(exc)) from exc
    # Anything else was already rejected by build_audio_input; pass through defensively.
    return audio.data, ctype


class GeminiSTTProvider:
    """Implements :class:`app.services.stt.base.SpeechToTextProvider` on Gemini."""

    name = PROVIDER_NAME

    def __init__(
        self,
        client: Any,
        *,
        model: str,
        wake_word: str = "",
        language_codes: list[str] | None = None,
        mode: str = "VERBATIM",
        timeout_seconds: float = 30.0,
    ) -> None:
        self._client = client
        self._model = model
        self._wake_word = wake_word
        self._language_codes = list(language_codes or [])
        self._mode = mode
        self._timeout = timeout_seconds

    @property
    def model(self) -> str:
        return self._model

    @property
    def uses_dedicated_model(self) -> bool:
        return "transcribe" in self._model.lower()

    def build_request(self, audio: AudioInput) -> tuple[list[Any], types.GenerateContentConfig]:
        data, mime_type = to_gemini_audio(audio)
        audio_part = types.Part.from_bytes(data=data, mime_type=mime_type)

        if self.uses_dedicated_model:
            transcription = types.AudioTranscriptionConfig(
                language_codes=self._language_codes, mode=self._mode
            )
            return [audio_part], types.GenerateContentConfig(
                audio_transcription_config=transcription
            )

        instruction = _GENERIC_TRANSCRIBE_INSTRUCTION
        if self._language_codes:
            instruction += f" Expected language(s): {', '.join(self._language_codes)}."
        return [audio_part, "Transcribe this audio."], types.GenerateContentConfig(
            system_instruction=instruction, temperature=0.0
        )

    async def transcribe(self, audio: AudioInput) -> TranscriptionResult:
        contents, config = self.build_request(audio)
        response = await gemini_call(
            lambda: self._client.aio.models.generate_content(
                model=self._model, contents=contents, config=config
            ),
            stage="stt",
            timeout_seconds=self._timeout,
        )
        text = self._extract_text(response)
        if text.strip().upper() == NO_SPEECH_TOKEN:
            text = ""
        text = strip_wake_word(text, self._wake_word)
        # Gemini does not return a detected language here; only report one we were told.
        language = self._language_codes[0] if len(self._language_codes) == 1 else None
        return TranscriptionResult(text=text, language=language)

    @staticmethod
    def _extract_text(response: Any) -> str:
        candidates = getattr(response, "candidates", None)
        if candidates is None:
            raise malformed("stt", "response has no candidates field")
        if not candidates:
            # No candidate at all: treat as "nothing recognised" (pipeline -> 422).
            _log_empty_stt(response, "no candidates")
            return ""
        parts = getattr(getattr(candidates[0], "content", None), "parts", None) or []
        pieces: list[str] = []
        for part in parts:
            if getattr(part, "thought", False):
                continue
            text = getattr(part, "text", None)
            if text and text.strip():
                pieces.append(text.strip())
                continue
            # Transcription models can return the transcript as a structured
            # ``audio_transcription`` part instead of plain text.
            tr = getattr(part, "audio_transcription", None)
            if tr is not None:
                tr_text = (getattr(tr, "text", None) or "").strip()
                if not tr_text:
                    words = getattr(tr, "words", None) or []
                    tr_text = " ".join(
                        str(getattr(w, "word", "") or "").strip() for w in words
                    ).strip()
                if tr_text:
                    pieces.append(tr_text)
        text = " ".join(pieces).strip()
        if not text:
            _log_empty_stt(response, "no transcript text")
        return text


def _log_empty_stt(response: Any, reason: str) -> None:
    """Explain an empty transcript without logging any audio or transcript content."""
    try:
        candidates = getattr(response, "candidates", None) or []
        cand = candidates[0] if candidates else None
        parts = getattr(getattr(cand, "content", None), "parts", None) or [] if cand else []
        part_fields = sorted({
            name for part in parts
            for name, value in part.model_dump(exclude_none=True).items()
            if name not in {"thought_signature"}
        }) if parts else []
        usage = getattr(response, "usage_metadata", None)
        details = getattr(usage, "prompt_tokens_details", None) or []
        audio_tokens = sum(
            int(getattr(d, "token_count", 0) or 0) for d in details
            if "AUDIO" in str(getattr(d, "modality", "")).upper()
        )
        feedback = getattr(response, "prompt_feedback", None)
        _logger.warning(
            "stt_empty_transcript",
            extra={
                "reason": reason,
                "finish_reason": str(getattr(cand, "finish_reason", None)),
                "part_count": len(parts),
                "part_fields": part_fields,
                "prompt_tokens": getattr(usage, "prompt_token_count", None),
                # ~25 audio tokens per second: tells us whether Gemini "heard" the whole clip.
                "audio_tokens": audio_tokens or None,
                "block_reason": str(getattr(feedback, "block_reason", None)),
                "model_version": getattr(response, "model_version", None),
            },
        )
    except Exception:  # noqa: BLE001 - diagnostics must never break a turn
        _logger.warning("stt_empty_transcript", extra={"reason": reason})
