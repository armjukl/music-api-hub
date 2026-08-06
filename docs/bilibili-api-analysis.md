# Bilibili 接口实现分析

本文基于当前项目代码，说明 Bilibili 请求是如何从 FastAPI 进入客户端、解析成统一模型，再交给 FFmpeg 或本地缓存的。同时列出 Bilibili 还可以扩展获取的常见信息。

## 1. 当前架构

```text
FastAPI 路由
    |
    v
BilibiliProvider
    |  搜索、收藏夹、解析、实时流、缓存、FFmpeg
    v
BilibiliClient
    |  WBI 签名、HTTP、Cookie、响应解析
    v
Bilibili Web API
    |
    +-- 搜索/详情/播放地址
    +-- WBI 签名元数据
    +-- 二维码登录和账号状态
```

主要代码位置：

- [app/main.py](../app/main.py)：FastAPI 对外路由和统一响应地址。
- [app/providers/bilibili.py](../app/providers/bilibili.py)：业务编排、FFmpeg、实时代理和文件缓存。
- [app/providers/bilibili_client.py](../app/providers/bilibili_client.py)：Bilibili HTTP 客户端、WBI 签名和字段解析。
- [app/providers/bilibili_models.py](../app/providers/bilibili_models.py)：搜索候选、内部播放数据、二维码会话等数据类。
- [app/models.py](../app/models.py)：FastAPI 对外暴露的统一模型。

## 2. 对外 API

所有 Bilibili 媒体接口都使用 `BV号:cid` 作为 `id`。冒号放在 URL 中时应编码为 `%3A`。

| 接口 | 作用 | 是否缓存 |
| --- | --- | --- |
| `GET /api/bilibili/search?q=...` | 搜索并展开为可播放的页面候选 | 不缓存搜索结果 |
| `GET /api/bilibili/favorites?media_id=...` | 读取公开收藏夹的一页视频候选 | 不缓存收藏夹结果 |
| `GET /api/bilibili/audio/resolve?id=BV...:cid` | 解析音频信息并返回 `stream_url`、`download_url` | 只解析，不生成文件 |
| `GET /api/bilibili/audio/stream?id=BV...:cid` | 实时将音频转为 MP3 | 不写入完整文件 |
| `GET /api/bilibili/audio/direct?id=BV...:cid` | 转换音频后返回本地 MP3 | 按 ID 缓存 |
| `GET /api/bilibili/video/resolve?id=BV...:cid` | 解析完整视频信息 | 只解析，不生成文件 |
| `GET /api/bilibili/video/stream?id=BV...:cid` | 实时合并或代理完整视频 | 不写入完整文件 |
| `GET /api/bilibili/video/direct?id=BV...:cid` | 合并完整视频后返回本地 MP4 | 按 ID 缓存 |

解析响应中的地址含义：

```json
{
  "stream_url": "/api/bilibili/audio/stream?id=BVxxxx%3A123",
  "download_url": "/api/bilibili/audio/direct?id=BVxxxx%3A123"
}
```

视频解析响应的地址对应 `/api/bilibili/video/stream` 和 `/api/bilibili/video/direct`。

`stream` 适合尽快开始播放。它会在请求期间解析临时地址并启动 FFmpeg，客户端断开后进程会被关闭。`direct` 会等待下载、转换或合流完成，然后以分段文件响应返回；同一 `id` 再次请求时直接读取缓存文件，并支持单段 HTTP Range 请求，浏览器可以拖动进度。开发环境建议使用 Uvicorn 的有限优雅退出时间，避免实时流连接让进程无限等待：`--timeout-graceful-shutdown 5`。

收藏夹接口使用 `media_id`、`page` 和 `limit` 参数，其中 `limit` 最大为 `20`。返回的条目同样是 `UnifiedTrack`，并附带音频 `stream_url`、`download_url`。当前只读取公开收藏夹中的普通视频条目；私密收藏夹和写入操作需要登录，尚未通过本服务开放。

