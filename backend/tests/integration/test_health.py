import httpx


def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_every_response_has_a_request_id(client):
    assert client.get("/health").headers["x-request-id"]


def test_valid_client_request_id_is_echoed(client):
    response = client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert response.headers["x-request-id"] == "abc-123"


def test_malformed_client_request_id_is_replaced(client):
    response = client.get("/health", headers={"X-Request-ID": "bad id with spaces!"})
    assert response.headers["x-request-id"] != "bad id with spaces!"
    assert response.headers["x-request-id"]


def test_provider_health(client):
    response = client.get("/api/v1/health/providers")
    assert response.status_code == 200
    assert response.json() == {"stt": "mock", "llm": "mock", "tts": "mock"}


async def test_health_via_async_httpx_client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
