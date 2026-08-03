"""Application exceptions and the consistent API error envelope.

Every error response has this shape (never a stack trace):

    {"error": {"code": "...", "message": "...", "request_id": "...", "details": [...]}}
"""

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import request_id_ctx

logger = logging.getLogger("app.errors")


class AppError(Exception):
    """Base class for errors that are safe to describe to API clients.

    ``public_message`` is returned to the client. ``log_detail`` holds the
    technical detail and is only ever written to the logs.
    """

    status_code: int = 500
    code: str = "internal_error"
    default_message: str = "An internal error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        detail: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.public_message = message or self.default_message
        self.log_detail = detail or self.public_message
        self.headers = headers
        super().__init__(self.public_message)


class InvalidRequestError(AppError):
    status_code = 400
    code = "invalid_request"
    default_message = "The request is invalid."


class InvalidAudioError(AppError):
    status_code = 400
    code = "invalid_audio"
    default_message = "The audio payload is invalid."


class NoSpeechDetectedError(InvalidAudioError):
    status_code = 422
    code = "no_speech_detected"
    default_message = "No speech was detected in the audio."


class UnsupportedAudioFormatError(AppError):
    status_code = 415
    code = "unsupported_audio_format"
    default_message = "The audio format is not supported."


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "payload_too_large"
    default_message = "The request payload is too large."


class AuthenticationError(AppError):
    status_code = 401
    code = "unauthorized"
    default_message = "Missing or invalid API key."


class ProviderError(AppError):
    """An STT / LLM / TTS provider failed. Details are logged, not returned."""

    status_code = 502
    code = "provider_error"
    default_message = "An upstream AI provider failed to process the request."

    def __init__(self, *, provider: str, stage: str, detail: str | None = None) -> None:
        self.provider = provider
        self.stage = stage
        super().__init__(
            detail=f"{stage} provider '{provider}' failed: {detail or 'unknown error'}"
        )


class ProviderAuthError(ProviderError):
    """The upstream provider rejected our credentials (bad / missing / unauthorised key)."""

    code = "provider_auth_error"
    default_message = "The AI provider rejected the server's credentials."


class ProviderRateLimitError(ProviderError):
    """Upstream rate limit or quota exhausted (e.g. free-tier limits)."""

    status_code = 503
    code = "provider_rate_limited"
    default_message = "The AI provider's usage limit was reached. Please try again later."


class ProviderTimeoutError(ProviderError):
    """The upstream provider did not answer in time."""

    status_code = 504
    code = "provider_timeout"
    default_message = "The AI provider took too long to respond."


class ProviderUnavailableError(ProviderError):
    """Network failure or upstream 5xx / overload."""

    status_code = 503
    code = "provider_unavailable"
    default_message = "The AI provider is temporarily unavailable. Please try again later."


class ConfigurationError(AppError):
    """Raised at startup for unusable configuration (e.g. unknown provider name)."""

    status_code = 500
    code = "configuration_error"
    default_message = "The server is misconfigured."


class SpotifyNotConfiguredError(AppError):
    status_code = 503
    code = "spotify_not_configured"
    default_message = "Spotify is not configured on the server."


class SpotifyNotConnectedError(AppError):
    status_code = 401
    code = "spotify_not_connected"
    default_message = "Spotify is not connected. Open the Spotify login URL first."


class SpotifyError(AppError):
    status_code = 502
    code = "spotify_error"
    default_message = "Spotify could not complete that request."


class SpotifyAuthExpiredError(SpotifyNotConnectedError):
    """Stored refresh token was revoked or expired (Spotify tokens last ~6 months)."""

    code = "spotify_auth_expired"
    default_message = "Spotify needs to be reconnected."


class SpotifyRateLimitError(SpotifyError):
    status_code = 503
    code = "spotify_rate_limited"
    default_message = "Spotify is busy right now. Please try again in a moment."


class SpotifyNoDeviceError(SpotifyError):
    status_code = 409
    code = "spotify_no_device"
    default_message = "No Spotify device is available. Open Spotify on a phone or computer first."


class SpotifyDeviceNotFoundError(SpotifyError):
    status_code = 404
    code = "spotify_device_not_found"
    default_message = "I couldn't find that device."


class SpotifyDeviceAmbiguousError(SpotifyError):
    """More than one device matches. ``candidates`` lets the assistant ask which one."""

    status_code = 409
    code = "spotify_device_ambiguous"
    default_message = "More than one device matches. Which one should I use?"

    def __init__(self, message: str | None = None, *, candidates: list[str], detail: str | None = None):
        self.candidates = candidates
        super().__init__(message, detail=detail)


class SpotifyNotFoundError(SpotifyError):
    status_code = 404
    code = "spotify_not_found"
    default_message = "I couldn't find that song."


class SpotifyNothingPlayingError(SpotifyError):
    status_code = 409
    code = "spotify_nothing_playing"
    default_message = "Nothing is playing on Spotify right now."


def build_error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    request_id: str | None,
    details: list[dict[str, str]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message, "request_id": request_id}
    if details:
        error["details"] = details
    return JSONResponse(status_code=status_code, content={"error": error}, headers=headers)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or request_id_ctx.get()


async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    extra = {
        "code": exc.code,
        "status_code": exc.status_code,
        "detail": exc.log_detail,
        "path": request.url.path,
    }
    if exc.status_code >= 500:
        logger.error("api_error", extra=extra, exc_info=exc)
    else:
        logger.warning("api_error", extra=extra)
    return build_error_response(
        status_code=exc.status_code,
        code=exc.code,
        message=exc.public_message,
        request_id=_request_id(request),
        headers=exc.headers,
    )


async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    details = [
        {
            "field": ".".join(str(part) for part in err.get("loc", ())),
            "message": str(err.get("msg", "")),
        }
        for err in exc.errors()
    ]
    logger.warning(
        "request_validation_failed",
        extra={"path": request.url.path, "error_count": len(details)},
    )
    return build_error_response(
        status_code=422,
        code="invalid_request",
        message="The request body or parameters are invalid.",
        request_id=_request_id(request),
        details=details,
    )


_HTTP_CODES = {
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
}


async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    try:
        message = HTTPStatus(exc.status_code).phrase
    except ValueError:
        message = "HTTP error"
    logger.warning("http_error", extra={"status_code": exc.status_code, "path": request.url.path})
    return build_error_response(
        status_code=exc.status_code,
        code=_HTTP_CODES.get(exc.status_code, "http_error"),
        message=message,
        request_id=_request_id(request),
        headers=getattr(exc, "headers", None),
    )


async def handle_unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "unhandled_exception",
        extra={"path": request.url.path, "error_type": type(exc).__name__},
        exc_info=exc,
    )
    return build_error_response(
        status_code=500,
        code="internal_error",
        message="An internal error occurred.",
        request_id=_request_id(request),
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, handle_app_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(Exception, handle_unhandled_exception)
