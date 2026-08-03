from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import get_app_settings, get_chat_service
from app.core.config import Settings
from app.schemas.chat import ChatRequest, ChatResponse
from app.schemas.common import ErrorResponse
from app.services.chat import ChatService

router = APIRouter(tags=["chat"])


@router.post(
    "/chat",
    response_model=ChatResponse,
    response_model_exclude_none=True,
    summary="Send a text message to the assistant",
    responses={
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
)
async def chat(
    payload: ChatRequest,
    service: Annotated[ChatService, Depends(get_chat_service)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> ChatResponse:
    session = payload.session_id or settings.conversation_default_session or None
    turn = await service.respond_turn(payload.message, session)
    return ChatResponse(
        response=turn.text,
        session_id=payload.session_id,  # echoed only when the client chose one
        tools_used=list(turn.tools_used) or None,
    )
