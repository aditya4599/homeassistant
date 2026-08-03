"""Application factory (kept separate from main.py so tests can build custom apps)."""

import logging

from fastapi import FastAPI

from app import __version__
from app.api import root_router, v1_router
from app.core.config import Settings, get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.services.chat import ChatService
from app.services.conversation import ConversationStore
from app.services.device.manager import DeviceManager
from app.services.factory import build_providers
from app.services.spotify.client import SpotifyClient
from app.services.tools.setup import build_tool_registry
from app.services.voice.pipeline import VoicePipeline
from app.services.wake import WakeWordGate

logger = logging.getLogger("app.startup")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    # Fail fast (ConfigurationError) if a provider name is unknown.
    providers = build_providers(settings)
    spotify = SpotifyClient(settings)
    # Spotify tools are only offered when Spotify credentials are configured.
    tools = build_tool_registry(settings, spotify=spotify if spotify.configured else None)
    chat_service = ChatService(
        llm=providers.llm,
        assistant_name=settings.assistant_name,
        tool_executor=tools,
        conversation=ConversationStore(
            max_turns=settings.conversation_max_turns,
            ttl_seconds=settings.conversation_ttl_seconds,
        ),
        fast_intents=settings.fast_intents_enabled,
        direct_tool_replies=settings.tool_direct_replies,
        timezone=settings.assistant_timezone,
    )
    wake = WakeWordGate(
        settings.wake_word, aliases=settings.wake_word_alias_list, mode=settings.wake_word_mode
    )
    voice_pipeline = VoicePipeline(
        stt=providers.stt, chat=chat_service, tts=providers.tts,
        wake=wake, wake_ack=settings.wake_ack_text,
    )  # fmt: skip

    hide_docs = settings.is_production
    app = FastAPI(
        title=f"{settings.assistant_name} Assistant Backend",
        version=__version__,
        docs_url=None if hide_docs else "/docs",
        redoc_url=None if hide_docs else "/redoc",
        openapi_url=None if hide_docs else "/openapi.json",
    )
    app.state.settings = settings
    app.state.providers = providers
    app.state.chat_service = chat_service
    app.state.voice_pipeline = voice_pipeline
    app.state.spotify = spotify
    app.state.tool_registry = tools
    app.state.device_manager = DeviceManager(settings)

    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(root_router)
    app.include_router(v1_router)

    logger.info("app_configured", extra={"settings": settings.safe_summary()})
    return app
