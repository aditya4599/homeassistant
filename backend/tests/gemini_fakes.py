"""Fake google-genai client + real SDK response objects. No network, no API key."""

from types import SimpleNamespace
from typing import Any

from google.genai import errors, types

from app.services.wav import generate_tone_wav


class FakeModels:
    def __init__(self, result: Any = None, error: BaseException | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


class FakeGeminiClient:
    def __init__(self, result: Any = None, error: BaseException | None = None) -> None:
        self.models = FakeModels(result, error)
        self.aio = SimpleNamespace(models=self.models)

    @property
    def last_call(self) -> dict[str, Any]:
        return self.models.calls[-1]


def response(*parts: types.Part, finish_reason: str = "STOP") -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=list(parts)),
                finish_reason=finish_reason,
            )
        ]
    )


def text_response(text: str, **kw: Any) -> types.GenerateContentResponse:
    return response(types.Part(text=text), **kw)


def audio_response(data: bytes, mime_type: str) -> types.GenerateContentResponse:
    return response(types.Part(inline_data=types.Blob(data=data, mime_type=mime_type)))


def empty_response() -> types.GenerateContentResponse:
    return types.GenerateContentResponse(candidates=[])


def api_error(code: int, status: str, message: str) -> errors.APIError:
    cls = errors.ClientError if code < 500 else errors.ServerError
    return cls(code, {"error": {"code": code, "status": status, "message": message}})


def tts_wav() -> bytes:
    return generate_tone_wav(400, sample_rate_hz=24_000)
