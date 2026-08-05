import pytest

from app.core.exceptions import NoSpeechDetectedError, ProviderError
from app.services.chat import ChatService
from app.services.llm.mock import MockLLMProvider
from app.services.stt.base import AudioInput
from app.services.stt.mock import MockSTTProvider
from app.services.tts.mock import MockTTSProvider
from app.services.voice.pipeline import VoicePipeline
from app.services.wav import read_wav_info
from tests.helpers import FailingLLM, FailingSTT, FakeLLM, FakeSTT, FakeTTS, make_wav


def _audio() -> AudioInput:
    return AudioInput(data=make_wav(), content_type="audio/wav", sample_rate_hz=16_000)


def _mock_pipeline() -> VoicePipeline:
    chat = ChatService(llm=MockLLMProvider(), assistant_name="Adi")
    return VoicePipeline(stt=MockSTTProvider(), chat=chat, tts=MockTTSProvider())


async def test_full_mock_flow():
    turn = await _mock_pipeline().run(_audio())
    assert turn.transcript == "Hello"
    assert turn.response_text == "Hello! I'm Adi."
    assert turn.audio.content_type == "audio/wav"
    assert read_wav_info(turn.audio.data).frame_count > 0
    assert set(turn.timings_ms) == {"stt", "llm", "tts", "total"}


async def test_pipeline_uses_whatever_providers_it_is_given():
    chat = ChatService(llm=FakeLLM("custom answer"), assistant_name="Adi")
    pipeline = VoicePipeline(stt=FakeSTT("custom question"), chat=chat, tts=FakeTTS())
    turn = await pipeline.run(_audio())
    assert (turn.transcript, turn.response_text) == ("custom question", "custom answer")
    assert turn.audio.data == b"RIFFfake"


async def test_blank_transcript_raises_no_speech():
    chat = ChatService(llm=FakeLLM(), assistant_name="Adi")
    pipeline = VoicePipeline(stt=FakeSTT("   "), chat=chat, tts=FakeTTS())
    with pytest.raises(NoSpeechDetectedError):
        await pipeline.run(_audio())


async def test_stt_failure_becomes_provider_error():
    chat = ChatService(llm=FakeLLM(), assistant_name="Adi")
    pipeline = VoicePipeline(stt=FailingSTT("secret detail"), chat=chat, tts=FakeTTS())
    with pytest.raises(ProviderError) as info:
        await pipeline.run(_audio())
    assert info.value.stage == "stt"
    assert "secret detail" not in info.value.public_message
    assert "secret detail" in info.value.log_detail


async def test_llm_failure_becomes_provider_error():
    chat = ChatService(llm=FailingLLM(), assistant_name="Adi")
    pipeline = VoicePipeline(stt=FakeSTT(), chat=chat, tts=FakeTTS())
    with pytest.raises(ProviderError) as info:
        await pipeline.run(_audio())
    assert info.value.stage == "llm"
