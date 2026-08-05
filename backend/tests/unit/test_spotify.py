import json
from pathlib import Path

import httpx
import pytest

from app.core.exceptions import SpotifyNotConnectedError
from app.services.spotify.client import SpotifyClient, SpotifyToken, SpotifyTokenStore
from app.services.spotify.tools import SpotifyToolExecutor
from app.services.llm.tools import ToolCall
from tests.helpers import make_settings


def test_token_store_round_trip(tmp_path: Path):
    path = tmp_path / "token.json"
    store = SpotifyTokenStore(str(path))
    token = SpotifyToken("access", "refresh", 1234.0, "scope")
    store.save(token)
    assert store.load() == token
    assert json.loads(path.read_text())["refresh_token"] == "refresh"


def test_authorization_url_contains_state_and_exact_redirect(tmp_path: Path):
    settings = make_settings(
        spotify_client_id="client",
        spotify_client_secret="secret",
        spotify_redirect_uri="http://127.0.0.1:8000/api/v1/spotify/callback",
        spotify_token_file=str(tmp_path / "token.json"),
    )
    client = SpotifyClient(settings)
    url = client.authorization_url()
    assert "client_id=client" in url
    assert "response_type=code" in url
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8000%2Fapi%2Fv1%2Fspotify%2Fcallback" in url
    assert "state=" in url
    assert "user-modify-playback-state" in url


async def test_access_token_requires_connection(tmp_path: Path):
    settings = make_settings(
        spotify_client_id="client", spotify_client_secret="secret", spotify_token_file=str(tmp_path / "x.json")
    )
    client = SpotifyClient(settings)
    with pytest.raises(SpotifyNotConnectedError):
        await client.access_token()


class FakeSpotifyClient:
    async def play_music(self, query, preferred_device=None, artist=None):
        track = {"name": "Love Dose", "artists": "Yo Yo Honey Singh", "album": "X", "uri": "spotify:track:x"}
        return {"track": track, "tracks": 1, "device": preferred_device, "mode": "track"}

    async def pause(self): pass
    async def next_track(self): pass
    async def previous_track(self): pass


async def test_tool_executor_play_music():
    executor = SpotifyToolExecutor(FakeSpotifyClient())
    result = json.loads(await executor.execute(ToolCall(name="play_music", arguments={"query": "Love Dose"})))
    assert result["ok"] is True
    assert result["track"]["name"] == "Love Dose"
    assert result["say"] == "Playing Love Dose by Yo Yo Honey Singh."
    assert {"play_music", "pause_music", "resume_music", "next_track", "previous_track",
            "current_playback", "transfer_playback"} <= {d.name for d in executor.definitions}
