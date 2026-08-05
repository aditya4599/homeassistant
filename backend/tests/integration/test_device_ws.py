import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.factory import create_app
from app.services.device.protocol import CLOSE_AUTH_FAILED
from app.services.wav import read_wav_info
from tests.helpers import make_settings

HELLO = {"type": "hello", "device_id": "esp32-desk", "firmware": "0.1",
         "capabilities": {"audio_input": True, "audio_output": True}}  # fmt: skip


def _client(**overrides):
    return TestClient(create_app(make_settings(**overrides)))


def _until(ws, msg_type):
    """Read messages until one of msg_type arrives; return (it, everything seen)."""
    seen = []
    while True:
        message = ws.receive()
        if message.get("bytes") is not None:
            seen.append(message["bytes"])
            continue
        data = json.loads(message["text"])
        seen.append(data)
        if data["type"] == msg_type:
            return data, seen


def test_handshake_command_and_ping():
    client = _client()
    with client.websocket_connect("/api/v1/device/ws") as ws:
        ws.send_json(HELLO)
        welcome = ws.receive_json()
        assert welcome["type"] == "welcome" and welcome["session_id"] == "device:esp32-desk"
        assert welcome["audio_input"]["sample_rate"] == 16000

        ws.send_json({"type": "ping", "id": "p1"})
        pong, _ = _until(ws, "pong")
        assert pong["reply_to"] == "p1"

        ws.send_json({"type": "command", "id": "c1", "text": "Adi, hello", "want_audio": False})
        reply, seen = _until(ws, "response")
        assert reply == {"type": "response", "reply_to": "c1", "text": "Hello! I'm Adi.",
                         "state": "idle", "audio": False, "transcript": "Hello"}  # fmt: skip
        assert any(isinstance(m, dict) and m.get("state") == "processing" for m in seen)

        clients = client.get("/api/v1/device/clients").json()
        assert clients["devices"][0]["device_id"] == "esp32-desk"


def test_audio_stream_in_and_audio_out():
    client = _client(device_audio_output_format="pcm_s16le", device_audio_output_sample_rate=16000)
    with client.websocket_connect("/api/v1/device/ws") as ws:
        ws.send_json(HELLO)
        ws.receive_json()
        ws.send_json({"type": "audio_start", "id": "a1",
                      "format": {"encoding": "pcm_s16le", "sample_rate": 16000}})  # fmt: skip
        for _ in range(4):
            ws.send_bytes(bytes(1600))  # 4 x 50 ms of silence (mock STT ignores content)
        ws.send_json({"type": "audio_end"})
        reply, _ = _until(ws, "response")
        assert reply["reply_to"] == "a1" and reply["audio"] is True
        start, _ = _until(ws, "audio_output_start")
        assert (start["encoding"], start["sample_rate"]) == ("pcm_s16le", 16000)
        end, seen = _until(ws, "audio_output_end")
        received = b"".join(m for m in seen if isinstance(m, bytes))
        assert len(received) == start["bytes"] and received[:4] != b"RIFF"


def test_wav_output_by_default_and_say_endpoint():
    client = _client()
    with client.websocket_connect("/api/v1/device/ws") as ws:
        ws.send_json(HELLO)
        ws.receive_json()
        assert client.post("/api/v1/device/clients/esp32-desk/say", json={"text": "Dinner!"}).json() == {
            "delivered": True, "audio": True}
        _until(ws, "response")
        start, _ = _until(ws, "audio_output_start")
        _, seen = _until(ws, "audio_output_end")
        wav = b"".join(m for m in seen if isinstance(m, bytes))
        assert read_wav_info(wav).frame_count > 0 and start["encoding"] == "wav"
    assert client.post("/api/v1/device/clients/nope/say", json={"text": "x"}).status_code == 404


def test_auth_required_when_token_configured():
    client = _client(device_tokens="esp32-desk:s3cret")
    with client.websocket_connect("/api/v1/device/ws") as ws:
        ws.send_json(HELLO | {"token": "wrong"})
        assert ws.receive_json()["code"] == "unauthorized"
        with pytest.raises(WebSocketDisconnect) as info:
            ws.receive_json()
        assert info.value.code == CLOSE_AUTH_FAILED
    with client.websocket_connect("/api/v1/device/ws") as ws:
        ws.send_json(HELLO | {"token": "s3cret"})
        assert ws.receive_json()["type"] == "welcome"


def test_first_message_must_be_hello_and_errors_are_clean():
    client = _client()
    with client.websocket_connect("/api/v1/device/ws") as ws:
        ws.send_json({"type": "command", "text": "hi"})
        assert ws.receive_json()["code"] == "hello_required"
    with client.websocket_connect("/api/v1/device/ws") as ws:
        ws.send_json(HELLO)
        ws.receive_json()
        ws.send_text("{not json")
        assert _until(ws, "error")[0]["code"] == "invalid_message"
        ws.send_bytes(b"\x00\x01")
        assert _until(ws, "error")[0]["code"] == "audio_not_started"


def test_reconnect_replaces_previous_connection():
    client = _client()
    with client.websocket_connect("/api/v1/device/ws") as first:
        first.send_json(HELLO)
        first.receive_json()
        with client.websocket_connect("/api/v1/device/ws") as second:
            second.send_json(HELLO)
            assert second.receive_json()["type"] == "welcome"
            assert first.receive_json()["code"] == "replaced"
            assert len(client.get("/api/v1/device/clients").json()["devices"]) == 1
