"""Spotify Connect speaker support: default device for ADI-device requests, pairing, volume."""

import pytest

from app.core.exceptions import SpotifyDeviceNotFoundError
from app.services.intents import match_intent
from app.services.spotify.tools import spotify_tools
from app.services.tools.base import current_session
from app.services.tools.registry import ToolRegistry

ADI = "ADI Speaker"


class FakeClient:
    def __init__(self, volume=50, active=True, adi_visible=True):
        self.volume = volume
        self.active = active
        self.adi_visible = adi_visible
        self.volume_before_mute = None
        self.calls = []

    async def play_music(self, query, preferred_device=None, artist=None):
        self.calls.append(("play", query, preferred_device))
        if preferred_device == ADI and not self.adi_visible:
            raise SpotifyDeviceNotFoundError(f"I couldn't find your {ADI}.")
        track = {"name": "Love Dose", "artists": "Yo Yo Honey Singh", "uri": "x"}
        return {"track": track, "device": preferred_device, "mode": "track"}

    async def volume_state(self):
        return (self.volume, {"id": "dev-active"}) if self.active else (None, None)

    async def choose_device(self, requested):
        if requested == ADI and self.adi_visible:
            return {"id": "dev-adi", "name": ADI, "volume_percent": 40}
        raise SpotifyDeviceNotFoundError(detail=requested)

    async def set_volume(self, percent, device_id=None):
        self.calls.append(("volume", percent, device_id))
        self.volume = percent


def _registry(client):
    return ToolRegistry(spotify_tools(client, default_device=ADI))


@pytest.fixture
def from_adi():
    token = current_session.set("device:esp32-desk")
    yield
    current_session.reset(token)


async def test_device_request_without_device_targets_adi_speaker(from_adi):
    client = FakeClient()
    result = await _registry(client).run("play_music", {"query": "Love Dose"})
    assert client.calls[-1] == ("play", "Love Dose", ADI)
    assert result.say == "Playing Love Dose by Yo Yo Honey Singh."  # no "on your ADI Speaker"


async def test_explicit_device_wins_and_non_device_requests_are_unchanged(from_adi):
    client = FakeClient()
    await _registry(client).run("play_music", {"query": "Love Dose", "device": "T2 Pro"})
    assert client.calls[-1][2] == "T2 Pro"
    token = current_session.set("default")  # e.g. /chat from a browser
    try:
        await _registry(client).run("play_music", {"query": "Love Dose"})
    finally:
        current_session.reset(token)
    assert client.calls[-1][2] is None


async def test_unpaired_speaker_gets_pairing_instructions(from_adi):
    result = await _registry(FakeClient(adi_visible=False)).run("play_music", {"query": "x"})
    assert not result.ok and result.error_code == "spotify_pairing_required"
    assert "choose ADI Speaker once" in result.say


@pytest.mark.parametrize(
    ("args", "percent", "say"),
    [({"level": 5}, 50, "Volume 5."), ({"level": 8}, 80, "Volume 8."),
     ({"level": 10}, 100, "Volume 10."), ({"percent": 35}, 35, "Volume 35 percent."),
     ({"level": 14}, 100, "Volume 10.")],
)  # fmt: skip
async def test_set_volume_levels(args, percent, say):
    client = FakeClient()
    result = await _registry(client).run("set_volume", args)
    assert client.calls[-1] == ("volume", percent, "dev-active")
    assert result.say == say and result.data["volume"] == percent / 100


async def test_volume_steps_mute_and_unmute():
    client = FakeClient(volume=50)
    reg = _registry(client)
    assert (await reg.run("volume_up")).data["volume_percent"] == 60
    assert (await reg.run("volume_down")).data["volume_percent"] == 50
    assert (await reg.run("mute_music")).say == "Muted."
    assert client.volume == 0 and client.volume_before_mute == 50
    assert (await reg.run("unmute_music")).data["volume_percent"] == 50
    client.volume = 0
    assert (await reg.run("volume_down")).data["volume_percent"] == 0  # clamps


async def test_volume_from_device_with_nothing_active_uses_adi_or_asks_to_pair(from_adi):
    client = FakeClient(active=False)
    await _registry(client).run("set_volume", {"level": 5})
    assert client.calls[-1] == ("volume", 50, "dev-adi")
    result = await _registry(FakeClient(active=False, adi_visible=False)).run("volume_up")
    assert result.error_code == "spotify_pairing_required"


@pytest.mark.parametrize(
    ("text", "tool", "args"),
    [("Volume 5", "set_volume", {"level": 5.0}), ("volume 10", "set_volume", {"level": 10.0}),
     ("volume 35 percent", "set_volume", {"percent": 35.0}), ("volume up", "volume_up", {}),
     ("turn it down", "volume_down", {}), ("mute", "mute_music", {}),
     ("unmute", "unmute_music", {})],
)  # fmt: skip
def test_spotify_volume_fast_intents(text, tool, args):
    intent = match_intent(text, {"set_volume", "volume_up", "volume_down", "mute_music", "unmute_music"})
    assert (intent.tool, intent.arguments) == (tool, args)
