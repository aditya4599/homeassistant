"""Provider factory / registry.

To add a provider, write a class that satisfies the relevant Protocol and add
ONE entry to the matching registry below. Nothing else in the codebase needs
to change.

External provider builders import their modules lazily so mock-only setups never
need the provider SDKs. Groq, OpenRouter and ElevenLabs use their HTTP APIs directly.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from app.core.config import Settings
from app.core.exceptions import ConfigurationError
from app.services.llm.base import LLMProvider
from app.services.llm.mock import MockLLMProvider
from app.services.stt.base import SpeechToTextProvider
from app.services.stt.mock import MockSTTProvider
from app.services.tts.base import TextToSpeechProvider
from app.services.tts.mock import MockTTSProvider

T = TypeVar("T")
ProviderBuilder = Callable[[Settings], T]


def _gemini_client(settings: Settings):
    from app.services.gemini.client import create_gemini_client

    return create_gemini_client(settings)


def _build_groq_stt(settings: Settings) -> SpeechToTextProvider:
    from app.services.stt.groq import GroqSTTProvider

    return GroqSTTProvider(
        settings,
        model=settings.groq_stt_model,
        language=settings.groq_stt_language,
        prompt=settings.groq_stt_prompt,
        timeout_seconds=settings.groq_timeout_seconds,
    )


def _build_openrouter_llm(settings: Settings) -> LLMProvider:
    from app.services.llm.openrouter import OpenRouterLLMProvider

    return OpenRouterLLMProvider(
        settings,
        model=settings.openrouter_llm_model,
        max_output_tokens=settings.openrouter_llm_max_output_tokens,
        temperature=settings.openrouter_llm_temperature,
        timeout_seconds=settings.openrouter_timeout_seconds,
        base_url=settings.openrouter_base_url,
        http_referer=settings.openrouter_http_referer,
        title=settings.openrouter_title,
    )


def _build_elevenlabs_tts(settings: Settings) -> TextToSpeechProvider:
    from app.services.tts.elevenlabs import ElevenLabsTTSProvider

    return ElevenLabsTTSProvider(
        settings,
        voice_id=settings.elevenlabs_tts_voice_id,
        model=settings.elevenlabs_tts_model,
        language_code=settings.elevenlabs_tts_language_code,
        output_format=settings.elevenlabs_tts_output_format,
        timeout_seconds=settings.elevenlabs_timeout_seconds,
    )


def _build_gemini_stt(settings: Settings) -> SpeechToTextProvider:
    from app.services.stt.gemini import GeminiSTTProvider

    return GeminiSTTProvider(
        _gemini_client(settings),
        model=settings.gemini_stt_model,
        # The wake word is handled provider-neutrally by the pipeline's WakeWordGate.
        wake_word="",
        language_codes=settings.gemini_stt_language_list,
        mode=settings.gemini_stt_mode,
        timeout_seconds=settings.gemini_timeout_seconds,
    )


def _build_gemini_llm(settings: Settings) -> LLMProvider:
    from app.services.llm.gemini import GeminiLLMProvider

    return GeminiLLMProvider(
        _gemini_client(settings),
        model=settings.gemini_llm_model,
        assistant_name=settings.assistant_name,
        max_output_tokens=settings.gemini_llm_max_output_tokens,
        temperature=settings.gemini_llm_temperature,
        thinking_level=settings.gemini_llm_thinking_level,
        timeout_seconds=settings.gemini_timeout_seconds,
    )


def _build_gemini_tts(settings: Settings) -> TextToSpeechProvider:
    from app.services.tts.gemini import GeminiTTSProvider

    return GeminiTTSProvider(
        _gemini_client(settings),
        model=settings.gemini_tts_model,
        voice=settings.gemini_tts_voice,
        timeout_seconds=settings.gemini_timeout_seconds,
    )


STT_REGISTRY: dict[str, ProviderBuilder[SpeechToTextProvider]] = {
    "mock": lambda settings: MockSTTProvider(),
    "gemini": _build_gemini_stt,
    "groq": _build_groq_stt,
}
LLM_REGISTRY: dict[str, ProviderBuilder[LLMProvider]] = {
    "mock": lambda settings: MockLLMProvider(assistant_name=settings.assistant_name),
    "gemini": _build_gemini_llm,
    "openrouter": _build_openrouter_llm,
}
TTS_REGISTRY: dict[str, ProviderBuilder[TextToSpeechProvider]] = {
    "mock": lambda settings: MockTTSProvider(),
    "gemini": _build_gemini_tts,
    "elevenlabs": _build_elevenlabs_tts,
}


@dataclass(frozen=True, slots=True)
class Providers:
    stt: SpeechToTextProvider
    llm: LLMProvider
    tts: TextToSpeechProvider


def _resolve(kind: str, registry: dict[str, ProviderBuilder[T]], name: str, settings: Settings) -> T:
    try:
        builder = registry[name]
    except KeyError:
        raise ConfigurationError(
            f"Unknown {kind} provider '{name}'. Available: {', '.join(sorted(registry))}."
        ) from None
    return builder(settings)


def build_stt_provider(settings: Settings) -> SpeechToTextProvider:
    return _resolve("stt", STT_REGISTRY, settings.stt_provider, settings)


def build_llm_provider(settings: Settings) -> LLMProvider:
    return _resolve("llm", LLM_REGISTRY, settings.llm_provider, settings)


def build_tts_provider(settings: Settings) -> TextToSpeechProvider:
    return _resolve("tts", TTS_REGISTRY, settings.tts_provider, settings)


def build_providers(settings: Settings) -> Providers:
    return Providers(
        stt=build_stt_provider(settings),
        llm=build_llm_provider(settings),
        tts=build_tts_provider(settings),
    )
