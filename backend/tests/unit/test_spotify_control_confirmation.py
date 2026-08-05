import logging

import httpx
import pytest

from app.core.exceptions import SpotifyError, SpotifyNotFoundError
from app.services.spotify import client as spotify_client
from app.services.spotify.client import friendly_spotify_error
from tests.spotify_fakes import FakeSpotifyAPI, make_client, track

PLAYING_ON_PHONE = {"is_playing": True, "device": {"id": "d-phone", "name": "vivo T2 Pro 5G"},
                    "item": {"uri": "spotify:track:x", "name": "Song"}}  # fmt: skip


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    monkeypatch.setattr(spotify_client, "PLAYBACK_CONFIRM_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(spotify_client, "PLAYBACK_CONFIRM_ATTEMPTS", 2)
    monkeypatch.setattr(spotify_client, "PLAY_RETRY_DELAY_SECONDS", 0.001)


def _pause_calls(api):
    return [(params, ) for m, p, params, _ in api.calls if m == "PUT" and p == "/v1/me/player/pause"]


async def test_pause_returns_once_spotify_reports_paused(tmp_path, monkeypatch):
    api = FakeSpotifyAPI()  # api.player is None -> Spotify reports nothing playing
    client = make_client(tmp_path, monkeypatch, api)
    await client.pause()
    assert len(_pause_calls(api)) == 1


async def test_403_while_still_playing_is_not_reported_as_paused(tmp_path, monkeypatch, caplog):
    api = FakeSpotifyAPI()
    api.player = PLAYING_ON_PHONE  # Spotify keeps reporting "playing"
    api.overrides[("PUT", "/v1/me/player/pause")] = httpx.Response(
        403, json={"error": {"status": 403, "message": "Player command failed: Restriction violated"}}
    )
    client = make_client(tmp_path, monkeypatch, api)
    with caplog.at_level(logging.INFO, logger="app.spotify"):
        with pytest.raises(SpotifyError) as info:
            await client.pause()
    assert "still shows the music playing" in info.value.public_message
    # one plain pause, then one aimed at the device Spotify says is playing
    assert _pause_calls(api) == [({},), ({"device_id": "d-phone"},)]
    warn = [r for r in caplog.records if r.getMessage() == "spotify_pause_not_confirmed"]
    assert warn and warn[0].active_device == "vivo T2 Pro 5G"


async def test_pause_403_when_already_paused_is_fine(tmp_path, monkeypatch):
    api = FakeSpotifyAPI()
    api.overrides[("PUT", "/v1/me/player/pause")] = httpx.Response(403, json={"error": {"status": 403, "message": "x"}})
    await make_client(tmp_path, monkeypatch, api).pause()


async def test_resume_with_nothing_to_resume_says_so(tmp_path, monkeypatch, caplog):
    api = FakeSpotifyAPI()  # nothing ever starts playing
    client = make_client(tmp_path, monkeypatch, api)
    with caplog.at_level(logging.INFO, logger="app.spotify"):
        with pytest.raises(SpotifyNotFoundError) as info:
            await client.resume("T2 Pro")
    assert info.value.public_message == "I don't have anything to resume. Tell me which song to play."
    assert [r for r in caplog.records if r.getMessage() == "spotify_resume_not_confirmed"]


async def test_resume_succeeds_when_spotify_reports_playing(tmp_path, monkeypatch):
    api = FakeSpotifyAPI()
    api.player = PLAYING_ON_PHONE
    result = await make_client(tmp_path, monkeypatch, api).resume("T2 Pro")
    assert result == {"device": "vivo T2 Pro 5G"}


class RejectFirstPlay(FakeSpotifyAPI):
    """Spotify says 'device not found' to the first play, then accepts it (speaker not yet active)."""

    def __init__(self):
        super().__init__(tracks=[track("Love Dose", "Yo Yo Honey Singh")])
        self.play_attempts = 0

    def handler(self, request):
        if request.method == "PUT" and request.url.path == "/v1/me/player/play":
            self.play_attempts += 1
            self.calls.append((request.method, request.url.path, dict(request.url.params), None))
            if self.play_attempts == 1:
                return httpx.Response(404, json={"error": {"status": 404, "message": "Device not found"}})
            return httpx.Response(204)
        return super().handler(request)


async def test_play_retries_after_a_transfer_only_when_spotify_rejects_it(tmp_path, monkeypatch, caplog):
    api = RejectFirstPlay()
    client = make_client(tmp_path, monkeypatch, api)
    with caplog.at_level(logging.INFO, logger="app.spotify"):
        result = await client.play_music("Love Dose", preferred_device="T2 Pro")
    assert result["device"] == "vivo T2 Pro 5G"
    puts = [p for m, p, *_ in api.calls if m == "PUT"]
    assert puts == ["/v1/me/player/play", "/v1/me/player", "/v1/me/player/play"]
    assert [r for r in caplog.records if r.getMessage() == "spotify_play_retry_after_transfer"]


@pytest.mark.parametrize(
    ("status", "message", "expected"),
    [(404, "Device not found", "can't reach that speaker"), (404, "No such thing", "couldn't find anything"),
     (403, "Restriction violated", "won't allow"), (400, "bad", "didn't accept"),
     (503, "oops", "having trouble"), (418, "teapot", None)],
)  # fmt: skip
def test_friendly_error_wording(status, message, expected):
    text = friendly_spotify_error(status, message)
    assert (text is None) if expected is None else expected in text


async def test_failed_call_spoken_text_is_specific_and_logged(tmp_path, monkeypatch, caplog):
    api = FakeSpotifyAPI()
    api.overrides[("POST", "/v1/me/player/next")] = httpx.Response(
        404, json={"error": {"status": 404, "message": "Device not found"}}
    )
    client = make_client(tmp_path, monkeypatch, api)
    await client.devices()  # lets the log name the target device
    with caplog.at_level(logging.INFO, logger="app.spotify"):
        with pytest.raises(SpotifyError) as info:
            await client.next_track(device_id="d-phone")
    assert "can't reach that speaker" in info.value.public_message
    failed = [r for r in caplog.records if r.getMessage() == "spotify_api_call" and r.status == 404][0]
    assert (failed.api_path, failed.device_id, failed.target_device) == ("/me/player/next", "d-phone", "vivo T2 Pro 5G")
    assert "Device not found" in failed.body
