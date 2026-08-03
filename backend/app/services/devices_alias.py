"""Tiny helper to match a spoken name against configured keys ("google chrome" -> "chrome")."""

from __future__ import annotations

import re


def _norm(text: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    words = [w for w in text.split() if w not in {"my", "the", "app", "application", "website", "site"}]
    return " ".join(words)


def best_key(table: dict[str, str], spoken: str) -> str | None:
    """Exact key, then key/value contained in the phrase (or vice versa). None if unsure."""
    want = _norm(spoken)
    if not want:
        return None
    for key in table:
        if _norm(key) == want:
            return key
    hits = {
        key
        for key, value in table.items()
        if _norm(key) and (_norm(key) in want or want in _norm(key) or want == _norm(value)
                           or _norm(value) in want)
    }  # fmt: skip
    return hits.pop() if len(hits) == 1 else None
