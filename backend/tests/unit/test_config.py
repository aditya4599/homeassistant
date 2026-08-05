import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import DEFAULT_SQLITE_URL, Settings, mask_database_url
from tests.helpers import make_settings


def test_defaults():
    s = Settings(_env_file=None)
    assert s.app_env == "development"
    assert s.app_port == 8000
    assert (s.stt_provider, s.llm_provider, s.tts_provider) == ("mock", "mock", "mock")
    assert s.assistant_name == "Adi"
    assert s.wake_word == "Adi"
    assert s.log_level == "INFO"
    assert s.api_key.get_secret_value() == ""


def test_environment_overrides(monkeypatch):
    monkeypatch.setenv("ASSISTANT_NAME", "Zed")
    monkeypatch.setenv("APP_PORT", "9001")
    monkeypatch.setenv("LLM_PROVIDER", "  MOCK ")
    s = Settings(_env_file=None)
    assert s.assistant_name == "Zed"
    assert s.app_port == 9001
    assert s.llm_provider == "mock"  # normalised


def test_log_level_is_normalised_and_validated():
    assert make_settings(log_level="debug").log_level == "DEBUG"
    with pytest.raises(ValidationError):
        make_settings(log_level="LOUD")


def test_invalid_port_rejected():
    with pytest.raises(ValidationError):
        make_settings(app_port=0)


def test_empty_provider_rejected():
    with pytest.raises(ValidationError):
        make_settings(stt_provider="   ")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", DEFAULT_SQLITE_URL),
        ("sqlite:///x.db", "sqlite:///x.db"),
        ("postgres://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgresql://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
    ],
)
def test_effective_database_url(raw, expected):
    assert make_settings(database_url=raw).effective_database_url == expected


def test_safe_summary_hides_secrets():
    s = make_settings(database_url="postgresql://user:pw123@host/db", api_key="topsecret")
    dumped = json.dumps(s.safe_summary())
    assert "pw123" not in dumped
    assert "topsecret" not in dumped
    assert s.safe_summary()["auth_enabled"] is True


def test_mask_database_url():
    assert mask_database_url("postgresql+psycopg://u:pw@h:5432/db") == (
        "postgresql+psycopg://u:***@h:5432/db"
    )
    assert mask_database_url("sqlite:///x.db") == "sqlite:///x.db"


def test_env_example_matches_settings_fields():
    """.env.example must document every setting and contain no unknown keys."""
    example = Path(__file__).resolve().parents[2] / ".env.example"
    keys = set()
    for line in example.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            keys.add(line.split("=", 1)[0].lower())
    assert keys == set(Settings.model_fields)


def test_gemini_defaults_and_secret_handling():
    s = make_settings(gemini_api_key="AIza-secret")
    assert s.gemini_stt_model == "gemini-3.5-transcribe"
    assert s.gemini_llm_model == "gemini-3.5-flash-lite"
    assert s.gemini_tts_model == "gemini-3.8-flash-lite-tts"
    assert s.gemini_tts_voice == "Kore"
    assert s.gemini_llm_temperature is None and s.gemini_llm_thinking_level == ""
    summary = json.dumps(s.safe_summary())
    assert "AIza-secret" not in summary
    assert s.safe_summary()["gemini_api_key_set"] is True


def test_gemini_env_parsing(monkeypatch):
    monkeypatch.setenv("GEMINI_LLM_TEMPERATURE", "")
    monkeypatch.setenv("GEMINI_LLM_THINKING_LEVEL", " LOW ")
    monkeypatch.setenv("GEMINI_STT_MODE", "smart")
    monkeypatch.setenv("GEMINI_STT_LANGUAGE_CODES", "en-IN, hi-IN,")
    s = Settings(_env_file=None)
    assert s.gemini_llm_temperature is None
    assert s.gemini_llm_thinking_level == "low"
    assert s.gemini_stt_mode == "SMART"
    assert s.gemini_stt_language_list == ["en-IN", "hi-IN"]


@pytest.mark.parametrize(
    "overrides",
    [{"gemini_llm_thinking_level": "turbo"}, {"gemini_stt_mode": "FAST"},
     {"gemini_llm_temperature": 5}, {"gemini_llm_max_output_tokens": 0},
     {"gemini_llm_model": "  "}],
)  # fmt: skip
def test_invalid_gemini_settings_rejected(overrides):
    with pytest.raises(ValidationError):
        make_settings(**overrides)
