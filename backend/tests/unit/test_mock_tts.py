import pytest

from app.core.exceptions import ProviderError
from app.services.tts.mock import MockTTSProvider
from app.services.wav import read_wav_info


async def test_returns_valid_wav():
    result = await MockTTSProvider().synthesize("Hello! I'm Adi.")
    assert result.content_type == "audio/wav"
    assert result.sample_rate_hz == 16_000
    info = read_wav_info(result.data)
    assert info.frame_count > 0
    assert info.sample_rate_hz == 16_000
    assert result.duration_ms == info.duration_ms


async def test_duration_scales_with_text_and_is_clamped():
    tts = MockTTSProvider()
    short = await tts.synthesize("a")
    medium = await tts.synthesize("a" * 20)
    long = await tts.synthesize("a" * 5000)
    assert short.duration_ms == 300
    assert medium.duration_ms == 1200
    assert long.duration_ms == 3000


async def test_is_deterministic():
    tts = MockTTSProvider()
    assert (await tts.synthesize("same")).data == (await tts.synthesize("same")).data


@pytest.mark.parametrize("text", ["", "   "])
async def test_empty_text_raises_provider_error(text):
    with pytest.raises(ProviderError):
        await MockTTSProvider().synthesize(text)
