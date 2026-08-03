"""Gemini client construction and error translation.

Everything that talks to the ``google-genai`` SDK goes through :func:`gemini_call`
so that every provider gets the same timeout and error mapping. Raw upstream
error text is only ever placed in ``log_detail`` (logged, never returned), and
the API key never appears in either.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from app.core.config import Settings
from app.core.exceptions import (
    AppError,
    ConfigurationError,
    InvalidAudioError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

T = TypeVar("T")

PROVIDER_NAME = "gemini"
_MAX_DETAIL_CHARS = 300


def create_gemini_client(settings: Settings) -> genai.Client:
    """Build a client from settings. Does not make any network call."""
    api_key = settings.gemini_api_key.get_secret_value().strip()
    if not api_key:
        raise ConfigurationError(
            "GEMINI_API_KEY is required when STT_PROVIDER, LLM_PROVIDER or "
            "TTS_PROVIDER is 'gemini', or when WEB_SEARCH_PROVIDER=gemini."
        )
    timeout_ms = int(settings.gemini_timeout_seconds * 1000)
    return genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=timeout_ms))


async def gemini_call(
    call: Callable[[], Awaitable[T]], *, stage: str, timeout_seconds: float
) -> T:
    """Run one SDK call with a hard timeout and translate any failure to an AppError."""
    try:
        # The SDK has its own HTTP timeout; this is a safety net slightly above it.
        return await asyncio.wait_for(call(), timeout=timeout_seconds + 5)
    except AppError:
        raise
    except Exception as exc:  # noqa: BLE001 - everything is translated below
        raise map_gemini_error(exc, stage=stage) from exc


def _clip(text: Any) -> str:
    text = str(text or "")
    return text if len(text) <= _MAX_DETAIL_CHARS else text[:_MAX_DETAIL_CHARS] + "..."


def map_gemini_error(exc: BaseException, *, stage: str) -> AppError:
    """Translate an SDK / transport exception into the app's error hierarchy."""
    p = PROVIDER_NAME

    if isinstance(exc, genai_errors.APIError):
        code = getattr(exc, "code", None)
        status = str(getattr(exc, "status", "") or "")
        message = str(getattr(exc, "message", "") or "")
        detail = f"HTTP {code} {status}: {_clip(message)}"
        lowered = message.lower()

        if code in (401, 403) or "api key" in lowered or "api_key" in lowered:
            return ProviderAuthError(provider=p, stage=stage, detail=detail)
        if code == 429 or status == "RESOURCE_EXHAUSTED":
            return ProviderRateLimitError(provider=p, stage=stage, detail=detail)
        if code in (408, 504) or status == "DEADLINE_EXCEEDED":
            return ProviderTimeoutError(provider=p, stage=stage, detail=detail)
        if code == 404:
            return ProviderError(
                provider=p, stage=stage, detail=f"model not found or unsupported - {detail}"
            )
        if code == 400 and stage == "stt":
            return InvalidAudioError(
                "The speech provider could not process this audio.", detail=detail
            )
        if isinstance(code, int) and code >= 500:
            return ProviderUnavailableError(provider=p, stage=stage, detail=detail)
        return ProviderError(provider=p, stage=stage, detail=detail)

    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException)):
        return ProviderTimeoutError(provider=p, stage=stage, detail=type(exc).__name__)
    if isinstance(exc, (httpx.TransportError, ConnectionError, OSError)):
        return ProviderUnavailableError(
            provider=p, stage=stage, detail=f"network error: {type(exc).__name__}"
        )
    return ProviderError(
        provider=p, stage=stage, detail=f"{type(exc).__name__}: {_clip(exc)}"
    )


def malformed(stage: str, detail: str) -> ProviderError:
    """Gemini answered, but not in a shape we can use."""
    return ProviderError(provider=PROVIDER_NAME, stage=stage, detail=f"malformed response: {detail}")
