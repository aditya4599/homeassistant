"""ESP32 / satellite-device endpoints: the WebSocket plus small REST helpers."""

from typing import Annotated

from fastapi import APIRouter, Depends, WebSocket
from pydantic import BaseModel, Field

from app.api.deps import get_app_settings, get_device_manager, get_voice_pipeline
from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.security import require_api_key
from app.services.device import protocol as p
from app.services.device.manager import DeviceManager
from app.services.device.session import DeviceSession
from app.services.voice.pipeline import VoicePipeline

router = APIRouter(prefix="/device", tags=["device"])


class DeviceNotConnectedError(AppError):
    status_code = 404
    code = "device_not_connected"
    default_message = "That device is not connected."


class SayRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    audio: bool = True


@router.websocket("/ws")
async def device_socket(websocket: WebSocket) -> None:
    """Device protocol v1. See ``app/services/device/protocol.py`` and the README."""
    state = websocket.app.state
    await DeviceSession(
        websocket, settings=state.settings, manager=state.device_manager, pipeline=state.voice_pipeline
    ).run()


@router.get("/clients", summary="Connected devices", dependencies=[Depends(require_api_key)])
async def list_clients(manager: Annotated[DeviceManager, Depends(get_device_manager)]) -> dict:
    return {"auth": manager.auth_mode, "devices": manager.list()}


@router.post(
    "/clients/{device_id}/say",
    summary="Speak text on a connected device (announcement / test)",
    dependencies=[Depends(require_api_key)],
)
async def say_on_device(
    device_id: str,
    body: SayRequest,
    manager: Annotated[DeviceManager, Depends(get_device_manager)],
    pipeline: Annotated[VoicePipeline, Depends(get_voice_pipeline)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> dict:
    conn = manager.get(device_id)
    if conn is None:
        raise DeviceNotConnectedError(detail=f"device {device_id!r} not connected")
    want_audio = body.audio and bool(conn.capabilities.get("audio_output"))
    await conn.send_json(p.response(text=body.text, reply_to=None, transcript=None, state="idle",
                                    has_audio=want_audio))  # fmt: skip
    if want_audio:
        from app.services.wav import convert_wav, read_wav_info

        audio = await pipeline.speak(body.text)
        channels = read_wav_info(audio.data).channels
        data, rate = convert_wav(audio.data, target_rate=conn.output_rate,
                                 output=conn.audio_output.encoding)  # fmt: skip
        size = settings.device_audio_chunk_bytes
        chunks = [data[i:i + size] for i in range(0, len(data), size)]
        await conn.send_audio(
            {"type": "audio_output_start", "reply_to": None, "encoding": conn.audio_output.encoding,
             "sample_rate": rate, "channels": channels, "sample_width": 2, "bytes": len(data),
             "chunks": len(chunks)},
            chunks,
            {"type": "audio_output_end", "reply_to": None},
        )  # fmt: skip
    return {"delivered": True, "audio": want_audio}
