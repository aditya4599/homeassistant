"""Spotify OAuth and playback routes."""

from typing import Annotated
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from app.api.deps import get_spotify
from app.core.exceptions import InvalidRequestError
from app.core.security import require_api_key
from app.services.spotify.client import SpotifyClient

router = APIRouter(prefix="/spotify", tags=["spotify"])


@router.get("/login", summary="Start Spotify OAuth")
async def spotify_login(spotify: Annotated[SpotifyClient, Depends(get_spotify)]) -> RedirectResponse:
    return RedirectResponse(spotify.authorization_url(), status_code=307)


@router.get("/callback", summary="Spotify OAuth callback")
async def spotify_callback(
    spotify: Annotated[SpotifyClient, Depends(get_spotify)],
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> PlainTextResponse:
    if error:
        # Don't reflect the raw query value back (it is user-controlled).
        reason = "access denied" if error == "access_denied" else "an error was returned"
        return PlainTextResponse(f"Spotify authorization was not completed: {reason}.", status_code=400)
    if not code or not state:
        raise InvalidRequestError("Spotify callback is missing code or state.")
    await spotify.handle_callback(code, state)
    return PlainTextResponse(
        "Spotify connected successfully. You can close this tab and return to Adi."
    )


@router.get("/status", summary="Show Spotify connection status", dependencies=[Depends(require_api_key)])
async def spotify_status(spotify: Annotated[SpotifyClient, Depends(get_spotify)]) -> dict:
    return {"configured": spotify.configured, "connected": spotify.connected}


@router.get("/devices", summary="List Spotify playback devices", dependencies=[Depends(require_api_key)])
async def spotify_devices(spotify: Annotated[SpotifyClient, Depends(get_spotify)]) -> dict:
    keys = ("id", "name", "type", "is_active", "is_restricted", "volume_percent")
    return {"devices": [{k: d.get(k) for k in keys} for d in await spotify.devices()]}


@router.post(
    "/play",
    summary="Play a song / artist on an optional device",
    dependencies=[Depends(require_api_key)],
)
async def spotify_play(
    spotify: Annotated[SpotifyClient, Depends(get_spotify)],
    query: str = Query(default="", max_length=300, description="Song, e.g. 'Love Dose by Diljit'"),
    artist: str | None = Query(default=None, max_length=100),
    device: str | None = Query(default=None, max_length=100, description="e.g. 'T2 Pro', 'MacBook'"),
) -> dict:
    if not query.strip() and not artist:
        raise InvalidRequestError("Provide query and/or artist.")
    return await spotify.play_music(query, preferred_device=device, artist=artist)


@router.post("/resume", summary="Resume Spotify playback", dependencies=[Depends(require_api_key)])
async def spotify_resume(
    spotify: Annotated[SpotifyClient, Depends(get_spotify)],
    device: str | None = Query(default=None, max_length=100),
) -> dict:
    return {"ok": True, **await spotify.resume(device)}


@router.get("/current", summary="What's playing now", dependencies=[Depends(require_api_key)])
async def spotify_current(spotify: Annotated[SpotifyClient, Depends(get_spotify)]) -> dict:
    return await spotify.current_playback()


@router.post(
    "/transfer", summary="Move playback to another device", dependencies=[Depends(require_api_key)]
)
async def spotify_transfer(
    spotify: Annotated[SpotifyClient, Depends(get_spotify)],
    device: str = Query(min_length=1, max_length=100),
) -> dict:
    return {"ok": True, **await spotify.transfer(device)}


@router.post("/pause", summary="Pause Spotify playback", dependencies=[Depends(require_api_key)])
async def spotify_pause(spotify: Annotated[SpotifyClient, Depends(get_spotify)]) -> dict[str, bool]:
    await spotify.pause()
    return {"ok": True}


@router.post("/next", summary="Skip to the next Spotify track", dependencies=[Depends(require_api_key)])
async def spotify_next(spotify: Annotated[SpotifyClient, Depends(get_spotify)]) -> dict[str, bool]:
    await spotify.next_track()
    return {"ok": True}


@router.post("/previous", summary="Skip to the previous Spotify track", dependencies=[Depends(require_api_key)])
async def spotify_previous(spotify: Annotated[SpotifyClient, Depends(get_spotify)]) -> dict[str, bool]:
    await spotify.previous_track()
    return {"ok": True}
