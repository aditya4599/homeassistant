"""Weather tool behind a small provider interface.

Default provider: Open-Meteo (free, no API key). Location comes from the user's
request or WEATHER_DEFAULT_LOCATION; nothing is hard-coded.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from app.core.exceptions import AppError
from app.services.llm.tools import ToolDefinition
from app.services.tools.base import Tool, ToolResult, arg_str, schema

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather interpretation codes -> spoken phrase
_WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "foggy",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle",
    57: "freezing drizzle", 61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain",
    67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "rain showers", 81: "rain showers", 82: "heavy rain showers", 85: "snow showers",
    86: "heavy snow showers", 95: "thunderstorms", 96: "thunderstorms with hail",
    99: "thunderstorms with hail",
}  # fmt: skip


class WeatherError(AppError):
    status_code = 502
    code = "weather_error"
    default_message = "I couldn't get the weather right now."


class LocationNotFoundError(WeatherError):
    status_code = 404
    code = "location_not_found"
    default_message = "I couldn't find that place."


class WeatherProvider(Protocol):
    name: str

    async def current(self, location: str) -> dict[str, Any]: ...


class OpenMeteoWeather:
    name = "open_meteo"

    def __init__(self, *, units: str = "metric", timeout_seconds: float = 10.0) -> None:
        self._imperial = units == "imperial"
        self._timeout = timeout_seconds

    async def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise WeatherError(detail=f"network error: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise WeatherError(detail=f"HTTP {response.status_code}: {response.text[:200]}")
        try:
            return response.json()
        except ValueError as exc:
            raise WeatherError(detail="invalid JSON") from exc

    async def current(self, location: str) -> dict[str, Any]:
        geo = await self._get(GEOCODE_URL, {"name": location, "count": 1, "format": "json"})
        places = geo.get("results") or []
        if not places:
            raise LocationNotFoundError(
                f"I couldn't find {location}.", detail=f"geocoding returned nothing for {location!r}"
            )
        place = places[0]
        params: dict[str, Any] = {
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
            "weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "forecast_days": 1,
            "timezone": "auto",
        }
        if self._imperial:
            params |= {"temperature_unit": "fahrenheit", "wind_speed_unit": "mph"}
        data = await self._get(FORECAST_URL, params)
        cur, daily = data.get("current") or {}, data.get("daily") or {}

        def first(key: str) -> Any:
            values = daily.get(key) or [None]
            return values[0]

        return {
            "location": ", ".join(p for p in (place.get("name"), place.get("country")) if p),
            "temperature": cur.get("temperature_2m"),
            "feels_like": cur.get("apparent_temperature"),
            "humidity": cur.get("relative_humidity_2m"),
            "wind_speed": cur.get("wind_speed_10m"),
            "condition": _WMO.get(cur.get("weather_code"), "unknown conditions"),
            "high": first("temperature_2m_max"),
            "low": first("temperature_2m_min"),
            "rain_chance": first("precipitation_probability_max"),
            "units": "fahrenheit" if self._imperial else "celsius",
        }


def _deg(value: Any) -> str:
    return str(round(value)) if isinstance(value, (int, float)) else "?"


def weather_tools(provider: WeatherProvider, default_location: str = "") -> list[Tool]:
    async def get_weather(args: dict[str, Any]) -> ToolResult:
        location = arg_str(args, "location") or default_location.strip()
        if not location:
            return ToolResult(
                ok=False, say="Which city should I check?", error_code="location_required"
            )
        w = await provider.current(location)
        city = w["location"].split(",")[0]
        say = f"It's {_deg(w['temperature'])} degrees and {w['condition']} in {city}"
        if w.get("high") is not None and w.get("low") is not None:
            say += f", with a high of {_deg(w['high'])} and a low of {_deg(w['low'])}"
        rain = w.get("rain_chance")
        if isinstance(rain, (int, float)) and rain >= 30:
            say += f". There's a {round(rain)} percent chance of rain"
        return ToolResult(ok=True, say=say + ".", data=w)

    return [
        Tool(ToolDefinition(
            "get_weather",
            "Current weather and today's forecast. Use for weather, temperature, rain questions.",
            schema({"location": {"type": "string", "description":
                    "City name if the user said one; omit to use the default location."}}),
        ), get_weather),
    ]  # fmt: skip
