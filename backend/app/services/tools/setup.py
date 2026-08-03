"""Assemble the enabled tools from settings (one place to add new tool modules)."""

from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.core.exceptions import ConfigurationError
from app.services.tools.calculator import calculator_tools
from app.services.tools.clock import clock_tools
from app.services.tools.registry import ToolRegistry


def build_tool_registry(
    settings: Settings, *, spotify: Any | None = None, controller: Any | None = None
) -> ToolRegistry:
    registry = ToolRegistry()
    registry.extend(clock_tools(settings.assistant_timezone))
    registry.extend(calculator_tools())

    if spotify is not None and getattr(spotify, "configured", True):
        from app.services.spotify.tools import spotify_tools

        registry.extend(spotify_tools(spotify, default_device=settings.spotify_default_device))

    if settings.weather_provider == "open_meteo":
        from app.services.tools.weather import OpenMeteoWeather, weather_tools

        provider = OpenMeteoWeather(
            units=settings.weather_units, timeout_seconds=settings.tools_timeout_seconds
        )
        registry.extend(weather_tools(provider, settings.weather_default_location))

    if settings.web_search_provider != "none":
        registry.extend(_web_search(settings))

    if settings.computer_control_enabled:
        from app.services.computer.controller import default_controller
        from app.services.tools.computer import computer_tools, parse_mapping

        registry.extend(
            computer_tools(
                controller or default_controller(),
                apps=parse_mapping(settings.computer_control_apps),
                websites=parse_mapping(settings.computer_control_websites),
                projects=parse_mapping(settings.computer_control_projects),
                project_app=settings.computer_control_project_app.strip(),
            )
        )
    return registry


def _web_search(settings: Settings) -> list:
    from app.services.tools.web_search import BraveSearch, GeminiGroundedSearch, web_search_tools

    if settings.web_search_provider == "brave":
        key = settings.brave_search_api_key.get_secret_value().strip()
        if not key:
            raise ConfigurationError("WEB_SEARCH_PROVIDER=brave requires BRAVE_SEARCH_API_KEY.")
        return web_search_tools(BraveSearch(key, timeout_seconds=settings.tools_timeout_seconds))

    from app.services.gemini.client import create_gemini_client

    client = create_gemini_client(settings)  # raises ConfigurationError without a key
    return web_search_tools(
        GeminiGroundedSearch(
            client,
            model=settings.web_search_gemini_model,
            timeout_seconds=settings.gemini_timeout_seconds,
        )
    )