## 3. Bilibili 内部接口

当前客户端使用的上游接口如下：

| 上游接口 | 当前用途 | WBI |
| --- | --- | --- |
| `/x/web-interface/nav` | 获取 WBI 图片元数据；也用于读取账号登录状态 | 否 |
| `/x/web-interface/wbi/search/type` | 搜索视频 | 是 |
| `/x/web-interface/wbi/view` | 获取视频详情、分 P 页面和上传者 | 是 |
| `/x/player/wbi/playurl` | 获取音频、视频、DASH 和渐进式播放地址 | 是 |
| `/x/v3/fav/resource/list` | 获取公开收藏夹的一页内容 | 否 |
| `/x/passport-login/web/qrcode/generate` | 生成二维码登录会话 | 否 |
| `/x/passport-login/web/qrcode/poll` | 查询二维码扫码状态 | 否 |

搜索请求目前使用这些参数：

```text
search_type=video
keyword=<关键词>
order=totalrank
duration=0
tids=0
page=<页码>
page_size=<数量>
```

视频详情使用 `bvid` 或 `aid`。播放地址使用 `qn=80`、`platform=pc`，并通过 `fnval` 选择格式：

- `fnval=0`：优先请求单个渐进式文件，用于可以直接代理的完整 MP4。
- `fnval=16`：请求 DASH，得到独立的视频轨和音频轨，需要 FFmpeg 合并。

## 4. WBI 签名流程

Bilibili 的搜索、详情和播放地址接口需要 WBI 参数签名，当前实现流程如下：

1. 请求 `/x/web-interface/nav`，读取 `data.wbi_img.img_url` 和 `data.wbi_img.sub_url`。
2. 分别取两个 URL 的文件名，去掉扩展名后拼接。
3. 按 Bilibili 的固定索引表抽取字符，取前 32 个字符作为 mixin key。
4. 清理参数值中的特殊字符，按参数名排序并加入 `wts` 时间戳。
5. 对排序后的查询字符串加上 mixin key，计算 MD5 得到 `w_rid`。
6. 将签名参数作为查询参数发送请求。

实现位于 `derive_wbi_mixin_key()`、`sign_wbi_params()` 和 `_wbi_data()`。

当前还有两层稳定性处理：

- WBI key 默认缓存 10 分钟，并使用锁避免并发请求重复刷新。
- 上游返回 `-403` 或 `-352` 时会刷新 key 并重试一次。

WBI key、`w_rid` 和播放 URL 都不应写入长期日志。播放 URL 通常带短时效签名。

## 5. 搜索、收藏夹和详情链路

### 搜索

`BilibiliProvider.search()` 先调用客户端的 `search_videos()`，拿到视频级搜索结果。客户端目前只保留搜索结果中的：

- `bvid`
- 清理 HTML 后的 `title`

Provider 随后最多并发 5 个详情请求，调用 `get_video()` 获取每个视频的页面列表，再将每个页面展开成一个 `BV号:cid` 候选。因此多 P 视频会产生多个可播放结果。

对外的 `UnifiedTrack` 当前包含：

```text
source, id, title, artist, album, duration_ms,
cover_url, source_url, playable, stream_url, download_url
```

Bilibili 的 `artist` 当前实际填的是上传者。上传者不一定是音乐作品的真实艺术家，这是数据语义上的限制。

`cover_url` 来自详情接口的 `pic` 字段；`published_at` 来自 `pubdate` 时间戳，以 UTC 输出为 ISO 8601。

### 视频详情

`/x/web-interface/wbi/view` 的详情解析目前保留：

- 视频：`bvid`、`title`、`uploader`
- 页面：`cid`、页码、分 P 标题、时长

Provider 再生成页面地址 `https://www.bilibili.com/video/<bvid>/?p=<page>`，并为每个页面生成两个本地 API 地址。

### 收藏夹

