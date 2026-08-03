"""Combines tool modules into one :class:`~app.services.llm.tools.ToolExecutor`."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Sequence
from typing import Any

from app.core.exceptions import AppError
from app.services.llm.tools import ToolCall, ToolDefinition
from app.services.tools.base import Tool, ToolResult

logger = logging.getLogger("app.tools")


class ToolRegistry:
    """Holds enabled tools; executes calls safely and never raises to the caller."""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.add(tool)

    def add(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def extend(self, tools: Iterable[Tool]) -> None:
        for tool in tools:
            self.add(tool)

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def has(self, name: str) -> bool:
        return name in self._tools

    @property
    def definitions(self) -> Sequence[ToolDefinition]:
        return tuple(t.definition for t in self._tools.values())

    async def run(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(ok=False, say="I can't do that yet.", error_code="unknown_tool")
        start = time.perf_counter()
        try:
            result = await tool.handler(dict(arguments or {}))
        except AppError as exc:
            # Public message is written for end users; technical detail stays in logs.
            logger.warning(
                "tool_failed",
                extra={"tool": name, "code": exc.code, "detail": exc.log_detail},
            )
            return ToolResult(ok=False, say=exc.public_message, error_code=exc.code)
        except (TypeError, ValueError) as exc:
            logger.warning("tool_bad_arguments", extra={"tool": name, "detail": str(exc)})
            return ToolResult(
                ok=False, say="I didn't quite get that.", error_code="invalid_arguments",
                data={"detail": str(exc)[:200]}, final=False,
            )  # fmt: skip
        except Exception as exc:  # noqa: BLE001 - a tool must never crash a turn
            logger.error(
                "tool_crashed", extra={"tool": name, "error_type": type(exc).__name__}, exc_info=exc
            )
            return ToolResult(ok=False, say="Sorry, something went wrong.", error_code="tool_error")
        logger.info(
            "tool_completed",
            extra={
                "tool": name,
                "ok": result.ok,
                "duration_ms": round((time.perf_counter() - start) * 1000, 2),
            },
        )
        return result

    async def execute(self, call: ToolCall) -> str:
        """``ToolExecutor`` protocol: JSON string fed back to the LLM."""
        return (await self.run(call.name, call.arguments)).to_json()
