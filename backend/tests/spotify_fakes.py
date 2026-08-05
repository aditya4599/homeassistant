"""Fake Spotify Web API (httpx MockTransport) for client tests - no network."""

import json
import time

import httpx

from app.services.spotify.client import SpotifyClient, SpotifyToken, SpotifyTokenStore
from tests.helpers import make_settings

DEVICES = [
    {"id": "d-phone", "name": "vivo T2 Pro 5G", "type": "Smartphone", "is_active": False},
    {"id": "d-mac", "name": "Vansh's MacBook Air", "type": "Computer", "is_active": True},
]


def track(name, artist, uri=None):
    return {"name": name, "uri": uri or f"spotify:track:{name.replace(' ', '')}",
            "artists": [{"name": artist}], "album": {"name": f"{name} (Single)"}}  # fmt: skip


class FakeSpotifyAPI:
    def __init__(self, devices=None, tracks=None):
        self.devices = devices if devices is not None else [dict(d) for d in DEVICES]
        self.tracks = tracks if tracks is not None else [
            track("Love Dose", "Yo Yo Honey Singh"), track("Believer", "Imagine Dragons"),
        ]  # fmt: skip
        self.calls: list[tuple[str, str, dict, object]] = []
        self.overrides: dict[tuple[str, str], httpx.Response] = {}
        self.token_response: httpx.Response | None = None
        self.player: dict | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content and request.headers.get(
            "content-type", "").startswith("application/json") else None  # fmt: skip
        path, method = request.url.path, request.method
        self.calls.append((method, path, dict(request.url.params), body))
        if path == "/api/token":
            return self.token_response or httpx.Response(
                200, json={"access_token": "new", "expires_in": 3600})
        if (method, path) in self.overrides:
            return self.overrides[(method, path)]
        if path == "/v1/search":
            q = request.url.params["q"].lower()
            words = [w for w in q.replace('"', " ").replace(":", " ").split()
                     if w not in {"track", "artist"}]  # fmt: skip
            hits = [t for t in self.tracks
                    if all(w in (t["name"] + " " + t["artists"][0]["name"]).lower() for w in words)]
            return httpx.Response(200, json={"tracks": {"items": hits}})
        if path == "/v1/me/player/devices":
            return httpx.Response(200, json={"devices": self.devices})
        if path == "/v1/me/player" and method == "GET":
            return httpx.Response(200, json=self.player) if self.player else httpx.Response(204)
        return httpx.Response(204)

    def paths(self, method=None):
        return [(m, p) for m, p, *_ in self.calls if method is None or m == method]


def make_client(tmp_path, monkeypatch, api: FakeSpotifyAPI, *, expired=False) -> SpotifyClient:
    settings = make_settings(spotify_client_id="cid", spotify_client_secret="secret",
                             spotify_token_file=str(tmp_path / "tok.json"))  # fmt: skip
    SpotifyTokenStore(settings.spotify_token_file).save(
        SpotifyToken("access", "refresh", time.time() - 10 if expired else time.time() + 3600)
    )
    transport = httpx.MockTransport(api.handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(
        "app.services.spotify.client.httpx.AsyncClient",
        lambda *a, **kw: real(*a, **{**kw, "transport": transport}),
    )
    return SpotifyClient(settings)
