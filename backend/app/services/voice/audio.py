"""Validation of uploaded audio (independent of FastAPI)."""

from app.core.exceptions import (
    InvalidAudioError,
    PayloadTooLargeError,
    UnsupportedAudioFormatError,
)
from app.services.stt.base import AudioInput
from app.services.wav import WavFormatError, read_wav_info

WAV_CONTENT_TYPES = frozenset({"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"})
RAW_CONTENT_TYPES = frozenset({"audio/pcm", "audio/l16", "application/octet-stream"})
SUPPORTED_CONTENT_TYPES = WAV_CONTENT_TYPES | RAW_CONTENT_TYPES


def normalize_content_type(value: str | None) -> str:
    """``"Audio/WAV; charset=x"`` -> ``"audio/wav"``."""
    return (value or "").split(";")[0].strip().lower()


def build_audio_input(data: bytes, content_type: str | None, *, max_bytes: int) -> AudioInput:
    """Validate raw request audio and wrap it as an :class:`AudioInput`."""
    ctype = normalize_content_type(content_type)
    if ctype not in SUPPORTED_CONTENT_TYPES:
        raise UnsupportedAudioFormatError(
            "Unsupported Content-Type. Send one of: " + ", ".join(sorted(SUPPORTED_CONTENT_TYPES)),
            detail=f"received content-type {ctype!r}",
        )
    if not data:
        raise InvalidAudioError("The audio payload is empty.")
    if len(data) > max_bytes:
        raise PayloadTooLargeError(
            f"Audio exceeds the maximum size of {max_bytes} bytes.",
            detail=f"received {len(data)} bytes",
        )

    sample_rate_hz: int | None = None
    if ctype in WAV_CONTENT_TYPES:
        try:
            info = read_wav_info(data)
        except WavFormatError as exc:
            raise InvalidAudioError(
                "The audio payload is not a valid PCM WAV file.", detail=str(exc)
            ) from exc
        if info.frame_count == 0:
            raise InvalidAudioError("The WAV file contains no audio samples.")
        sample_rate_hz = info.sample_rate_hz

    return AudioInput(data=data, content_type=ctype, sample_rate_hz=sample_rate_hz)
