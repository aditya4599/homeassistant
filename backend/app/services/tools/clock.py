"""Time and date tools (timezone from ASSISTANT_TIMEZONE, or the server's local zone)."""

from __future__ import annotations

from datetime import datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services.llm.tools import ToolDefinition
from app.services.tools.base import Tool, ToolResult, arg_str, schema


def resolve_timezone(name: str | None) -> tzinfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown timezone {name!r}") from None


def now_in(tz: tzinfo | None) -> datetime:
    return datetime.now(tz).astimezone(tz) if tz else datetime.now().astimezone()


def spoken_time(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def spoken_date(dt: datetime) -> str:
    return f"{dt.strftime('%A')}, {dt.strftime('%B')} {dt.day}, {dt.year}"


def clock_tools(default_timezone: str = "") -> list[Tool]:
    default_tz = resolve_timezone(default_timezone)  # validated at startup
    tz_arg = {
        "type": "string",
        "description": "IANA timezone such as 'Asia/Tokyo' ONLY if the user asked about "
        "another place. Omit for the user's local time.",
    }

    def _zone(args: dict[str, Any]) -> tuple[tzinfo | None, str | None]:
        name = arg_str(args, "timezone")
        return (resolve_timezone(name), name) if name else (default_tz, None)

    async def get_time(args: dict[str, Any]) -> ToolResult:
        tz, name = _zone(args)
        dt = now_in(tz)
        where = f" in {name.split('/')[-1].replace('_', ' ')}" if name else ""
        return ToolResult(
            ok=True, say=f"It's {spoken_time(dt)}{where}.",
            data={"iso": dt.isoformat(timespec="seconds"), "timezone": str(dt.tzinfo)},
        )  # fmt: skip

    async def get_date(args: dict[str, Any]) -> ToolResult:
        tz, _ = _zone(args)
        dt = now_in(tz)
        return ToolResult(
            ok=True, say=f"Today is {spoken_date(dt)}.",
            data={"date": dt.date().isoformat(), "weekday": dt.strftime("%A")},
        )  # fmt: skip

    return [
        Tool(ToolDefinition("get_time", "Current local time ('what time is it').",
                            schema({"timezone": tz_arg})), get_time),
        Tool(ToolDefinition("get_date", "Today's date and weekday.",
                            schema({"timezone": tz_arg})), get_date),
    ]  # fmt: skip
