from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse

from app.models import ResolvedAudio, ResolvedVideo, SourceInfo, UnifiedTrack
from app.providers.bilibili import (
    BilibiliFfmpegStream,
    BilibiliProvider,
    BilibiliProviderError,
)
from app.providers.spotify import SpotifyProvider, SpotifyProviderError

@asynccontextmanager
async def lifespan(app: FastAPI):
    spotify = SpotifyProvider()
    bilibili = BilibiliProvider()
    try:
        await bilibili.start()
        app.state.spotify = spotify
        app.state.bilibili = bilibili
        yield
    finally:
        await spotify.close()
        await bilibili.close()


app = FastAPI(
    title="Music API Hub",
    version="0.1.0",
    description="统一聚合 spotDL 与 Bilibili 音频能力",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health(request: Request) -> dict[str, str]:
    return {
        "status": "ok",
        "spotify": "configured",
        "bilibili": "ready" if request.app.state.bilibili.ready else "unavailable",
    }


@app.get("/api/sources", response_model=list[SourceInfo])
async def sources() -> list[SourceInfo]:
    return [
        SourceInfo(
            id="spotify",
            label="Spotify / spotDL",
            capabilities=["search", "resolve-metadata"],
        ),
        SourceInfo(
            id="bilibili",
            label="Bilibili",
            capabilities=[
                "search",
                "favorites",
                "audio.resolve",
                "audio.stream",
                "audio.direct",
                "video.resolve",
                "video.stream",
                "video.direct",
            ],
        ),
    ]


@app.get("/api/spotify/search", response_model=list[UnifiedTrack])
async def spotify_search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=120),
    limit: int = Query(10, ge=1, le=50),
) -> list[UnifiedTrack]:
    try:
        return await request.app.state.spotify.search(q, limit=limit)
    except SpotifyProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/spotify/resolve", response_model=list[UnifiedTrack])
async def spotify_resolve(
    request: Request,
    url: str = Query(..., min_length=1, max_length=500),
) -> list[UnifiedTrack]:
    try:
        return await request.app.state.spotify.resolve(url)
    except SpotifyProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/spotify/download")
async def spotify_download(
    request: Request,
    url: str = Query(..., min_length=1, max_length=500),
) -> dict[str, str]:
    try:
        path = await request.app.state.spotify.download(url)
        return {"path": path}
    except SpotifyProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/bilibili/search", response_model=list[UnifiedTrack])
async def bilibili_search(
    request: Request,
    q: str = Query(..., min_length=1, max_length=120),
    limit: int = Query(10, ge=1, le=50),
    page: int = Query(1, ge=1, le=100),
) -> list[UnifiedTrack]:
    return await _search_bilibili(request, q, limit=limit, page=page)


@app.get("/api/bilibili/favorites", response_model=list[UnifiedTrack])
async def bilibili_favorites(
    request: Request,
    media_id: int = Query(..., gt=0),
    limit: int = Query(10, ge=1, le=20),
    page: int = Query(1, ge=1, le=100),
) -> list[UnifiedTrack]:
    try:
        tracks = await request.app.state.bilibili.favorites(
            media_id,
            limit=limit,
            page=page,
        )
        return [_with_bilibili_audio_track_urls(track) for track in tracks]
    except BilibiliProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get(
    "/api/bilibili/audio/resolve",
    response_model=ResolvedAudio,
    name="bilibili_audio_resolve",
)
async def bilibili_resolve(
    request: Request,
    id: str = Query(..., min_length=1, max_length=160),
) -> ResolvedAudio:
    return await _resolve_bilibili_audio(request, id)


