"""Tracks connected devices and authenticates them. Supports any number of devices."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings
from app.core.security import keys_match

logger = logging.getLogger("app.device")


@dataclass
class DeviceConnection:
    device_id: str
    websocket: Any
    session_id: str
    name: str | None = None
    firmware: str | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    audio_output: Any = None  # AudioFormat (encoding/channels) negotiated at hello
    output_rate: int = 0  # 0 = send TTS audio at its native rate
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.monotonic)
    status: dict[str, Any] = field(default_factory=dict)
    state: str = "idle"
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def touch(self) -> None:
        self.last_seen = time.monotonic()

    async def send_json(self, payload: dict[str, Any]) -> None:
        async with self.send_lock:
            await self.websocket.send_json(payload)

    async def send_audio(self, start: dict[str, Any], chunks: list[bytes], end: dict[str, Any]) -> None:
        # Hold the lock for the whole stream so JSON messages can't interleave with it.
        async with self.send_lock:
            await self.websocket.send_json(start)
            for chunk in chunks:
                await self.websocket.send_bytes(chunk)
            await self.websocket.send_json(end)

    def summary(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "firmware": self.firmware,
            "state": self.state,
            "status": self.status,
            "capabilities": self.capabilities,
            "connected_at": self.connected_at,
            "idle_seconds": round(time.monotonic() - self.last_seen, 1),
        }


class DeviceManager:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._devices: dict[str, DeviceConnection] = {}
        self._connect_counts: dict[str, int] = {}

    # ------------------------------------------------------------------ auth
    @property
    def auth_mode(self) -> str:
        if self._settings.device_token_map:
            return "per-device"
        if self._settings.effective_device_shared_token:
            return "shared"
        return "disabled"

    def authenticate(self, device_id: str, token: str | None) -> bool:
        per_device = self._settings.device_token_map
        if per_device:
            expected = per_device.get(device_id)
            return bool(expected) and keys_match(token, expected)
        shared = self._settings.effective_device_shared_token
        if shared:
            return keys_match(token, shared)
        # No credentials configured: allow only outside production.
        return not self._settings.is_production

    # ------------------------------------------------------------------ registry
    async def register(self, conn: DeviceConnection) -> DeviceConnection | None:
        """Add a connection; returns the previous connection for this ID (caller closes it)."""
        previous = self._devices.get(conn.device_id)
        self._devices[conn.device_id] = conn
        self._connect_counts[conn.device_id] = self._connect_counts.get(conn.device_id, 0) + 1
        logger.info(
            "device_connected",
            extra={"device_id": conn.device_id, "reconnect": previous is not None
                   or self._connect_counts[conn.device_id] > 1},
        )  # fmt: skip
        return previous

    def unregister(self, conn: DeviceConnection) -> None:
        # Only remove if it's still the current connection (a reconnect may have replaced it).
        if self._devices.get(conn.device_id) is conn:
            del self._devices[conn.device_id]
            logger.info("device_disconnected", extra={"device_id": conn.device_id})

    def get(self, device_id: str) -> DeviceConnection | None:
        return self._devices.get(device_id)

    def list(self) -> list[dict[str, Any]]:
        return [
            c.summary() | {"connections_total": self._connect_counts.get(c.device_id, 1)}
            for c in self._devices.values()
        ]
