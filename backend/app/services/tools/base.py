"""Tool building blocks shared by every tool module."""

from __future__ import annotations

import json
from contextvars import ContextVar
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.services.llm.tools import ToolDefinition


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Outcome of one tool call.

    ``say`` is a short, natural sentence suitable to speak as-is. When ``final`` is
    true (and direct replies are enabled) the assistant speaks ``say`` without a
    second LLM round-trip; otherwise the LLM phrases an answer from ``data``.
    """

    ok: bool
    say: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    final: bool = True
    error_code: str | None = None

    def to_json(self) -> str:
        payload: dict[str, Any] = {"ok": self.ok, **self.data}
        if self.say:
            payload["say"] = self.say
        if self.error_code:
            payload["error_code"] = self.error_code
        payload["final"] = self.final
        return json.dumps(payload, ensure_ascii=False, default=str)


# Conversation/session the current tool call belongs to ("device:<id>" for the ADI speaker).
current_session: ContextVar[str | None] = ContextVar("current_session", default=None)


def from_device() -> bool:
    """True when the current request came from a connected ADI device."""
    return (current_session.get() or "").startswith("device:")


Handler = Callable[[dict[str, Any]], Awaitable[ToolResult]]


@dataclass(frozen=True, slots=True)
class Tool:
    definition: ToolDefinition
    handler: Handler

    @property
    def name(self) -> str:
        return self.definition.name


def schema(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


def arg_str(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    value = str(value).strip()
    return value or None
