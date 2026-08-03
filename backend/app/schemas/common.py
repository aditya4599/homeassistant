"""Schemas shared across endpoints."""

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str


class ProviderHealthResponse(BaseModel):
    """Names of the active providers (does not ping upstream services)."""

    stt: str
    llm: str
    tts: str


class ErrorFieldDetail(BaseModel):
    field: str
    message: str


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str | None = None
    details: list[ErrorFieldDetail] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody
