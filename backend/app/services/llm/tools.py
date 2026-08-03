"""Provider-neutral tool-calling types (placeholders for Part 2+)."""

from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Describes a tool the LLM may call (JSON-schema style parameters)."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A tool invocation requested by the LLM."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    id: str | None = None


class ToolExecutor(Protocol):
    """Executes validated tool calls outside the LLM provider."""

    @property
    def definitions(self) -> Sequence[ToolDefinition]: ...

    async def execute(self, call: ToolCall) -> str: ...
