"""Resolve a spoken device name ("my T2 Pro", "the laptop") to a Spotify device.

Pure logic (no network) so it is easy to test. Matching tiers, strongest first:

1. exact name (case/punctuation-insensitive)
2. every spoken word appears in the device name ("t2 pro" -> "vivo T2 Pro 5G")
3. the device name is contained in the request ("my vivo t2 pro 5g phone")
4. category aliases ("phone" -> Smartphone, "laptop"/"mac" -> Computer, ...)

The first tier that produces matches decides. Several matches in that tier is an
ambiguity - we never pick one at random.
"""

from __future__ import annotations

import re
from typing import Any

from app.core.exceptions import (
    SpotifyDeviceAmbiguousError,
    SpotifyDeviceNotFoundError,
    SpotifyNoDeviceError,
)

_FILLER = {"my", "the", "your", "a", "an", "on", "to", "this", "that", "device", "spotify"}

# spoken word -> (Spotify device types, extra name hints)
_ALIASES: dict[str, tuple[set[str], set[str]]] = {
    "phone": ({"smartphone"}, set()),
    "mobile": ({"smartphone"}, set()),
    "smartphone": ({"smartphone"}, set()),
    "cell": ({"smartphone"}, set()),
    "iphone": ({"smartphone"}, {"iphone"}),
    "android": ({"smartphone"}, set()),
    "laptop": ({"computer"}, set()),
    "computer": ({"computer"}, set()),
    "pc": ({"computer"}, set()),
    "desktop": ({"computer"}, set()),
    "mac": ({"computer"}, {"mac"}),
    "macbook": ({"computer"}, {"mac"}),
    "imac": ({"computer"}, {"mac"}),
    "tablet": ({"tablet"}, set()),
    "ipad": ({"tablet"}, {"ipad"}),
    "tv": ({"tv"}, set()),
    "television": ({"tv"}, set()),
    "speaker": ({"speaker"}, set()),
    "car": ({"automobile"}, set()),
    "browser": ({"computer"}, {"web player", "chrome", "browser"}),
    "chrome": ({"computer"}, {"chrome", "web player"}),
}


def normalize(text: str) -> str:
    text = text.lower().replace("’", "'")
    text = re.sub(r"'s\b", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _tokens(text: str) -> list[str]:
    return [t for t in normalize(text).split() if t not in _FILLER]


def describe(device: dict[str, Any]) -> str:
    return str(device.get("name") or "Unknown device")


def usable_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [d for d in devices if d.get("id") and not d.get("is_restricted", False)]


def resolve_device(devices: list[dict[str, Any]], requested: str | None) -> dict[str, Any]:
    """Return the device to use, or raise a Spotify*Device* error."""
    usable = usable_devices(devices)
    if not usable:
        raise SpotifyNoDeviceError(detail="Spotify reported no usable devices.")

    if not requested or not _tokens(requested):
        active = next((d for d in usable if d.get("is_active")), None)
        if active:
            return active
        if len(usable) == 1:
            return usable[0]
        # Several idle devices and none active: better to ask than to guess.
        raise _ambiguous(usable, requested or "")

    req_norm = " ".join(_tokens(requested))
    req_tokens = set(req_norm.split())

    def name_of(d: dict[str, Any]) -> str:
        return normalize(describe(d))

    tiers = [
        [d for d in usable if name_of(d) == req_norm],
        [d for d in usable if req_tokens <= set(name_of(d).split())],
        [d for d in usable if name_of(d) and name_of(d) in req_norm],
    ]

    alias_types: set[str] = set()
    alias_hints: set[str] = set()
    for token in req_tokens:
        if token in _ALIASES:
            types, hints = _ALIASES[token]
            alias_types |= types
            alias_hints |= hints
    if alias_types:
        by_type = [d for d in usable if str(d.get("type", "")).lower() in alias_types]
        hinted = [d for d in by_type if any(h in name_of(d) for h in alias_hints)]
        tiers.append(hinted or by_type)

    for matches in tiers:
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise _ambiguous(matches, requested)

    raise SpotifyDeviceNotFoundError(
        f"I couldn't find your {requested.strip()}.",
        detail=f"no device matched {requested!r}; available: "
        + ", ".join(describe(d) for d in usable),
    )


def _ambiguous(matches: list[dict[str, Any]], requested: str) -> SpotifyDeviceAmbiguousError:
    names = [describe(d) for d in matches]
    if len(names) == 2:
        listed = f"{names[0]} or {names[1]}"
    else:
        listed = ", ".join(names[:-1]) + f", or {names[-1]}"
    kind = _kind_word(requested, matches)
    count = {2: "two", 3: "three", 4: "four"}.get(len(names), str(len(names)))
    return SpotifyDeviceAmbiguousError(
        f"I found {count} {kind}: {listed}. Which one should I use?",
        candidates=names,
        detail=f"{requested!r} matched {names}",
    )


def _kind_word(requested: str, matches: list[dict[str, Any]]) -> str:
    types = {str(d.get("type", "")).lower() for d in matches}
    if types == {"smartphone"}:
        return "phones"
    if types == {"computer"}:
        return "computers"
    return "devices"
