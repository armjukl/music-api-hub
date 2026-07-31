from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class UnifiedTrack(BaseModel):
    source: str
    id: str
    title: str
    artist: str = ""
    album: str = ""
    duration_ms: int | None = None
    cover_url: str | None = None
    published_at: datetime | None = None
    source_url: str | None = None
    playable: bool = False
    stream_url: str | None = None
    download_url: str | None = None


class ResolvedAudio(BaseModel):
    source: str
    id: str
    audio_url: str
    mime_type: str | None = None
    duration_ms: int | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    source_url: str | None = None
    stream_url: str | None = None
    download_url: str | None = None


class ResolvedVideo(BaseModel):
    source: str
    id: str
    video_url: str
    mime_type: str | None = None
    duration_ms: int | None = None
    source_url: str | None = None
    stream_url: str | None = None
    download_url: str | None = None


class SourceInfo(BaseModel):
    id: str
    label: str
    capabilities: list[str]


class ErrorResponse(BaseModel):
    detail: str
