import json
import logging
import sys

from app.core.logging import JsonFormatter, redact, request_id_ctx


def _record(**extra) -> logging.LogRecord:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", (), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_output_has_core_fields_and_request_id():
    token = request_id_ctx.set("rid-1")
    try:
        out = json.loads(JsonFormatter().format(_record(status_code=200)))
    finally:
        request_id_ctx.reset(token)
    assert out["event"] == "hello"
    assert out["level"] == "INFO"
    assert out["logger"] == "t"
    assert out["request_id"] == "rid-1"
    assert out["status_code"] == 200
    assert "timestamp" in out


def test_sensitive_keys_are_redacted():
    line = JsonFormatter().format(
        _record(api_key="sk-abc", Authorization="Bearer zzz", nested={"password": "pw", "ok": 1})
    )
    assert "sk-abc" not in line and "zzz" not in line and '"pw"' not in line
    out = json.loads(line)
    assert out["nested"]["ok"] == 1
    assert out["nested"]["password"] == "***REDACTED***"


def test_redact_leaves_normal_values():
    assert redact("duration_ms", 12.5) == 12.5


def test_exception_is_included_in_internal_logs():
    try:
        raise RuntimeError("kaboom")
    except RuntimeError:
        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    assert "kaboom" in json.loads(JsonFormatter().format(record))["exception"]