@app.get("/api/bilibili/audio/stream", name="bilibili_audio_stream")
async def bilibili_audio_stream(
    request: Request,
    id: str = Query(..., min_length=1, max_length=160),
) -> StreamingResponse:
    try:
        upstream, _audio = await request.app.state.bilibili.open_mp3_stream(id)
    except BilibiliProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    async def iterator():
        async for chunk in upstream.iter_chunks():
            yield chunk

    return StreamingResponse(
        iterator(),
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/bilibili/audio/direct", name="bilibili_audio_direct")
async def bilibili_audio_direct(
    request: Request,
    id: str = Query(..., min_length=1, max_length=160),
) -> Response:
    try:
        path = await request.app.state.bilibili.cache_mp3(id)
    except BilibiliProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return _cached_media_response(request, path, "audio/mpeg")


@app.get("/api/bilibili/video/resolve", response_model=ResolvedVideo)
async def bilibili_video_resolve(
    request: Request,
    id: str = Query(..., min_length=1, max_length=160),
) -> ResolvedVideo:
    try:
        video = await request.app.state.bilibili.resolve_video(id)
        return _with_bilibili_video_stream_url(video, id)
    except BilibiliProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/bilibili/video/stream", name="bilibili_video_stream")
async def bilibili_video_stream(
    request: Request,
    id: str = Query(..., min_length=1, max_length=160),
) -> StreamingResponse:
    try:
        upstream, video = await request.app.state.bilibili.open_video_stream(
            id,
            range_header=request.headers.get("range"),
        )
    except BilibiliProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if isinstance(upstream, BilibiliFfmpegStream):
        async def iterator():
            async for chunk in upstream.iter_chunks():
                yield chunk

        return StreamingResponse(
            iterator(),
            media_type=video.mime_type or "video/mp4",
            headers={"Cache-Control": "no-store"},
        )

    response_headers = {"Accept-Ranges": "bytes"}
    for header_name in ("Content-Length", "Content-Range"):
        value = upstream.headers.get(header_name)
        if value:
            response_headers[header_name] = value

    async def iterator():
        try:
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                yield chunk
        finally:
            upstream.release()

    return StreamingResponse(
        iterator(),
        status_code=upstream.status,
        media_type=upstream.headers.get("Content-Type") or video.mime_type or "video/mp4",
        headers=response_headers,
    )


@app.get("/api/bilibili/video/direct", name="bilibili_video_direct")
async def bilibili_video_direct(
    request: Request,
    id: str = Query(..., min_length=1, max_length=160),
) -> Response:
    try:
        path = await request.app.state.bilibili.cache_video(id)
    except BilibiliProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return _cached_media_response(request, path, "video/mp4")


async def _search_bilibili(
    request: Request,
    query: str,
    *,
    limit: int,
    page: int,
) -> list[UnifiedTrack]:
    try:
        tracks = await request.app.state.bilibili.search(
            query,
            limit=limit,
            page=page,
        )
        return [_with_bilibili_audio_track_urls(track) for track in tracks]
    except BilibiliProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def _resolve_bilibili_audio(
    request: Request,
    track_id: str,
) -> ResolvedAudio:
    try:
        audio = await request.app.state.bilibili.resolve(track_id)
        return _with_bilibili_audio_urls(audio, track_id)
    except BilibiliProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _cached_media_response(request: Request, path: Path, media_type: str) -> Response:
    size = path.stat().st_size
    byte_range = _parse_byte_range(request.headers.get("range"), size)
    if request.headers.get("range") and byte_range is None:
        return Response(
            status_code=416,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Range": f"bytes */{size}",
            },
        )

    if byte_range is None:
        start, end = 0, size - 1
        status_code = 200
    else:
        start, end = byte_range
        status_code = 206
    content_length = end - start + 1
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(content_length),
        "Cache-Control": "public, max-age=31536000, immutable",
    }
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    return StreamingResponse(
        _iter_file_range(path, start, content_length),
        status_code=status_code,
        media_type=media_type,
        headers=headers,
    )


async def _iter_file_range(path, start: int, content_length: int):
    remaining = content_length
    with path.open("rb") as media_file:
        media_file.seek(start)
        while remaining > 0:
            chunk = await asyncio.to_thread(
                media_file.read, min(64 * 1024, remaining)
            )
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def _parse_byte_range(
    header: str | None, size: int
) -> tuple[int, int] | None:
    if not header:
        return None
    if not header.startswith("bytes=") or "," in header:
        return None
    value = header[6:].strip()
    if "-" not in value:
        return None
    start_text, end_text = (part.strip() for part in value.split("-", 1))
    try:
        if not start_text:
            suffix_length = int(end_text)
            if suffix_length <= 0:
                return None
            return max(0, size - suffix_length), size - 1

        start = int(start_text)
        if start < 0 or start >= size:
            return None
        end = size - 1 if not end_text else int(end_text)
        if end < start:
            return None
        return start, min(end, size - 1)
    except ValueError:
        return None


def _bilibili_audio_url_fields(track_id: str) -> dict[str, str]:
    encoded_id = quote(track_id, safe="")
    return {
        "stream_url": f"/api/bilibili/audio/stream?id={encoded_id}",
        "download_url": f"/api/bilibili/audio/direct?id={encoded_id}",
    }


def _with_bilibili_audio_track_urls(track: UnifiedTrack) -> UnifiedTrack:
    return track.model_copy(update=_bilibili_audio_url_fields(track.id))


def _with_bilibili_audio_urls(audio: ResolvedAudio, track_id: str) -> ResolvedAudio:
    return audio.model_copy(
        update={
            **_bilibili_audio_url_fields(track_id),
        }
    )


def _with_bilibili_video_stream_url(video: ResolvedVideo, track_id: str) -> ResolvedVideo:
    encoded_id = quote(track_id, safe="")
    return video.model_copy(
        update={
            "stream_url": f"/api/bilibili/video/stream?id={encoded_id}",
            "download_url": f"/api/bilibili/video/direct?id={encoded_id}",
        }
    )
