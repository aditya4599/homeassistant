"""Request-ID + access-log middleware."""

import logging
import re
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.exceptions import build_error_response
from app.core.logging import request_id_ctx

logger = logging.getLogger("app.request")

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def resolve_request_id(incoming: str | None) -> str:
    """Reuse a well-formed client-supplied ID, otherwise generate one."""
    if incoming and _VALID_REQUEST_ID.match(incoming):
        return incoming
    return uuid.uuid4().hex


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = resolve_request_id(request.headers.get(REQUEST_ID_HEADER))
        token = request_id_ctx.set(request_id)
        request.state.request_id = request_id
        start = time.perf_counter()
        method, path = request.method, request.url.path

        logger.info(
            "request_received",
            extra={
                "method": method,
                "path": path,
                "client": request.client.host if request.client else None,
            },
        )
        try:
            try:
                response = await call_next(request)
            except Exception as exc:
                # Last line of defence: never leak internals, always return the envelope.
                logger.error(
                    "request_failed",
                    extra={"method": method, "path": path, "error_type": type(exc).__name__},
                    exc_info=exc,
                )
                response = build_error_response(
                    status_code=500,
                    code="internal_error",
                    message="An internal error occurred.",
                    request_id=request_id,
                )
            response.headers[REQUEST_ID_HEADER] = request_id
            logger.info(
                "request_completed",
                extra={
                    "method": method,
                    "path": path,
                    "status_code": response.status_code,
                    "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                },
            )
            return response
        finally:
            request_id_ctx.reset(token)
