import json

import httpx
import pytest

from app.core.exceptions import (
    SpotifyAuthExpiredError,
    SpotifyDeviceAmbiguousError,
    SpotifyDeviceNotFoundError,
    SpotifyNoDeviceError,
    SpotifyNotFoundError,
    SpotifyRateLimitError,
)
from app.services.llm.tools import ToolCall
from app.services.spotify.devices import resolve_device
from app.services.spotify.tools import SpotifyToolExecutor
from tests.spotify_fakes import FakeSpotifyAPI, make_client, track

PHONE = {"id": "1", "name": "vivo T2 Pro 5G", "type": "Smartphone", "is_active": False}
PHONE2 = {"id": "2", "name": "Pixel 8", "type": "Smartphone", "is_active": False}
MAC = {"id": "3", "name": "Vansh's MacBook Air", "type": "Computer", "is_active": True}
WEB = {"id": "4", "name": "Web Player (Chrome)", "type": "Computer", "is_active": False}


@pytest.mark.parametrize(
    ("requested", "expected"),
    [("T2 Pro", "1"), ("my t2 pro", "1"), ("vivo T2 Pro", "1"), ("vivo T2 Pro 5G", "1"),
     ("phone", "1"), ("mobile", "1"), ("MacBook", "3"), ("mac", "3"), ("laptop", "3"),
     ("my MacBook Air", "3"), (None, "3"), ("", "3")],
)  # fmt: skip
def test_resolve_device(requested, expected):
    assert resolve_device([PHONE, MAC], requested)["id"] == expected


def test_phone_is_ambiguous_with_two_phones():
    with pytest.raises(SpotifyDeviceAmbiguousError) as info:
        resolve_device([PHONE, PHONE2, MAC], "my phone")
    assert info.value.candidates == ["vivo T2 Pro 5G", "Pixel 8"]
    assert info.value.public_message.startswith("I found two phones")
    # ...but naming the phone precisely works
    assert resolve_device([PHONE, PHONE2, MAC], "T2 Pro")["id"] == "1"


def test_computer_alias_ambiguous_but_name_hint_resolves():
    with pytest.raises(SpotifyDeviceAmbiguousError):
        resolve_device([MAC, WEB], "computer")
    assert resolve_device([MAC, WEB], "MacBook")["id"] == "3"
    assert resolve_device([MAC, WEB], "chrome")["id"] == "4"


def test_device_not_found_and_no_devices():
    with pytest.raises(SpotifyDeviceNotFoundError, match="T2 Pro"):
        resolve_device([MAC], "T2 Pro")
    with pytest.raises(SpotifyNoDeviceError):
        resolve_device([], None)
    with pytest.raises(SpotifyDeviceAmbiguousError):  # nothing active, several idle
        resolve_device([PHONE | {"is_active": False}, PHONE2], None)


async def test_play_on_named_device_sends_a_single_targeted_play(tmp_path, monkeypatch):
    api = FakeSpotifyAPI()
    client = make_client(tmp_path, monkeypatch, api)
    result = await client.play_music("Love Dose", preferred_device="T2 Pro")
    assert result["track"]["name"] == "Love Dose" and result["device"] == "vivo T2 Pro 5G"
    puts = [(p, params, body) for m, p, params, body in api.calls if m == "PUT"]
    # One command only: a separate transfer first made the speaker receive two Load frames.
    assert puts == [("/v1/me/player/play", {"device_id": "d-phone"},
                     {"uris": ["spotify:track:LoveDose"]})]  # fmt: skip
    assert client.last_device["id"] == "d-phone"


async def test_by_artist_split_falls_back_to_title(tmp_path, monkeypatch):
    api = FakeSpotifyAPI()
    client = make_client(tmp_path, monkeypatch, api)
    # "Love Dose by Diljit" has no exact hit -> falls back to plain title search.
    result = await client.play_music("Love Dose by Diljit")
    assert result["track"]["artists"] == "Yo Yo Honey Singh"
    queries = [params["q"] for m, p, params, _ in api.calls if p == "/v1/search"]
    assert queries[0] == 'track:"Love Dose" artist:"Diljit"'


