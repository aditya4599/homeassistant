"""Spotify tools for the LLM (provider-neutral: built on ``app.services.tools``)."""

from __future__ import annotations

from typing import Any

from app.core.exceptions import (
    SpotifyDeviceAmbiguousError,
    SpotifyDeviceNotFoundError,
    SpotifyNoDeviceError,
)
from app.services.llm.tools import ToolDefinition
from app.services.tools.base import Tool, ToolResult, arg_str, from_device, schema
from app.services.tools.registry import ToolRegistry

_DEVICE_ARG = {
    "type": "string",
    "description": (
        "Optional playback device exactly as the user said it, e.g. 'T2 Pro', "
        "'vivo T2 Pro', 'phone', 'MacBook', 'laptop'. Omit if the user named no device."
    ),
}


def _on(spoken: str | None) -> str:
    """Echo the user's own words: "my T2 Pro" -> " on your T2 Pro"."""
    if not spoken:
        return ""
    words = spoken.strip().split()
    while words and words[0].lower() in {"my", "the", "your", "on"}:
        words.pop(0)
    return f" on your {' '.join(words)}" if words else ""


def _track_phrase(track: dict[str, Any]) -> str:
    name = track.get("name") or "that"
    artists = str(track.get("artists") or "").split(",")[0].strip()
    return f"{name} by {artists}" if artists else name


def _ambiguous(exc: SpotifyDeviceAmbiguousError) -> ToolResult:
    return ToolResult(
        ok=False, say=exc.public_message, error_code=exc.code, data={"candidates": exc.candidates}
    )


VOLUME_STEP = 0.1  # "volume up/down" step on the normalized 0.0-1.0 scale


def _normalized(args: dict[str, Any]) -> float:
    """``level`` 0-10 ("volume 5" = 50%) or ``percent`` 0-100 -> 0.0..1.0."""
    if args.get("percent") is not None:
        value = float(args["percent"]) / 100.0
    elif args.get("level") is not None:
        value = float(args["level"]) / 10.0
    else:
        raise ValueError("level or percent is required")
    return max(0.0, min(1.0, value))


def _spoken_volume(percent: int) -> str:
    return f"Volume {percent // 10}." if percent % 10 == 0 else f"Volume {percent} percent."


