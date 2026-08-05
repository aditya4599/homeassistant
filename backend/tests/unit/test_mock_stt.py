import pytest

from app.services.stt.base import AudioInput
from app.services.stt.mock import MockSTTProvider


async def test_returns_fixed_transcript():
    result = await MockSTTProvider().transcribe(AudioInput(data=b"\x00\x01"))
    assert result.text == "Hello"
    assert result.language == "en"
    assert result.confidence == 1.0


async def test_custom_transcript():
    result = await MockSTTProvider(transcript="What time is it").transcribe(AudioInput(data=b"x"))
    assert result.text == "What time is it"


async def test_empty_audio_is_rejected():
    with pytest.raises(ValueError):
        await MockSTTProvider().transcribe(AudioInput(data=b""))


def test_name():
    assert MockSTTProvider().name == "mock"
