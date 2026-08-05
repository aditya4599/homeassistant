from urllib.parse import unquote

from fastapi.testclient import TestClient

from app.factory import create_app
from app.services.wav import read_wav_info
from tests.helpers import make_settings, make_wav


def _post(client, body: bytes, content_type: str = "audio/wav"):
    return client.post("/api/v1/voice", content=body, headers={"Content-Type": content_type})


def test_voice_happy_path(client):
    response = _post(client, make_wav())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/wav")
    assert response.headers["cache-control"] == "no-store"
    assert read_wav_info(response.content).frame_count > 0
    assert unquote(response.headers["x-transcript"]) == "Hello"
    assert unquote(response.headers["x-response-text"]) == "Hello! I'm Adi."
    assert int(response.headers["x-audio-duration-ms"]) > 0


def test_voice_accepts_raw_pcm(client):
    assert _post(client, bytes(3200), "application/octet-stream").status_code == 200


def test_empty_body_is_invalid_audio(client):
    response = _post(client, b"")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_audio"


def test_corrupt_wav_is_invalid_audio(client):
    response = _post(client, b"definitely not a wav")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_audio"


def test_unsupported_content_type(client):
    response = _post(client, b"hello", "text/plain")
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_audio_format"


def test_oversized_audio_is_rejected():
    client = TestClient(create_app(make_settings(max_audio_bytes=1000)))
    response = _post(client, make_wav())  # ~6.4 KB > 1000 bytes
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def test_error_responses_carry_request_id(client):
    response = _post(client, b"")
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]
