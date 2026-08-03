"""Metadata about a voice turn, exposed as (ASCII-safe) response headers."""

from urllib.parse import quote

from pydantic import BaseModel

_MAX_HEADER_TEXT_CHARS = 200


class VoiceMetadata(BaseModel):
    transcript: str
    response_text: str
    audio_content_type: str
    audio_duration_ms: int | None = None

    def to_headers(self) -> dict[str, str]:
        """URL-encoded so non-ASCII text (e.g. Hindi) is safe in HTTP headers."""
        headers = {
            "X-Transcript": quote(self.transcript[:_MAX_HEADER_TEXT_CHARS], safe=""),
            "X-Response-Text": quote(self.response_text[:_MAX_HEADER_TEXT_CHARS], safe=""),
        }
        if self.audio_duration_ms is not None:
            headers["X-Audio-Duration-Ms"] = str(self.audio_duration_ms)
        return headers
