import pytest

from app.core.exceptions import ProviderError, ProviderUnavailableError
from app.services.tts.base import TextToSpeechProvider
from app.services.tts.gemini import GeminiTTSProvider, audio_bytes_to_wav
from app.services.wav import read_wav_info
from tests.gemini_fakes import (
    FakeGeminiClient,
    api_error,
    audio_response,
    empty_response,
    text_response,
    tts_wav,
)


def _tts(client, model="gemini-3.8-flash-lite-tts", voice="Kore") -> GeminiTTSProvider:
    return GeminiTTSProvider(client, model=model, voice=voice)


def test_satisfies_tts_interface():
    assert isinstance(_tts(FakeGeminiClient()), TextToSpeechProvider)


async def test_wav_output_is_passed_through():
    wav = tts_wav()
    client = FakeGeminiClient(audio_response(wav, "audio/wav"))
    result = await _tts(client).synthesize("That's 200.")

    assert result.data == wav
    assert result.content_type == "audio/wav"
    assert result.sample_rate_hz == 24_000
    assert result.duration_ms == 400
    call = client.last_call
    assert call["model"] == "gemini-3.8-flash-lite-tts"
    assert call["contents"] == "That's 200."
    config = call["config"]
    assert [m.value if hasattr(m, "value") else m for m in config.response_modalities] == ["AUDIO"]
    assert config.speech_config.voice_config.voice == "Kore"


async def test_raw_pcm_output_is_wrapped_in_wav():
    pcm = bytes(4800)  # 100 ms of 24 kHz mono 16-bit silence
    client = FakeGeminiClient(audio_response(pcm, "audio/L16;codec=pcm;rate=24000"))
    result = await _tts(client).synthesize("hi")
    info = read_wav_info(result.data)
    assert (info.sample_rate_hz, info.channels, info.sample_width_bytes) == (24_000, 1, 2)
    assert info.frame_count == 2400 and result.duration_ms == 100


async def test_legacy_preview_model_uses_prebuilt_voice_config():
    client = FakeGeminiClient(audio_response(tts_wav(), "audio/wav"))
    await _tts(client, model="gemini-3.1-flash-tts-preview", voice="Puck").synthesize("hi")
    voice_config = client.last_call["config"].speech_config.voice_config
    assert voice_config.prebuilt_voice_config.voice_name == "Puck"
    assert voice_config.voice is None


def test_pcm_rate_is_read_from_mime_type():
    _, rate = audio_bytes_to_wav(bytes(3200), "audio/l16;rate=16000")
    assert rate == 16_000


@pytest.mark.parametrize(
    "bad",
    [empty_response(), text_response("no audio here"), audio_response(b"\x00" * 10, "audio/mulaw")],
)
async def test_malformed_response_is_provider_error(bad):
    with pytest.raises(ProviderError):
        await _tts(FakeGeminiClient(bad)).synthesize("hi")


async def test_blank_text_is_rejected_without_calling_gemini():
    client = FakeGeminiClient()
    with pytest.raises(ProviderError):
        await _tts(client).synthesize("   ")
    assert client.models.calls == []


async def test_api_error_is_translated():
    client = FakeGeminiClient(error=api_error(503, "UNAVAILABLE", "overloaded"))
    with pytest.raises(ProviderUnavailableError):
        await _tts(client).synthesize("hi")
