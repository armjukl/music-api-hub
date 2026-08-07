# Music API Hub

统一聚合 Spotify/spotDL 与 Bilibili 音频、完整视频能力的 FastAPI 服务。两个来源都作为项目内模块运行，不依赖 AstrBot 插件或单独启动的 spotDL 服务。

## 当前能力

- `GET /api/sources`：列出可用来源和能力。
- `GET /api/bilibili/search?q=歌曲名&page=2`：搜索 Bilibili 视频页并展开为可播放候选。
- `GET /api/bilibili/favorites?media_id=收藏夹ID&page=1&limit=10`：读取公开收藏夹中的视频，并返回与搜索一致的可播放候选。
- `GET /api/bilibili/audio/resolve?id=BV...:cid`：解析 Bilibili 音频，并返回实时播放地址和缓存直链。
- `GET /api/bilibili/audio/stream?id=BV...:cid`：实时使用 FFmpeg 将 Bilibili 音频转换为 MP3，不写入缓存。
- `GET /api/bilibili/audio/direct?id=BV...:cid`：先转换并按 ID 缓存 MP3，转换完成后返回本地文件。
- `GET /api/bilibili/video/resolve?id=BV...:cid`：解析 Bilibili 完整视频，并返回实时播放地址和缓存直链。
- `GET /api/bilibili/video/mp4?id=BV...:cid`：使用 `fnval=1` 请求并代理单文件 MP4；上游没有渐进式 MP4 时返回错误。
- `GET /api/bilibili/video/stream?id=BV...:cid`：实时合并并返回完整视频，不写入缓存。
- `GET /api/bilibili/video/direct?id=BV...:cid`：先合并并按 ID 缓存完整视频，合并完成后返回本地文件。
- `GET /api/spotify/search?q=歌曲名`：使用内置 spotDL 模块搜索。
- `GET /api/spotify/resolve?url=Spotify歌曲链接`：使用内置 spotDL 模块解析元数据。
- `POST /api/spotify/download?url=Spotify歌曲链接`：使用内置 spotDL 模块下载并返回文件路径。
- `GET /health`：服务健康检查。

## 模块化接口入口

新功能应使用以下规范入口，路径按“来源 / 媒体类型 / 动作”组织：

| 功能 | 规范入口 |
| --- | --- |
| 搜索 Bilibili 视频页 | `GET /api/bilibili/search?q=关键词&page=1` |
| 读取公开 Bilibili 收藏夹 | `GET /api/bilibili/favorites?media_id=收藏夹ID&page=1&limit=10` |
| 解析音频 | `GET /api/bilibili/audio/resolve?id=BV号:cid` |
| 实时音频 MP3 | `GET /api/bilibili/audio/stream?id=BV号:cid` |
| 缓存音频 MP3 | `GET /api/bilibili/audio/direct?id=BV号:cid` |
| 解析视频 | `GET /api/bilibili/video/resolve?id=BV号:cid` |
| 渐进式 MP4（fnval=1） | `GET /api/bilibili/video/mp4?id=BV号:cid` |
| 实时视频 MP4 | `GET /api/bilibili/video/stream?id=BV号:cid` |
| 缓存视频 MP4 | `GET /api/bilibili/video/direct?id=BV号:cid` |

搜索、收藏夹结果与音频解析响应中的 `stream_url`、`download_url` 会返回规范的 `audio` 路径。服务不再提供 `/api/search`、`/api/resolve` 或旧的 `/api/bilibili/resolve`、`/api/bilibili/stream`、`/api/bilibili/direct`。

新增来源模块应提供自己的 `/api/{source}/search`，并按媒体类型提供 `/api/{source}/{media_type}/{action}` 入口。

## 运行

```bash
uv sync
cp .env.example .env
uv run uvicorn app.main:app --host 0.0.0.0 --port 8787 --timeout-graceful-shutdown 5
```

Linux 和 Windows 使用同一条命令。`--timeout-graceful-shutdown 5` 用于避免实时音频/视频连接一直占用进程；按 `Ctrl+C` 后最多等待 5 秒，Uvicorn 会取消仍未结束的流任务。spotDL 会在第一次调用 Spotify 时按需初始化，Bilibili 客户端在服务启动时初始化。

Spotify 默认使用 spotDL 的免费客户端模式；如果需要 Spotify 官方 API，配置：

```bash
export SPOTIFY_USE_OFFICIAL_API=true
export SPOTIFY_CLIENT_ID=your-client-id
export SPOTIFY_CLIENT_SECRET=your-client-secret
```