def spotify_tools(client: Any, default_device: str = "") -> list[Tool]:
    """Build the Spotify tool set around a :class:`SpotifyClient` (or compatible fake).

    ``default_device`` (SPOTIFY_DEFAULT_DEVICE, e.g. "ADI Speaker") is used when a request
    comes from the ADI device itself and no device was named.
    """
    default_device = default_device.strip()
    pairing_say = (
        f"To play music on me, open Spotify on your phone, tap the devices button and choose "
        f"{default_device} once. After that, just ask."
    )

    def target(spoken: str | None) -> tuple[str | None, bool]:
        """(device to use, whether it is the implicit default)."""
        if spoken:
            return spoken, False
        if default_device and from_device():
            return default_device, True
        return None, False

    def pairing() -> ToolResult:
        return ToolResult(ok=False, say=pairing_say, error_code="spotify_pairing_required")

    async def play_music(args: dict[str, Any]) -> ToolResult:
        query, artist = arg_str(args, "query"), arg_str(args, "artist")
        spoken = arg_str(args, "device")
        device, implicit = target(spoken)
        try:
            result = await client.play_music(
                query or ("" if artist else "music"), preferred_device=device, artist=artist
            )
        except SpotifyDeviceAmbiguousError as exc:
            return _ambiguous(exc)
        except (SpotifyDeviceNotFoundError, SpotifyNoDeviceError):
            if implicit:
                return pairing()
            raise
        mode = result.get("mode")
        where = _on(spoken)
        if mode == "resume":
            say = f"Playing{where}."
        elif mode == "artist":
            say = f"Playing songs by {artist}{where}."
        else:
            say = f"Playing {_track_phrase(result['track'])}{where}."
        return ToolResult(ok=True, say=say, data={"action": "play_music", **result})

    async def play_last_searched(args: dict[str, Any]) -> ToolResult:
        device, implicit = target(arg_str(args, "device"))
        try:
            result = await client.play_last_searched(device)
        except SpotifyDeviceAmbiguousError as exc:
            return _ambiguous(exc)
        except (SpotifyDeviceNotFoundError, SpotifyNoDeviceError):
            if implicit:
                return pairing()
            raise
        return ToolResult(
            ok=True, say=f"Playing {_track_phrase(result['track'])}.",
            data={"action": "play_last_searched", **result},
        )  # fmt: skip

    async def pause_music(args: dict[str, Any]) -> ToolResult:
        await client.pause()
        return ToolResult(ok=True, say="Paused.", data={"action": "pause_music"})

    async def resume_music(args: dict[str, Any]) -> ToolResult:
        spoken = arg_str(args, "device")
        device, implicit = target(spoken)
        try:
            result = await client.resume(device)
        except SpotifyDeviceAmbiguousError as exc:
            return _ambiguous(exc)
        except (SpotifyDeviceNotFoundError, SpotifyNoDeviceError):
            if implicit:
                return pairing()
            raise
        return ToolResult(
            ok=True, say=f"Playing again{_on(spoken)}.",
            data={"action": "resume_music", **result},
        )  # fmt: skip

    async def next_track(args: dict[str, Any]) -> ToolResult:
        await client.next_track()
        return ToolResult(ok=True, say="Skipping.", data={"action": "next_track"})

    async def previous_track(args: dict[str, Any]) -> ToolResult:
        await client.previous_track()
        return ToolResult(ok=True, say="Going back.", data={"action": "previous_track"})

    async def current_playback(args: dict[str, Any]) -> ToolResult:
        now = await client.current_playback()
        phrase = _track_phrase(now)
        say = f"{phrase}." if now.get("is_playing") else f"{phrase}. It's paused."
        return ToolResult(ok=True, say=say, data={"action": "current_playback", **now})

    async def transfer_playback(args: dict[str, Any]) -> ToolResult:
        device = arg_str(args, "device")
        if not device:
            raise ValueError("device is required")
        try:
            result = await client.transfer(device)
        except SpotifyDeviceAmbiguousError as exc:
            return _ambiguous(exc)
        return ToolResult(
            ok=True, say=f"Playing it{_on(device)}.",
            data={"action": "transfer_playback", **result},
        )  # fmt: skip

    async def volume_device() -> tuple[int | None, str | None] | ToolResult:
        """Current volume and the device to change: the active one, else the ADI speaker."""
        current, device = await client.volume_state()
        if device is not None:
            return current, device.get("id")
        if default_device and from_device():
            try:
                found = await client.choose_device(default_device)
            except (SpotifyDeviceNotFoundError, SpotifyNoDeviceError):
                return pairing()
            vol = found.get("volume_percent")
            return (int(vol) if vol is not None else None), found.get("id")
        raise SpotifyNoDeviceError(detail="no active device for a volume change")

    async def change_volume(percent: int, action: str) -> ToolResult:
        picked = await volume_device()
        if isinstance(picked, ToolResult):
            return picked
        _, device_id = picked
        await client.set_volume(percent, device_id)
        if percent > 0:
            client.volume_before_mute = None
        return ToolResult(
            ok=True, say=_spoken_volume(percent),
            data={"action": action, "volume": percent / 100.0, "volume_percent": percent},
        )  # fmt: skip

    async def set_volume(args: dict[str, Any]) -> ToolResult:
        return await change_volume(round(_normalized(args) * 100), "set_volume")

    async def step_volume(direction: int) -> ToolResult:
        picked = await volume_device()
        if isinstance(picked, ToolResult):
            return picked
        current, device_id = picked
        now = (current if current is not None else 50) / 100.0
        new = max(0.0, min(1.0, now + direction * VOLUME_STEP))
        percent = round(new * 100)
        await client.set_volume(percent, device_id)
        if percent > 0:
            client.volume_before_mute = None
        return ToolResult(
            ok=True, say=_spoken_volume(percent),
            data={"action": "volume_up" if direction > 0 else "volume_down",
                  "volume": new, "volume_percent": percent},
        )  # fmt: skip

    async def volume_up(args: dict[str, Any]) -> ToolResult:
        return await step_volume(+1)

    async def volume_down(args: dict[str, Any]) -> ToolResult:
        return await step_volume(-1)

    async def mute_music(args: dict[str, Any]) -> ToolResult:
        picked = await volume_device()
        if isinstance(picked, ToolResult):
            return picked
        current, device_id = picked
        if current:
            client.volume_before_mute = current
        await client.set_volume(0, device_id)
        return ToolResult(ok=True, say="Muted.", data={"action": "mute_music", "volume": 0.0})

    async def unmute_music(args: dict[str, Any]) -> ToolResult:
        picked = await volume_device()
        if isinstance(picked, ToolResult):
            return picked
        current, device_id = picked
        restore = client.volume_before_mute or (current if current else 50)
        await client.set_volume(restore, device_id)
        client.volume_before_mute = None
        return ToolResult(
            ok=True, say="Unmuted.",
            data={"action": "unmute_music", "volume": restore / 100.0, "volume_percent": restore},
        )  # fmt: skip

    async def list_devices(args: dict[str, Any]) -> ToolResult:
        names = [str(d.get("name")) for d in await client.devices() if d.get("name")]
        say = (
            "Spotify is available on " + ", ".join(names) + "."
            if names
            else "I don't see any Spotify devices. Open Spotify on a phone or computer."
        )
        return ToolResult(ok=True, say=say, data={"devices": names})

    no_args = schema()
    device_only = schema({"device": _DEVICE_ARG})
    return [
        Tool(ToolDefinition(
            "play_music",
            "Play music on Spotify: a song (optionally with artist), songs by an artist, "
            "or just music. Use for 'play X', 'play X by Y', 'play something by Y', "
            "'play music on my T2 Pro'.",
            schema({
                "query": {"type": "string", "description": "Song title only, e.g. 'Love Dose'. "
                          "Use 'music' if the user didn't name a song."},
                "artist": {"type": "string", "description": "Artist name, if the user gave one."},
                "device": _DEVICE_ARG,
            }),
        ), play_music),
        Tool(ToolDefinition(
            "play_last_searched",
            "Play the most recently searched or played song again "
            "('play the latest song I searched').",
            device_only,
        ), play_last_searched),
        Tool(ToolDefinition("pause_music", "Pause Spotify ('pause', 'stop the music').", no_args),
             pause_music),
        Tool(ToolDefinition(
            "resume_music",
            "Resume paused Spotify playback ('resume', 'continue', 'play again', 'unpause').",
            device_only,
        ), resume_music),
        Tool(ToolDefinition("next_track", "Skip to the next song ('next', 'skip').", no_args),
             next_track),
        Tool(ToolDefinition("previous_track", "Go to the previous song ('previous', 'go back').",
                            no_args), previous_track),
        Tool(ToolDefinition(
            "current_playback",
            "What is playing right now on Spotify: track, artist, album, device, paused or not.",
            no_args,
        ), current_playback),
        Tool(ToolDefinition(
            "transfer_playback",
            "Move whatever is currently playing to another device ('play this on my phone').",
            schema({"device": _DEVICE_ARG}, ["device"]),
        ), transfer_playback),
        Tool(ToolDefinition("list_spotify_devices", "List available Spotify devices.", no_args),
             list_devices),
        Tool(ToolDefinition(
            "set_volume",
            "Set the Spotify music volume. 'volume 5' means level 5 of 10 (50%); use percent "
            "only if the user says percent.",
            schema({
                "level": {"type": "number", "description": "0-10 scale, e.g. 5 for 'volume 5'."},
                "percent": {"type": "number", "description": "0-100, only when said as percent."},
            }),
        ), set_volume),
        Tool(ToolDefinition("volume_up", "Turn the music up one step ('volume up', 'louder').",
                            no_args), volume_up),
        Tool(ToolDefinition("volume_down", "Turn the music down one step ('volume down', "
                            "'quieter').", no_args), volume_down),
        Tool(ToolDefinition("mute_music", "Mute the music ('mute').", no_args), mute_music),
        Tool(ToolDefinition("unmute_music", "Unmute the music ('unmute').", no_args),
             unmute_music),
    ]  # fmt: skip


class SpotifyToolExecutor(ToolRegistry):
    """Backwards-compatible executor exposing only the Spotify tools."""

    def __init__(self, client: Any, default_device: str = "") -> None:
        super().__init__(spotify_tools(client, default_device=default_device))
