import pytest

from app.core.exceptions import ProviderError
from app.services.chat import ChatService
from app.services.llm.mock import MockLLMProvider
from tests.helpers import FailingLLM, FakeLLM


async def test_respond_returns_llm_text():
    service = ChatService(llm=MockLLMProvider(), assistant_name="Adi")
    assert await service.respond("Hello") == "Hello! I'm Adi."


def test_build_messages_has_system_prompt_with_name():
    messages = ChatService(llm=FakeLLM(), assistant_name="Zed").build_messages("hi")
    assert [m.role for m in messages] == ["system", "user"]
    assert "Zed" in messages[0].content
    assert messages[1].content == "hi"


async def test_empty_llm_response_is_a_provider_error():
    with pytest.raises(ProviderError):
        await ChatService(llm=FakeLLM("   "), assistant_name="Adi").respond("hi")


async def test_llm_exception_is_wrapped():
    with pytest.raises(ProviderError):
        await ChatService(llm=FailingLLM(), assistant_name="Adi").respond("hi")
