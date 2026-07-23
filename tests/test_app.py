from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import app
from app.providers.bilibili import BilibiliProvider


def test_health_and_sources() -> None:
    with TestClient(app) as client:
        health = client.get("/health")
        sources = client.get("/api/sources")

    assert health.status_code == 200
    assert health.json()["bilibili"] == "ready"
    assert sources.status_code == 200
    assert {item["id"] for item in sources.json()} == {"spotify", "bilibili"}


class FakeFfmpegStream:
    async def iter_chunks(self):
        yield b"realtime audio"


class FakeRemoteContent:
    async def iter_chunked(self, _chunk_size):
        yield b"realtime video"


class FakeRemoteResponse:
    status = 200
    headers = {"Content-Type": "video/mp4"}
    content = FakeRemoteContent()

    def release(self) -> None:
        return None


def test_bilibili_media_routes_separate_stream_and_direct(tmp_path, monkeypatch) -> None:
    async def open_mp3_stream(_provider, _track_id):
        return FakeFfmpegStream(), None

    async def open_video_stream(_provider, _track_id, *, range_header):
        return FakeRemoteResponse(), SimpleNamespace(mime_type="video/mp4")

    async def cache_mp3(_provider, _track_id):
        path = tmp_path / "cached.mp3"
        path.write_bytes(b"cached mp3")
        return path

    async def cache_video(_provider, _track_id):
        path = tmp_path / "cached.mp4"
        path.write_bytes(b"cached video")
        return path

    monkeypatch.setattr(BilibiliProvider, "open_mp3_stream", open_mp3_stream)
    monkeypatch.setattr(BilibiliProvider, "open_video_stream", open_video_stream)
    monkeypatch.setattr(BilibiliProvider, "cache_mp3", cache_mp3)
    monkeypatch.setattr(BilibiliProvider, "cache_video", cache_video)

    with TestClient(app) as client:
        audio_stream = client.get("/api/bilibili/stream?id=BV1fixture%3A123")
        audio_direct = client.get("/api/bilibili/direct?id=BV1fixture%3A123")
        video_stream = client.get("/api/bilibili/video/stream?id=BV1fixture%3A123")
        video_direct = client.get("/api/bilibili/video/direct?id=BV1fixture%3A123")

    assert audio_stream.status_code == 200
    assert audio_stream.content == b"realtime audio"
    assert audio_stream.headers["content-type"] == "audio/mpeg"
    assert audio_direct.status_code == 200
    assert audio_direct.content == b"cached mp3"
    assert audio_direct.headers["content-type"] == "audio/mpeg"
    audio_range = client.get(
        "/api/bilibili/direct?id=BV1fixture%3A123",
        headers={"Range": "bytes=1-6"},
    )
    assert video_stream.status_code == 200
    assert video_stream.content == b"realtime video"
    assert video_stream.headers["content-type"] == "video/mp4"
    assert video_direct.status_code == 200
    assert video_direct.content == b"cached video"
    assert video_direct.headers["content-type"] == "video/mp4"
    video_range = client.get(
        "/api/bilibili/video/direct?id=BV1fixture%3A123",
        headers={"Range": "bytes=1-6"},
    )
    invalid_range = client.get(
        "/api/bilibili/direct?id=BV1fixture%3A123",
        headers={"Range": "bytes=999-"},
    )

    assert audio_range.status_code == 206
    assert audio_range.content == b"ached "
    assert audio_range.headers["content-range"] == "bytes 1-6/10"
    assert audio_range.headers["content-length"] == "6"
    assert video_range.status_code == 206
    assert video_range.content == b"ached "
    assert video_range.headers["content-range"] == "bytes 1-6/12"
    assert invalid_range.status_code == 416
    assert invalid_range.headers["content-range"] == "bytes */10"
