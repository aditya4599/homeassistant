import re
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from app.api.deps import get_app_settings, get_voice_pipeline
from app.core.config import Settings
from app.core.exceptions import InvalidRequestError, PayloadTooLargeError
from app.schemas.common import ErrorResponse
from app.schemas.voice import VoiceMetadata
from app.services.voice.audio import build_audio_input
from app.services.voice.pipeline import VoicePipeline

router = APIRouter(tags=["voice"])
_SESSION_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


async def _read_body_limited(request: Request, max_bytes: int) -> bytes:
    """Read the raw body, aborting as soon as it exceeds ``max_bytes``."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise PayloadTooLargeError(
            f"Audio exceeds the maximum size of {max_bytes} bytes.",
            detail=f"declared content-length {declared}",
        )
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise PayloadTooLargeError(
                f"Audio exceeds the maximum size of {max_bytes} bytes.",
                detail="streamed body exceeded limit",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post(
    "/voice",
    summary="Send audio, receive a spoken reply (audio/wav)",
    responses={
        200: {
            "description": "Spoken reply. Transcript/response text are in X-Transcript and "
            "X-Response-Text (URL-encoded); X-Assistant-State is 'idle' or 'listening' "
            "(only the wake word was heard - record the command next).",
            "content": {"audio/wav": {"schema": {"type": "string", "format": "binary"}}},
        },
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        413: {"model": ErrorResponse},
        415: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        204: {"description": "Ignored: WAKE_WORD_MODE=require and no wake word was heard."},
    },
)
async def voice(
    request: Request,
    pipeline: Annotated[VoicePipeline, Depends(get_voice_pipeline)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> Response:
    """Body = raw audio bytes (e.g. ``Content-Type: audio/wav``), not multipart."""
    data = await _read_body_limited(request, settings.max_audio_bytes)
    audio = build_audio_input(
        data, request.headers.get("content-type"), max_bytes=settings.max_audio_bytes
    )
    session_id = request.headers.get("x-session-id") or settings.conversation_default_session or None
    if session_id and not _SESSION_RE.match(session_id):
        raise InvalidRequestError("X-Session-ID must be 1-64 characters: letters, digits, _.:-")
    turn = await pipeline.run(audio, session_id)

    if turn.ignored or turn.audio is None:
        return Response(
            status_code=204,
            headers={"X-Wake-Word": "not-detected", "X-Assistant-State": "idle",
                     "Cache-Control": "no-store"},
        )  # fmt: skip

    metadata = VoiceMetadata(
        transcript=turn.transcript,
        response_text=turn.response_text,
        audio_content_type=turn.audio.content_type,
        audio_duration_ms=turn.audio.duration_ms,
    )
    headers = {
        **metadata.to_headers(),
        "Cache-Control": "no-store",
        "X-Assistant-State": turn.state,
        "X-Wake-Word": "detected" if turn.wake_detected else "absent",
    }
    if turn.tools_used:
        headers["X-Tools-Used"] = ",".join(turn.tools_used)
    return Response(content=turn.audio.data, media_type=turn.audio.content_type, headers=headers)
