import logging


def test_request_received_and_completed_are_logged(client, caplog):
    with caplog.at_level(logging.INFO, logger="app.request"):
        client.get("/health")

    events = {r.getMessage(): r for r in caplog.records if r.name == "app.request"}
    assert "request_received" in events
    assert "request_completed" in events
    done = events["request_completed"]
    assert done.status_code == 200
    assert done.path == "/health"
    assert done.duration_ms >= 0


def test_provider_calls_are_logged_without_content(client, caplog):
    with caplog.at_level(logging.INFO):
        client.post("/api/v1/chat", json={"message": "my private message"})

    assert any(r.getMessage() == "provider_call_completed" for r in caplog.records)
    # User content must never be written to the logs.
    assert not any("my private message" in str(r.__dict__) for r in caplog.records
                   if r.name.startswith("app."))  # fmt: skip