async def test_artist_mode_plays_multiple_tracks(tmp_path, monkeypatch):
    api = FakeSpotifyAPI(tracks=[track("Tum Hi Ho", "Arijit Singh"), track("Kesariya", "Arijit Singh")])
    client = make_client(tmp_path, monkeypatch, api)
    result = await client.play_music("something", artist="Arijit Singh")
    assert result["mode"] == "artist" and result["tracks"] == 2


async def test_song_not_found(tmp_path, monkeypatch):
    client = make_client(tmp_path, monkeypatch, FakeSpotifyAPI(tracks=[]))
    with pytest.raises(SpotifyNotFoundError):
        await client.play_music("zzzz nothing")


async def test_rate_limit_and_no_active_device(tmp_path, monkeypatch):
    api = FakeSpotifyAPI()
    client = make_client(tmp_path, monkeypatch, api)
    api.overrides[("POST", "/v1/me/player/next")] = httpx.Response(
        429, headers={"Retry-After": "5"}, json={"error": {"status": 429, "message": "x"}})
    with pytest.raises(SpotifyRateLimitError):
        await client.next_track()
    api.overrides[("PUT", "/v1/me/player/pause")] = httpx.Response(
        404, json={"error": {"status": 404, "message": "none", "reason": "NO_ACTIVE_DEVICE"}})
    with pytest.raises(SpotifyNoDeviceError):
        await client.pause()


async def test_pause_when_already_paused_is_ok(tmp_path, monkeypatch):
    api = FakeSpotifyAPI()
    client = make_client(tmp_path, monkeypatch, api)
    api.overrides[("PUT", "/v1/me/player/pause")] = httpx.Response(
        403, json={"error": {"status": 403, "message": "Restriction violated"}})
    await client.pause()


async def test_expired_refresh_token_asks_to_reconnect(tmp_path, monkeypatch):
    api = FakeSpotifyAPI()
    api.token_response = httpx.Response(400, json={"error": "invalid_grant",
                                                   "error_description": "Refresh token revoked"})
    client = make_client(tmp_path, monkeypatch, api, expired=True)
    with pytest.raises(SpotifyAuthExpiredError) as info:
        await client.pause()
    assert info.value.public_message == "Spotify needs to be reconnected."
    assert not client.connected  # stale token removed so /status shows reconnect needed


async def test_current_playback_and_tools(tmp_path, monkeypatch):
    api = FakeSpotifyAPI()
    api.player = {"is_playing": True, "device": {"name": "vivo T2 Pro 5G"},
                  "item": track("Love Dose", "Yo Yo Honey Singh") | {"type": "track"}}
    executor = SpotifyToolExecutor(make_client(tmp_path, monkeypatch, api))
    now = json.loads(await executor.execute(ToolCall("current_playback")))
    assert now["say"] == "Love Dose by Yo Yo Honey Singh."
    assert now["device"] == "vivo T2 Pro 5G"
    played = json.loads(await executor.execute(ToolCall("play_music", {"query": "Believer", "device": "MacBook"})))
    assert played["say"] == "Playing Believer by Imagine Dragons on your MacBook."
    assert played["device"] == "Vansh's MacBook Air"
    again = json.loads(await executor.execute(ToolCall("play_last_searched")))
    assert again["ok"] and again["track"]["name"] == "Believer"


async def test_tool_reports_ambiguity_and_not_connected(tmp_path, monkeypatch):
    api = FakeSpotifyAPI(devices=[PHONE | {"id": "a"}, PHONE2])
    executor = SpotifyToolExecutor(make_client(tmp_path, monkeypatch, api))
    result = json.loads(await executor.execute(ToolCall("play_music", {"query": "Love Dose", "device": "phone"})))
    assert result["ok"] is False and result["error_code"] == "spotify_device_ambiguous"
    assert result["candidates"] == ["vivo T2 Pro 5G", "Pixel 8"]
    assert "Which one" in result["say"]
