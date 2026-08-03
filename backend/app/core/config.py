"""Typed application settings, loaded from environment variables / .env."""

import re
from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SQLITE_URL = "sqlite:///./assistant.db"
_VALID_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})


class Settings(BaseSettings):
    """All runtime configuration. Field names map to UPPER_CASE env vars."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- App ---
    app_env: Literal["development", "testing", "staging", "production"] = "development"
    app_host: str = "0.0.0.0"
    app_port: int = Field(default=8000, ge=1, le=65535)

    # --- Database (empty => local SQLite file; nothing uses the DB yet in Part 1) ---
    database_url: str = ""

    # --- Providers (names are resolved by app.services.factory) ---
    stt_provider: str = "mock"
    llm_provider: str = "mock"
    tts_provider: str = "mock"

    # --- Assistant identity ---
    assistant_name: str = Field(default="Adi", min_length=1)
    wake_word: str = Field(default="Adi", min_length=1)

    # --- Logging ---
    log_level: str = "INFO"

    # --- Security / limits ---
    # Optional shared secret. Empty => authentication disabled (fine for local dev).
    api_key: SecretStr = SecretStr("")
    max_audio_bytes: int = Field(default=5_000_000, gt=0)

    # --- OpenRouter / Groq / ElevenLabs providers ------------------------------
    # Only the credentials for the selected providers are required.
    openrouter_api_key: SecretStr = SecretStr("")
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_llm_model: str = Field(default="openrouter/free", min_length=1)
    openrouter_llm_max_output_tokens: int = Field(default=512, gt=0, le=8192)
    openrouter_llm_temperature: float | None = Field(default=0.4, ge=0.0, le=2.0)
    openrouter_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    openrouter_http_referer: str = ""
    openrouter_title: str = "Adi Voice Assistant"

    groq_api_key: SecretStr = SecretStr("")
    groq_stt_model: str = Field(default="whisper-large-v3-turbo", min_length=1)
    # Leave blank to let Whisper auto-detect Hindi, English, or Hinglish.
    groq_stt_language: str = ""
    groq_stt_prompt: str = ""
    groq_timeout_seconds: float = Field(default=60.0, gt=0, le=600)

    elevenlabs_api_key: SecretStr = SecretStr("")
    # ElevenLabs requires a real voice ID, not a Gemini voice name such as "Kore".
    # This default is a public ElevenLabs voice ID that works with multilingual_v2.
    elevenlabs_tts_voice_id: str = Field(default="JBFqnCBsd6RMkjVDRZzb", min_length=1)
    elevenlabs_tts_model: str = Field(default="eleven_multilingual_v2", min_length=1)
    # Leave blank for automatic language detection from the generated text.
    elevenlabs_tts_language_code: str = ""
    # pcm_16000 is ideal for the ESP32 path; the provider wraps it as WAV.
    elevenlabs_tts_output_format: str = "pcm_16000"
    elevenlabs_timeout_seconds: float = Field(default=60.0, gt=0, le=600)

    # --- Google Gemini (only used when a *_PROVIDER is "gemini") ---
    # Defaults are models whose Standard tier is listed as "Free of charge" on
    # https://ai.google.dev/gemini-api/docs/pricing (checked 2026-09-30). Free-tier
    # rate limits still apply and pricing can change - re-check before relying on it.
    gemini_api_key: SecretStr = SecretStr("")
    gemini_stt_model: str = Field(default="gemini-3.5-transcribe", min_length=1)
    gemini_llm_model: str = Field(default="gemini-3.5-flash-lite", min_length=1)
    gemini_tts_model: str = Field(default="gemini-3.8-flash-lite-tts", min_length=1)
    gemini_tts_voice: str = Field(default="Kore", min_length=1)
    # Hard cap on output tokens. NOTE: Gemini counts *thinking* tokens against this
    # limit, so a very small value can truncate or empty the reply.
    gemini_llm_max_output_tokens: int = Field(default=1024, gt=0)
    # Empty => model default. Gemini 3 docs recommend leaving this at the default.
    gemini_llm_temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    # Empty => model default. One of: minimal, low, medium, high (support varies per model).
    gemini_llm_thinking_level: str = ""
    # Comma-separated BCP-47 codes (e.g. "en-IN,hi-IN"). Empty => automatic detection.
    gemini_stt_language_codes: str = ""
    # Only used by dedicated transcription models: VERBATIM (default) or SMART.
    gemini_stt_mode: Literal["VERBATIM", "SMART"] = "VERBATIM"
    gemini_timeout_seconds: float = Field(default=30.0, gt=0, le=600)

    # --- Spotify (optional; used for personal playback/tool calling) ---
    spotify_client_id: SecretStr = SecretStr("")
    spotify_client_secret: SecretStr = SecretStr("")
    spotify_redirect_uri: str = "http://127.0.0.1:8000/api/v1/spotify/callback"
    spotify_scopes: str = "user-read-playback-state user-modify-playback-state"
    spotify_token_file: str = ".spotify_token.json"
    spotify_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    # Spotify Connect name of the ADI speaker itself (e.g. "ADI Speaker"). Commands spoken to
    # the ADI device with no device named play there. Empty = keep using the active device.
    spotify_default_device: str = ""

    # --- Assistant behaviour ---
    # IANA timezone for time/date answers, e.g. "Asia/Kolkata". Empty => server local time.
    assistant_timezone: str = ""
    # strip: remove a leading wake word; require: ignore speech without it; off: no handling.
    wake_word_mode: Literal["strip", "require", "off"] = "strip"
    # Extra spellings STT may produce for the wake word, comma-separated (e.g. "Aadi,Addy").
    wake_word_aliases: str = ""
    # Spoken when the user says only the wake word; the device should keep listening.
    wake_ack_text: str = Field(default="Yes?", min_length=1)
    # Short-term memory for follow-ups ("pause it"). 0 disables.
    conversation_max_turns: int = Field(default=6, ge=0, le=50)
    conversation_ttl_seconds: float = Field(default=900.0, gt=0)
    # Session used when a client sends no session ID (single-user setups). Empty => stateless.
    conversation_default_session: str = "default"
    # Handle exact short commands ("pause", "what time is it") without calling the LLM.
    fast_intents_enabled: bool = True
    # Speak a tool's own confirmation ("Paused.") instead of a second LLM call.
    tool_direct_replies: bool = True

    # --- Tools ---
    tools_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    weather_provider: Literal["open_meteo", "none"] = "open_meteo"
    weather_default_location: str = ""
    weather_units: Literal["metric", "imperial"] = "metric"
    web_search_provider: Literal["none", "gemini", "brave"] = "none"
    web_search_gemini_model: str = "gemini-2.5-flash"
    brave_search_api_key: SecretStr = SecretStr("")
    # Computer control is OFF by default and only ever touches allowlisted targets.
    computer_control_enabled: bool = False
    computer_control_apps: str = (
        "spotify=Spotify;chrome=Google Chrome;safari=Safari;terminal=Terminal;"
        "vs code=Visual Studio Code;finder=Finder"
    )
    computer_control_websites: str = (
        "youtube=https://www.youtube.com;gmail=https://mail.google.com;github=https://github.com"
    )
    computer_control_projects: str = ""
    computer_control_project_app: str = "Visual Studio Code"

    # --- ESP32 / device WebSocket (/api/v1/device/ws) ---
    device_ws_enabled: bool = True
    # Per-device credentials "kitchen:token1,desk:token2". Takes precedence over the shared token.
    device_tokens: SecretStr = SecretStr("")
    # One token for all devices. If both are empty, API_KEY is used; if that is empty too,
    # device auth is disabled (development only - refused when APP_ENV=production).
    device_shared_token: SecretStr = SecretStr("")
    device_hello_timeout_seconds: float = Field(default=10.0, gt=0)
    device_heartbeat_seconds: float = Field(default=20.0, gt=0)
    device_timeout_seconds: float = Field(default=60.0, gt=0)
    # Default microphone format if the device doesn't declare one in audio_start.
    device_audio_input_format: Literal["pcm_s16le", "wav"] = "pcm_s16le"
    device_audio_input_sample_rate: int = Field(default=16000, ge=8000, le=48000)
    device_audio_input_channels: int = Field(default=1, ge=1, le=2)
    # Speaker output: "wav" or headerless "pcm_s16le"; sample rate 0 = keep TTS native rate.
    device_audio_output_format: Literal["wav", "pcm_s16le"] = "wav"
    device_audio_output_sample_rate: int = Field(default=0, ge=0, le=48000)
    device_audio_chunk_bytes: int = Field(default=4096, ge=256, le=65536)
    device_max_audio_seconds: float = Field(default=30.0, gt=0, le=300)
    # Remove DC offset and boost quiet microphone audio before speech-to-text.
    device_audio_normalize: bool = True
    # If set, every device recording is saved here as a WAV so you can listen to it
    # (privacy: leave empty in normal use).
    device_debug_audio_dir: str = ""

    @field_validator("stt_provider", "llm_provider", "tts_provider")
    @classmethod
    def _normalize_provider_name(cls, value: str) -> str:
        value = value.strip().lower()
        if not value:
            raise ValueError("provider name must not be empty")
        return value

    @field_validator("openrouter_llm_temperature", mode="before")
    @classmethod
    def _blank_openrouter_temperature_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "openrouter_llm_model", "openrouter_base_url", "groq_stt_model",
        "elevenlabs_tts_voice_id", "elevenlabs_tts_model", "elevenlabs_tts_output_format"
    )
    @classmethod
    def _strip_provider_strings(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("openrouter_http_referer", "openrouter_title", "groq_stt_language",
                     "groq_stt_prompt", "elevenlabs_tts_language_code")
    @classmethod
    def _strip_optional_provider_strings(cls, value: str) -> str:
        return value.strip()

    @field_validator("gemini_llm_temperature", mode="before")
    @classmethod
    def _blank_temperature_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("gemini_llm_thinking_level")
    @classmethod
    def _normalize_thinking_level(cls, value: str) -> str:
        value = value.strip().lower()
        if value and value not in {"minimal", "low", "medium", "high"}:
            raise ValueError("gemini_llm_thinking_level must be minimal, low, medium, high or empty")
        return value

    @field_validator("gemini_stt_mode", mode="before")
    @classmethod
    def _normalize_stt_mode(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator(
        "gemini_stt_model", "gemini_llm_model", "gemini_tts_model", "gemini_tts_voice"
    )
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("assistant_timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        value = value.strip()
        if value:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError(f"unknown timezone {value!r} (use an IANA name like Asia/Kolkata)") from None
        return value

    @field_validator("wake_word_mode", "weather_provider", "web_search_provider",
                     "weather_units", "device_audio_input_format", "device_audio_output_format",
                     mode="before")
    @classmethod
    def _lower_choice(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        value = value.strip().upper()
        if value not in _VALID_LOG_LEVELS:
            raise ValueError(f"log_level must be one of {sorted(_VALID_LOG_LEVELS)}")
        return value

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def uses_gemini(self) -> bool:
        return "gemini" in (self.stt_provider, self.llm_provider, self.tts_provider)

    @property
    def gemini_stt_language_list(self) -> list[str]:
        return [c.strip() for c in self.gemini_stt_language_codes.split(",") if c.strip()]

    @property
    def wake_word_alias_list(self) -> list[str]:
        return [a.strip() for a in self.wake_word_aliases.split(",") if a.strip()]

    @property
    def device_token_map(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for item in self.device_tokens.get_secret_value().split(","):
            if ":" in item:
                device_id, token = item.split(":", 1)
                if device_id.strip() and token.strip():
                    mapping[device_id.strip()] = token.strip()
        return mapping

    @property
    def effective_device_shared_token(self) -> str:
        return (
            self.device_shared_token.get_secret_value().strip()
            or self.api_key.get_secret_value().strip()
        )

    @property
    def effective_database_url(self) -> str:
        """Database URL with sensible defaults and a PostgreSQL driver suffix."""
        url = self.database_url.strip()
        if not url:
            return DEFAULT_SQLITE_URL
        if url.startswith("postgres://"):
            return "postgresql+psycopg://" + url[len("postgres://") :]
        if url.startswith("postgresql://"):
            return "postgresql+psycopg://" + url[len("postgresql://") :]
        return url

    def safe_summary(self) -> dict[str, Any]:
        """Settings that are safe to log (no secrets, DB password masked)."""
        return {
            "app_env": self.app_env,
            "app_host": self.app_host,
            "app_port": self.app_port,
            "stt_provider": self.stt_provider,
            "llm_provider": self.llm_provider,
            "tts_provider": self.tts_provider,
            "assistant_name": self.assistant_name,
            "wake_word": self.wake_word,
            "log_level": self.log_level,
            "database_url": mask_database_url(self.effective_database_url),
            "auth_enabled": bool(self.api_key.get_secret_value()),
            "max_audio_bytes": self.max_audio_bytes,
            "openrouter_api_key_set": bool(self.openrouter_api_key.get_secret_value()),
            "openrouter_llm_model": self.openrouter_llm_model,
            "groq_api_key_set": bool(self.groq_api_key.get_secret_value()),
            "groq_stt_model": self.groq_stt_model,
            "elevenlabs_api_key_set": bool(self.elevenlabs_api_key.get_secret_value()),
            "elevenlabs_tts_model": self.elevenlabs_tts_model,
            "elevenlabs_tts_voice_id_set": bool(self.elevenlabs_tts_voice_id),
            "gemini_api_key_set": bool(self.gemini_api_key.get_secret_value()),
            "gemini_stt_model": self.gemini_stt_model,
            "gemini_llm_model": self.gemini_llm_model,
            "gemini_tts_model": self.gemini_tts_model,
            "gemini_tts_voice": self.gemini_tts_voice,
            "spotify_configured": bool(
                self.spotify_client_id.get_secret_value().strip()
                and self.spotify_client_secret.get_secret_value().strip()
                and self.spotify_redirect_uri.strip()
            ),
            "spotify_redirect_uri": self.spotify_redirect_uri,
            "assistant_timezone": self.assistant_timezone or "server-local",
            "wake_word_mode": self.wake_word_mode,
            "conversation_max_turns": self.conversation_max_turns,
            "weather_provider": self.weather_provider,
            "web_search_provider": self.web_search_provider,
            "computer_control_enabled": self.computer_control_enabled,
            "device_ws_enabled": self.device_ws_enabled,
            "device_auth": (
                "per-device" if self.device_token_map
                else "shared" if self.effective_device_shared_token else "disabled"
            ),
            "device_audio_output": f"{self.device_audio_output_format}@"
            f"{self.device_audio_output_sample_rate or 'native'}",
        }


def mask_database_url(url: str) -> str:
    """Replace the password in ``scheme://user:password@host/db`` with ``***``."""
    return re.sub(r"(://[^:/@\s]+:)[^@\s]*(@)", r"\1***\2", url)


@lru_cache
def get_settings() -> Settings:
    return Settings()
