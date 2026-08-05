from urllib.parse import unquote

from fastapi.testclient import TestClient

from app.factory import create_app
from tests.helpers import FakeSTT, make_settings, make_wav


def test_chat_tools_in_mock_mode():
    client = TestClient(create_app(make_settings()))
    body = client.post("/api/v1/chat", json={"message": "Calculate 25 times 36"}).json()
    assert body == {"response": "That's 900.", "tools_used": ["calculate"]}
    body = client.post("/api/v1/chat", json={"message": "What time is it?", "session_id": "s-1"}).json()
    assert body["response"].startswith("It's ") and body["session_id"] == "s-1"
    assert client.get("/api/v1/health/tools").json()["tools"][:3] == ["get_time", "get_date", "calculate"]


def test_voice_wake_word_modes():
    app = create_app(make_settings(wake_word_mode="require"))
    client = TestClient(app)
    chat = app.state.voice_pipeline._chat
    from app.services.voice.pipeline import VoicePipeline

    def send(transcript):
        app.state.voice_pipeline = VoicePipeline(
            FakeSTT(transcript), chat, app.state.providers.tts,
            wake=app.state.voice_pipeline._wake, wake_ack="Yes?",
        )  # fmt: skip
        return client.post("/api/v1/voice", content=make_wav(), headers={"Content-Type": "audio/wav"})

    ignored = send("turn off the lights in the other room")
    assert ignored.status_code == 204 and ignored.headers["x-wake-word"] == "not-detected"
    listening = send("Adi")
    assert listening.status_code == 200 and listening.headers["x-assistant-state"] == "listening"
    assert unquote(listening.headers["x-response-text"]) == "Yes?"
    ok = send("Adi, calculate 2 plus 2")
    assert unquote(ok.headers["x-response-text"]) == "That's 4."
    assert ok.headers["x-tools-used"] == "calculate" and ok.headers["x-wake-word"] == "detected"
