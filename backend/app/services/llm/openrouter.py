"""OpenRouter chat-completions LLM provider.

OpenRouter exposes an OpenAI-compatible chat-completions API. This implementation
keeps the project's provider-neutral Message/ToolDefinition interfaces and translates
tool calls back into ToolCall objects for the existing ChatService loop.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx

from app.core.config import Settings
from app.core.exceptions import (
    ConfigurationError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.services.llm.base import LLMResponse, Message
from app.services.llm.tools import ToolCall, ToolDefinition

PROVIDER_NAME = "openrouter"


class OpenRouterLLMProvider:
    name = PROVIDER_NAME

    def __init__(
        self,
        settings: Settings,
        *,
        model: str,
        max_output_tokens: int = 512,
        temperature: float | None = 0.4,
        timeout_seconds: float = 60.0,
        base_url: str = "https://openrouter.ai/api/v1",
        http_referer: str = "",
        title: str = "Adi Voice Assistant",
    ) -> None:
        key = settings.openrouter_api_key.get_secret_value().strip()
        if not key:
            raise ConfigurationError("LLM_PROVIDER=openrouter requires OPENROUTER_API_KEY.")
        self._api_key = key
        self._model = model.strip()
        self._max_output_tokens = max_output_tokens
        self._temperature = temperature
        self._timeout = timeout_seconds
        self._base_url = base_url.rstrip("/")
        self._http_referer = http_referer.strip()
        self._title = title.strip()

    @property
    def model(self) -> str:
        return self._model

    async def generate(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition] | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [self._message_to_openrouter(m) for m in messages],
            "max_tokens": self._max_output_tokens,
            "stream": False,
        }
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        if tools:
            payload["tools"] = [self._tool_to_openrouter(t) for t in tools]
            payload["tool_choice"] = "auto"

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if self._http_referer:
            headers["HTTP-Referer"] = self._http_referer
        if self._title:
            headers["X-Title"] = self._title

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                provider=self.name, stage="llm", detail="OpenRouter request timed out"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                provider=self.name,
                stage="llm",
                detail=f"OpenRouter network error: {type(exc).__name__}",
            ) from exc

        if response.status_code in (401, 403):
            raise ProviderAuthError(
                provider=self.name, stage="llm", detail=_error_text(response)
            )
        if response.status_code == 429:
            raise ProviderRateLimitError(
                provider=self.name, stage="llm", detail=_error_text(response)
            )
        if response.status_code >= 500:
            raise ProviderUnavailableError(
                provider=self.name, stage="llm", detail=_error_text(response)
            )
        if response.status_code >= 400:
            raise ProviderError(provider=self.name, stage="llm", detail=_error_text(response))

        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(
                provider=self.name, stage="llm", detail="OpenRouter returned invalid JSON"
            ) from exc

        return self.parse_response(payload)

    @staticmethod
    def _message_to_openrouter(message: Message) -> dict[str, Any]:
        # The shared ChatService intentionally keeps provider-neutral tool messages.
        # OpenRouter can accept the result as a user-visible tool-result context on the
        # follow-up request, avoiding provider-specific state in the conversation store.
        if message.role == "tool":
            label = f"[Tool result: {message.name or 'tool'}]"
            return {"role": "user", "content": f"{label}\n{message.content}"}
        return {"role": message.role, "content": message.content}

    @staticmethod
    def _tool_to_openrouter(tool: ToolDefinition) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters or {"type": "object", "properties": {}},
            },
        }

    @staticmethod
    def parse_response(payload: dict[str, Any]) -> LLMResponse:
        choices = payload.get("choices") or []
        if not choices:
            error = payload.get("error")
            raise ProviderError(
                provider=PROVIDER_NAME,
                stage="llm",
                detail=f"OpenRouter returned no choices: {error or 'unknown response'}",
            )
        choice = choices[0] or {}
        message = choice.get("message") or {}
        text = message.get("content") or ""
        if isinstance(text, list):
            text = "".join(
                str(part.get("text", "")) for part in text if isinstance(part, dict)
            )
        tool_calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") or {}
            name = function.get("name")
            if not name:
                continue
            arguments_raw = function.get("arguments") or "{}"
            try:
                arguments = json.loads(arguments_raw) if isinstance(arguments_raw, str) else arguments_raw
            except json.JSONDecodeError as exc:
                raise ProviderError(
                    provider=PROVIDER_NAME,
                    stage="llm",
                    detail=f"OpenRouter returned invalid tool arguments for {name}",
                ) from exc
            if not isinstance(arguments, dict):
                raise ProviderError(
                    provider=PROVIDER_NAME,
                    stage="llm",
                    detail=f"OpenRouter returned non-object tool arguments for {name}",
                )
            tool_calls.append(
                ToolCall(name=name, arguments=arguments, id=raw.get("id"))
            )

        finish_reason = "tool_calls" if tool_calls else str(choice.get("finish_reason") or "stop")
        if not str(text).strip() and not tool_calls:
            raise ProviderError(
                provider=PROVIDER_NAME,
                stage="llm",
                detail=f"OpenRouter returned no text (finish_reason={finish_reason})",
            )
        return LLMResponse(
            text=str(text).strip(),
            tool_calls=tuple(tool_calls),
            finish_reason=finish_reason,
        )


def _error_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                return str(error.get("message") or error)
            return str(error or payload)
    except (ValueError, json.JSONDecodeError):
        pass
    return response.text[:500] or f"HTTP {response.status_code}"
