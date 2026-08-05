import json

import pytest

from app.services.chat import ChatService
from app.services.conversation import ConversationStore
from app.services.intents import match_intent
from app.services.llm.base import LLMResponse
from app.services.llm.mock import MockLLMProvider
from app.services.llm.tools import ToolCall
from app.services.tools.base import Tool, ToolResult, schema
from app.services.tools.calculator import CalculationError, calculate, format_number
from app.services.tools.clock import clock_tools
from app.services.tools.computer import computer_tools
from app.services.tools.registry import ToolRegistry
from app.services.llm.tools import ToolDefinition
from app.services.wake import WakeWordGate

MUSIC = {"pause_music", "resume_music", "next_track", "previous_track", "current_playback",
         "get_time", "get_date"}  # fmt: skip


@pytest.mark.parametrize(("text", "tool"), [
    ("pause", "pause_music"), ("Pause Spotify.", "pause_music"), ("resume", "resume_music"),
    ("continue playing", "resume_music"), ("play again", "resume_music"),
    ("next song", "next_track"), ("skip", "next_track"), ("previous song", "previous_track"),
    ("go back", "previous_track"), ("What's playing?", "current_playback"),
    ("What song is this?", "current_playback"), ("Who is playing?", "current_playback"),
    ("Tell me what's playing.", "current_playback"), ("What time is it?", "get_time"),
    ("What's today's date?", "get_date"),
    ("explain what pause means", None), ("play Love Dose", None), ("skip leg day", None),
])  # fmt: skip
def test_fast_intents(text, tool):
    intent = match_intent(text, MUSIC)
    assert (intent.tool if intent else None) == tool


def test_fast_intent_requires_tool_to_exist():
    assert match_intent("pause", {"get_time"}) is None


@pytest.mark.parametrize(("expr", "result"), [
    ("27 times 43", "1,161"), ("15% of 840", "126"), ("What's 25 x 36?", "900"),
    ("sqrt(16) + 1", "5"), ("10 divided by 4", "2.5"),
])  # fmt: skip
def test_calculator(expr, result):
    assert format_number(calculate(expr)) == result


@pytest.mark.parametrize("expr", ["__import__('os').system('ls')", "9**9**9", "1/0", "x+1", "[1]*9"])
def test_calculator_rejects_unsafe(expr):
    with pytest.raises(CalculationError):
        calculate(expr)


def test_wake_word_gate():
    gate = WakeWordGate("Adi", aliases=["Aadi"], mode="require")
    assert gate.process("Adi, play Love Dose").command == "Play Love Dose"
    assert gate.process("hey aadi pause").detected
    assert not gate.process("play music").accepted
    assert gate.process("Adinath temple").accepted is False
    strip = WakeWordGate("Adi")
    assert strip.process("play music").accepted and strip.process("play music").command == "play music"


async def test_clock_tools_use_timezone():
    tools = {t.name: t for t in clock_tools("Asia/Kolkata")}
    result = await tools["get_time"].handler({})
    assert result.ok and result.say.startswith("It's ") and "+05:30" in result.data["iso"]
    with pytest.raises(ValueError):
        clock_tools("Mars/Base")


class FakeController:
    def __init__(self):
        self.calls = []

    async def open_app(self, name): self.calls.append(("open_app", name))
    async def close_app(self, name): self.calls.append(("close_app", name))
    async def open_url(self, url): self.calls.append(("open_url", url))
    async def open_path(self, path, app=None): self.calls.append(("open_path", path, app))


async def test_computer_tools_are_allowlisted():
    ctl = FakeController()
    reg = ToolRegistry(computer_tools(
        ctl, apps={"spotify": "Spotify", "chrome": "Google Chrome"},
        websites={"youtube": "https://www.youtube.com"}, projects={"adi": "~/code/adi"},
    ))  # fmt: skip
    assert (await reg.run("open_app", {"name": "Google Chrome"})).ok
    assert (await reg.run("open_app", {"name": "YouTube"})).ok  # falls through to website
    denied = await reg.run("open_app", {"name": "rm -rf /"})
    assert not denied.ok and denied.error_code == "not_allowed"
    assert (await reg.run("open_project", {})).ok  # single project -> no name needed
    assert ctl.calls[:2] == [("open_app", "Google Chrome"), ("open_url", "https://www.youtube.com")]
    assert all("rm" not in str(c) for c in ctl.calls)


def _registry(log):
    async def pause(args):
        log.append("pause")
        return ToolResult(ok=True, say="Paused.")

    async def play(args):
        log.append(("play", args))
        return ToolResult(ok=True, say=f"Playing {args['query']}.")

    return ToolRegistry([
        Tool(ToolDefinition("pause_music", "pause", schema()), pause),
        Tool(ToolDefinition("play_music", "play", schema({"query": {"type": "string"},
                                                          "device": {"type": "string"}})), play),
    ])  # fmt: skip


class CountingLLM(MockLLMProvider):
    def __init__(self):
        super().__init__("Adi")
        self.calls = []

    async def generate(self, messages, tools=None):
        self.calls.append(list(messages))
        return await super().generate(messages, tools)


async def test_fast_intent_skips_llm_and_tool_direct_reply_skips_second_call():
    log, llm = [], CountingLLM()
    chat = ChatService(llm, "Adi", _registry(log))
    turn = await chat.respond_turn("pause")
    assert (turn.text, turn.via, len(llm.calls)) == ("Paused.", "fast_intent", 0)
    turn = await chat.respond_turn("play Love Dose on my T2 Pro")
    assert turn.text == "Playing Love Dose."
    assert turn.via == "tool_direct" and len(llm.calls) == 1
    assert log[-1] == ("play", {"query": "Love Dose", "device": "T2 Pro"})


async def test_conversation_history_is_sent_for_follow_ups():
    llm = CountingLLM()
    chat = ChatService(llm, "Adi", _registry([]), conversation=ConversationStore(max_turns=3),
                       fast_intents=False)  # fmt: skip
    await chat.respond("play Love Dose", session_id="s1")
    await chat.respond("tell me a joke", session_id="s1")
    roles = [(m.role, m.content) for m in llm.calls[-1] if m.role != "system"]
    assert roles[:2] == [("user", "play Love Dose"), ("assistant", "Playing Love Dose.")]
    await chat.respond("hello", session_id="other")
    assert [m.role for m in llm.calls[-1]] == ["system", "user"]  # sessions isolated


async def test_prompt_uses_assistant_name_and_mentions_tools():
    chat = ChatService(MockLLMProvider("Zed"), "Zed", _registry([]))
    system = chat.build_messages("hi")[0].content
    assert "You are Zed" in system and "Adi" not in system and "functions" in system


async def test_non_final_tool_result_goes_back_to_llm():
    class SearchLLM:
        name = "s"

        def __init__(self):
            self.n = 0

        async def generate(self, messages, tools=None):
            self.n += 1
            if self.n == 1:
                return LLMResponse(text="", tool_calls=(ToolCall("web_search", {"query": "x"}),))
            assert json.loads(messages[-2].content)["summary"] == "facts"
            return LLMResponse(text="Here's what I found.")

    async def search(args):
        return ToolResult(ok=True, data={"summary": "facts"}, final=False)

    reg = ToolRegistry([Tool(ToolDefinition("web_search", "s", schema()), search)])
    assert await ChatService(SearchLLM(), "Adi", reg).respond("search x") == "Here's what I found."
