import logging

from fastapi.testclient import TestClient

from app.factory import create_app
from app.services.chat import ChatService
from app.services.tts.mock import MockTTSProvider
from app.services.voice.pipeline import VoicePipeline
from tests.helpers import FailingLLM, FailingSTT, FakeLLM, make_settings, make_wav


def test_unhandled_exception_returns_generic_500(settings):
    app = create_app(settings)

    async def boom():
        raise RuntimeError("super secret internal detail")

    app.add_api_route("/boom", boom, methods=["GET"])
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/boom")
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["request_id"] == response.headers["x-request-id"]
    assert "super secret" not in response.text
    assert "Traceback" not in response.text


def test_chat_provider_failure_is_502_and_does_not_leak(settings, caplog):
    app = create_app(settings)
    app.state.chat_service = ChatService(
        llm=FailingLLM("upstream rejected key sk-123"), assistant_name="Adi"
    )
    client = TestClient(app, raise_server_exceptions=False)

    with caplog.at_level(logging.INFO):
        response = client.post("/api/v1/chat", json={"message": "Hello"})

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_error"
    assert "sk-123" not in response.text
    # ...but the technical detail IS available in the internal logs.
    assert any("sk-123" in str(getattr(r, "detail", "")) for r in caplog.records)


def test_voice_provider_failure_is_502(settings):
    app = create_app(settings)
    chat = ChatService(llm=FakeLLM(), assistant_name="Adi")
    app.state.voice_pipeline = VoicePipeline(
        stt=FailingSTT("provider exploded"), chat=chat, tts=MockTTSProvider()
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/v1/voice", content=make_wav(), headers={"Content-Type": "audio/wav"}
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_error"
    assert "exploded" not in response.text
