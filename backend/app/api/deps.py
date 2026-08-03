"""FastAPI dependencies. Services live on ``app.state`` (built in app.factory)."""

from fastapi import Request

from app.core.config import Settings
from app.services.chat import ChatService
from app.services.factory import Providers
from app.services.voice.pipeline import VoicePipeline


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_providers(request: Request) -> Providers:
    return request.app.state.providers


def get_chat_service(request: Request) -> ChatService:
    return request.app.state.chat_service


def get_voice_pipeline(request: Request) -> VoicePipeline:
    return request.app.state.voice_pipeline


def get_spotify(request: Request):
    return request.app.state.spotify


def get_device_manager(request: Request):
    return request.app.state.device_manager


def get_tool_registry(request: Request):
    return request.app.state.tool_registry
