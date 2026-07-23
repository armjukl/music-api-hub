from app.providers.bilibili_client import BilibiliClient


class DashFixtureClient(BilibiliClient):
    def __init__(self) -> None:
        super().__init__(session=object())

    async def _wbi_data(self, endpoint, params):
        if params["fnval"] == 0:
            return {"timelength": 123000, "durl": []}
        return {
            "timelength": 123000,
            "dash": {
                "video": [
                    {
                        "id": 80,
                        "baseUrl": "https://cdn.example.test/720-avc.m4s",
                        "backupUrl": ["https://backup.example.test/720-avc.m4s"],
                        "bandwidth": 900000,
                        "width": 1280,
                        "height": 720,
                        "codecs": "avc1.64001f",
                        "mimeType": "video/mp4",
                    },
                    {
                        "id": 100,
                        "baseUrl": "https://cdn.example.test/1080-avc.m4s",
                        "bandwidth": 2200000,
                        "width": 1920,
                        "height": 1080,
                        "codecs": "avc1.640028",
                        "mimeType": "video/mp4",
                    },
                    {
                        "id": 120,
                        "baseUrl": "https://cdn.example.test/1440-hevc.m4s",
                        "bandwidth": 4200000,
                        "width": 2560,
                        "height": 1440,
                        "codecs": "hev1.1.6.L150",
                        "mimeType": "video/mp4",
                    },
                ],
                "audio": [
                    {
                        "id": 30280,
                        "baseUrl": "https://cdn.example.test/128.m4s",
                        "bandwidth": 128000,
                        "mimeType": "audio/mp4",
                    },
                    {
                        "id": 30216,
                        "baseUrl": "https://cdn.example.test/192.m4s",
                        "backupUrl": ["https://backup.example.test/192.m4s"],
                        "bandwidth": 192000,
                        "mimeType": "audio/mp4",
                    },
                ],
            },
        }

    async def stream_headers(self, bvid: str):
        return {"Referer": f"https://www.bilibili.com/video/{bvid}/"}


async def test_resolve_video_merges_selected_dash_tracks() -> None:
    video = await DashFixtureClient().resolve_video("BV1fixture", 123)

    assert video.url == "https://cdn.example.test/1080-avc.m4s"
    assert video.audio_url == "https://cdn.example.test/192.m4s"
    assert video.audio_backup_urls == ("https://backup.example.test/192.m4s",)
    assert video.duration_ms == 123000


async def test_search_videos_accepts_page_number() -> None:
    client = DashFixtureClient()
    requests = []

    async def search_data(endpoint, params):
        requests.append(params)
        return {"result": []}

    client._wbi_data = search_data
    await client.search_videos("夏霞", page=2)

    assert requests[0]["page"] == 2
