"""Wake-word handling for transcripts: "Adi, play Love Dose" -> command "Play Love Dose".

Detection of the wake word in *audio* happens on the device (ESP32). The backend
only sees the transcript, and this gate decides what to do with it:

* ``strip``   (default) - remove a leading wake word if present; process everything.
* ``require`` - ignore utterances that don't start with the wake word. Use this for
  devices that stream audio without local wake-word detection, so background speech
  can't trigger actions.
* ``off``     - pass transcripts through untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

WakeMode = Literal["strip", "require", "off"]
_PREFIXES = r"(?:(?:hey|hi|hello|ok|okay|yo)[\s,]+)?"


@dataclass(frozen=True, slots=True)
class WakeResult:
    detected: bool
    command: str
    #: False => the utterance should be ignored (require mode, no wake word).
    accepted: bool = True


class WakeWordGate:
    def __init__(self, wake_word: str, *, aliases: list[str] | None = None, mode: WakeMode = "strip"):
        names = [n.strip() for n in [wake_word, *(aliases or [])] if n and n.strip()]
        self.mode = mode
        self.wake_word = wake_word.strip()
        alternatives = "|".join(re.escape(n) for n in sorted(set(names), key=len, reverse=True))
        self._pattern = (
            re.compile(rf"^\s*{_PREFIXES}(?:{alternatives})\b[\s,.!?:;\-]*", re.IGNORECASE)
            if alternatives
            else None
        )

    def process(self, transcript: str) -> WakeResult:
        text = transcript.strip()
        if self.mode == "off" or self._pattern is None:
            return WakeResult(detected=False, command=text)
        match = self._pattern.match(text)
        if not match:
            return WakeResult(detected=False, command=text, accepted=self.mode != "require")
        command = text[match.end():].strip()
        if command:
            command = command[0].upper() + command[1:]
        return WakeResult(detected=True, command=command)


def strip_wake_word(text: str, wake_word: str) -> str:
    """Backwards-compatible helper used by the Gemini STT provider."""
    if not wake_word.strip():
        return text.strip()
    return WakeWordGate(wake_word).process(text).command
