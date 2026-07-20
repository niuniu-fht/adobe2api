# Seedance 2.0 Fast 本机调用文档

本文档对应本项目的 Adobe Firefly 反代实现。服务地址为
`http://127.0.0.1:6001`，新请求统一使用
`sd2-fast-{时长}-{比例}-{分辨率}` 模型名。

Seedance 2.0 标准版使用 `sd2-*`，支持 1080p，详见
[`SEEDANCE2_API.md`](SEEDANCE2_API.md)。

前端固定参数模型名：

```text
sd2-fast-4s-16x9-480p
sd2-fast-4s-16x9-720p
sd2-fast-8s-9x16-480p
sd2-fast-8s-9x16-720p
```

完整格式为 `sd2-fast-{4s|6s|8s}-{16x9|9x16}-{480p|720p}`，共 12 个组合。
Fast 的 Adobe schema 当前没有 1080p，因此不注册 Fast 1080p 模型名。
`firefly-seedance2-fast` 作为旧客户端兼容别名继续保留。

新项目推荐使用官方兼容异步接口，详见
[`SEEDANCE_OFFICIAL_API.md`](SEEDANCE_OFFICIAL_API.md)。

## 实测结果

2026-07-19 先在 Chrome 的 Adobe Firefly 页面明确选择
`Seedance 2.0 Fast`，再使用与页面完全相同的参数调用本机 6001：

- 时长：4 秒
- 分辨率：480p
- 纵横比：16:9
- 音频：关闭
- seed：135790
- 提示词：`A red kite drifts above a quiet green field, soft daylight, static camera.`
- HTTP 状态：200
- 调用耗时：100.1 秒
- Adobe 任务状态：`COMPLETED`
- Adobe 上游任务 ID：`4d1f5c29-9923-4c81-bbea-dc730772fa5d`
- 结果：MP4，375,513 字节，无音频轨

实测输出：

`http://127.0.0.1:6001/generated/41150c3af5034b03a7c67ed385680550.mp4`

成片内嵌的 Adobe C2PA 清单直接标记了实际生成模型：

```text
com.adobe.details = Seedance
com.adobe.modelId = seedance
com.adobe.modelVersion = seedance_2.0_fast
claim_generator = Adobe_Firefly
```

因此本次 6001 调用实际到达 Seedance 2.0 Fast，而不是 Firefly Video、
Sora、Veo 或 Kling。Chrome 页面提交后生成了对应历史卡片，但该浏览器账号的
组织权限没有创建上游网络任务；6001 使用项目 token 对同一组参数成功完成 Adobe
异步任务。

## 接口

```text
POST /v1/chat/completions
Authorization: Bearer <service_api_key>
Content-Type: application/json
```

### 请求参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---:|---:|---|
| `model` | string | 必填 | `sd2-fast-{4s|6s|8s}-{16x9|9x16}-{480p|720p}` |
| `messages` | array | 必填 | OpenAI Chat 格式，最后一条用户消息提供提示词 |
| `generate_audio` | boolean | `true` | 是否生成同步音频 |
| `seed` | integer | 随机 | 0 到 4294967295 |
| `video_reference_mode` | string | `frame` | `frame` 为首尾帧，`image` 为普通图像参考 |
| `stream` | boolean | `false` | 返回 Chat Completions JSON 或 SSE |

提示词最大 2500 个字符。`fps` 不作为请求参数；Firefly 页面固定显示 24 FPS，
Adobe 提交载荷中不包含该字段。

### 文字生成视频

```bash
curl -X POST "http://127.0.0.1:6001/v1/chat/completions" \
  -H "Authorization: Bearer <service_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "sd2-fast-4s-16x9-480p",
    "messages": [{
      "role": "user",
      "content": "A red kite drifts above a quiet green field, soft daylight, static camera."
    }],
    "generate_audio": false,
    "seed": 135790,
    "stream": false
  }'
```

