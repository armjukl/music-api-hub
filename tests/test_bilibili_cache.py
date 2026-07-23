from types import SimpleNamespace

import pytest

import app.providers.bilibili as bilibili_module
from app.providers.bilibili import BilibiliProvider


class FakeMediaClient:
    def __init__(self) -> None:
        self.audio_calls = 0
        self.video_calls = 0

    async def resolve_audio(self, bvid: str, cid: int):
        self.audio_calls += 1
        return SimpleNamespace(
            url=f"https://cdn.example.test/{bvid}/{cid}.m4s",
            backup_urls=(),
            headers={},
        )

    async def resolve_video(self, bvid: str, cid: int):
        self.video_calls += 1
        return SimpleNamespace(
            url=f"https://cdn.example.test/{bvid}/{cid}.mp4",
            backup_urls=(),
            audio_url=None,
            audio_backup_urls=(),
            headers={},
        )


@pytest.fixture
def provider(tmp_path, monkeypatch) -> BilibiliProvider:
    monkeypatch.setattr(bilibili_module, "BILIBILI_CACHE_DIR", tmp_path)
    instance = BilibiliProvider()
    instance._client = FakeMediaClient()
    return instance


async def test_cache_mp3_hits_for_the_same_id(provider: BilibiliProvider, monkeypatch) -> None:
    ffmpeg_arguments = []

    async def download(_urls, _headers, target, *, label):
        target.write_bytes(b"source")

    async def convert(arguments, output_path, _error_message):
        ffmpeg_arguments.extend(arguments)
        output_path.write_bytes(b"mp3")

    monkeypatch.setattr(provider, "_download_to_file", download)
    monkeypatch.setattr(provider, "_run_ffmpeg", convert)

    first = await provider.cache_mp3("BV1fixture:123")
    second = await provider.cache_mp3("BV1fixture:123")

    assert first == second
    assert first.read_bytes() == b"mp3"
    assert provider._client.audio_calls == 1
    assert "-f" in ffmpeg_arguments
    assert ffmpeg_arguments[ffmpeg_arguments.index("-f") + 1] == "mp3"


async def test_cache_video_hits_for_the_same_id(provider: BilibiliProvider, monkeypatch) -> None:
    async def download(_urls, _headers, target, *, label):
        target.write_bytes(b"video")

    monkeypatch.setattr(provider, "_download_to_file", download)

    first = await provider.cache_video("BV1fixture:456")
    second = await provider.cache_video("BV1fixture:456")

    assert first == second
    assert first.read_bytes() == b"video"
    assert provider._client.video_calls == 1


async def test_cache_video_sets_mp4_output_format(provider: BilibiliProvider, monkeypatch) -> None:
    ffmpeg_arguments = []

    async def resolve_video(_bvid, _cid):
        return SimpleNamespace(
            url="https://cdn.example.test/video.m4s",
            backup_urls=(),
            audio_url="https://cdn.example.test/audio.m4s",
            audio_backup_urls=(),
            headers={},
        )

    async def download(_urls, _headers, target, *, label):
        target.write_bytes(b"media")

    async def convert(arguments, output_path, _error_message):
        ffmpeg_arguments.extend(arguments)
        output_path.write_bytes(b"video")

    provider._client.resolve_video = resolve_video
    monkeypatch.setattr(provider, "_download_to_file", download)
    monkeypatch.setattr(provider, "_run_ffmpeg", convert)

    path = await provider.cache_video("BV1fixture:789")

    assert path.read_bytes() == b"video"
    assert "-f" in ffmpeg_arguments
    assert ffmpeg_arguments[ffmpeg_arguments.index("-f") + 1] == "mp4"
