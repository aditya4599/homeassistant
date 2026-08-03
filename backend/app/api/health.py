from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_providers
from app.schemas.common import HealthResponse, ProviderHealthResponse
from app.services.factory import Providers

# Mounted at the root: GET /health
router = APIRouter(tags=["health"])

# Mounted under /api/v1: GET /api/v1/health/providers
v1_router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Liveness check")
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@v1_router.get(
    "/health/providers",
    response_model=ProviderHealthResponse,
    summary="Active STT / LLM / TTS providers",
)
async def provider_health(
    providers: Annotated[Providers, Depends(get_providers)],
) -> ProviderHealthResponse:
    return ProviderHealthResponse(
        stt=providers.stt.name, llm=providers.llm.name, tts=providers.tts.name
    )


@v1_router.get(
    "/health/tools",
    summary="Enabled assistant tools and device connections",
)
async def tool_health(request: Request) -> dict:
    state = request.app.state
    return {
        "tools": state.tool_registry.names,
        "spotify": {"configured": state.spotify.configured, "connected": state.spotify.connected},
        "devices_connected": len(state.device_manager.list()),
    }
