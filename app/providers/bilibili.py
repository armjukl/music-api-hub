from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp

from app.models import ResolvedAudio, ResolvedVideo, UnifiedTrack
from app.providers.bilibili_client import BilibiliClient
from app.settings import BILIBILI_CACHE_DIR, BILIBILI_MP3_BITRATE, SPOTIFY_FFMPEG_PATH


class BilibiliProviderError(RuntimeError):
    """Raised when the Bilibili adapter cannot return usable data."""


@dataclass(slots=True)
class BilibiliFfmpegStream:
    """A media stream produced by FFmpeg from a remote source."""

    process: asyncio.subprocess.Process
    mime_type: str = "video/mp4"
    failure_message: str = "FFmpeg 处理媒体失败"

    async def iter_chunks(self, chunk_size: int = 64 * 1024):
        if self.process.stdout is None:
            raise BilibiliProviderError("FFmpeg 没有输出视频流")
        try:
            while True:
                chunk = await self.process.stdout.read(chunk_size)
                if not chunk:
                    break
                yield chunk
            return_code = await self.process.wait()
            if return_code != 0:
                raise BilibiliProviderError(self.failure_message)
        finally:
            await self.close()

    async def close(self) -> None:
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=3)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()


class BilibiliProvider:
    source = "bilibili"

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._client: Any = None
        self._cache_locks: dict[Path, asyncio.Lock] = {}

    async def start(self) -> None:
        try:
            self._session = aiohttp.ClientSession()
            self._client = BilibiliClient(self._session)
        except Exception as exc:
            await self.close()
            raise BilibiliProviderError("无法加载 Bilibili 插件客户端") from exc

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
        self._client = None

    @property
    def ready(self) -> bool:
        return self._client is not None

    async def search(
        self, query: str, *, limit: int = 10, page: int = 1
    ) -> list[UnifiedTrack]:
        client = self._require_client()
        hits = await client.search_videos(
            query,
            limit=min(max(limit, 1), 20),
            page=max(1, min(page, 100)),
        )
        semaphore = asyncio.Semaphore(5)

        async def expand(hit: Any) -> list[UnifiedTrack]:
            bvid = str(getattr(hit, "bvid", "")).strip()
            if not bvid:
                return []
            try:
                async with semaphore:
                    video = await client.get_video(bvid)
            except Exception:
                return []
            return _expand_video(video)

        groups = await asyncio.gather(*(expand(hit) for hit in hits))
        tracks = [track for group in groups for track in group]
        return tracks[: max(1, min(limit, 50))]

    async def resolve(self, track_id: str) -> ResolvedAudio:
        client = self._require_client()
        bvid, cid = _parse_track_id(track_id)
        try:
            audio = await client.resolve_audio(bvid, cid)
        except Exception as exc:
            raise BilibiliProviderError("Bilibili 未返回可播放音频") from exc
        return ResolvedAudio(
            source=self.source,
            id=track_id,
            audio_url=audio.url,
            mime_type=audio.mime_type,
            duration_ms=audio.duration_ms,
            source_url=f"https://www.bilibili.com/video/{bvid}",
        )

    async def open_stream(self, track_id: str, range_header: str | None = None) -> tuple[Any, Any]:
        client = self._require_client()
        bvid, cid = _parse_track_id(track_id)
        try:
            audio = await client.resolve_audio(bvid, cid)
            headers = await client.stream_headers(bvid)
            response = await self._open_remote_stream(
                (audio.url, *audio.backup_urls),
                headers,
                range_header=range_header,
                label="音频",
            )
            return response, audio
        except BilibiliProviderError:
            raise
        except Exception as exc:
            raise BilibiliProviderError("Bilibili 音频代理请求失败") from exc

    async def open_mp3_stream(self, track_id: str) -> tuple[BilibiliFfmpegStream, Any]:
        client = self._require_client()
        bvid, cid = _parse_track_id(track_id)
        try:
            audio = await client.resolve_audio(bvid, cid)
            return await self._open_mp3_stream(audio), audio
        except BilibiliProviderError:
            raise
        except Exception as exc:
            raise BilibiliProviderError("Bilibili MP3 音频流请求失败") from exc

    async def cache_mp3(self, track_id: str, *, bitrate: int | None = None) -> Path:
        client = self._require_client()
        bvid, cid = _parse_track_id(track_id)
        selected_bitrate = _normalise_bitrate(bitrate)
        cache_path = _cache_path(track_id, "audio", selected_bitrate)
        async with self._cache_lock(cache_path):
            if _is_cached(cache_path):
                return cache_path

            source_path = _temporary_path(cache_path, ".source")
            output_path = _temporary_path(cache_path, ".output")
            try:
                audio = await client.resolve_audio(bvid, cid)
                await self._download_to_file(
                    (audio.url, *audio.backup_urls),
                    dict(audio.headers),
                    source_path,
                    label="音频",
                )
                await self._run_ffmpeg(
                    [
                        "-i",
                        str(source_path),
                        "-vn",
                        "-c:a",
                        "libmp3lame",
                        "-b:a",
                        f"{selected_bitrate}k",
                        "-id3v2_version",
                        "3",
                        "-f",
                        "mp3",
                        "-y",
                        str(output_path),
                    ],
                    output_path,
                    "无法将 Bilibili 音频转换为 MP3",
                )
                os.replace(output_path, cache_path)
                return cache_path
            except BilibiliProviderError:
                raise
            except Exception as exc:
                raise BilibiliProviderError("Bilibili MP3 下载失败") from exc
            finally:
                _unlink_quietly(source_path)
                _unlink_quietly(output_path)

    async def cache_video(self, track_id: str) -> Path:
        client = self._require_client()
        bvid, cid = _parse_track_id(track_id)
        cache_path = _cache_path(track_id, "video")
        async with self._cache_lock(cache_path):
            if _is_cached(cache_path):
                return cache_path

            video_path = _temporary_path(cache_path, ".video")
            audio_path = _temporary_path(cache_path, ".audio")
            output_path = _temporary_path(cache_path, ".output")
            try:
                video = await client.resolve_video(bvid, cid)
                if video.audio_url:
                    await self._download_to_file(
                        (video.url, *video.backup_urls),
                        dict(video.headers),
                        video_path,
                        label="视频",
                    )
                    await self._download_to_file(
                        (video.audio_url, *video.audio_backup_urls),
                        dict(video.headers),
                        audio_path,
                        label="音频",
                    )
                    await self._run_ffmpeg(
                        [
                            "-i",
                            str(video_path),
                            "-i",
                            str(audio_path),
                            "-map",
                            "0:v:0",
                            "-map",
                            "1:a:0",
                            "-c:v",
                            "copy",
                            "-c:a",
                            "copy",
                            "-movflags",
                            "+faststart",
                            "-f",
                            "mp4",
                            "-y",
                            str(output_path),
                        ],
                        output_path,
                        "无法合并 Bilibili 音视频",
                    )
                else:
                    await self._download_to_file(
                        (video.url, *video.backup_urls),
                        dict(video.headers),
                        output_path,
                        label="视频",
                    )
                os.replace(output_path, cache_path)
                return cache_path
            except BilibiliProviderError:
                raise
            except Exception as exc:
                raise BilibiliProviderError("Bilibili 视频下载失败") from exc
            finally:
                _unlink_quietly(video_path)
                _unlink_quietly(audio_path)
                _unlink_quietly(output_path)

    async def resolve_video(self, track_id: str) -> ResolvedVideo:
        client = self._require_client()
        bvid, cid = _parse_track_id(track_id)
        try:
            video = await client.resolve_video(bvid, cid)
        except Exception as exc:
            raise BilibiliProviderError("Bilibili 未返回可播放视频流") from exc
        return ResolvedVideo(
            source=self.source,
            id=track_id,
            video_url=video.url,
            mime_type=video.mime_type,
            duration_ms=video.duration_ms,
            source_url=f"https://www.bilibili.com/video/{bvid}",
        )

    async def open_video_stream(self, track_id: str, range_header: str | None = None) -> tuple[Any, Any]:
        client = self._require_client()
        bvid, cid = _parse_track_id(track_id)
        try:
            video = await client.resolve_video(bvid, cid)
            if video.audio_url:
                return await self._open_dash_video_stream(video), video
            headers = await client.stream_headers(bvid)
            response = await self._open_remote_stream(
                (video.url, *video.backup_urls),
                headers,
                range_header=range_header,
                label="视频",
            )
            return response, video
        except BilibiliProviderError:
            raise
        except Exception as exc:
            raise BilibiliProviderError("Bilibili 视频代理请求失败") from exc

    def _require_client(self) -> Any:
        if self._client is None:
            raise BilibiliProviderError("Bilibili provider 尚未初始化")
        return self._client

    async def _open_dash_video_stream(self, video: Any) -> BilibiliFfmpegStream:
        ffmpeg = _ffmpeg_binary()
        if not ffmpeg:
            raise BilibiliProviderError(
                "Bilibili 返回了音视频分离流，但项目中未找到 FFmpeg"
            )

        header_text = _ffmpeg_header_text(video.headers)
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-headers",
            header_text,
            "-i",
            video.url,
            "-headers",
            header_text,
            "-i",
            video.audio_url,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-movflags",
            "+frag_keyframe+empty_moov+default_base_moof",
            "-f",
            "mp4",
            "pipe:1",
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise BilibiliProviderError("无法启动 FFmpeg 合并 Bilibili 视频") from exc
        return BilibiliFfmpegStream(process=process)

    async def _open_mp3_stream(self, audio: Any) -> BilibiliFfmpegStream:
        ffmpeg = _ffmpeg_binary()
        if not ffmpeg:
            raise BilibiliProviderError("项目中未找到 FFmpeg，无法转换 MP3")

        header_text = _ffmpeg_header_text(dict(audio.headers))
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-headers",
            header_text,
            "-i",
            audio.url,
            "-vn",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            "-f",
            "mp3",
            "pipe:1",
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise BilibiliProviderError("无法启动 FFmpeg 转换 MP3") from exc
        return BilibiliFfmpegStream(
            process=process,
            mime_type="audio/mpeg",
            failure_message="FFmpeg 转换 Bilibili 音频为 MP3 失败",
        )

    def _cache_lock(self, path: Path) -> asyncio.Lock:
        return self._cache_locks.setdefault(path, asyncio.Lock())

    async def _download_to_file(
        self,
        urls: tuple[str, ...],
        headers: dict[str, str],
        target: Path,
        *,
        label: str,
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        request_headers = dict(headers)
        request_headers.pop("Range", None)
        last_status: int | None = None
        for url in urls:
            _unlink_quietly(target)
            try:
                async with self._session.get(
                    url,
                    headers=request_headers,
                    allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(
                        connect=30,
                        sock_read=120,
                        total=None,
                    ),
                ) as response:
                    if response.status >= 400:
                        last_status = response.status
                        continue
                    with target.open("wb") as output:
                        async for chunk in response.content.iter_chunked(128 * 1024):
                            if chunk:
                                output.write(chunk)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                continue
            if _is_cached(target):
                return
        status_text = f"HTTP {last_status}" if last_status is not None else "网络错误"
        raise BilibiliProviderError(f"Bilibili {label}下载失败：{status_text}")

    async def _run_ffmpeg(
        self,
        arguments: list[str],
        output_path: Path,
        error_message: str,
    ) -> None:
        ffmpeg = _ffmpeg_binary()
        if not ffmpeg:
            raise BilibiliProviderError("项目中未找到 FFmpeg")
        _unlink_quietly(output_path)
        try:
            process = await asyncio.create_subprocess_exec(
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                *arguments,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()
        except OSError as exc:
            raise BilibiliProviderError("无法启动 FFmpeg") from exc
        if process.returncode != 0 or not _is_cached(output_path):
            detail = stderr.decode("utf-8", "replace").strip()
            suffix = f"：{detail[:240]}" if detail else ""
            raise BilibiliProviderError(f"{error_message}{suffix}")

    async def _open_remote_stream(
        self,
        urls: tuple[str, ...],
        headers: dict[str, str],
        *,
        range_header: str | None,
        label: str,
    ) -> Any:
        request_headers = dict(headers)
        if range_header:
            request_headers["Range"] = range_header
        last_status: int | None = None
        for url in urls:
            try:
                response = await self._session.get(
                    url,
                    headers=request_headers,
                    allow_redirects=True,
                )
            except aiohttp.ClientError:
                continue
            if response.status < 400:
                return response
            last_status = response.status
            response.release()
        status_text = f"HTTP {last_status}" if last_status is not None else "网络错误"
        raise BilibiliProviderError(f"Bilibili {label}代理请求失败：{status_text}")


def _expand_video(video: Any) -> list[UnifiedTrack]:
    bvid = str(getattr(video, "bvid", "")).strip()
    title = str(getattr(video, "title", "")).strip() or bvid
    artist = str(getattr(video, "uploader", "")).strip()
    pages = tuple(getattr(video, "pages", ()))
    tracks: list[UnifiedTrack] = []
    for page in pages:
        cid = int(getattr(page, "cid", 0) or 0)
        if not bvid or cid <= 0:
            continue
        page_title = str(getattr(page, "title", "")).strip()
        display_title = page_title if len(pages) > 1 and page_title else title
        page_index = int(getattr(page, "index", 1) or 1)
        track_id = f"{bvid}:{cid}"
        encoded_id = quote(track_id, safe="")
        tracks.append(
            UnifiedTrack(
                source="bilibili",
                id=track_id,
                title=display_title,
                artist=artist,
                duration_ms=int(getattr(page, "duration_ms", 0) or 0) or None,
                source_url=f"https://www.bilibili.com/video/{bvid}/?p={page_index}",
                playable=True,
                stream_url=f"/api/bilibili/stream?id={encoded_id}",
                download_url=f"/api/bilibili/direct?id={encoded_id}",
            )
        )
    return tracks


def _parse_track_id(track_id: str) -> tuple[str, int]:
    bvid, separator, raw_cid = track_id.partition(":")
    if not separator or not bvid or not raw_cid.isdigit() or int(raw_cid) <= 0:
        raise BilibiliProviderError("Bilibili track id 格式应为 BV号:cid")
    return bvid, int(raw_cid)


def _ffmpeg_binary() -> str | None:
    if SPOTIFY_FFMPEG_PATH.is_file():
        return str(SPOTIFY_FFMPEG_PATH)
    return shutil.which("ffmpeg")


def _ffmpeg_header_text(headers: dict[str, str]) -> str:
    return "".join(
        f"{name}: {value}\r\n"
        for name, value in headers.items()
        if name.lower() not in {"host", "content-length", "range"}
    )


def _normalise_bitrate(value: int | None) -> int:
    bitrate = BILIBILI_MP3_BITRATE if value is None else value
    if isinstance(bitrate, bool) or not isinstance(bitrate, int):
        raise BilibiliProviderError("MP3 码率必须是整数")
    if not 32 <= bitrate <= 320:
        raise BilibiliProviderError("MP3 码率必须在 32 到 320 kbps 之间")
    return bitrate


def _cache_path(track_id: str, kind: str, bitrate: int | None = None) -> Path:
    cache_key = f"bilibili\0{kind}\0{track_id}\0{bitrate or ''}"
    digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
    suffix = ".mp3" if kind == "audio" else ".mp4"
    return BILIBILI_CACHE_DIR / f"{digest}{suffix}"


def _temporary_path(cache_path: Path, label: str) -> Path:
    return cache_path.with_name(f".{cache_path.name}.{secrets.token_hex(8)}{label}")


def _is_cached(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
