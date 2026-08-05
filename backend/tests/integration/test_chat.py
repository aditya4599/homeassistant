from fastapi.testclient import TestClient

from app.factory import create_app
from tests.helpers import make_settings


def test_chat_hello(client):
    response = client.post("/api/v1/chat", json={"message": "Hello"})
    assert response.status_code == 200
    assert response.json() == {"response": "Hello! I'm Adi."}


def test_chat_non_greeting(client):
    response = client.post("/api/v1/chat", json={"message": "What is 2+2?"})
    assert response.status_code == 200
    assert "You said: What is 2+2?" in response.json()["response"]


def test_chat_uses_configured_assistant_name():
    client = TestClient(create_app(make_settings(assistant_name="Zed")))
    assert client.post("/api/v1/chat", json={"message": "hi"}).json() == {
        "response": "Hello! I'm Zed."
    }


def test_blank_message_is_rejected_with_error_envelope(client):
    response = client.post("/api/v1/chat", json={"message": "   "})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["request_id"] == response.headers["x-request-id"]
    assert any(d["field"] == "body.message" for d in error["details"])


def test_missing_field_is_rejected(client):
    response = client.post("/api/v1/chat", json={})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_invalid_json_is_rejected(client):
    response = client.post(
        "/api/v1/chat", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_wrong_method_uses_error_envelope(client):
    response = client.get("/api/v1/chat")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_unknown_route_uses_error_envelope(client):
    response = client.get("/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
