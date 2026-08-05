import pytest
from google.genai import types

from app.core.exceptions import ProviderError, ProviderRateLimitError
from app.services.llm.base import LLMProvider, Message
from app.services.llm.gemini import GeminiLLMProvider
from app.services.llm.tools import ToolDefinition
from tests.gemini_fakes import FakeGeminiClient, api_error, empty_response, response, text_response


def _provider(client, **kw) -> GeminiLLMProvider:
    return GeminiLLMProvider(client, model="gemini-test", assistant_name="Adi", **kw)


def test_satisfies_llm_interface():
    assert isinstance(_provider(FakeGeminiClient()), LLMProvider)
    assert _provider(FakeGeminiClient()).name == "gemini"


async def test_success_returns_text_and_sends_expected_request():
    client = FakeGeminiClient(text_response("That's 200."))
    result = await _provider(client, max_output_tokens=321).generate(
        [Message("system", "Be brief."), Message("user", "What is 25 times 8?")]
    )
    assert (result.text, result.finish_reason, result.tool_calls) == ("That's 200.", "stop", ())

    call = client.last_call
    assert call["model"] == "gemini-test"
    config: types.GenerateContentConfig = call["config"]
    assert config.system_instruction == "Be brief."
    assert config.max_output_tokens == 321
    assert config.temperature is None and config.thinking_config is None and config.tools is None
    assert [c.role for c in call["contents"]] == ["user"]


async def test_conversation_history_roles_are_mapped():
    client = FakeGeminiClient(text_response("ok"))
    await _provider(client).generate(
        [
            Message("user", "Hi"),
            Message("assistant", "Hello!"),
            Message("user", "My name is Vansh."),
            Message("user", "What's my name?"),  # consecutive user turns get merged
        ]
    )
    contents = client.last_call["contents"]
    assert [c.role for c in contents] == ["user", "model", "user"]
    assert [p.text for p in contents[2].parts] == ["My name is Vansh.", "What's my name?"]


async def test_default_system_prompt_uses_assistant_name():
    client = FakeGeminiClient(text_response("ok"))
    await GeminiLLMProvider(client, model="m", assistant_name="Zed").generate([Message("user", "hi")])
    prompt = client.last_call["config"].system_instruction
    assert "Zed" in prompt and "Adi" not in prompt


async def test_optional_generation_settings_are_forwarded():
    client = FakeGeminiClient(text_response("ok"))
    await _provider(client, temperature=0.3, thinking_level="low").generate([Message("user", "hi")])
    config = client.last_call["config"]
    assert config.temperature == 0.3
    assert config.thinking_config.thinking_level.value == "LOW"


async def test_tools_are_declared_and_tool_calls_parsed():
    call_part = types.Part(
        function_call=types.FunctionCall(name="get_time", args={"tz": "Asia/Kolkata"}, id="c1")
    )
    client = FakeGeminiClient(response(call_part))
    tool = ToolDefinition(
        name="get_time",
        description="Current time",
        parameters={"type": "object", "properties": {"tz": {"type": "string"}}},
    )
    result = await _provider(client).generate([Message("user", "time?")], tools=[tool])

    config = client.last_call["config"]
    assert config.tools[0].function_declarations[0].name == "get_time"
    assert config.automatic_function_calling.disable is True
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls[0].name == "get_time"
    assert result.tool_calls[0].arguments == {"tz": "Asia/Kolkata"}
    assert result.tool_calls[0].id == "c1"


async def test_thought_parts_are_never_returned():
    client = FakeGeminiClient(
        response(types.Part(text="internal reasoning", thought=True), types.Part(text="Answer."))
    )
    assert (await _provider(client).generate([Message("user", "q")])).text == "Answer."


async def test_max_tokens_finish_reason_is_mapped():
    client = FakeGeminiClient(text_response("partial", finish_reason="MAX_TOKENS"))
    assert (await _provider(client).generate([Message("user", "q")])).finish_reason == "length"


@pytest.mark.parametrize("bad", [empty_response(), response()])
async def test_malformed_or_empty_response_is_provider_error(bad):
    with pytest.raises(ProviderError, match="upstream AI provider"):
        await _provider(FakeGeminiClient(bad)).generate([Message("user", "q")])


async def test_api_error_is_translated():
    client = FakeGeminiClient(error=api_error(429, "RESOURCE_EXHAUSTED", "quota"))
    with pytest.raises(ProviderRateLimitError):
        await _provider(client).generate([Message("user", "q")])


async def test_conversation_must_end_with_user_message():
    with pytest.raises(ValueError):
        await _provider(FakeGeminiClient()).generate([Message("assistant", "hi")])
