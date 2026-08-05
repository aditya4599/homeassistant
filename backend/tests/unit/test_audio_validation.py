import pytest

from app.core.exceptions import (
    InvalidAudioError,
    PayloadTooLargeError,
    UnsupportedAudioFormatError,
)
from app.services.voice.audio import build_audio_input, normalize_content_type
from tests.helpers import make_wav

LIMIT = 1_000_000


def test_normalize_content_type():
    assert normalize_content_type("Audio/WAV; charset=binary") == "audio/wav"
    assert normalize_content_type(None) == ""


def test_valid_wav_is_accepted_and_sample_rate_detected():
    audio = build_audio_input(make_wav(), "audio/wav", max_bytes=LIMIT)
    assert audio.content_type == "audio/wav"
    assert audio.sample_rate_hz == 16_000


def test_raw_pcm_is_accepted_without_header_checks():
    audio = build_audio_input(b"\x00\x01" * 100, "application/octet-stream", max_bytes=LIMIT)
    assert audio.sample_rate_hz is None


@pytest.mark.parametrize("ctype", [None, "", "text/plain", "application/json"])
def test_unsupported_content_type(ctype):
    with pytest.raises(UnsupportedAudioFormatError):
        build_audio_input(make_wav(), ctype, max_bytes=LIMIT)


def test_empty_audio_rejected():
    with pytest.raises(InvalidAudioError):
        build_audio_input(b"", "audio/wav", max_bytes=LIMIT)


def test_corrupt_wav_rejected():
    with pytest.raises(InvalidAudioError):
        build_audio_input(b"this is not a wav", "audio/wav", max_bytes=LIMIT)


def test_too_large_rejected():
    with pytest.raises(PayloadTooLargeError):
        build_audio_input(make_wav(), "audio/wav", max_bytes=100)
