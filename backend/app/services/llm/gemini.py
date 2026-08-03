"""Gemini LLM provider (google-genai SDK, ``models.generate_content``)."""

from collections.abc import Sequence
from typing import Any

from google.genai import types

from app.services.gemini.client import PROVIDER_NAME, gemini_call, malformed
from app.services.llm.base import LLMResponse, Message
from app.services.llm.tools import ToolCall, ToolDefinition
from app.services.prompts import build_voice_system_prompt

_FINISH_REASONS = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
}


class GeminiLLMProvider:
    """Implements :class:`app.services.llm.base.LLMProvider` on top of Gemini.

    * ``system`` messages -> ``system_instruction`` (a voice-tuned default built from
      ``assistant_name`` is used when the caller sends none).
    * ``user`` / ``assistant`` messages -> ``user`` / ``model`` turns (full history).
    * ``tool`` messages -> passed as clearly-labelled user text. The shared ``Message``
      type carries no tool name / call id yet, so native ``FunctionResponse`` parts
      are a follow-up once tools are actually executed (see README).
    * ``tools`` -> Gemini function declarations; requested calls come back as
      :class:`ToolCall` objects (nothing is executed here).
    """

    name = PROVIDER_NAME

    def __init__(
        self,
        client: Any,
        *,
        model: str,
        assistant_name: str,
        max_output_tokens: int = 1024,
        temperature: float | None = None,
        thinking_level: str = "",
        timeout_seconds: float = 30.0,
    ) -> None:
        self._client = client
        self._model = model
        self._assistant_name = assistant_name
        self._max_output_tokens = max_output_tokens
        self._temperature = temperature
        self._thinking_level = thinking_level
        self._timeout = timeout_seconds

    @property
    def model(self) -> str:
        return self._model

    # ------------------------------------------------------------------ request
    def build_request(
        self, messages: Sequence[Message], tools: Sequence[ToolDefinition] | None = None
    ) -> tuple[list[types.Content], types.GenerateContentConfig]:
        system_parts = [m.content.strip() for m in messages if m.role == "system" and m.content.strip()]
        system_instruction = (
            "\n\n".join(system_parts)
            if system_parts
            else build_voice_system_prompt(self._assistant_name)
        )

        contents: list[types.Content] = []
        for message in messages:
            if message.role == "system":
                continue
            if message.role == "assistant":
                role, text = "model", message.content
            elif message.role == "tool":
                role, text = "user", f"[Tool result]\n{message.content}"
            else:
                role, text = "user", message.content
            if not text.strip():
                continue
            part = types.Part(text=text)
            # Gemini expects alternating turns: merge consecutive same-role messages.
            if contents and contents[-1].role == role:
                contents[-1].parts.append(part)
            else:
                contents.append(types.Content(role=role, parts=[part]))

        if not contents or contents[-1].role != "user":
            raise ValueError("the conversation must end with a user message")

        config_kwargs: dict[str, Any] = {
            "system_instruction": system_instruction,
            "max_output_tokens": self._max_output_tokens,
        }
        if self._temperature is not None:
            config_kwargs["temperature"] = self._temperature
        if self._thinking_level:
            config_kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_level=self._thinking_level
            )
        if tools:
            config_kwargs["tools"] = [
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=tool.name,
                            description=tool.description,
                            parameters_json_schema=tool.parameters or None,
                        )
                        for tool in tools
                    ]
                )
            ]
            # We only declare tools; never let the SDK try to execute anything.
            config_kwargs["automatic_function_calling"] = (
                types.AutomaticFunctionCallingConfig(disable=True)
            )
        return contents, types.GenerateContentConfig(**config_kwargs)

    # ------------------------------------------------------------------ call
    async def generate(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] | None = None,
    ) -> LLMResponse:
        contents, config = self.build_request(messages, tools)
        response = await gemini_call(
            lambda: self._client.aio.models.generate_content(
                model=self._model, contents=contents, config=config
            ),
            stage="llm",
            timeout_seconds=self._timeout,
        )
        return self.parse_response(response)

    # ------------------------------------------------------------------ response
    @staticmethod
    def parse_response(response: Any) -> LLMResponse:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            feedback = getattr(response, "prompt_feedback", None)
            reason = getattr(feedback, "block_reason", None)
            raise malformed("llm", f"no candidates (block_reason={reason})")

        candidate = candidates[0]
        parts = getattr(getattr(candidate, "content", None), "parts", None) or []

        texts: list[str] = []
        tool_calls: list[ToolCall] = []
        for part in parts:
            call = getattr(part, "function_call", None)
            if call is not None and getattr(call, "name", None):
                tool_calls.append(
                    ToolCall(
                        name=call.name,
                        arguments=dict(getattr(call, "args", None) or {}),
                        id=getattr(call, "id", None),
                    )
                )
                continue
            text = getattr(part, "text", None)
            if text and not getattr(part, "thought", False):  # never speak thought summaries
                texts.append(text)

        raw_reason = getattr(candidate, "finish_reason", None)
        reason_name = getattr(raw_reason, "name", None) or (str(raw_reason) if raw_reason else "STOP")
        finish_reason = "tool_calls" if tool_calls else _FINISH_REASONS.get(reason_name, reason_name.lower())

        text = "".join(texts).strip()
        if not text and not tool_calls:
            raise malformed("llm", f"no text or tool calls (finish_reason={reason_name})")
        return LLMResponse(text=text, tool_calls=tuple(tool_calls), finish_reason=finish_reason)
