import asyncio
import logging

import httpx
import pytest

from app.core.exceptions import SpotifyNoDeviceError
from app.services.spotify import client as spotify_client
from app.services.spotify.client import spotify_uri_to_gid
from tests.spotify_fakes import FakeSpotifyAPI, make_client, track

REAL_URI = "spotify:track:5sWHDYs0csV6RS48xBl0tH"


def _records(caplog, message):
    return [r for r in caplog.records if r.name == "app.spotify" and r.getMessage() == message]


def test_uri_to_gid_matches_known_vector():
    # Test vector from librespot's SpotifyId conversion tests.
    assert spotify_uri_to_gid(REAL_URI) == "b39fe8081e1f4c54be38e8d6f9f12bb9"


@pytest.mark.parametrize("bad", [None, "", "x:y", "spotify:track:short", "spotify:track:" + "!" * 22])
def test_uri_to_gid_rejects_bad_input(bad):
    assert spotify_uri_to_gid(bad) is None


async def test_play_logs_device_choice_request_and_api_status(tmp_path, monkeypatch, caplog):
    api = FakeSpotifyAPI(tracks=[track("Zaroor", "Aparshakti Khurana", uri=REAL_URI)])
    client = make_client(tmp_path, monkeypatch, api)
    with caplog.at_level(logging.INFO, logger="app.spotify"):
        await client.play_music("Zaroor", preferred_device="T2 Pro")

    (selected,) = _records(caplog, "spotify_device_selected")
    assert selected.target_device == "vivo T2 Pro 5G" and selected.target_is_active is False
    assert "Vansh's MacBook Air (active)" in selected.listed_devices
    assert not _records(caplog, "spotify_transfer_before_play")

    (request,) = _records(caplog, "spotify_play_request")
    assert request.track_uri == REAL_URI
    assert request.track_gid == "b39fe8081e1f4c54be38e8d6f9f12bb9"
    assert (request.target_device, request.target_device_id) == ("vivo T2 Pro 5G", "d-phone")

    calls = [(r.http_method, r.api_path, r.status, r.device_id) for r in _records(caplog, "spotify_api_call")]
    assert calls == [("PUT", "/me/player/play", 204, "d-phone")]
    assert _records(caplog, "spotify_api_call")[0].target_device == "vivo T2 Pro 5G"
    assert _records(caplog, "spotify_play_accepted")
    assert not client._background_tasks  # verification is off without an ADI default device


async def test_failed_play_logs_status_and_reason(tmp_path, monkeypatch, caplog):
    api = FakeSpotifyAPI()
    client = make_client(tmp_path, monkeypatch, api)
    api.overrides[("PUT", "/v1/me/player/play")] = httpx.Response(
        404, json={"error": {"status": 404, "message": "Device not found", "reason": "NO_ACTIVE_DEVICE"}}
    )
    with caplog.at_level(logging.INFO, logger="app.spotify"):
        with pytest.raises(SpotifyNoDeviceError):
            await client.play_music("Love Dose", preferred_device="MacBook")
    failed = [r for r in _records(caplog, "spotify_api_call") if r.status == 404]
    assert failed and failed[0].reason == "NO_ACTIVE_DEVICE" and "Device not found" in failed[0].body


def _enable_verification(client, monkeypatch):
    client._verify_playback = True
    monkeypatch.setattr(spotify_client, "PLAY_VERIFY_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(spotify_client, "PLAY_VERIFY_ATTEMPTS", 3)


async def test_verification_confirms_when_target_device_plays_the_track(tmp_path, monkeypatch, caplog):
    api = FakeSpotifyAPI(tracks=[track("Zaroor", "Aparshakti Khurana", uri=REAL_URI)])
    client = make_client(tmp_path, monkeypatch, api)
    _enable_verification(client, monkeypatch)
    api.player = {"is_playing": True, "device": {"id": "d-phone", "name": "vivo T2 Pro 5G"},
                  "item": {"uri": REAL_URI, "name": "Zaroor"}}  # fmt: skip
    with caplog.at_level(logging.INFO, logger="app.spotify"):
        await client.play_music("Zaroor", preferred_device="T2 Pro")
        await asyncio.gather(*client._background_tasks)
    (verified,) = _records(caplog, "spotify_play_verified")
    assert verified.active_device == "vivo T2 Pro 5G" and verified.attempt == 1
    assert not _records(caplog, "spotify_play_not_confirmed")


async def test_verification_warns_when_another_device_is_still_playing(tmp_path, monkeypatch, caplog):
    api = FakeSpotifyAPI(tracks=[track("Zaroor", "Aparshakti Khurana", uri=REAL_URI)])
    client = make_client(tmp_path, monkeypatch, api)
    _enable_verification(client, monkeypatch)
    api.player = {"is_playing": True, "device": {"id": "d-mac", "name": "Vansh's MacBook Air"},
                  "item": {"uri": "spotify:track:other", "name": "Other"}}  # fmt: skip
    with caplog.at_level(logging.INFO, logger="app.spotify"):
        await client.play_music("Zaroor", preferred_device="T2 Pro")
        await asyncio.gather(*client._background_tasks)
    (warn,) = _records(caplog, "spotify_play_not_confirmed")
    assert warn.levelno == logging.WARNING
    assert (warn.active_device, warn.playing_name, warn.expected_name) == ("Vansh's MacBook Air", "Other", "Zaroor")
    assert warn.expected_uri == REAL_URI and warn.attempt == 3
