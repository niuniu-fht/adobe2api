# Grok 视频异步兼容接口

本项目在 `6001` 提供 xAI Grok Imagine Video 的异步请求形态，底层仍使用已接入的
Adobe Seedance 视频桥接。接口路径、`request_id` 和查询响应按 xAI 视频协议组织。
`model` 可以使用 Grok 名称，也可以直接使用项目固定模型名：

```text
grok-imagine-video
grok-imagine-video-1.5
sd2-{4s|6s|8s}-{16x9|9x16}-{720p|1080p}
sd2-fast-{4s|6s|8s}-{16x9|9x16}-{480p|720p}
```

xAI 官方协议参考：
[Video generation](https://docs.x.ai/developers/model-capabilities/video/generation) |
[REST API reference](https://docs.x.ai/developers/rest-api-reference/inference/videos)

## 创建任务

```text
POST http://127.0.0.1:6001/v1/videos/generations
Authorization: Bearer <service_api_key>
Content-Type: application/json
```

也兼容 `POST /v1/videos`。

```json
{
  "model": "sd2-fast-8s-16x9-480p",
  "prompt": "A paper boat moves across a rain puddle, soft daylight.",
  "generate_audio": true
}
```

创建接口立即返回：

```json
{
  "request_id": "REQUEST_ID"
}
```

请求体支持 `duration` 或 `seconds`，时长范围为 4 到 15 秒；支持的比例为
`16:9`、`9:16`、`4:3`、`3:4`、`1:1`；分辨率支持 `480p`、`720p`、`1080p`。
1080p 会映射到 Adobe Seedance 2.0 标准版，480p/720p 映射到 Seedance 2.0 Fast。

## 图像输入

首帧使用 `image`，值可为公开 URL 或图片 Data URL：

```json
{
  "model": "sd2-8s-16x9-720p",
  "prompt": "Animate this still image with a slow camera move.",
  "image": {
    "url": "https://HOST/first.png"
  }
}
```

普通参考图使用 `reference_images`，最多 9 张：

```json
{
  "model": "sd2-fast-4s-9x16-480p",
  "prompt": "Keep the character design and visual style consistent.",
  "reference_images": [
    {"url": "https://HOST/style.png"},
    {"url": "data:image/png;base64,<IMAGE_BASE64>"}
  ],
  "generate_audio": false
}
```

`image` 和 `reference_images` 同时出现时返回参数错误。`file_id` 需要 xAI Files
服务，本机桥接直接使用 URL/Data URL，因此请求中使用 `url`。`933`、`930` 及
`9:33`、`9:30` 没有对应的 Seedance 固定模型定义，会返回参数错误。

## 查询任务

```text
GET http://127.0.0.1:6001/v1/videos/REQUEST_ID
Authorization: Bearer <service_api_key>
```

生成中：

```json
{
  "status": "pending",
  "progress": 37,
  "model": "grok-imagine-video-1.5"
}
```

生成成功：

```json
{
  "status": "done",
  "progress": 100,
  "model": "grok-imagine-video-1.5",
  "video": {
    "url": "http://127.0.0.1:6001/generated/REQUEST_ID.mp4",
    "duration": 8,
    "respect_moderation": true
  }
}
```

失败：

```json
{
  "status": "failed",
  "error": {
    "code": "service_unavailable",
    "message": "video generation failed"
  }
}
```

## 后台实时日志

管理后台请求日志使用 SSE 长连接：

```text
GET /api/v1/logs/stream
```

连接建立后每秒推送一次当前运行任务，事件格式为：

```text
event: logs
data: {"items":[...],"total":1,"ts":1784467000}
```

运行中的日志会持续显示：

- 当前状态 `进行中`
- Adobe 上游任务 ID
- 已运行秒数
- 当前进度百分比

Adobe 未返回数值进度时，服务按轮询耗时提供单调递增的近似进度；最终以
`status=done` 或 `status=failed` 为准。任务完成后，运行日志从 SSE 列表移入历史日志。
