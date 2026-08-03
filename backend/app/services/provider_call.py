"""Uniform wrapper for calling any STT/LLM/TTS provider."""

import logging
import time
from collections.abc import Awaitable
from typing import TypeVar

from app.core.exceptions import AppError, ProviderError

logger = logging.getLogger("app.providers")

T = TypeVar("T")


async def call_provider(stage: str, provider: str, call: Awaitable[T]) -> T:
    """Await ``call``; log timing; convert unexpected failures to :class:`ProviderError`.

    ``AppError`` subclasses raised by a provider pass through unchanged. Anything
    else becomes a 502 ``provider_error`` whose technical detail is logged only.
    """
    start = time.perf_counter()
    try:
        result = await call
    except AppError:
        raise
    except Exception as exc:
        logger.error(
            "provider_call_failed",
            extra={
                "stage": stage,
                "provider": provider,
                "error_type": type(exc).__name__,
                "duration_ms": round((time.perf_counter() - start) * 1000, 2),
            },
        )
        raise ProviderError(
            provider=provider, stage=stage, detail=f"{type(exc).__name__}: {exc}"
        ) from exc
    logger.info(
        "provider_call_completed",
        extra={
            "stage": stage,
            "provider": provider,
            "duration_ms": round((time.perf_counter() - start) * 1000, 2),
        },
    )
    return result
