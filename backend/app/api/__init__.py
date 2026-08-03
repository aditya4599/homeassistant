"""Router assembly."""

from fastapi import APIRouter, Depends

from app.api import chat, device, health, spotify, voice
from app.core.security import require_api_key

# Unversioned routes (GET /health).
root_router = APIRouter()
root_router.include_router(health.router)

# Versioned routes. chat + voice are protected when API_KEY is configured.
v1_router = APIRouter(prefix="/api/v1")
v1_router.include_router(health.v1_router)
v1_router.include_router(chat.router, dependencies=[Depends(require_api_key)])
v1_router.include_router(voice.router, dependencies=[Depends(require_api_key)])
v1_router.include_router(spotify.router)
# Device router: WebSocket authenticates itself (hello token); REST parts use X-API-Key.
v1_router.include_router(device.router)
