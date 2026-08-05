import pytest

from app.core.exceptions import ConfigurationError
from app.factory import create_app
from app.services.factory import (
    LLM_REGISTRY,
    STT_REGISTRY,
    TTS_REGISTRY,
    Providers,
    build_providers,
)
from app.services.llm.base import LLMProvider
from app.services.llm.mock import MockLLMProvider
from app.services.stt.base import SpeechToTextProvider
from app.services.stt.mock import MockSTTProvider
from app.services.tts.base import TextToSpeechProvider
from app.services.tts.mock import MockTTSProvider
from tests.helpers import FakeSTT, make_settings


def test_mocks_satisfy_the_interfaces():
    assert isinstance(MockSTTProvider(), SpeechToTextProvider)
    assert isinstance(MockLLMProvider(), LLMProvider)
    assert isinstance(MockTTSProvider(), TextToSpeechProvider)


def test_object_missing_the_method_does_not_satisfy_interface():
    class Incomplete:
        name = "x"

    assert not isinstance(Incomplete(), SpeechToTextProvider)
    assert not isinstance(Incomplete(), LLMProvider)
    assert not isinstance(Incomplete(), TextToSpeechProvider)


def test_factory_builds_mock_providers_by_default():
    providers = build_providers(make_settings())
    assert isinstance(providers, Providers)
    assert (providers.stt.name, providers.llm.name, providers.tts.name) == ("mock",) * 3


def test_factory_passes_assistant_name_to_llm():
    providers = build_providers(make_settings(assistant_name="Zed"))
    assert isinstance(providers.llm, MockLLMProvider)


@pytest.mark.parametrize(
    ("field", "kind"),
    [("stt_provider", "stt"), ("llm_provider", "llm"), ("tts_provider", "tts")],
)
def test_unknown_provider_raises_configuration_error(field, kind):
    with pytest.raises(ConfigurationError, match=f"Unknown {kind} provider 'nope'"):
        build_providers(make_settings(**{field: "nope"}))


def test_registries_contain_mock_and_gemini():
    # Each kind also has one extra hosted provider (Groq STT, OpenRouter LLM, ElevenLabs TTS).
    assert set(STT_REGISTRY) == {"mock", "gemini", "groq"}
    assert set(LLM_REGISTRY) == {"mock", "gemini", "openrouter"}
    assert set(TTS_REGISTRY) == {"mock", "gemini", "elevenlabs"}


def test_providers_are_replaceable_via_registry(monkeypatch):
    monkeypatch.setitem(STT_REGISTRY, "fake", lambda settings: FakeSTT())
    providers = build_providers(make_settings(stt_provider="fake"))
    assert providers.stt.name == "fake"
    assert providers.llm.name == "mock"


def test_app_refuses_to_start_with_unknown_provider():
    with pytest.raises(ConfigurationError):
        create_app(make_settings(tts_provider="nope"))
