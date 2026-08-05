import pytest

from app.core.exceptions import ConfigurationError
from app.services.factory import build_providers
from app.services.llm.gemini import GeminiLLMProvider
from app.services.llm.mock import MockLLMProvider
from app.services.stt.gemini import GeminiSTTProvider
from app.services.stt.mock import MockSTTProvider
from app.services.tts.gemini import GeminiTTSProvider
from app.services.tts.mock import MockTTSProvider
from tests.helpers import make_settings

KEY = "test-key-not-real"


def test_all_mock_still_selects_mocks():
    p = build_providers(make_settings())
    assert isinstance(p.stt, MockSTTProvider)
    assert isinstance(p.llm, MockLLMProvider)
    assert isinstance(p.tts, MockTTSProvider)


def test_all_gemini_selects_gemini_with_configured_models():
    p = build_providers(
        make_settings(
            stt_provider="gemini", llm_provider="gemini", tts_provider="gemini",
            gemini_api_key=KEY, gemini_stt_model="stt-m", gemini_llm_model="llm-m",
            gemini_tts_model="tts-m",
        )
    )  # fmt: skip
    assert isinstance(p.stt, GeminiSTTProvider) and p.stt.model == "stt-m"
    assert isinstance(p.llm, GeminiLLMProvider) and p.llm.model == "llm-m"
    assert isinstance(p.tts, GeminiTTSProvider) and p.tts.model == "tts-m"
    assert (p.stt.name, p.llm.name, p.tts.name) == ("gemini",) * 3


def test_mixed_configuration():
    p = build_providers(make_settings(stt_provider="gemini", gemini_api_key=KEY))
    assert isinstance(p.stt, GeminiSTTProvider)
    assert isinstance(p.llm, MockLLMProvider)
    assert isinstance(p.tts, MockTTSProvider)


@pytest.mark.parametrize("field", ["stt_provider", "llm_provider", "tts_provider"])
def test_gemini_without_key_fails_fast(field):
    with pytest.raises(ConfigurationError, match="GEMINI_API_KEY"):
        build_providers(make_settings(**{field: "gemini"}))


def test_provider_names_are_case_insensitive():
    p = build_providers(make_settings(llm_provider=" Gemini ", gemini_api_key=KEY))
    assert isinstance(p.llm, GeminiLLMProvider)
