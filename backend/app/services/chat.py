"""Conversation logic: history, fast intents, LLM + provider-neutral tool execution."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from app.core.exceptions import ProviderError
from app.services.conversation import ConversationStore
from app.services.intents import match_intent
from app.services.llm.base import LLMProvider, LLMResponse, Message
from app.services.llm.tools import ToolExecutor
from app.services.prompts import build_voice_system_prompt
from app.services.provider_call import call_provider

logger = logging.getLogger("app.chat")

_MAX_TOOL_ROUNDS = 3
_FOLLOW_UP = (
    "Use the result(s) above to answer my original request in one or two short spoken "
    "sentences. Don't mention tools or technical details."
)


@dataclass(frozen=True, slots=True)
class ChatTurn:
    text: str
    tools_used: tuple[str, ...] = ()
    session_id: str | None = None
    via: str = "llm"  # "llm" | "fast_intent" | "tool_direct"


@dataclass
class _Clock:
    timezone: str = ""

    def now_text(self) -> str | None:
        try:
            from app.services.tools.clock import now_in, resolve_timezone, spoken_date, spoken_time

            dt = now_in(resolve_timezone(self.timezone))
            return f"{spoken_date(dt)}, {spoken_time(dt)}"
        except ValueError:
            return None


class ChatService:
    def __init__(
        self,
        llm: LLMProvider,
        assistant_name: str,
        tool_executor: ToolExecutor | None = None,
        *,
        conversation: ConversationStore | None = None,
        fast_intents: bool = True,
        direct_tool_replies: bool = True,
        timezone: str = "",
    ) -> None:
        self._llm = llm
        self._assistant_name = assistant_name
        self._tool_executor = tool_executor
        self._conversation = conversation or ConversationStore(max_turns=0)
        self._fast_intents = fast_intents
        self._direct = direct_tool_replies
        self._clock = _Clock(timezone)

    @property
    def tool_names(self) -> list[str]:
        if self._tool_executor is None:
            return []
        return [d.name for d in self._tool_executor.definitions]

    def build_messages(self, user_text: str, history: list[Message] | None = None) -> list[Message]:
        system = build_voice_system_prompt(
            self._assistant_name,
            tools_available=bool(self._tool_executor),
            now_text=self._clock.now_text(),
        )
        return [
            Message(role="system", content=system),
            *(history or []),
            Message(role="user", content=user_text),
        ]

    async def respond(self, user_text: str, session_id: str | None = None) -> str:
        return (await self.respond_turn(user_text, session_id)).text

    async def respond_turn(self, user_text: str, session_id: str | None = None) -> ChatTurn:
        from app.services.tools.base import current_session

        token = current_session.set(session_id)  # lets tools target the ADI speaker
        try:
            turn = await self._respond(user_text, session_id)
        finally:
            current_session.reset(token)
        self._conversation.record(session_id, user_text, turn.text)
        return turn

    # ------------------------------------------------------------------ internals
    async def _generate(self, messages: list[Message], *, tools_enabled: bool) -> LLMResponse:
        tools = self._tool_executor.definitions if tools_enabled and self._tool_executor else None
        return await call_provider("llm", self._llm.name, self._llm.generate(messages, tools=tools))

    async def _respond(self, user_text: str, session_id: str | None) -> ChatTurn:
        fast = await self._try_fast_intent(user_text, session_id)
        if fast is not None:
            return fast

        messages = self.build_messages(user_text, self._conversation.history(session_id))
        result = await self._generate(messages, tools_enabled=True)
        used: list[str] = []

        for _ in range(_MAX_TOOL_ROUNDS):
            if not result.tool_calls:
                text = result.text.strip()
                if not text:
                    raise ProviderError(provider=self._llm.name, stage="llm", detail="empty response")
                return ChatTurn(text, tuple(used), session_id)

            if self._tool_executor is None:
                raise ProviderError(
                    provider=self._llm.name,
                    stage="llm",
                    detail="model requested a tool but no tool executor is configured",
                )

            if result.text.strip():
                messages.append(Message(role="assistant", content=result.text.strip()))
            outputs: list[str] = []
            for call in result.tool_calls:
                used.append(call.name)
                output = await self._tool_executor.execute(call)
                outputs.append(output)
                messages.append(
                    Message(role="tool", name=call.name, tool_call_id=call.id, content=output)
                )

            direct = self._direct_reply(outputs)
            if direct:
                return ChatTurn(direct, tuple(used), session_id, via="tool_direct")

            messages.append(Message(role="user", content=_FOLLOW_UP))
            # No tools on the follow-up: prevents loops; we only want the final wording.
            result = await self._generate(messages, tools_enabled=False)

        raise ProviderError(
            provider=self._llm.name, stage="llm", detail="tool execution exceeded the safety limit"
        )

    def _direct_reply(self, outputs: list[str]) -> str | None:
        """Speak tool ``say`` lines directly when every result is final (skips an LLM call)."""
        if not self._direct:
            return None
        lines: list[str] = []
        for output in outputs:
            try:
                data = json.loads(output)
            except (TypeError, ValueError):
                return None
            if not isinstance(data, dict) or not data.get("final") or not data.get("say"):
                return None
            lines.append(str(data["say"]).strip())
        return " ".join(dict.fromkeys(lines)) or None

    async def _try_fast_intent(self, user_text: str, session_id: str | None) -> ChatTurn | None:
        executor = self._tool_executor
        if not (self._fast_intents and executor is not None and hasattr(executor, "run")):
            return None
        intent = match_intent(user_text, set(self.tool_names))
        if intent is None:
            return None
        result = await executor.run(intent.tool, intent.arguments)  # type: ignore[attr-defined]
        logger.info("fast_intent", extra={"tool": intent.tool, "ok": result.ok})
        text = result.say or ("Done." if result.ok else "Sorry, I couldn't do that.")
        return ChatTurn(text, (intent.tool,), session_id, via="fast_intent")
