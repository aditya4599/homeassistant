from urllib.parse import unquote

import pytest
from pydantic import ValidationError

from app.schemas.chat import ChatRequest
from app.schemas.voice import VoiceMetadata


def test_chat_request_strips_whitespace():
    assert ChatRequest(message="  Hello  ").message == "Hello"


@pytest.mark.parametrize("value", ["", "   ", "x" * 4001])
def test_chat_request_rejects_bad_messages(value):
    with pytest.raises(ValidationError):
        ChatRequest(message=value)


def test_voice_headers_are_ascii_and_round_trip():
    text = "नमस्ते, कैसे हो?"
    headers = VoiceMetadata(
        transcript=text, response_text="Hello! I'm Adi.", audio_content_type="audio/wav",
        audio_duration_ms=900,
    ).to_headers()  # fmt: skip
    assert all(v.isascii() for v in headers.values())
    assert unquote(headers["X-Transcript"]) == text
    assert unquote(headers["X-Response-Text"]) == "Hello! I'm Adi."
    assert headers["X-Audio-Duration-Ms"] == "900"


def test_voice_headers_truncate_long_text():
    headers = VoiceMetadata(
        transcript="a" * 500, response_text="b", audio_content_type="audio/wav"
    ).to_headers()
    assert len(unquote(headers["X-Transcript"])) == 200
    assert "X-Audio-Duration-Ms" not in headers
