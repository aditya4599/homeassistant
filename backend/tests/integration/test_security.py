from fastapi.testclient import TestClient

from app.factory import create_app
from tests.helpers import make_settings, make_wav


def _client() -> TestClient:
    return TestClient(create_app(make_settings(api_key="topsecret")))


def test_auth_disabled_when_no_key_configured(client):
    assert client.post("/api/v1/chat", json={"message": "hi"}).status_code == 200


def test_missing_key_is_rejected():
    response = _client().post("/api/v1/chat", json={"message": "hi"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_wrong_key_is_rejected():
    response = _client().post(
        "/api/v1/chat", json={"message": "hi"}, headers={"X-API-Key": "wrong"}
    )
    assert response.status_code == 401


def test_correct_key_is_accepted():
    response = _client().post(
        "/api/v1/chat", json={"message": "hi"}, headers={"X-API-Key": "topsecret"}
    )
    assert response.status_code == 200


def test_voice_is_protected_too():
    client = _client()
    body, ctype = make_wav(), {"Content-Type": "audio/wav"}
    assert client.post("/api/v1/voice", content=body, headers=ctype).status_code == 401
    ok = client.post("/api/v1/voice", content=body, headers={**ctype, "X-API-Key": "topsecret"})
    assert ok.status_code == 200


def test_health_endpoints_stay_public():
    client = _client()
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/health/providers").status_code == 200
