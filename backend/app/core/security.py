"""Optional API-key authentication.

If ``API_KEY`` is empty (default) authentication is disabled. If it is set,
protected routes require the header ``X-API-Key: <key>``.
"""

import hmac
from typing import Annotated

from fastapi import Header, Request

from app.core.config import Settings
from app.core.exceptions import AuthenticationError


def keys_match(provided: str | None, expected: str) -> bool:
    """Constant-time comparison (bytes, so non-ASCII input cannot raise)."""
    if not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def mask_secret(value: str, visible: int = 0) -> str:
    """Return a log-safe representation of a secret."""
    if not value:
        return ""
    return (value[:visible] if visible else "") + "***"


async def require_api_key(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    settings: Settings = request.app.state.settings
    expected = settings.api_key.get_secret_value()
    if not expected:
        return
    if not keys_match(x_api_key, expected):
        raise AuthenticationError()
