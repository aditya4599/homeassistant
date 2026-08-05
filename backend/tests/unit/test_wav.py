import pytest

from app.services.wav import WavFormatError, generate_tone_wav, read_wav_info


def test_generated_wav_is_valid():
    data = generate_tone_wav(500, sample_rate_hz=16_000)
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    info = read_wav_info(data)
    assert info.sample_rate_hz == 16_000
    assert info.channels == 1
    assert info.sample_width_bytes == 2
    assert info.frame_count == 8000
    assert info.duration_ms == 500


def test_generation_is_deterministic():
    assert generate_tone_wav(100) == generate_tone_wav(100)


@pytest.mark.parametrize("kwargs", [{"duration_ms": 0}, {"duration_ms": 10, "sample_rate_hz": 0},
                                    {"duration_ms": 10, "amplitude": 0}])  # fmt: skip
def test_generation_validates_arguments(kwargs):
    with pytest.raises(ValueError):
        generate_tone_wav(**kwargs)


@pytest.mark.parametrize("data", [b"", b"abc", b"not a wav file at all"])
def test_read_wav_info_rejects_garbage(data):
    with pytest.raises(WavFormatError):
        read_wav_info(data)


def test_pcm_to_wav_roundtrip():
    from app.services.wav import is_wav, pcm_to_wav

    wav = pcm_to_wav(bytes(4801), sample_rate_hz=24_000)  # trailing half-frame dropped
    assert is_wav(wav)
    info = read_wav_info(wav)
    assert (info.sample_rate_hz, info.frame_count) == (24_000, 2400)
    with pytest.raises(ValueError):
        pcm_to_wav(b"", sample_rate_hz=24_000)
