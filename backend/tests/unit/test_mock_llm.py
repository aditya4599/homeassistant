import pytest

from app.services.llm.base import Message
from app.services.llm.mock import MockLLMProvider
from app.services.llm.tools import ToolDefinition


async def test_hello_gets_predictable_reply():
    result = await MockLLMProvider().generate([Message(role="user", content="Hello")])
    assert result.text == "Hello! I'm Adi."
    assert result.tool_calls == ()
    assert result.finish_reason == "stop"


@pytest.mark.parametrize("text", ["hi there", "Hey!", "HELLO, how are you?"])
async def test_greeting_variants(text):
    result = await MockLLMProvider().generate([Message(role="user", content=text)])
    assert result.text == "Hello! I'm Adi."


async def test_uses_configured_assistant_name():
    result = await MockLLMProvider(assistant_name="Zed").generate(
        [Message(role="user", content="Hello")]
    )
    assert result.text == "Hello! I'm Zed."


async def test_non_greeting_echoes_message():
    result = await MockLLMProvider().generate([Message(role="user", content="What time is it?")])
    assert "You said: What time is it?" in result.text


async def test_uses_last_user_message_and_ignores_system():
    messages = [
        Message(role="system", content="be brief"),
        Message(role="user", content="something else"),
        Message(role="assistant", content="ok"),
        Message(role="user", content="hello"),
    ]
    assert (await MockLLMProvider().generate(messages)).text == "Hello! I'm Adi."


async def test_tools_are_accepted_and_ignored():
    tools = [ToolDefinition(name="lights", description="Toggle lights")]
    result = await MockLLMProvider().generate([Message(role="user", content="hi")], tools=tools)
    assert result.tool_calls == ()


async def test_requires_a_user_message():
    with pytest.raises(ValueError):
        await MockLLMProvider().generate([Message(role="system", content="x")])
