"""Platform controllers. Only allowlisted targets ever reach these methods.

Commands run with ``asyncio.create_subprocess_exec`` (argument vector, never a shell),
so names/URLs cannot inject extra commands. Only macOS is implemented today; other
platforms get a clear "not supported" error rather than guessed behaviour.
"""

from __future__ import annotations

import asyncio
import re
import sys
from typing import Protocol

from app.core.exceptions import AppError

_SAFE_APP_NAME = re.compile(r"^[A-Za-z0-9 .&+\-()]{1,64}$")


class ComputerControlError(AppError):
    status_code = 502
    code = "computer_control_error"
    default_message = "I couldn't do that on the computer."


class ComputerController(Protocol):
    async def open_app(self, app_name: str) -> None: ...
    async def close_app(self, app_name: str) -> None: ...
    async def open_url(self, url: str) -> None: ...
    async def open_path(self, path: str, app_name: str | None = None) -> None: ...


async def _run(*argv: str, timeout: float = 10.0) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (OSError, asyncio.TimeoutError) as exc:
        raise ComputerControlError(detail=f"{argv[0]} failed: {type(exc).__name__}") from exc
    if proc.returncode != 0:
        raise ComputerControlError(
            detail=f"{argv[0]} exited {proc.returncode}: {stderr.decode(errors='replace')[:200]}"
        )


def _check_app(name: str) -> str:
    if not _SAFE_APP_NAME.match(name):
        raise ComputerControlError(detail=f"refusing unsafe application name {name!r}")
    return name


class MacOSController:
    async def open_app(self, app_name: str) -> None:
        await _run("open", "-a", _check_app(app_name))

    async def close_app(self, app_name: str) -> None:
        # The name is validated (no quotes possible) before being placed in AppleScript.
        await _run("osascript", "-e", f'tell application "{_check_app(app_name)}" to quit')

    async def open_url(self, url: str) -> None:
        if not url.startswith(("https://", "http://")):
            raise ComputerControlError(detail=f"refusing non-http URL {url!r}")
        await _run("open", url)

    async def open_path(self, path: str, app_name: str | None = None) -> None:
        argv = ["open", "-a", _check_app(app_name), path] if app_name else ["open", path]
        await _run(*argv)


class UnsupportedController:
    def __init__(self, platform: str) -> None:
        self._platform = platform

    async def _fail(self, *_: object) -> None:
        raise ComputerControlError(
            "Computer control isn't supported on this machine yet.",
            detail=f"no controller for platform {self._platform!r}",
        )

    open_app = close_app = open_url = open_path = _fail  # type: ignore[assignment]


def default_controller() -> ComputerController:
    if sys.platform == "darwin":
        return MacOSController()
    return UnsupportedController(sys.platform)
