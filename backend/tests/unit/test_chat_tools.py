import json

from app.services.chat import ChatService
from app.services.llm.base import LLMResponse
from app.services.llm.tools import ToolCall, ToolDefinition


class ToolLLM:
    name = "fake-llm"

    def __init__(self):
        self.calls = []

    async def generate(self, messages, tools=None):
        self.calls.append((list(messages), tools))
        if len(self.calls) == 1:
            return LLMResponse(tool_calls=(ToolCall(name="play_music", arguments={"query": "Love Dose"}, id="1"),), finish_reason="tool_calls", text="")
        return LLMResponse(text="Playing Love Dose.")


class Executor:
    definitions = (ToolDefinition(name="play_music", description="play", parameters={"type": "object"}),)

    async def execute(self, call):
        return json.dumps({"ok": True, "track": "Love Dose"})


async def test_chat_executes_tool_then_generates_final_reply():
    llm = ToolLLM()
    service = ChatService(llm=llm, assistant_name="Adi", tool_executor=Executor())
    assert await service.respond("play Love Dose") == "Playing Love Dose."
    assert len(llm.calls) == 2
    assert llm.calls[0][1] is not None
    assert llm.calls[1][1] is None
    assert any(m.role == "tool" and m.name == "play_music" for m in llm.calls[1][0])
