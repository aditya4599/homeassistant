import asyncio

import httpx
import pytest

from app.core.exceptions import (
    ConfigurationError,
    InvalidAudioError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.services.gemini.client import create_gemini_client, gemini_call, map_gemini_error
from tests.gemini_fakes import api_error
from tests.helpers import make_settings


@pytest.mark.parametrize(
    ("exc", "stage", "expected", "http_status"),
    [
        (api_error(400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key."),
         "llm", ProviderAuthError, 502),
        (api_error(403, "PERMISSION_DENIED", "denied"), "tts", ProviderAuthError, 502),
        (api_error(429, "RESOURCE_EXHAUSTED", "Quota exceeded"), "llm", ProviderRateLimitError, 503),
        (api_error(504, "DEADLINE_EXCEEDED", "slow"), "llm", ProviderTimeoutError, 504),
        (api_error(503, "UNAVAILABLE", "overloaded"), "tts", ProviderUnavailableError, 503),
        (api_error(500, "INTERNAL", "oops"), "llm", ProviderUnavailableError, 503),
        (api_error(404, "NOT_FOUND", "models/x is not found"), "llm", ProviderError, 502),
        (api_error(400, "INVALID_ARGUMENT", "Unsupported audio"), "stt", InvalidAudioError, 400),
        (api_error(400, "INVALID_ARGUMENT", "bad request"), "llm", ProviderError, 502),
        (httpx.ReadTimeout("t"), "llm", ProviderTimeoutError, 504),
        (asyncio.TimeoutError(), "stt", ProviderTimeoutError, 504),
        (httpx.ConnectError("no route"), "tts", ProviderUnavailableError, 503),
        (RuntimeError("weird"), "llm", ProviderError, 502),
    ],
)  # fmt: skip
def test_error_mapping(exc, stage, expected, http_status):
    mapped = map_gemini_error(exc, stage=stage)
    assert type(mapped) is expected
    assert mapped.status_code == http_status


def test_public_message_never_contains_upstream_text():
    mapped = map_gemini_error(api_error(429, "RESOURCE_EXHAUSTED", "project 12345 quota"), stage="llm")
    assert "12345" not in mapped.public_message
    assert "12345" in mapped.log_detail  # ...but the operator can see it in logs


def test_404_is_reported_as_unsupported_model_in_logs():
    mapped = map_gemini_error(api_error(404, "NOT_FOUND", "no such model"), stage="llm")
    assert "model not found or unsupported" in mapped.log_detail


async def test_gemini_call_translates_exceptions():
    async def boom():
        raise api_error(429, "RESOURCE_EXHAUSTED", "quota")

    with pytest.raises(ProviderRateLimitError):
        await gemini_call(boom, stage="llm", timeout_seconds=1)


async def test_gemini_call_enforces_hard_timeout(monkeypatch):
    async def slow():
        await asyncio.sleep(10)

    # Shrink the safety margin so the test is fast.
    monkeypatch.setattr(asyncio, "wait_for", _fast_wait_for(asyncio.wait_for))
    with pytest.raises(ProviderTimeoutError):
        await gemini_call(slow, stage="tts", timeout_seconds=0.01)


def _fast_wait_for(original):
    async def wait_for(aw, timeout):
        return await original(aw, timeout=0.01)

    return wait_for


def test_client_requires_api_key():
    with pytest.raises(ConfigurationError, match="GEMINI_API_KEY"):
        create_gemini_client(make_settings(gemini_api_key=""))


def test_client_builds_without_network():
    client = create_gemini_client(make_settings(gemini_api_key="test-key-not-real"))
    assert hasattr(client, "aio")
