import pytest
from google.genai import types

from app.core.exceptions import InvalidAudioError, ProviderAuthError
from app.services.stt.base import AudioInput, SpeechToTextProvider
from app.services.stt.gemini import GeminiSTTProvider, strip_wake_word
from app.services.wav import is_wav, read_wav_info
from tests.gemini_fakes import FakeGeminiClient, api_error, empty_response, text_response
from tests.helpers import make_wav


def _stt(client, model="gemini-3.5-transcribe", **kw) -> GeminiSTTProvider:
    return GeminiSTTProvider(client, model=model, wake_word="Adi", **kw)


def _wav_input() -> AudioInput:
    return AudioInput(data=make_wav(), content_type="audio/wav", sample_rate_hz=16_000)


def _audio_part(call) -> types.Part:
    return next(c for c in call["contents"] if isinstance(c, types.Part))


def test_satisfies_stt_interface():
    assert isinstance(_stt(FakeGeminiClient()), SpeechToTextProvider)


async def test_dedicated_model_request_and_wake_word_removal():
    client = FakeGeminiClient(text_response("Adi, what is the weather today?"))
    result = await _stt(client, language_codes=["en-IN"], mode="SMART").transcribe(_wav_input())

    assert result.text == "What is the weather today?"  # transcribed, not answered
    assert result.language == "en-IN"
    call = client.last_call
    assert call["model"] == "gemini-3.5-transcribe"
    assert len(call["contents"]) == 1  # audio only, no prompt
    part = _audio_part(call)
    assert part.inline_data.mime_type == "audio/wav"
    assert part.inline_data.data == _wav_input().data
    atc = call["config"].audio_transcription_config
    assert atc.language_codes == ["en-IN"] and atc.mode.value == "SMART"
    assert call["config"].system_instruction is None


async def test_auto_language_detection_by_default():
    client = FakeGeminiClient(text_response("नमस्ते"))
    result = await _stt(client).transcribe(_wav_input())
    assert result.text == "नमस्ते"
    assert result.language is None
    assert client.last_call["config"].audio_transcription_config.language_codes == []


async def test_general_model_uses_transcribe_only_instruction():
    client = FakeGeminiClient(text_response("what is 25 times 8"))
    stt = _stt(client, model="gemini-3.5-flash-lite")
    assert not stt.uses_dedicated_model
    result = await stt.transcribe(_wav_input())
    assert result.text == "what is 25 times 8"
    config = client.last_call["config"]
    assert "Do not answer" in config.system_instruction
    assert config.audio_transcription_config is None and config.temperature == 0.0


async def test_general_model_no_speech_token_becomes_empty():
    stt = _stt(FakeGeminiClient(text_response("<NO_SPEECH>")), model="gemini-3.5-flash-lite")
    assert (await stt.transcribe(_wav_input())).text == ""


async def test_raw_pcm_is_wrapped_as_wav():
    client = FakeGeminiClient(text_response("hello"))
    pcm = AudioInput(data=bytes(3200), content_type="application/octet-stream")
    await _stt(client).transcribe(pcm)
    part = _audio_part(client.last_call)
    assert part.inline_data.mime_type == "audio/wav" and is_wav(part.inline_data.data)
    assert read_wav_info(part.inline_data.data).sample_rate_hz == 16_000


async def test_no_candidates_means_no_speech():
    assert (await _stt(FakeGeminiClient(empty_response())).transcribe(_wav_input())).text == ""


async def test_empty_audio_is_rejected_before_calling_gemini():
    client = FakeGeminiClient(text_response("x"))
    with pytest.raises(InvalidAudioError):
        await _stt(client).transcribe(AudioInput(data=b"", content_type="audio/wav"))
    assert client.models.calls == []


async def test_gemini_rejecting_audio_is_invalid_audio():
    client = FakeGeminiClient(error=api_error(400, "INVALID_ARGUMENT", "Could not decode audio"))
    with pytest.raises(InvalidAudioError):
        await _stt(client).transcribe(_wav_input())


async def test_invalid_key_is_auth_error():
    client = FakeGeminiClient(error=api_error(400, "INVALID_ARGUMENT", "API key not valid"))
    with pytest.raises(ProviderAuthError):
        await _stt(client).transcribe(_wav_input())


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Adi, what is the weather today?", "What is the weather today?"),
        ("hey adi what time is it", "What time is it"),
        ("OK Adi. Play music", "Play music"),
        ("Adinath temple timings", "Adinath temple timings"),
        ("What do you think, Adi?", "What do you think, Adi?"),
        ("Adi", ""),
    ],
)
def test_strip_wake_word(text, expected):
    assert strip_wake_word(text, "Adi") == expected


async def test_transcript_in_audio_transcription_part_is_used():
    part = types.Part(audio_transcription=types.Transcription(text="play love dose"))
    from tests.gemini_fakes import response as make_response

    stt = _stt(FakeGeminiClient(make_response(part)))
    assert (await stt.transcribe(_wav_input())).text == "play love dose"


async def test_transcript_rebuilt_from_words_when_text_missing():
    words = [types.WordInfo(word="what"), types.WordInfo(word="time")]
    part = types.Part(audio_transcription=types.Transcription(words=words))
    from tests.gemini_fakes import response as make_response

    assert (await _stt(FakeGeminiClient(make_response(part))).transcribe(_wav_input())).text == "what time"


async def test_empty_transcript_logs_diagnostics_without_content(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="app.stt"):
        result = await _stt(FakeGeminiClient(text_response(""))).transcribe(_wav_input())
    assert result.text == ""
    record = next(r for r in caplog.records if r.getMessage() == "stt_empty_transcript")
    assert record.finish_reason and record.part_count == 1
