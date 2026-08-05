import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.factory import create_app
from tests.helpers import make_settings

_ENV_KEYS = [
    "APP_ENV", "APP_HOST", "APP_PORT", "DATABASE_URL", "STT_PROVIDER", "LLM_PROVIDER",
    "TTS_PROVIDER", "ASSISTANT_NAME", "WAKE_WORD", "LOG_LEVEL", "API_KEY", "MAX_AUDIO_BYTES",
    "GEMINI_API_KEY", "GEMINI_STT_MODEL", "GEMINI_LLM_MODEL", "GEMINI_TTS_MODEL",
    "GEMINI_TTS_VOICE", "GEMINI_LLM_MAX_OUTPUT_TOKENS", "GEMINI_LLM_TEMPERATURE",
    "GEMINI_LLM_THINKING_LEVEL", "GEMINI_STT_LANGUAGE_CODES", "GEMINI_STT_MODE",
    "GEMINI_TIMEOUT_SECONDS",
    "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REDIRECT_URI",
    "SPOTIFY_SCOPES", "SPOTIFY_TOKEN_FILE", "SPOTIFY_TIMEOUT_SECONDS",
]  # fmt: skip


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch):
    """Make tests independent of the developer's shell environment."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)