### 首尾帧生成视频

在最后一条用户消息中传入一到两张图片。第一张映射为首帧，第二张映射为
尾帧。图片 URL 也可替换为 `data:image/...;base64,...`。

```json
{
  "model": "sd2-fast-8s-16x9-720p",
  "messages": [{
    "role": "user",
    "content": [
      {"type": "text", "text": "Make a smooth transition between the two frames."},
      {"type": "image_url", "image_url": {"url": "https://HOST/first.png"}},
      {"type": "image_url", "image_url": {"url": "https://HOST/last.png"}}
    ]
  }],
  "video_reference_mode": "frame"
}
```

使用 `video_reference_mode: "image"` 时支持最多 9 张普通图像参考。需要在一次
请求中传图片、视频或音频 URL/Base64 时，使用官方兼容的 `content[]` 异步接口，
完整示例见 `SEEDANCE_OFFICIAL_API.md`。

### 响应

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "model": "sd2-fast-4s-16x9-480p",
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "```html\n<video src='http://127.0.0.1:6001/generated/RESULT.mp4' controls></video>\n```"
    },
    "finish_reason": "stop"
  }]
}
```

接口会等待 Adobe 异步任务完成后再返回。调用端超时建议设置为 720 秒以上。

## Adobe 与 Seedance 原生 API 的差别

| 能力 | Adobe Firefly 包装接口 | BytePlus Seedance 2.0 原生接口 |
|---|---|---|
| 创建任务 | `/v2/3p-videos/generate-async` | `/api/v3/contents/generations/tasks` |
| 模型标识 | `seedance` + `seedance_2.0_fast` | `model` 或 endpoint ID |
| 提示词 | 顶层 `prompt` 字符串 | `content[]` 中的 `type=text` |
| 时长 | `duration`，4-15，Firefly 默认 8 | `duration`，4-15 或 `-1`，原生默认 5 |
| 分辨率 | Fast 为 480p/720p，通过 `size` 表达 | `resolution` 为 480p/720p；Fast 不含 1080p/4k |
| 纵横比 | `generationSettings.aspectRatio` | `ratio`，另支持 `adaptive` |
| 音频 | `generateAudio` | `generate_audio` |
| seed | Adobe schema 与页面提供 `seeds` | 原生文档标注 Seedance 2.0 不支持 `seed` |
| FPS | 页面固定 24，提交体不传 `fps` | Seedance 2.0 不支持 `frames` 参数 |
| 参考素材 | 先上传为 Adobe blob，再用 `referenceBlobs` 和 `usage` | `content[]` 直接传图像、视频、音频 URL/Base64/asset ID |
| 任务查询 | 响应 header 或 `links.result` 给出轮询地址 | 创建返回任务 ID，再调用任务查询接口 |
| 额外能力 | Firefly 侧不暴露 callback/priority/watermark | 原生提供 callback、priority、watermark、超时和末帧返回 |

Adobe discovery 的 `referenceBlobs` 总数量上限为 9；其中图片最多 9 张、视频最多
3 个、音频最多 3 个，仍受总数 9 的约束。本机 6001 按该上限校验。

## 重放脚本

仓库根目录提供 `web_replay.js`：

```powershell
$env:ADOBE2API_KEY="<service_api_key>"
$env:MODEL="sd2-fast-4s-16x9-480p"
$env:GENERATE_AUDIO="false"
node .\web_replay.js "A paper boat moving through rain"
```

## 官方资料

- Adobe Firefly：[使用 Seedance 生成视频](https://helpx.adobe.com/cn/firefly/web/work-with-audio-and-video/work-with-video/generate-videos-using-seedance.html)
- BytePlus ModelArk：[Seedance 2.0 API Reference](https://docs.byteplus.com/en/docs/ModelArk/1520757)
- BytePlus ModelArk：[Retrieve a video generation task](https://docs.byteplus.com/en/docs/ModelArk/1521309)
