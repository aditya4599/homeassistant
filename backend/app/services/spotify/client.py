"""Small async Spotify Web API client with OAuth authorization-code flow.

The client is intentionally limited to the endpoints Adi needs. It stores only the
refresh token and token metadata in a local JSON file for this single-user prototype;
move that secret to encrypted/Supabase storage before multi-user deployment.
"""

from __future__ import annotations

import asyncio
import logging
import re
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import Settings
from app.core.exceptions import (
    SpotifyAuthExpiredError,
    SpotifyError,
    SpotifyNoDeviceError,
    SpotifyNotConfiguredError,
    SpotifyNotConnectedError,
    SpotifyNotFoundError,
    SpotifyNothingPlayingError,
    SpotifyRateLimitError,
)
from app.services.spotify.devices import describe, normalize, resolve_device

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"
DEFAULT_SCOPE = "user-read-playback-state user-modify-playback-state"
_STATE_TTL_SECONDS = 600
_TOKEN_SAFETY_SECONDS = 60
# Development-mode apps are capped at 10 search results per request (Feb 2026).
SEARCH_LIMIT = 10
# Words that mean "just play something" rather than a specific song.
GENERIC_QUERIES = {"", "music", "some music", "something", "a song", "songs", "anything", "spotify"}


logger = logging.getLogger("app.spotify")

# --- Playback diagnostics -------------------------------------------------------------
# After a play command, GET /me/player is polled to see whether the target device really
# started the requested track. Log-only: it never changes what the assistant says.
PLAY_VERIFY_ATTEMPTS = 6
PLAY_VERIFY_INTERVAL_SECONDS = 1.0

# Control commands (pause/resume) are only reported as done once Spotify's own player state
# agrees. Spotify answers 204 as soon as it *accepts* a command, and 403 can mean "already
# paused" or "refused"; neither says what the speaker actually did.
PLAYBACK_CONFIRM_ATTEMPTS = 6
PLAYBACK_CONFIRM_INTERVAL_SECONDS = 0.5
# Pause before retrying a play command that Spotify rejected because the speaker was not
# yet its active device (after a transfer).
PLAY_RETRY_DELAY_SECONDS = 0.8


def friendly_spotify_error(status: int, message: str) -> str | None:
    """Spoken wording for a failed Spotify call, instead of one generic sentence."""
    text = (message or "").lower()
    if status == 404 and "device" in text:
        return "Spotify can't reach that speaker right now. Open Spotify and pick it once more."
    if status == 404:
        return "Spotify couldn't find anything to play there."
    if status == 403:
        return "Spotify won't allow that right now."
    if status == 400:
        return "Spotify didn't accept that command."
    if status >= 500:
        return "Spotify is having trouble right now."
    return None

_BASE62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def spotify_uri_to_gid(uri: str | None) -> str | None:
    """``spotify:track:<22 base62 chars>`` -> 32-char hex GID.

    The speaker firmware logs the GID of every track in a Load frame, so this lets a
    track requested by the backend be matched against what ADI actually received.
    """
    parts = (uri or "").split(":")
    if len(parts) != 3 or len(parts[2]) != 22:
        return None
    value = 0
    for char in parts[2]:
        digit = _BASE62.find(char)
        if digit < 0:
            return None
        value = value * 62 + digit
    return f"{value:032x}" if value < (1 << 128) else None


@dataclass(frozen=True, slots=True)
class SpotifyToken:
    access_token: str
    refresh_token: str
    expires_at: float
    scope: str = ""


class SpotifyTokenStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path).expanduser()

    def load(self) -> SpotifyToken | None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        try:
            return SpotifyToken(
                access_token=str(raw["access_token"]),
                refresh_token=str(raw["refresh_token"]),
                expires_at=float(raw["expires_at"]),
                scope=str(raw.get("scope", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def save(self, token: SpotifyToken) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {
            "access_token": token.access_token,
            "refresh_token": token.refresh_token,
            "expires_at": token.expires_at,
            "scope": token.scope,
        }
        temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        temp.replace(self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass


def _delete_token(store: "SpotifyTokenStore") -> None:
    try:
        store.path.unlink()
    except OSError:
        pass


class SpotifyOAuthStateStore:
    """Single-user in-memory CSRF state store for the local OAuth flow."""

    def __init__(self) -> None:
        self._states: dict[str, float] = {}

    def create(self) -> str:
        self._purge()
        state = secrets.token_urlsafe(32)
        self._states[state] = time.time() + _STATE_TTL_SECONDS
        return state

    def consume(self, state: str) -> bool:
        self._purge()
        expiry = self._states.pop(state, None)
        return bool(expiry and expiry >= time.time())

    def _purge(self) -> None:
        now = time.time()
        self._states = {k: v for k, v in self._states.items() if v >= now}


class SpotifyClient:
    def __init__(self, settings: Settings) -> None:
        self._client_id = settings.spotify_client_id.get_secret_value().strip()
        self._client_secret = settings.spotify_client_secret.get_secret_value().strip()
        self.redirect_uri = settings.spotify_redirect_uri.strip()
        self.scopes = settings.spotify_scopes.strip() or DEFAULT_SCOPE
        self._timeout = settings.spotify_timeout_seconds
        self._store = SpotifyTokenStore(settings.spotify_token_file)
        self._state = SpotifyOAuthStateStore()
        self._refresh_lock = asyncio.Lock()
        # Volume (0-100) before "mute", restored by "unmute".
        self.volume_before_mute: int | None = None
        # Conversational memory for follow-ups ("pause", "play the song I searched").
        self.last_track: dict[str, Any] | None = None
        self.last_device: dict[str, Any] | None = None
        # Verify (and log) whether the speaker started the track, when an ADI speaker is
        # configured as the default device.
        self._verify_playback = bool(settings.spotify_default_device.strip())
        self._background_tasks: set[asyncio.Task[None]] = set()
        # device id -> name, from the last device list (so API logs can name the target)
        self._device_names: dict[str, str] = {}

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret and self.redirect_uri)

    @property
    def connected(self) -> bool:
        return self._store.load() is not None

    def require_configured(self) -> None:
        if not self.configured:
            raise SpotifyNotConfiguredError(
                detail="Set SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET and SPOTIFY_REDIRECT_URI."
            )

    def authorization_url(self) -> str:
        self.require_configured()
        state = self._state.create()
        query = urlencode(
            {
                "client_id": self._client_id,
                "response_type": "code",
                "redirect_uri": self.redirect_uri,
                "scope": self.scopes,
                "state": state,
                "show_dialog": "false",
            }
        )
        return f"{AUTHORIZE_URL}?{query}"

    async def handle_callback(self, code: str, state: str) -> None:
        self.require_configured()
        if not self._state.consume(state):
            raise SpotifyError(detail="OAuth state was missing, expired, or invalid.")
        data = await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
            }
        )
        self._store.save(self._parse_token(data, fallback_refresh_token=""))

    async def _refresh(self, token: SpotifyToken) -> SpotifyToken:
        async with self._refresh_lock:
            latest = self._store.load()
            if latest and latest.expires_at > time.time() + _TOKEN_SAFETY_SECONDS:
                return latest
            try:
                data = await self._token_request(
                    {"grant_type": "refresh_token", "refresh_token": token.refresh_token}
                )
            except SpotifyError as exc:
                if "invalid_grant" in exc.log_detail:
                    # Revoked, or past Spotify's ~6-month refresh-token lifetime.
                    _delete_token(self._store)
                    raise SpotifyAuthExpiredError(detail=exc.log_detail) from exc
                raise
            refreshed = self._parse_token(data, fallback_refresh_token=token.refresh_token)
            self._store.save(refreshed)
            return refreshed

    async def access_token(self) -> str:
        self.require_configured()
        token = self._store.load()
        if token is None:
            raise SpotifyNotConnectedError(
                detail="No Spotify refresh token is stored. Complete /api/v1/spotify/login."
            )
        if token.expires_at <= time.time() + _TOKEN_SAFETY_SECONDS:
            token = await self._refresh(token)
        return token.access_token

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        auth = (self._client_id, self._client_secret)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    TOKEN_URL,
                    data=data,
                    auth=auth,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
        except httpx.HTTPError as exc:
            raise SpotifyError(detail=f"token endpoint network error: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            detail = self._response_detail(response)
            raise SpotifyError(detail=f"Spotify token endpoint returned {response.status_code}: {detail}")
        try:
            return response.json()
        except ValueError as exc:
            raise SpotifyError(detail="Spotify token endpoint returned invalid JSON.") from exc

    @staticmethod
    def _parse_token(data: dict[str, Any], fallback_refresh_token: str) -> SpotifyToken:
        try:
            access = str(data["access_token"])
            expires_in = int(data.get("expires_in", 3600))
            refresh = str(data.get("refresh_token") or fallback_refresh_token)
        except (KeyError, TypeError, ValueError) as exc:
            raise SpotifyError(detail="Spotify returned an incomplete token response.") from exc
        if not refresh:
            raise SpotifyError(detail="Spotify did not return a refresh token.")
        return SpotifyToken(
            access_token=access,
            refresh_token=refresh,
            expires_at=time.time() + max(60, expires_in),
            scope=str(data.get("scope", "")),
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        token = await self.access_token()
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {token}"
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.request(method, f"{API_BASE}{path}", headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            logger.warning(
                "spotify_api_call",
                extra={"http_method": method.upper(), "api_path": path,
                       "status": None, "error": type(exc).__name__},
            )  # fmt: skip
            raise SpotifyError(detail=f"Spotify API network error: {type(exc).__name__}") from exc
        self._log_api_call(method, path, response, started, kwargs)

        if response.status_code == 204:
            return None
        if response.status_code == 401:
            # Retry once after forcing a refresh; this handles a token revoked/expired
            # earlier than its advertised expiry without an unbounded retry loop.
            current = self._store.load()
            if current:
                try:
                    refreshed = await self._refresh(current)
                    headers["Authorization"] = f"Bearer {refreshed.access_token}"
                    async with httpx.AsyncClient(timeout=self._timeout) as client:
                        response = await client.request(method, f"{API_BASE}{path}", headers=headers, **kwargs)
                    self._log_api_call(method, path, response, started, kwargs)
                except SpotifyError:
                    raise
            if response.status_code == 401:
                raise SpotifyNotConnectedError(detail="Spotify rejected the stored authorization.")
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "unknown")
            raise SpotifyRateLimitError(
                detail=f"Spotify rate-limited the request (Retry-After: {retry_after}): "
                f"{self._response_detail(response)}"
            )
        if response.status_code >= 400:
            detail = self._response_detail(response)
            reason = self._response_reason(response)
            log = f"Spotify API returned {response.status_code} ({reason or '-'}): {detail}"
            if reason == "NO_ACTIVE_DEVICE":
                raise SpotifyNoDeviceError(detail=log)
            if reason == "PREMIUM_REQUIRED":
                raise SpotifyError("Spotify Premium is required for playback control.", detail=log)
            error = SpotifyError(friendly_spotify_error(response.status_code, detail), detail=log)
            error.spotify_status = response.status_code  # type: ignore[attr-defined]
            raise error
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise SpotifyError(detail="Spotify returned invalid JSON.") from exc

    def _log_api_call(
        self, method: str, path: str, response: httpx.Response, started: float, kwargs: dict[str, Any]
    ) -> None:
        """Log every Spotify control call (PUT/POST...) and every failed call, with its status."""
        if method.upper() == "GET" and response.status_code < 400:
            return
        params = kwargs.get("params") or {}
        body = kwargs.get("json")
        device_ids = body.get("device_ids") if isinstance(body, dict) else None
        extra: dict[str, Any] = {
            "http_method": method.upper(),
            "api_path": path,
            "status": response.status_code,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "device_id": params.get("device_id") or (device_ids[0] if device_ids else None),
        }
        extra["target_device"] = self._device_names.get(extra["device_id"] or "")
        if response.status_code >= 400:
            extra["reason"] = self._response_reason(response) or None
            extra["body"] = self._response_detail(response)[:200]
        logger.info("spotify_api_call", extra=extra)

    @staticmethod
    def _response_reason(response: httpx.Response) -> str:
        try:
            error = (response.json() or {}).get("error")
        except (ValueError, AttributeError):
            return ""
        return str(error.get("reason") or "") if isinstance(error, dict) else ""

    @staticmethod
    def _response_detail(response: httpx.Response) -> str:
        try:
            data = response.json()
            if isinstance(data, dict):
                error = data.get("error")
                if isinstance(error, dict):
                    return str(error.get("message") or error.get("reason") or error)[:300]
                return str(error or data)[:300]
        except ValueError:
            pass
        return response.text[:300]

    # ------------------------------------------------------------------ search
    @staticmethod
    def split_query(query: str, artist: str | None = None) -> tuple[str, str | None]:
        """ "Love Dose by Diljit" -> ("Love Dose", "Diljit"); strips "the song ..." fillers."""
        q = " ".join((query or "").split())
        q = re.sub(r"^(?:the\s+)?(?:song|track)\s+", "", q, flags=re.IGNORECASE)
        if not artist:
            match = re.match(r"^(.+?)\s+by\s+(.+)$", q, flags=re.IGNORECASE)
            if match:
                q, artist = match.group(1).strip(), match.group(2).strip()
        return q.strip().strip("\"'"), (artist.strip() if artist else None)

    async def _search_tracks(self, q: str, limit: int = 5) -> list[dict[str, Any]]:
        data = await self._request(
            "GET",
            "/search",
            params={"q": q, "type": "track", "limit": min(limit, SEARCH_LIMIT), "market": "from_token"},
        )
        return [t for t in ((data or {}).get("tracks") or {}).get("items") or [] if t and t.get("uri")]

    @staticmethod
    def _summarize(track: dict[str, Any]) -> dict[str, Any]:
        artists = ", ".join(a.get("name", "") for a in track.get("artists", []) if a.get("name"))
        return {
            "uri": track.get("uri"),
            "name": track.get("name"),
            "artists": artists,
            "album": (track.get("album") or {}).get("name"),
        }

    async def search_track(self, query: str, artist: str | None = None) -> dict[str, Any]:
        title, artist = self.split_query(query, artist)
        if not title and not artist:
            raise SpotifyNotFoundError(detail="Music search query is empty.")

        attempts: list[str] = []
        if title and artist:
            attempts += [f'track:"{title}" artist:"{artist}"', f"{title} {artist}"]
        attempts.append(title or f'artist:"{artist}"')

        for q in attempts:
            tracks = await self._search_tracks(q)
            if tracks:
                best = self._best_match(tracks, title, artist)
                self.last_track = self._summarize(best)
                return self.last_track
        raise SpotifyNotFoundError(detail=f"No Spotify track matched {query!r} (artist={artist!r}).")

    @staticmethod
    def _best_match(tracks: list[dict[str, Any]], title: str, artist: str | None) -> dict[str, Any]:
        want = normalize(title)
        want_artist = normalize(artist or "")

        def score(t: dict[str, Any]) -> int:
            name = normalize(t.get("name", ""))
            artists = normalize(" ".join(a.get("name", "") for a in t.get("artists", [])))
            s = 0
            if want and name == want:
                s += 4
            elif want and name.startswith(want):
                s += 2
            if want_artist and want_artist in artists:
                s += 3
            return s

        # max() keeps Spotify's own ranking as the tiebreaker (first wins).
        return max(tracks, key=score)

    async def artist_tracks(self, artist: str) -> list[dict[str, Any]]:
        # Artist top-tracks was removed for dev-mode apps (Feb 2026): use filtered search.
        tracks = await self._search_tracks(f'artist:"{artist}"', limit=SEARCH_LIMIT)
        want = normalize(artist)
        own = [
            t for t in tracks
            if any(want in normalize(a.get("name", "")) for a in t.get("artists", []))
        ]  # fmt: skip
        if not (own or tracks):
            raise SpotifyNotFoundError(f"I couldn't find anything by {artist}.", detail=artist)
        return own or tracks

    # ------------------------------------------------------------------ devices
    async def devices(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/me/player/devices")
        devices = list((data or {}).get("devices") or [])
        self._device_names = {str(d["id"]): str(d.get("name")) for d in devices if d.get("id")}
        return devices

    async def choose_device(self, requested: str | None) -> dict[str, Any]:
        return resolve_device(await self.devices(), requested)

    async def transfer_playback(self, device_id: str, *, play: bool = False) -> None:
        await self._request("PUT", "/me/player", json={"device_ids": [device_id], "play": play})

    async def _prepare_device(self, requested: str | None) -> dict[str, Any]:
        devices = await self.devices()
        device = resolve_device(devices, requested)
        logger.info(
            "spotify_device_selected",
            extra={
                "requested_device": requested,
                "target_device": device.get("name"),
                "target_device_id": device.get("id"),
                "target_is_active": bool(device.get("is_active")),
                "listed_devices": [
                    f"{d.get('name')}{' (active)' if d.get('is_active') else ''}" for d in devices
                ],
            },
        )
        # No separate transfer here any more: "PUT /me/player/play?device_id=..." already makes
        # that device the target. A transfer first made Spotify send the speaker a Load frame
        # (cspot starts playing whatever it carries), and the play command a second one.
        # play_uris() falls back to transfer-then-retry only if Spotify rejects the play.
        self.last_device = device
        return device

    # ------------------------------------------------------------------ playback
    async def play_track(self, uri: str, device_id: str | None = None) -> None:
        await self.play_uris([uri], device_id)

    async def play_uris(
        self,
        uris: list[str],
        device_id: str | None = None,
        device_name: str | None = None,
        track_name: str | None = None,
    ) -> None:
        params = {"device_id": device_id} if device_id else None
        first = uris[0] if uris else None
        logger.info(
            "spotify_play_request",
            extra={
                "track_uri": first,
                "track_gid": spotify_uri_to_gid(first),
                "track_name": track_name,
                "uri_count": len(uris),
                "target_device": device_name,
                "target_device_id": device_id,
            },
        )
        try:
            await self._request("PUT", "/me/player/play", params=params, json={"uris": uris})
        except SpotifyError as exc:
            not_active = isinstance(exc, SpotifyNoDeviceError) or getattr(exc, "spotify_status", None) == 404
            if not (device_id and not_active):
                raise
            # Spotify does not yet treat the speaker as an available player: transfer, then retry once.
            logger.info(
                "spotify_play_retry_after_transfer",
                extra={"target_device": device_name, "target_device_id": device_id, "detail": exc.log_detail[:200]},
            )
            await self.transfer_playback(device_id, play=False)
            await asyncio.sleep(PLAY_RETRY_DELAY_SECONDS)
            await self._request("PUT", "/me/player/play", params=params, json={"uris": uris})
        logger.info("spotify_play_accepted", extra={"track_uri": first, "target_device": device_name})
        if self._verify_playback and first:
            task = asyncio.create_task(self._verify_play(first, device_id, device_name, track_name))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def _verify_play(
        self, uri: str, device_id: str | None, device_name: str | None, track_name: str | None
    ) -> None:
        """Log whether the target device actually started the requested track (diagnostic only).

        Spotify answers the play command with 204 as soon as it *accepts* it; that says
        nothing about whether the speaker started the track.
        """
        last: dict[str, Any] = {}
        try:
            for attempt in range(1, PLAY_VERIFY_ATTEMPTS + 1):
                await asyncio.sleep(PLAY_VERIFY_INTERVAL_SECONDS)
                state = await self.playback_state() or {}
                item = state.get("item") or {}
                device = state.get("device") or {}
                last = {
                    "attempt": attempt,
                    "active_device": device.get("name"),
                    "active_device_id": device.get("id"),
                    "is_playing": bool(state.get("is_playing")),
                    "playing_uri": item.get("uri"),
                    "playing_name": item.get("name"),
                }
                on_target = bool(device_id) and device.get("id") == device_id
                same_track = item.get("uri") == uri or (
                    bool(track_name) and item.get("name") == track_name
                )
                if on_target and same_track and last["is_playing"]:
                    logger.info(
                        "spotify_play_verified",
                        extra={**last, "expected_uri": uri, "target_device": device_name},
                    )
                    return
            logger.warning(
                "spotify_play_not_confirmed",
                extra={
                    **last,
                    "expected_uri": uri,
                    "expected_name": track_name,
                    "target_device": device_name,
                    "target_device_id": device_id,
                    "waited_seconds": round(PLAY_VERIFY_ATTEMPTS * PLAY_VERIFY_INTERVAL_SECONDS, 1),
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
            logger.warning(
                "spotify_play_verify_failed",
                extra={"error": type(exc).__name__, "detail": getattr(exc, "log_detail", str(exc))[:200]},
            )

    async def play_music(
        self,
        query: str | None = None,
        device_id: str | None = None,
        preferred_device: str | None = None,
        artist: str | None = None,
    ) -> dict[str, Any]:
        """Play a song, an artist, or (for a vague request) resume, on an optional device.

        Returns ``{"track": ..., "tracks": n, "device": name, "mode": ...}``.
        """
        title, artist = self.split_query(query or "", artist)
        generic = normalize(title) in GENERIC_QUERIES

        if generic and not artist:
            return await self.resume(preferred_device) | {"mode": "resume"}

        if generic and artist:
            tracks = [self._summarize(t) for t in await self.artist_tracks(artist)]
            track, uris, mode = tracks[0], [t["uri"] for t in tracks], "artist"
        else:
            track = await self.search_track(title, artist)
            uris, mode = [track["uri"]], "track"

        device = (
            {"id": device_id, "name": None}
            if device_id
            else await self._prepare_device(preferred_device)
        )
        await self.play_uris(
            uris, device_id=device["id"], device_name=device.get("name"), track_name=track.get("name")
        )
        self.last_track = track
        return {"track": track, "tracks": len(uris), "device": device.get("name"), "mode": mode}

    async def play_last_searched(self, preferred_device: str | None = None) -> dict[str, Any]:
        if not self.last_track:
            raise SpotifyNotFoundError(
                "I don't have a recent song to play yet.", detail="no last_track in memory"
            )
        device = await self._prepare_device(preferred_device)
        await self.play_uris(
            [self.last_track["uri"]],
            device_id=device["id"],
            device_name=device.get("name"),
            track_name=self.last_track.get("name"),
        )
        return {"track": self.last_track, "device": device.get("name"), "mode": "track"}

    async def transfer(self, requested: str) -> dict[str, Any]:
        """Move current playback to another device ("play this on my phone")."""
        device = await self.choose_device(requested)
        await self.transfer_playback(device["id"], play=True)
        self.last_device = device
        return {"device": describe(device)}

    async def _wait_for_playing(self, expected: bool) -> tuple[bool, dict[str, Any] | None]:
        """Poll Spotify's player state until ``is_playing`` equals ``expected`` (or give up)."""
        state: dict[str, Any] | None = None
        for attempt in range(1, PLAYBACK_CONFIRM_ATTEMPTS + 1):
            await asyncio.sleep(PLAYBACK_CONFIRM_INTERVAL_SECONDS)
            try:
                state = await self.playback_state()
            except SpotifyError as exc:
                # Can't check: don't turn a command that Spotify accepted into a failure.
                logger.warning("spotify_state_check_failed", extra={"detail": exc.log_detail[:200]})
                return True, None
            if bool((state or {}).get("is_playing")) == expected:
                logger.info(
                    "spotify_state_confirmed",
                    extra={
                        "expected_playing": expected,
                        "attempt": attempt,
                        "active_device": ((state or {}).get("device") or {}).get("name"),
                    },
                )
                return True, state
        return False, state

    def _observed(self, state: dict[str, Any] | None) -> dict[str, Any]:
        device = (state or {}).get("device") or {}
        item = (state or {}).get("item") or {}
        return {
            "is_playing": bool((state or {}).get("is_playing")),
            "active_device": device.get("name"),
            "active_device_id": device.get("id"),
            "playing_name": item.get("name"),
        }

    async def resume(self, preferred_device: str | None = None) -> dict[str, Any]:
        if preferred_device:
            device = await self.choose_device(preferred_device)
            await self.transfer_playback(device["id"], play=True)
            self.last_device = device
            await self._require_playing(describe(device))
            return {"device": describe(device)}
        try:
            await self._request("PUT", "/me/player/play")
        except SpotifyNoDeviceError:
            # Nothing active: fall back to the device used last in this session.
            if not self.last_device:
                raise
            await self.transfer_playback(self.last_device["id"], play=True)
        except SpotifyError as exc:
            if getattr(exc, "spotify_status", None) != 403:  # 403 = already playing, checked below
                raise
        await self._require_playing(describe(self.last_device) if self.last_device else None)
        return {"device": describe(self.last_device) if self.last_device else None}

    async def _require_playing(self, device_label: str | None) -> None:
        confirmed, state = await self._wait_for_playing(True)
        if confirmed:
            return
        logger.warning(
            "spotify_resume_not_confirmed",
            extra={"target_device": device_label, **self._observed(state)},
        )
        raise SpotifyNotFoundError(
            "I don't have anything to resume. Tell me which song to play.",
            detail=f"Spotify still reports nothing playing after resume: {self._observed(state)}",
        )

    async def volume_state(self) -> tuple[int | None, dict[str, Any] | None]:
        """Current volume (0-100) of the active device, and that device."""
        state = await self.playback_state()
        device = (state or {}).get("device") or None
        if device is None:
            active = next((d for d in await self.devices() if d.get("is_active")), None)
            device = active
        volume = device.get("volume_percent") if device else None
        return (int(volume) if volume is not None else None), device

    async def set_volume(self, percent: int, device_id: str | None = None) -> None:
        """Set the Spotify volume, 0-100 (Spotify Connect devices map it to 0..65535)."""
        params: dict[str, Any] = {"volume_percent": max(0, min(100, int(round(percent))))}
        if device_id:
            params["device_id"] = device_id
        await self._request("PUT", "/me/player/volume", params=params)

    async def pause(self, device_id: str | None = None) -> None:
        params = {"device_id": device_id} if device_id else None
        try:
            await self._request("PUT", "/me/player/pause", params=params)
        except SpotifyError as exc:
            if getattr(exc, "spotify_status", None) != 403:
                raise
            # 403 is Spotify's answer when already paused, but also when it refuses; the
            # real state is checked below instead of assuming the best.
        confirmed, state = await self._wait_for_playing(False)
        if confirmed:
            return

        observed = self._observed(state)
        logger.warning("spotify_pause_not_confirmed", extra={"target_device_id": device_id, **observed})
        # Still playing: aim the command at the device Spotify says is playing, once.
        target = device_id or observed["active_device_id"]
        if target:
            try:
                await self._request("PUT", "/me/player/pause", params={"device_id": target})
            except SpotifyError as exc:
                if getattr(exc, "spotify_status", None) != 403:
                    raise
            confirmed, state = await self._wait_for_playing(False)
            if confirmed:
                return
            observed = self._observed(state)
        raise SpotifyError(
            "I asked Spotify to pause, but it still shows the music playing.",
            detail=f"pause not confirmed after retry: {observed}",
        )

    async def next_track(self, device_id: str | None = None) -> None:
        params = {"device_id": device_id} if device_id else None
        await self._request("POST", "/me/player/next", params=params)

    async def previous_track(self, device_id: str | None = None) -> None:
        params = {"device_id": device_id} if device_id else None
        await self._request("POST", "/me/player/previous", params=params)

    async def playback_state(self) -> dict[str, Any] | None:
        return await self._request("GET", "/me/player", params={"market": "from_token"})

    async def current_playback(self) -> dict[str, Any]:
        state = await self.playback_state()
        item = (state or {}).get("item")
        if not state or not item:
            raise SpotifyNothingPlayingError(detail="no current playback item")
        summary = self._summarize(item)
        if item.get("type") == "episode":
            summary["artists"] = (item.get("show") or {}).get("name") or ""
        device = state.get("device") or {}
        return {
            **summary,
            "is_playing": bool(state.get("is_playing")),
            "device": device.get("name"),
            "progress_ms": state.get("progress_ms"),
            "duration_ms": item.get("duration_ms"),
        }
