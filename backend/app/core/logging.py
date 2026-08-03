"""Structured (JSON) logging with per-request correlation IDs.

Usage:
    logger = logging.getLogger("app.something")
    logger.info("event_name", extra={"key": "value"})

Every ``extra`` field becomes a top-level JSON key. Keys that look sensitive
(password, token, api key, ...) are redacted automatically.
"""

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")

_REDACTED = "***REDACTED***"
_SENSITIVE_KEY = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|authorization|credential)",
    re.IGNORECASE,
)
# Attributes every LogRecord has; anything else came from ``extra=``.
_RESERVED_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
}
_HANDLER_MARKER = "_adi_structured_handler"


def redact(key: str, value: Any) -> Any:
    """Redact ``value`` if ``key`` looks sensitive; recurse into dicts."""
    if _SENSITIVE_KEY.search(key):
        return _REDACTED
    if isinstance(value, dict):
        return {str(k): redact(str(k), v) for k, v in value.items()}
    return value


class JsonFormatter(logging.Formatter):
    """Render each log record as one line of JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            "request_id": request_id_ctx.get(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS or key.startswith("_") or key in payload:
                continue
            payload[key] = redact(key, value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON handler on the root logger (idempotent)."""
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, _HANDLER_MARKER, False):
            root.removeHandler(existing)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    setattr(handler, _HANDLER_MARKER, True)
    root.addHandler(handler)
    root.setLevel(level)

    # Route uvicorn's own logs through our formatter; we log requests ourselves.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
