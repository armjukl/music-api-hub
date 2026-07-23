from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.models import UnifiedTrack
from app.settings import (
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    SPOTIFY_FORMAT,
    SPOTIFY_FFMPEG_PATH,
    SPOTIFY_OUTPUT_DIR,
    SPOTIFY_USE_OFFICIAL_API,
)


class SpotifyProviderError(RuntimeError):
    """Raised when the embedded spotDL adapter cannot answer a request."""


class SpotifyProvider:
    source = "spotify"

    def __init__(self) -> None:
        self._spotdl: Any = None
        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._spotdl is not None:
            return
        async with self._start_lock:
            if self._spotdl is not None:
                return
            try:
                from spotdl import Spotdl

                SPOTIFY_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                self._spotdl = await asyncio.to_thread(
                    Spotdl,
                    client_id=SPOTIFY_CLIENT_ID,
                    client_secret=SPOTIFY_CLIENT_SECRET,
                    headless=True,
                    use_official_api=SPOTIFY_USE_OFFICIAL_API,
                    downloader_settings={
                        "output": str(SPOTIFY_OUTPUT_DIR),
                        "format": SPOTIFY_FORMAT,
                        "simple_tui": True,
                        "ffmpeg": str(SPOTIFY_FFMPEG_PATH) if SPOTIFY_FFMPEG_PATH.is_file() else "ffmpeg",
                    },
                )
            except Exception as exc:
                raise SpotifyProviderError(
                    "无法初始化内置 spotDL，请确认已安装 FFmpeg 和 spotdl 依赖"
                ) from exc

    async def close(self) -> None:
        self._spotdl = None

    async def search(self, query: str, *, limit: int = 10) -> list[UnifiedTrack]:
        songs = await self._search_songs(query)
        return [_song_to_track(song) for song in songs[: max(1, min(limit, 50))]]

    async def resolve(self, url: str) -> list[UnifiedTrack]:
        songs = await self._search_songs(url)
        return [_song_to_track(song) for song in songs]

    async def download(self, url: str) -> str:
        self._require_spotdl()
        songs = await self._search_songs(url)
        if not songs:
            raise SpotifyProviderError("Spotify 没有找到可下载歌曲")
        try:
            async with self._lock:
                _, path = await asyncio.to_thread(self._spotdl.download, songs[0])
        except Exception as exc:
            raise SpotifyProviderError("Spotify 歌曲下载失败") from exc
        if path is None:
            raise SpotifyProviderError("spotDL 没有生成下载文件")
        return str(Path(path).resolve())

    async def _search_songs(self, query: str) -> list[Any]:
        await self.start()
        try:
            async with self._lock:
                return await asyncio.to_thread(self._spotdl.search, [query])
        except Exception as exc:
            raise SpotifyProviderError("Spotify 搜索或解析失败") from exc

    def _require_spotdl(self) -> None:
        if self._spotdl is None:
            raise SpotifyProviderError("Spotify provider 尚未初始化")


def _song_to_track(song: Any) -> UnifiedTrack:
    duration = getattr(song, "duration", None)
    duration_ms = int(duration * 1000) if isinstance(duration, (int, float)) else None
    source_url = _optional_string(getattr(song, "url", None))
    return UnifiedTrack(
        source="spotify",
        id=str(getattr(song, "song_id", "") or source_url or ""),
        title=str(getattr(song, "name", "") or "未知歌曲"),
        artist=str(getattr(song, "artist", "") or ""),
        album=str(getattr(song, "album_name", "") or ""),
        duration_ms=duration_ms,
        cover_url=_optional_string(getattr(song, "cover_url", None)),
        source_url=source_url,
        playable=True,
    )


def _optional_string(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
