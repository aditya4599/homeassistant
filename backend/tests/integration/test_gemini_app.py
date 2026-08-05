"""Full app wired with Gemini providers, but the SDK client is faked (no network)."""

import logging
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

import app.services.gemini.client as gemini_client_module
from app.factory import create_app
from app.services.wav import read_wav_info
from tests.gemini_fakes import FakeGeminiClient, api_error, audio_response, text_response, tts_wav
from tests.helpers import make_settings, make_wav

SECRET_KEY = "AIzaSy-THIS-IS-A-FAKE-TEST-KEY"


class ScriptedClient(FakeGeminiClient):
    """Returns a different canned response depending on which model is called."""

    def __init__(self, by_model: dict):
        super().__init__()
        self._by_model = by_model

        async def generate_content(**kwargs):
            self.models.calls.append(kwargs)
            outcome = self._by_model[kwargs["model"]]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        self.models.generate_content = generate_content


def _gemini_app(monkeypatch, by_model):
    fake = ScriptedClient(by_model)
    monkeypatch.setattr(gemini_client_module, "create_gemini_client", lambda settings: fake)
    settings = make_settings(
        stt_provider="gemini", llm_provider="gemini", tts_provider="gemini",
        gemini_api_key=SECRET_KEY, gemini_stt_model="stt", gemini_llm_model="llm",
        gemini_tts_model="tts",
    )  # fmt: skip
    return TestClient(create_app(settings), raise_server_exceptions=False), fake


def _ok_models():
    return {
        "stt": text_response("Adi, what is 25 times 8?"),
        "llm": text_response("That's 200."),
        "tts": audio_response(tts_wav(), "audio/wav"),
    }


def test_health_reports_gemini(monkeypatch):
    client, _ = _gemini_app(monkeypatch, _ok_models())
    assert client.get("/api/v1/health/providers").json() == {
        "stt": "gemini", "llm": "gemini", "tts": "gemini",
    }  # fmt: skip


def test_voice_end_to_end_with_gemini(monkeypatch):
    client, fake = _gemini_app(monkeypatch, _ok_models())
    response = client.post(
        "/api/v1/voice", content=make_wav(), headers={"Content-Type": "audio/wav"}
    )
    assert response.status_code == 200
    assert unquote(response.headers["x-transcript"]) == "What is 25 times 8?"
    assert unquote(response.headers["x-response-text"]) == "That's 200."
    assert read_wav_info(response.content).sample_rate_hz == 24_000
    assert [c["model"] for c in fake.models.calls] == ["stt", "llm", "tts"]
    # The LLM received the stripped transcript, and the voice-tuned system prompt.
    llm_call = fake.models.calls[1]
    assert llm_call["contents"][-1].parts[0].text == "What is 25 times 8?"
    assert "Adi" in llm_call["config"].system_instruction


def test_chat_with_gemini(monkeypatch):
    client, _ = _gemini_app(monkeypatch, _ok_models())
    response = client.post("/api/v1/chat", json={"message": "What is 25 times 8?"})
    assert response.status_code == 200
    assert response.json() == {"response": "That's 200."}


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (api_error(429, "RESOURCE_EXHAUSTED", "quota for project 999"), 503, "provider_rate_limited"),
        (api_error(400, "INVALID_ARGUMENT", "API key not valid"), 502, "provider_auth_error"),
        (api_error(503, "UNAVAILABLE", "model overloaded"), 503, "provider_unavailable"),
    ],
)
def test_gemini_errors_become_clean_api_errors(monkeypatch, caplog, error, status, code):
    models = _ok_models()
    models["llm"] = error
    client, _ = _gemini_app(monkeypatch, models)
    with caplog.at_level(logging.INFO):
        response = client.post("/api/v1/chat", json={"message": "hi"})
    assert response.status_code == status
    body = response.json()["error"]
    assert body["code"] == code
    assert body["request_id"] == response.headers["x-request-id"]
    assert "999" not in response.text and "overloaded" not in response.text
    assert SECRET_KEY not in caplog.text and SECRET_KEY not in response.text


def test_api_key_never_logged_at_startup(monkeypatch, caplog):
    with caplog.at_level(logging.DEBUG):
        _gemini_app(monkeypatch, _ok_models())
    assert SECRET_KEY not in caplog.text
    assert any(getattr(r, "settings", {}).get("gemini_api_key_set") for r in caplog.records)
