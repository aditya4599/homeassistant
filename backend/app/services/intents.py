"""Deterministic fast path for short, unambiguous commands.

"pause", "next song", "what's playing", "what time is it" don't need an LLM: matching
them locally saves two model calls (latency + free-tier quota) and makes them work
even with the mock LLM. Only WHOLE utterances that match are handled; anything else
("explain what pause means") goes to the LLM as usual.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_POLITE = r"(?:please\s+)?(?:can you\s+|could you\s+)?"
_TAIL = r"(?:\s+please)?[\s.!?]*$"
_MUSIC = r"(?:\s+(?:the\s+)?(?:music|song|spotify|it|this|playback))?"

_RULES: list[tuple[str, str]] = [
    ("pause_music", rf"^{_POLITE}(?:pause|stop){_MUSIC}{_TAIL}"),
    ("resume_music", rf"^{_POLITE}(?:resume|unpause|continue(?:\s+playing)?|play\s+again|"
                     rf"keep\s+playing){_MUSIC}{_TAIL}"),
    ("next_track", rf"^{_POLITE}(?:next|skip)(?:\s+(?:song|track|this(?:\s+song)?|it))?{_TAIL}"),
    ("previous_track", rf"^{_POLITE}(?:previous|go\s+back|last)(?:\s+(?:song|track))?{_TAIL}"),
    ("current_playback", r"^(?:what(?:'s|\s+is)\s+(?:currently\s+)?playing(?:\s+now)?|"
                         r"what\s+song\s+is\s+(?:this|playing)|who\s+is\s+(?:this|playing|singing)|"
                         r"tell\s+me\s+what(?:'s|\s+is)\s+playing)" + _TAIL),
    ("get_time", r"^(?:what(?:'s|\s+is)\s+the\s+time|what\s+time\s+is\s+it)(?:\s+now)?" + _TAIL),
    ("get_date", r"^(?:what(?:'s|\s+is)\s+(?:today's|the)\s+date(?:\s+today)?|"
                 r"what\s+day\s+is\s+(?:it|today))" + _TAIL),
]
_VOL = r"(?:the\s+)?(?:volume|music|sound)"
_RULES += [
    # "volume 5" / "set volume to 8" / "volume 10 out of 10" -> level on a 0-10 scale
    ("set_volume", rf"^(?:set\s+)?{_VOL}\s+(?:to\s+|at\s+)?(?P<level>10|[0-9](?:\.5)?)"
                   rf"(?:\s+(?:out\s+)?of\s+10)?{_TAIL}"),
    # "volume 35 percent" / "volume to 35%"
    ("set_volume", rf"^(?:set\s+)?{_VOL}\s+(?:to\s+|at\s+)?(?P<percent>100|[0-9]{{1,2}})\s*(?:%|percent){_TAIL}"),
    ("volume_up", rf"^{_POLITE}(?:{_VOL}\s+up|turn\s+(?:it|{_VOL})\s+up|turn\s+up\s+{_VOL}|louder|"
                  rf"increase\s+{_VOL}|raise\s+{_VOL}){_TAIL}"),
    ("volume_down", rf"^{_POLITE}(?:{_VOL}\s+down|turn\s+(?:it|{_VOL})\s+down|turn\s+down\s+{_VOL}|"
                    rf"quieter|softer|decrease\s+{_VOL}|lower\s+{_VOL}){_TAIL}"),
    ("mute_music", rf"^{_POLITE}mute(?:\s+(?:it|{_VOL}|spotify))?{_TAIL}"),
    ("unmute_music", rf"^{_POLITE}unmute(?:\s+(?:it|{_VOL}|spotify))?{_TAIL}"),
]
_COMPILED = [(tool, re.compile(p, re.IGNORECASE)) for tool, p in _RULES]


@dataclass(frozen=True, slots=True)
class Intent:
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)


def match_intent(text: str, available: set[str] | list[str]) -> Intent | None:
    clean = " ".join(text.replace("’", "'").split())
    for tool, pattern in _COMPILED:
        if tool in available and (m := pattern.match(clean)):
            args = {k: float(v) for k, v in m.groupdict().items() if v is not None}
            return Intent(tool, args)
    return None
