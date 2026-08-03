"""LLM provider interface."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from app.services.llm.tools import ToolCall, ToolDefinition

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str
    # Optional metadata used when a tool result is fed back to an LLM. Existing
    # callers remain source-compatible because both fields have defaults.
    name: str | None = None
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str = "stop"


@runtime_checkable
class LLMProvider(Protocol):
    """Anything with a ``name`` and an async ``generate`` can be an LLM provider."""

    name: str

    async def generate(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] | None = None,
    ) -> LLMResponse: ...
