"""Voice pipeline: audio -> STT -> wake-word gate -> chat (LLM + tools) -> TTS -> audio."""

import logging
import time
from dataclasses import dataclass, field

from app.core.exceptions import NoSpeechDetectedError
from app.services.chat import ChatService
from app.services.provider_call import call_provider
from app.services.stt.base import AudioInput, SpeechToTextProvider
from app.services.tts.base import AudioResult, TextToSpeechProvider
from app.services.wake import WakeWordGate

logger = logging.getLogger("app.voice")


@dataclass(frozen=True, slots=True)
class VoiceTurn:
    transcript: str
    response_text: str
    audio: AudioResult | None
    timings_ms: dict[str, float] = field(default_factory=dict)
    wake_detected: bool = False
    #: True when the utterance was ignored (WAKE_WORD_MODE=require and no wake word).
    ignored: bool = False
    #: "idle" after a normal reply; "listening" when the device should keep the mic open.
    state: str = "idle"
    tools_used: tuple[str, ...] = ()


class VoicePipeline:
    def __init__(
        self,
        stt: SpeechToTextProvider,
        chat: ChatService,
        tts: TextToSpeechProvider,
        *,
        wake: WakeWordGate | None = None,
        wake_ack: str = "Yes?",
    ) -> None:
        self._stt = stt
        self._chat = chat
        self._tts = tts
        self._wake = wake
        self._wake_ack = wake_ack

    async def run(self, audio: AudioInput, session_id: str | None = None) -> VoiceTurn:
        timings: dict[str, float] = {}
        total_start = time.perf_counter()

        start = time.perf_counter()
        transcription = await call_provider("stt", self._stt.name, self._stt.transcribe(audio))
        timings["stt"] = _elapsed_ms(start)

        transcript = transcription.text.strip()
        if not transcript:
            raise NoSpeechDetectedError()
        turn = await self._respond(transcript, session_id, timings, synthesize=True)
        timings["total"] = _elapsed_ms(total_start)
        # Transcript / response text are deliberately NOT logged (privacy).
        logger.info(
            "voice_turn_completed",
            extra={
                "timings_ms": timings,
                "ignored": turn.ignored,
                "state": turn.state,
                "tools_used": list(turn.tools_used),
                "response_audio_bytes": len(turn.audio.data) if turn.audio else 0,
                "transcript_chars": len(transcript),
            },
        )
        return turn

    async def respond_text(
        self, text: str, session_id: str | None = None, *, synthesize: bool = True
    ) -> VoiceTurn:
        """Typed/forwarded command (e.g. from the ESP32 WebSocket) through the same path."""
        timings: dict[str, float] = {}
        start = time.perf_counter()
        turn = await self._respond(text.strip(), session_id, timings, synthesize=synthesize,
                                   require_wake=False)  # fmt: skip
        timings["total"] = _elapsed_ms(start)
        return turn

    async def speak(self, text: str) -> AudioResult:
        """TTS only (announcements pushed to a device)."""
        return await call_provider("tts", self._tts.name, self._tts.synthesize(text))

    async def _respond(
        self,
        transcript: str,
        session_id: str | None,
        timings: dict[str, float],
        *,
        synthesize: bool,
        require_wake: bool = True,
    ) -> VoiceTurn:
        command, detected, state = transcript, False, "idle"
        if self._wake is not None:
            gate = self._wake.process(transcript)
            command, detected = gate.command, gate.detected
            if require_wake and not gate.accepted:
                return VoiceTurn(transcript, "", None, timings, ignored=True)
        if not command:
            if not detected:
                raise NoSpeechDetectedError()
            # Just the wake word: acknowledge and keep listening for the command.
            response_text, state, tools = self._wake_ack, "listening", ()
        else:
            start = time.perf_counter()
            chat_turn = await self._chat.respond_turn(command, session_id)
            timings["llm"] = _elapsed_ms(start)
            response_text, tools = chat_turn.text, chat_turn.tools_used

        audio_result = None
        if synthesize:
            start = time.perf_counter()
            audio_result = await call_provider(
                "tts", self._tts.name, self._tts.synthesize(response_text)
            )
            timings["tts"] = _elapsed_ms(start)
        return VoiceTurn(
            transcript=command or transcript,
            response_text=response_text,
            audio=audio_result,
            timings_ms=timings,
            wake_detected=detected,
            state=state,
            tools_used=tuple(tools),
        )


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)
