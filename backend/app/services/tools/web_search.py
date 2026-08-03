"""Web search tool behind a provider interface (WEB_SEARCH_PROVIDER).

* ``gemini`` - Gemini with Grounding with Google Search (uses GEMINI_API_KEY).
* ``brave``  - Brave Search API (BRAVE_SEARCH_API_KEY).
* ``none``   - tool not offered (default).

Results are not ``final``: the LLM turns them into a short spoken answer.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from app.core.exceptions import AppError
from app.services.llm.tools import ToolDefinition
from app.services.tools.base import Tool, ToolResult, arg_str, schema

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"


class SearchError(AppError):
    status_code = 502
    code = "web_search_error"
    default_message = "I couldn't search the web right now."


class SearchProvider(Protocol):
    name: str

    async def search(self, query: str) -> dict[str, Any]: ...


class BraveSearch:
    name = "brave"

    def __init__(self, api_key: str, *, timeout_seconds: float = 10.0, count: int = 5) -> None:
        self._key = api_key
        self._timeout = timeout_seconds
        self._count = count

    async def search(self, query: str) -> dict[str, Any]:
        headers = {"Accept": "application/json", "X-Subscription-Token": self._key}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    BRAVE_URL, params={"q": query, "count": self._count}, headers=headers
                )
        except httpx.HTTPError as exc:
            raise SearchError(detail=f"network error: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise SearchError(detail=f"Brave HTTP {response.status_code}: {response.text[:200]}")
        results = ((response.json() or {}).get("web") or {}).get("results") or []
        return {
            "results": [
                {"title": r.get("title"), "snippet": r.get("description"), "url": r.get("url")}
                for r in results[: self._count]
            ]
        }


class GeminiGroundedSearch:
    """Asks a Gemini model with the Google Search grounding tool enabled."""

    name = "gemini"

    def __init__(self, client: Any, *, model: str, timeout_seconds: float = 30.0) -> None:
        self._client = client
        self._model = model
        self._timeout = timeout_seconds

    async def search(self, query: str) -> dict[str, Any]:
        from google.genai import types

        from app.services.gemini.client import gemini_call

        config = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            system_instruction="Answer the query factually in at most 4 sentences using "
            "current search results. Plain text only.",
        )
        response = await gemini_call(
            lambda: self._client.aio.models.generate_content(
                model=self._model, contents=query, config=config
            ),
            stage="search",
            timeout_seconds=self._timeout,
        )
        candidates = getattr(response, "candidates", None) or []
        parts = getattr(getattr(candidates[0], "content", None), "parts", None) or [] if candidates else []
        text = " ".join(p.text for p in parts if getattr(p, "text", None) and not getattr(p, "thought", False))
        if not text.strip():
            raise SearchError(detail="grounded search returned no text")
        sources: list[str] = []
        grounding = getattr(candidates[0], "grounding_metadata", None) if candidates else None
        for chunk in getattr(grounding, "grounding_chunks", None) or []:
            web = getattr(chunk, "web", None)
            if web and getattr(web, "title", None):
                sources.append(web.title)
        return {"summary": text.strip(), "sources": sources[:5]}


def web_search_tools(provider: SearchProvider) -> list[Tool]:
    async def web_search(args: dict[str, Any]) -> ToolResult:
        query = arg_str(args, "query")
        if not query:
            raise ValueError("query is required")
        return ToolResult(ok=True, data={"query": query, **await provider.search(query)}, final=False)

    return [
        Tool(ToolDefinition(
            "web_search",
            "Search the web for current or recent information, news, or facts you are unsure "
            "about ('search the web for', 'look up', 'latest').",
            schema({"query": {"type": "string"}}, ["query"]),
        ), web_search),
    ]  # fmt: skip
