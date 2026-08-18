"""First real Gemini check: TTS -> STT -> LLM, using the settings in .env.

    python scripts/gemini_smoke_test.py

Uses 3 API calls (plus 1 more TTS for the reply). Writes:
    question.wav  - Gemini speaking the test question (use it to test /api/v1/voice)
    reply.wav     - Gemini speaking the LLM's answer
Needs GEMINI_API_KEY in .env (the *_PROVIDER settings are ignored here).
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.core.exceptions import AppError  # noqa: E402
from app.services.factory import _build_gemini_llm, _build_gemini_stt, _build_gemini_tts  # noqa: E402
from app.services.llm.base import Message  # noqa: E402
from app.services.prompts import build_voice_system_prompt  # noqa: E402
from app.services.stt.base import AudioInput  # noqa: E402
from app.services.wake import WakeWordGate  # noqa: E402

QUESTION = "{name}, what is 25 times 8?"


async def main() -> int:
    settings = get_settings()
    try:
        stt, llm, tts = (b(settings) for b in (_build_gemini_stt, _build_gemini_llm, _build_gemini_tts))
        question = QUESTION.format(name=settings.wake_word)

        print(f"1/4 TTS  [{settings.gemini_tts_model}] speaking: {question!r}")
        spoken = await tts.synthesize(question)
        Path("question.wav").write_bytes(spoken.data)
        print(f"     -> question.wav ({spoken.duration_ms} ms, {spoken.sample_rate_hz} Hz)")

        print(f"2/4 STT  [{settings.gemini_stt_model}] transcribing question.wav")
        heard = await stt.transcribe(AudioInput(data=spoken.data, content_type="audio/wav"))
        command = WakeWordGate(settings.wake_word, aliases=settings.wake_word_alias_list).process(
            heard.text
        ).command
        print(f"     -> heard {heard.text!r}; command {command!r} (wake word stripped)")

        print(f"3/4 LLM  [{settings.gemini_llm_model}]")
        answer = await llm.generate(
            [Message("system", build_voice_system_prompt(settings.assistant_name)),
             Message("user", command or question)]
        )  # fmt: skip
        print(f"     -> {answer.text!r}   (finish_reason={answer.finish_reason})")

        print("4/4 TTS  speaking the answer")
        reply = await tts.synthesize(answer.text)
        Path("reply.wav").write_bytes(reply.data)
        print(f"     -> reply.wav ({reply.duration_ms} ms)\nAll Gemini providers work.")
        return 0
    except AppError as exc:
        # This is an operator tool, so show the technical detail (it never contains the key).
        print(f"\nFAILED: {exc.code}: {exc.public_message}\n  detail: {exc.log_detail}")
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