项目会优先使用 `bin/ffmpeg.exe`，也可以通过 `SPOTIFY_FFMPEG_PATH` 配置 FFmpeg 路径。Linux 请将对应的可执行文件放在 `bin/ffmpeg`，或让 `ffmpeg` 位于 `PATH`。完整视频的 DASH 音视频合流需要 FFmpeg；部分 YouTube 下载场景还需要 Deno。`bin/` 下的二进制文件不提交到 Git 仓库，发布包需要另外携带，或让对应程序位于 `PATH`。项目不再需要 `astrbot_plugin_listen_music` 或 `spotify-downloader` 目录。

## 媒体接口

所有 Bilibili 媒体接口都使用 `BV号:cid` 作为 `id`，音频和视频按照相同的 `stream/direct` 规则工作：

| 类型 | 实时转换 | 缓存直链 |
| --- | --- | --- |
| 音频 MP3 | `/api/bilibili/audio/stream` | `/api/bilibili/audio/direct` |
| 完整视频 MP4 | `/api/bilibili/video/stream` | `/api/bilibili/video/direct` |

`stream` 会在请求期间解析上游地址并启动 FFmpeg，适合立即播放；请求断开后不会留下完整缓存文件。`direct` 会等待转换或合并完成，再返回本地文件，并支持浏览器的 HTTP Range 拖动播放；相同 `id` 再次请求时直接命中 `BILIBILI_CACHE_DIR`。

冒号在 URL 中应编码为 `%3A`，例如 `BVxxxx%3A123`。

## 收藏夹接口

`GET /api/bilibili/favorites` 读取公开收藏夹的一个分页。参数 `media_id` 必填，`page` 默认 `1`，`limit` 默认 `10`、最大 `20`。

上游条目的 `bvid` 和 `ugc.first_cid` 会组合为现有媒体接口可直接使用的 `BV号:cid`。当前只返回普通视频条目（`type=2`），跳过失效条目或缺少首个 `cid` 的条目；多 P 视频默认对应第一个分 P。私密收藏夹需要账号 Cookie，当前服务尚未开放。

## 统一响应

搜索和收藏夹结果会规范为：

```json
{
  "source": "bilibili",
  "id": "BVxxxx:123",
  "title": "歌曲名",
  "artist": "上传者",
  "duration_ms": 240000,
  "cover_url": null,
  "source_url": "https://www.bilibili.com/video/BVxxxx",
  "stream_url": "/api/bilibili/audio/stream?id=BVxxxx%3A123",
  "download_url": "/api/bilibili/audio/direct?id=BVxxxx%3A123"
}
```

`source_url` 是 Bilibili 视频页面。实时播放使用 `stream_url`，需要等待转换完成并复用缓存时使用 `download_url`。`audio_url` 和 `video_url` 可能带短时效签名，不要长期保存或直接交给浏览器。

视频解析响应另外包含 `mp4_url`，它是使用 `fnval=1` 获取的 Bilibili 上游渐进式 MP4 临时直链。该地址只在上游返回单文件 MP4 时可用，并可能因签名过期或 Referer 限制失效；需要稳定访问时应使用本服务的 `stream_url` 或 `download_url`。`/api/bilibili/video/mp4` 是同一模式的后端代理入口。

完整视频同样使用返回结果中的 `stream_url`，例如：

```html
<video controls src="http://127.0.0.1:8787/api/bilibili/video/stream?id=BVxxxx%3A123"></video>
```

需要先缓存再播放时，将地址改为 `/api/bilibili/video/direct?id=BVxxxx%3A123`。

## 缓存与配置

- 缓存目录默认是 `downloads/bilibili`，可通过 `BILIBILI_CACHE_DIR` 修改。
- MP3 缓存码率默认是 `320 kbps`，可通过 `BILIBILI_MP3_BITRATE` 配置，范围为 `32` 到 `320`。
- 同一个 `id` 的音频和视频使用不同缓存文件；音频缓存码率变化后会生成新的音频缓存。
- 音频与视频解析响应中的上游媒体地址可能过期，客户端应优先使用响应中的 `stream_url` 或 `download_url`。

`/api/bilibili/video/stream` 表示实时合流；需要缓存视频时请使用 `/api/bilibili/video/direct`。

请遵守 Bilibili 的服务条款与版权规定。

## 开发文档

- [Bilibili 接口实现分析](docs/bilibili-api-analysis.md)
