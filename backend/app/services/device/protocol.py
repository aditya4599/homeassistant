"""Device <-> backend WebSocket protocol (version 1).

Transport: one WebSocket per device at ``/api/v1/device/ws``.
* Text frames carry JSON objects with a ``type`` field.
* Binary frames carry raw audio: device->server between ``audio_start`` and
  ``audio_end``; server->device between ``audio_output_start`` and ``audio_output_end``.
  Devices that can't send binary frames may use ``audio_chunk`` with base64 ``data``.

Every client message may carry an ``id``; server replies echo it as ``reply_to``.
Unknown fields are ignored so both sides can add fields without breaking older peers.
See README "ESP32 communication" for the full message table and flows.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

PROTOCOL_VERSION = 1
DEVICE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$"

# Close codes (4000-4999 are application-defined).
CLOSE_REPLACED = 4000  # same device_id connected again elsewhere
CLOSE_AUTH_FAILED = 4001
CLOSE_PROTOCOL_ERROR = 4002
CLOSE_DISABLED = 4003
CLOSE_HEARTBEAT_TIMEOUT = 4008
CLOSE_NORMAL = 1000

AudioEncoding = Literal["pcm_s16le", "wav"]


class _Msg(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    id: str | None = Field(default=None, max_length=64)


class AudioFormat(BaseModel):
    model_config = ConfigDict(extra="ignore")
    encoding: AudioEncoding = "pcm_s16le"
    sample_rate: int = Field(default=16000, ge=8000, le=48000)
    channels: int = Field(default=1, ge=1, le=2)
    #: Only 16-bit samples are supported today; declared so it can grow later.
    sample_width: Literal[2] = 2


class Capabilities(BaseModel):
    model_config = ConfigDict(extra="allow")
    audio_input: bool = False
    audio_output: bool = False
    #: True if the device detects the wake word locally (recommended).
    wake_word_on_device: bool = False
    binary_frames: bool = True


# ------------------------------------------------------------------ client -> server
class Hello(_Msg):
    type: Literal["hello"]
    device_id: str = Field(pattern=DEVICE_ID_PATTERN)
    token: str | None = Field(default=None, max_length=512)
    protocol: int = PROTOCOL_VERSION
    firmware: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=64)
    capabilities: Capabilities = Field(default_factory=Capabilities)
    #: Preferred speaker format; server falls back to its configured default.
    audio_output: AudioFormat | None = None


class Ping(_Msg):
    type: Literal["ping"]
    ts: float | None = None


class Pong(_Msg):
    type: Literal["pong"]
    ts: float | None = None


class Command(_Msg):
    type: Literal["command"]
    text: str = Field(min_length=1, max_length=2000)
    want_audio: bool = True


class AudioStart(_Msg):
    type: Literal["audio_start"]
    format: AudioFormat | None = None
    want_audio: bool = True


class AudioChunk(_Msg):
    type: Literal["audio_chunk"]
    data: str = Field(max_length=200_000)  # base64


class AudioEnd(_Msg):
    type: Literal["audio_end"]


class AudioCancel(_Msg):
    type: Literal["audio_cancel"]


class Status(_Msg):
    type: Literal["status"]
    state: str | None = Field(default=None, max_length=32)
    battery: float | None = None
    rssi: int | None = None
    volume: float | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class Bye(_Msg):
    type: Literal["bye"]
    reason: str | None = Field(default=None, max_length=200)


ClientMessage = Annotated[
    Hello | Ping | Pong | Command | AudioStart | AudioChunk | AudioEnd | AudioCancel | Status | Bye,
    Field(discriminator="type"),
]
CLIENT_MESSAGE = TypeAdapter(ClientMessage)


def parse_client_message(raw: str | bytes) -> Hello | Ping | Pong | Command | AudioStart | AudioChunk | AudioEnd | AudioCancel | Status | Bye:
    """Validate a JSON text frame. Raises ``pydantic.ValidationError`` / ``ValueError``."""
    return CLIENT_MESSAGE.validate_json(raw)


# ------------------------------------------------------------------ server -> client
def welcome(*, session_id: str, device_id: str, heartbeat: float, timeout: float,
            audio_input: AudioFormat, audio_output: dict[str, Any], assistant_name: str,
            wake_word: str, wake_word_mode: str, server_version: str) -> dict[str, Any]:  # fmt: skip
    return {
        "type": "welcome",
        "protocol": PROTOCOL_VERSION,
        "session_id": session_id,
        "device_id": device_id,
        "server": {"name": assistant_name, "version": server_version},
        "heartbeat_seconds": heartbeat,
        "timeout_seconds": timeout,
        "audio_input": audio_input.model_dump(),
        # sample_rate None => TTS native rate, announced in each audio_output_start.
        "audio_output": audio_output,
        "wake_word": wake_word,
        "wake_word_mode": wake_word_mode,
    }


def response(*, text: str, reply_to: str | None, transcript: str | None, state: str,
             has_audio: bool, tools_used: list[str] | None = None,
             ignored: bool = False) -> dict[str, Any]:  # fmt: skip
    msg: dict[str, Any] = {
        "type": "response", "reply_to": reply_to, "text": text, "state": state,
        "audio": has_audio,
    }  # fmt: skip
    if transcript is not None:
        msg["transcript"] = transcript
    if tools_used:
        msg["tools_used"] = tools_used
    if ignored:
        msg["ignored"] = True
    return msg


def status(state: str, **extra: Any) -> dict[str, Any]:
    return {"type": "status", "state": state, **extra}


def error(code: str, message: str, reply_to: str | None = None) -> dict[str, Any]:
    return {"type": "error", "code": code, "message": message, "reply_to": reply_to}
