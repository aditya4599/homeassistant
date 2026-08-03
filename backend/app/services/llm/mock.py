"""Mock LLM: deterministic replies, no network, no credits.

When tools are offered it recognises a few simple phrasings ("play X on Y", "calculate
...") and emits tool calls, so the full tool loop can be exercised offline. After a
tool round it speaks the tool's ``say`` line.
"""

import json
import re
from collections.abc import Sequence

from app.services.llm.base import LLMResponse, Message
from app.services.llm.tools import ToolCall, ToolDefinition

_GREETINGS = frozenset({"hello", "hi", "hey"})
_PLAY = re.compile(
    r"^play\s+(?P<q>.+?)(?:\s+by\s+(?P<artist>.+?))?(?:\s+on\s+(?:my\s+|the\s+)?(?P<dev>.+?))?[.!?]*$",
    re.IGNORECASE,
)
_CALC = re.compile(r"^(?:calculate|compute)\s+(?P<e>.+?)[.?!]*$", re.IGNORECASE)
_WEATHER = re.compile(r"weather(?:\s+in\s+(?P<loc>[\w\s]+?))?[.?!]*$", re.IGNORECASE)


class MockLLMProvider:
    name = "mock"

    def __init__(self, assistant_name: str = "Adi") -> None:
        self._assistant_name = assistant_name

    async def generate(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] | None = None,
    ) -> LLMResponse:
        user_text = next((m.content for m in reversed(messages) if m.role == "user"), None)
        if user_text is None:
            raise ValueError("no user message provided")

        last_tool = next((m for m in reversed(messages) if m.role == "tool"), None)
        if last_tool is not None and not tools:
            return LLMResponse(text=self._from_tool(last_tool.content))

        if tools:
            call = self._tool_call(user_text, {t.name for t in tools})
            if call is not None:
                return LLMResponse(text="", tool_calls=(call,), finish_reason="tool_calls")
        return LLMResponse(text=self._reply(user_text))

    @staticmethod
    def _tool_call(text: str, available: set[str]) -> ToolCall | None:
        text = text.strip()
        if "play_music" in available and (m := _PLAY.match(text)):
            args = {"query": m.group("q").strip()}
            if m.group("artist"):
                args["artist"] = m.group("artist").strip()
            if m.group("dev"):
                args["device"] = m.group("dev").strip()
            return ToolCall(name="play_music", arguments=args, id="mock-1")
        if "calculate" in available and (m := _CALC.match(text)):
            return ToolCall(name="calculate", arguments={"expression": m.group("e")}, id="mock-1")
        if "get_weather" in available and (m := _WEATHER.search(text)):
            args = {"location": m.group("loc").strip()} if m.group("loc") else {}
            return ToolCall(name="get_weather", arguments=args, id="mock-1")
        return None

    @staticmethod
    def _from_tool(content: str) -> str:
        try:
            data = json.loads(content)
        except ValueError:
            return "Done."
        return str(data.get("say") or ("Done." if data.get("ok") else "Sorry, that didn't work."))

    def _reply(self, user_text: str) -> str:
        words = re.sub(r"[^\w\s]", "", user_text.lower()).split()
        if words and words[0] in _GREETINGS:
            return f"Hello! I'm {self._assistant_name}."
        return f"I'm {self._assistant_name}, running in mock mode. You said: {user_text}"
