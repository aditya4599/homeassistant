"""Short-term conversation memory so follow-ups work ("Play Love Dose" -> "Pause it").

In-memory, per session, bounded by turns and idle TTL. Deliberately not persisted:
it only needs to span a few minutes of conversation. The rest of the API stays
stateless - a session is just an ID the client sends (or the ESP32's device ID).
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field

from app.services.llm.base import Message

MAX_SESSIONS = 200


@dataclass
class _Session:
    messages: list[Message] = field(default_factory=list)
    touched: float = field(default_factory=time.monotonic)


class ConversationStore:
    def __init__(self, *, max_turns: int = 6, ttl_seconds: float = 900.0) -> None:
        self._max_messages = max(0, max_turns) * 2
        self._ttl = ttl_seconds
        self._sessions: OrderedDict[str, _Session] = OrderedDict()

    @property
    def enabled(self) -> bool:
        return self._max_messages > 0

    def history(self, session_id: str | None) -> list[Message]:
        if not session_id or not self.enabled:
            return []
        session = self._sessions.get(session_id)
        if session is None:
            return []
        if time.monotonic() - session.touched > self._ttl:
            del self._sessions[session_id]
            return []
        return list(session.messages)

    def record(self, session_id: str | None, user_text: str, assistant_text: str) -> None:
        if not session_id or not self.enabled:
            return
        session = self._sessions.pop(session_id, None) or _Session()
        if time.monotonic() - session.touched > self._ttl:
            session = _Session()
        session.messages += [
            Message(role="user", content=user_text),
            Message(role="assistant", content=assistant_text),
        ]
        session.messages = session.messages[-self._max_messages:]
        session.touched = time.monotonic()
        self._sessions[session_id] = session
        while len(self._sessions) > MAX_SESSIONS:
            self._sessions.popitem(last=False)

    def clear(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