`BilibiliProvider.favorites()` 调用 `/x/v3/fav/resource/list`，使用 `media_id`、`pn`、`ps` 和 `platform=web` 请求一个分页。该接口的普通视频条目包含 `bvid`、标题、UP 主、封面、时长、发布时间，以及嵌套字段 `ugc.first_cid`。

客户端将 `bvid` 和 `ugc.first_cid` 直接组合为 `BV号:cid`，因此不需要为收藏夹中的每项额外请求视频详情。对外结果与搜索结果使用相同的 `UnifiedTrack` 模型和音频播放地址。

`ugc.first_cid` 只代表第一个分 P。当前路由不展开多 P 视频，并跳过非视频条目、失效条目或缺少该字段的条目；如需完整分 P 列表，应再使用 `/x/web-interface/wbi/view` 查询对应 `bvid`。

## 6. 音频播放地址链路

客户端调用 `/x/player/wbi/playurl`，优先读取 `dash.audio`：

1. 过滤没有有效 URL 的音频轨。
2. 优先选择不超过 192 kbps 的最高码率。
3. 如果所有轨道都高于 192 kbps，则选择最低可用码率。
4. 保存主 URL、备用 URL、MIME 类型、时长和请求头。

如果没有 DASH 音频，则只接受一个渐进式 `durl` 项作为兼容回退。多个 `durl` 段不会被当作单个完整音频直接返回。

实时音频接口使用 FFmpeg：

```text
上游音频流 -> FFmpeg libmp3lame 192k -> HTTP audio/mpeg
```

缓存音频接口则是：

```text
上游音频流 -> 临时源文件 -> FFmpeg -> 临时 MP3 -> 原子替换为缓存文件
```

实时转换码率目前固定为 192 kbps；缓存 MP3 使用 `BILIBILI_MP3_BITRATE`，默认 320 kbps。

## 7. 视频播放地址链路

客户端先用 `fnval=0` 请求渐进式地址。如果返回的 `durl` 只有一个完整文件，直接使用它；否则再次用 `fnval=16` 请求 DASH。

DASH 视频轨选择规则：

1. 有 H.264/AVC 时优先使用 AVC，避免浏览器对 HEVC 的兼容问题。
2. 在候选中选择分辨率、带宽和轨道 ID 最高的轨道。

DASH 音频轨沿用音频的 192 kbps 选择策略。

实时视频有两种结果：

- 渐进式单文件：向上游代理请求，保留 `Range`、`Content-Length` 和 `Content-Range`。
- DASH 分离流：FFmpeg 同时读取视频和音频，复制编码后以 fragmented MP4 从 stdout 输出。

缓存视频时，DASH 分离流会先分别下载视频和音频，再用 FFmpeg `-c:v copy -c:a copy` 合并，并通过 `+faststart` 优化 MP4。缓存使用按路径的异步锁和临时文件，完成后用 `os.replace()` 原子替换，避免把半成品暴露给读取方。

## 8. 当前还可以获取的信息

下面这些信息通常可以从 Bilibili 接口获得，但当前项目尚未完整映射到统一响应。

| 信息 | 常见来源 | 可用于 |
| --- | --- | --- |
| 简介、版权类型 | `/x/web-interface/wbi/view` | 丰富搜索结果和详情页 |
| `aid`、分区 ID/名称、视频总页数 | `/x/web-interface/wbi/view` | 支持 AV/BV 双 ID、分类展示 |
| 播放量、点赞、投币、收藏、分享、评论数、弹幕数 | 详情响应中的 `stat` | 热度排序和详情展示 |
| 上传者 UID、头像、签名 | 详情响应中的 `owner`，或用户空间接口 | 作者信息和头像 |
| 标签 | `/x/tag/archive/tags?bvid=...` 等标签接口 | 标签搜索和分类 |
| 相关推荐 | `/x/web-interface/archive/related?bvid=...` | 推荐列表 |
| 支持的画质和格式 | `/x/player/wbi/playurl` | 让调用方选择 360p/720p/1080p 等 |
| 所有 DASH 音频码率 | 播放响应中的 `dash.audio` | 让用户选择音质 |
| 所有 DASH 视频轨道、分辨率、编码、帧率 | 播放响应中的 `dash.video` | 让用户选择清晰度和兼容编码 |
| Dolby、Hi-Res、无损等能力标记 | 播放响应中的格式和音频轨字段 | 音质能力展示 |
| 字幕列表和字幕地址 | `/x/player/v2` 或播放器相关接口 | 字幕选择、下载或转写 |
| 视频章节、互动节点、看点 | 播放器 v2 或互动视频接口 | 播放进度和章节导航 |
| 弹幕 | `/x/v2/dm/web/seg.so` 等弹幕接口 | 弹幕列表或实时弹幕 |
| 评论和楼中楼 | `/x/v2/reply/main` 等评论接口 | 评论展示和搜索 |
| 登录状态、用户昵称、头像 | 当前已使用的 `/x/web-interface/nav` | 账号状态显示 |
| 私密收藏夹、历史、点赞、投币、关注 | 用户相关接口 | 需要登录的个人功能 |

以上接口的字段和权限可能随 Bilibili 调整。特别是字幕、评论、弹幕和个人操作通常有登录要求、频率限制或额外签名参数，不能仅凭匿名请求保证可用。

## 9. 推荐扩展顺序

### 第一阶段：丰富现有结果

`cover_url` 和 `published_at` 已填充。接下来可以在 `BilibiliVideo` 和 `UnifiedTrack` 继续增加以下稳定字段：

- `aid`
- `description`
- `owner_id`
- `view_count`
- `danmaku_count`
- `comment_count`
- `like_count`
- `tags`

这些字段主要来自搜索和详情响应，不改变播放链路，风险较低。

### 第二阶段：播放选项

把当前客户端内部已经解析的 DASH 轨道转换为公开的 `AudioFormat` 和 `VideoFormat` 模型，让调用方可以请求指定音质、分辨率或编码。当前固定选择规则适合简单播放，但不适合高清下载或带宽自适应。

### 第三阶段：字幕、章节和相关推荐

这些信息适合做独立的只读接口，例如：

```text
GET /api/bilibili/details?id=BV...:cid
GET /api/bilibili/tags?id=BV...:cid
GET /api/bilibili/related?id=BV...:cid
GET /api/bilibili/subtitles?id=BV...:cid
```

建议不要把这些字段全部塞进 `/resolve`，否则播放解析会同时承担大量元数据请求。

### 第四阶段：账号功能

底层客户端已经有二维码生成、轮询、取消和账号 profile 方法，但当前 FastAPI 没有暴露这些路由，而且 `BilibiliProvider.start()` 也没有注入持久化的 `credentials_getter`。如果接入账号功能，需要先设计 Cookie 的安全存储、过期处理和多用户隔离，再开放接口。

## 10. 当前限制和注意事项

- `artist` 是上传者近似值，不应标记为官方音乐艺术家。
- 收藏夹路由只支持公开收藏夹，且仅使用每个视频的 `ugc.first_cid`；多 P 视频不会展开为多个候选。
- `cover_url`、统计数据、标签和搜索原始字段目前会被丢弃。
- WBI key 会过期，播放 URL 也会过期；缓存直链应使用本服务的 `direct` 地址。
- 缓存目前没有 TTL、大小上限或清理任务，长期运行需要增加磁盘回收策略。
- 同一 ID 的缓存键还包含媒体类型，音频缓存还包含码率；修改缓存码率后会产生新的音频文件。
- 实时视频的 DASH 输出是 fragmented MP4，适合边下边播；需要稳定可拖动的文件应使用 `video/direct`。
- 上游接口属于平台内部 Web API，字段、签名规则、访问权限和频率限制都可能变化；请求失败时应保留降级和重试策略。
- 不应在日志、错误响应或前端长期保存 Cookie、`w_rid` 和带签名的上游 URL。
