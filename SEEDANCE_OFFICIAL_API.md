# Seedance 官方兼容 API 用户文档

本项目在 `6001` 提供与 ModelArk Seedance 视频任务接口相同的核心调用形态：

```text
创建任务：POST /api/v3/contents/generations/tasks
查询任务：GET  /api/v3/contents/generations/tasks/{id}
```

客户端使用 `model + content[] + generate_audio` 请求结构。时长、比例和分辨率
全部编码在 `model` 名称中，后端仍通过 Adobe Firefly 生成，Adobe 上游为：

```text
POST https://firefly-3p.ff.adobe.io/v2/3p-videos/generate-async
```

这是一套“官方请求格式兼容层”，并非把请求转发到 BytePlus 的 Ark 域名。

## 实测状态

2026-07-19 使用官方格式完成 Fast 真实任务：

```text
任务 ID：b1d1a2ed2b08431ea0a5ba5d1065fd2a
状态流转：running -> succeeded
耗时：80.5 秒
调用模型：sd2-fast-4s-16x9-480p
参数：480p / 16:9 / 4 秒 / 静音
输出：450,166 字节 MP4
C2PA：com.adobe.modelVersion=seedance_2.0_fast
```

实测视频：

`http://127.0.0.1:6002/generated/b1d1a2ed2b08431ea0a5ba5d1065fd2a.mp4`

## 1. 快速开始

### 1.1 认证

两种请求头任选一种：

```text
Authorization: Bearer <service_api_key>
X-API-Key: <service_api_key>
```

服务地址：

```text
http://127.0.0.1:6002
```

### 1.2 模型

| 模型 | 推荐请求值 | Adobe 映射 |
|---|---|---|
| Seedance 2.0 | `sd2-4s-16x9-1080p` | `seedance / seedance_2.0` |
| Seedance 2.0 Fast | `sd2-fast-4s-16x9-480p` | `seedance / seedance_2.0_fast` |

新请求统一使用下面的固定参数模型名。

### 1.3 前端固定模型名

前端可以直接把时长、比例和分辨率编码在 `model` 中，不再重复传三个参数：

```text
标准版：sd2-{4s|6s|8s}-{16x9|9x16}-{720p|1080p}
Fast：  sd2-fast-{4s|6s|8s}-{16x9|9x16}-{480p|720p}
```

完整组合各 12 个。例如：

```text
sd2-4s-16x9-1080p
sd2-8s-9x16-720p
sd2-fast-4s-16x9-480p
sd2-fast-8s-9x16-720p
```

模型名是固定配置的唯一来源，新请求只传模型名即可。请求体出现
`duration`、`seconds`、`ratio`、`aspect_ratio` 或 `aspectRatio` 时返回 HTTP 400。
Fast 的 Adobe schema 当前没有 1080p，因此目录中不会注册
`sd2-fast-*-*-1080p`。

### 1.4 创建文字生成视频任务

```bash
curl -X POST "http://127.0.0.1:6002/api/v3/contents/generations/tasks" \
  -H "Authorization: Bearer <service_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "sd2-fast-4s-16x9-480p",
    "content": [
      {
        "type": "text",
        "text": "A paper boat moves across a rain puddle, soft daylight."
      }
    ],
    "generate_audio": true
  }'
```

创建成功立即返回任务 ID：

```json
{
  "id": "TASK_ID"
}
```

### 1.5 查询任务

```bash
curl "http://127.0.0.1:6002/api/v3/contents/generations/tasks/TASK_ID" \
  -H "Authorization: Bearer <service_api_key>"
```

生成中：

```json
{
  "id": "TASK_ID",
  "model": "sd2-4s-16x9-720p",
  "status": "running",
  "error": null,
  "content": null,
  "resolution": "720p",
  "ratio": "16:9",
  "duration": 4,
  "progress": 37,
  "framespersecond": 24
}
```

生成成功：

```json
{
  "id": "TASK_ID",
  "model": "sd2-4s-16x9-720p",
  "status": "succeeded",
  "error": null,
  "content": {
    "video_url": "http://127.0.0.1:6002/generated/RESULT.mp4"
  },
  "resolution": "720p",
  "ratio": "16:9",
  "duration": 4,
  "progress": 100,
  "framespersecond": 24
}
```

任务状态包括：

```text
queued -> running -> succeeded
                  -> failed
```

## 2. 比例说明

固定模型名对外提供两个比例：

```text
16x9 -> Adobe 16:9
9x16 -> Adobe 9:16
```

比例位必须写进模型名，例如：

```text
sd2-6s-16x9-720p
sd2-fast-8s-9x16-480p
```

`9x33`、`9x30`、`933`、`930` 没有对应的模型定义，提交这类模型名会返回
HTTP 400。旧基础别名仍保留原动态比例参数，仅用于已有客户端兼容。

## 3. 分辨率与能力差异

| 固定模型分辨率 | Seedance 2.0 标准版 | Seedance 2.0 Fast |
|---|---|---|
| 480p | 未注册 | 可用 |
| 720p | 可用 | 可用 |
| 1080p | 可用 | 未注册，Adobe schema 未开放 |
| 4K | 未注册 | 未注册 |

固定模型没有默认分辨率，必须从模型名选择。4K 与 Fast 1080p 都不会出现在
`/v1/models` 中。

## 4. 请求参数

| 参数 | 类型 | 默认值 | 当前行为 |
|---|---:|---:|---|
| `model` | string | 必填 | 使用第 1.3 节固定模型名，已包含时长、比例、分辨率 |
| `content` | array | 必填 | 文字、图片、视频和音频参考内容 |
| `generate_audio` | boolean | `true` | 生成同步音频 |
| `seed` | integer | 空 | Adobe 扩展，范围 0 到 4294967295 |
| `callback_url` | string | 空 | 向该 URL 推送 running 和最终状态 |
| `user` | string | 空 | 在查询和回调响应中原样返回 |
| `service_tier` | string | `default` | 当前接受 `default` |
| `watermark` | boolean | `false` | 当前接受 `false` |
| `camera_fixed` | boolean | `false` | 当前接受 `false` |
| `return_last_frame` | boolean | `false` | 当前接受 `false` |

固定模型提供 4、6、8 秒三个时长。原生文档把 Seedance 2.0 的 `seed` 标为未启用，
而 Adobe Firefly schema 和页面提供 seed；本项目将其作为 Adobe 扩展保留。

## 5. 首尾帧

```json
{
  "model": "sd2-8s-16x9-720p",
  "content": [
    {"type": "text", "text": "Make a smooth transition."},
    {
      "type": "image_url",
      "image_url": {"url": "https://HOST/first.png"},
      "role": "first_frame"
    },
    {
      "type": "image_url",
      "image_url": {"url": "https://HOST/last.png"},
      "role": "last_frame"
    }
  ]
}
```

可以只传 `first_frame`。同时传首尾帧时每种角色最多一张。

## 6. 普通图像参考

标准版和 Fast 最多接收 9 张 `reference_image`：

```json
{
  "model": "sd2-fast-4s-16x9-480p",
  "content": [
    {"type": "text", "text": "Use the visual style of these references."},
    {
      "type": "image_url",
      "image_url": {"url": "https://HOST/reference-1.png"},
      "role": "reference_image"
    }
  ],
  "generate_audio": false
}
```

首尾帧模式与普通参考图模式属于两种输入方式，同一任务中不混用。

图片值支持以下三种形式：

```text
https://HOST/reference.png
data:image/png;base64,<IMAGE_BASE64>
<IMAGE_BASE64>
```

纯 Base64 按 JPEG 处理；需要明确 PNG/WebP 类型时使用带 MIME 的 Data URL。
单张图片上限 50MB。调用方直接把值放进创建任务的 JSON，不需要先调用本机
上传接口。

## 7. 视频与音频参考

### 7.1 一次请求如何完成映射

图片、视频和音频都直接放在一次 `POST /api/v3/contents/generations/tasks`
请求中。客户端视角只有“创建任务”和后续“查询任务”两类调用。

`6001` 内部按下面的链路完成 Adobe 协议转换：

| 客户端内容 | `6001` 内部上传 | 取回的素材 ID | 生成请求映射 |
|---|---|---|---|
| `first_frame` | `POST /v2/storage/image` | `images[0].id` | `referenceBlobs[].usage=frame, order=1` |
| `last_frame` | `POST /v2/storage/image` | `images[0].id` | `referenceBlobs[].usage=frame, order=2` |
| `reference_image` | `POST /v2/storage/image` | `images[0].id` | `referenceBlobs[].usage=style` |
| `reference_video` | `POST /v2/storage/video` | `assets[0].id` | `referenceBlobs[].usage=source` + `mention.label=VideoN` |
| `reference_audio` | `POST /v2/storage/audio` | `assets[0].id` | `referenceBlobs[].usage=source` + `mention.label=AudioN` |

URL 下载和 Base64 解码都在内存中进行，参考素材不会写入本机
`data/generated`。素材 ID 上传是 Adobe 生成网关要求的内部步骤，不是调用方的
预上传流程。

`mention` 只由 `6001` 内部生成，调用方不需要填写。Adobe schema 要求其 `id`
恰好 21 个字符；`VideoN` / `AudioN` 标签用于让 Adobe 把 `source` 素材分别归类
为原生 `referenceVideos` / `referenceAudios`。

### 7.2 输入类型和限制

| `content[].type` | `role` | 数量 | URL | Data URL | 纯 Base64 |
|---|---|---:|---|---|---|
| `text` | 无 | 多段合并 | - | - | - |
| `image_url` | `first_frame` | 0-1 | 支持 | 支持 | 支持 |
| `image_url` | `last_frame` | 0-1 | 支持 | 支持 | 支持 |
| `image_url` | `reference_image` | 0-9 | 支持 | 支持 | 支持 |
| `video_url` | `reference_video` | 0-3 | 支持 | 支持 | 支持 |
| `audio_url` | `reference_audio` | 0-3 | 支持 | 支持 | 支持 |

多模态参考总数最多 9 个，即
`reference_image + reference_video + reference_audio <= 9`。音频参考需要同时有
至少一张 `reference_image` 或一个 `reference_video`。首尾帧模式和多模态参考
模式是两种独立模式，同一任务中不混用。

文件约束：

| 类型 | 大小上限 | 支持的扩展名 | 接受的 MIME |
|---|---:|---|---|
| 图片 | 每张 50MB | JPG/JPEG、PNG、WebP | `image/jpeg`、`image/png`、`image/webp` |
| 视频 | 每个 50MB | MP4、MOV、M4V | `video/mp4`、`video/quicktime`、`video/x-m4v` |
| 音频 | 每个 50MB | MP3、WAV、M4A、AAC、AIF/AIFF | `audio/mpeg`、`audio/wav`、`audio/mp4`、`audio/aac`、`audio/aiff` 及常见 `x-*` 别名 |

纯 Base64 没有 MIME 元数据，视频按 `video/mp4`、音频按 `audio/mpeg` 处理。
其他容器请使用带 MIME 的 Data URL，或让 URL 响应返回正确的 `Content-Type`。

BytePlus 原生文档的多模态组合上限可能高于 Adobe schema；Adobe discovery 对
`referenceBlobs` 的总数量上限是 9，因此本机 6001 按 Adobe 的 9 个总参考素材
执行校验，避免提交后被上游拒绝。

BytePlus 原生 `video_url` 主要描述 URL 输入；本桥接层额外接受视频 Data URL
和纯 Base64，并在内存中转换为 Adobe 视频素材。外部调用方不需要也不应该自己
构造 Adobe `assets[].id`，该 ID 只出现在网关发往 Adobe 的内部载荷中。

### 7.3 首帧 URL

```json
{
  "model": "sd2-4s-16x9-720p",
  "content": [
    {"type": "text", "text": "The camera slowly moves toward the subject."},
    {
      "type": "image_url",
      "image_url": {"url": "https://HOST/first-frame.png"},
      "role": "first_frame"
    }
  ],
  "generate_audio": true
}
```

### 7.4 首帧和尾帧，混合 URL 与 Base64

```json
{
  "model": "sd2-8s-16x9-1080p",
  "content": [
    {"type": "text", "text": "Create one continuous transition."},
    {
      "type": "image_url",
      "image_url": {"url": "https://HOST/first-frame.jpg"},
      "role": "first_frame"
    },
    {
      "type": "image_url",
      "image_url": {"url": "data:image/png;base64,<LAST_FRAME_BASE64>"},
      "role": "last_frame"
    }
  ],
  "generate_audio": false
}
```

### 7.5 多张参考图片，包含纯 Base64

```json
{
  "model": "sd2-fast-4s-16x9-480p",
  "content": [
    {"type": "text", "text": "Keep the character and visual style consistent."},
    {
      "type": "image_url",
      "image_url": {"url": "https://HOST/character.jpg"},
      "role": "reference_image"
    },
    {
      "type": "image_url",
      "image_url": {"url": "<JPEG_BASE64>"},
      "role": "reference_image"
    }
  ],
  "generate_audio": false
}
```

### 7.6 参考视频 URL

```json
{
  "model": "sd2-6s-16x9-720p",
  "content": [
    {"type": "text", "text": "Follow the camera motion and pacing."},
    {
      "type": "video_url",
      "video_url": {"url": "https://HOST/reference.mp4"},
      "role": "reference_video"
    }
  ],
  "generate_audio": true
}
```

### 7.7 参考视频 Base64

```json
{
  "model": "sd2-4s-16x9-720p",
  "content": [
    {"type": "text", "text": "Use the movement in the reference video."},
    {
      "type": "video_url",
      "video_url": {"url": "data:video/mp4;base64,<VIDEO_BASE64>"},
      "role": "reference_video"
    }
  ],
  "generate_audio": false
}
```

也可以把 `video_url.url` 直接写成纯 `<MP4_BASE64>`。

### 7.8 图片、视频、音频完整混合参考

下面是一次请求携带所有支持素材类型的完整示例：

```json
{
  "model": "sd2-8s-16x9-720p",
  "content": [
    {
      "type": "text",
      "text": "Keep the subject, follow the camera motion, and match the rhythm."
    },
    {
      "type": "image_url",
      "image_url": {"url": "https://HOST/subject.png"},
      "role": "reference_image"
    },
    {
      "type": "image_url",
      "image_url": {"url": "data:image/jpeg;base64,<STYLE_IMAGE_BASE64>"},
      "role": "reference_image"
    },
    {
      "type": "video_url",
      "video_url": {"url": "https://HOST/camera-motion.mov"},
      "role": "reference_video"
    },
    {
      "type": "video_url",
      "video_url": {"url": "data:video/mp4;base64,<SECOND_VIDEO_BASE64>"},
      "role": "reference_video"
    },
    {
      "type": "audio_url",
      "audio_url": {"url": "https://HOST/rhythm.mp3"},
      "role": "reference_audio"
    },
    {
      "type": "audio_url",
      "audio_url": {"url": "data:audio/wav;base64,<SECOND_AUDIO_BASE64>"},
      "role": "reference_audio"
    }
  ],
  "generate_audio": true,
  "seed": 2468,
  "callback_url": "https://HOST/seedance/callback",
  "user": "ORDER_ID"
}
```

该请求依然只调用一次创建接口。`6001` 完成素材转换后提交给 Adobe 的核心结构
如下：

```json
{
  "modelId": "seedance",
  "modelVersion": "seedance_2.0",
  "referenceBlobs": [
    {"id": "IMAGE_ASSET_ID_1", "usage": "style"},
    {"id": "IMAGE_ASSET_ID_2", "usage": "style"},
    {"id": "VIDEO_ASSET_ID_1", "usage": "source", "mention": {"id": "seedance-video-ref-01", "label": "Video1"}},
    {"id": "VIDEO_ASSET_ID_2", "usage": "source", "mention": {"id": "seedance-video-ref-02", "label": "Video2"}},
    {"id": "AUDIO_ASSET_ID_1", "usage": "source", "mention": {"id": "seedance-audio-ref-01", "label": "Audio1"}},
    {"id": "AUDIO_ASSET_ID_2", "usage": "source", "mention": {"id": "seedance-audio-ref-02", "label": "Audio2"}}
  ]
}
```

### 7.9 音频 Base64 的最小合法组合

音频需要图片或视频参考。下面使用一张参考图和一段纯 Base64 MP3：

```json
{
  "model": "sd2-4s-16x9-720p",
  "content": [
    {
      "type": "image_url",
      "image_url": {"url": "https://HOST/subject.jpg"},
      "role": "reference_image"
    },
    {
      "type": "audio_url",
      "audio_url": {"url": "<MP3_BASE64>"},
      "role": "reference_audio"
    }
  ],
  "generate_audio": true
}
```

### 7.10 本机实测记录

- 视频 URL 参考：任务 `7bb6c15ebaa34cb786eb0a62dabe6728`，模型
  `sd2-fast-4s-16x9-480p`，480p/16:9/4 秒，状态
  `running -> succeeded`；输出 395,415 字节，C2PA 含
  `modelVersion=seedance_2.0_fast`。
- 图片 URL + 5.65 秒 WAV Data URL：任务
  `6916fe3ade074056b62d79fecb3c7ad8`，状态 `running -> succeeded`；输出
  504,812 字节，C2PA 为 `seedance_2.0_fast`。
- 视频 URL + 5.65 秒 WAV Data URL：任务
  `1353e2e893a84a149778d4be8fdfb16f`，状态 `running -> succeeded`；输出
  767,491 字节，C2PA 为 `seedance_2.0_fast`。这条结果验证了视频和音频在同一
  创建请求中的完整混合映射。

测试中 0.25 秒系统提示音被 Seedance 原生任务判为无效输入；参考音频示例使用
可正常解码且时长合理的音频文件。

## 8. 回调

创建任务时传入：

```json
{
  "callback_url": "https://HOST/seedance/callback"
}
```

服务会用 `POST` 推送与查询接口相同的 JSON，包含 `running` 和最终状态。回调
接收端应按任务 `id` 做幂等处理。

## 9. Node.js 重放脚本

仓库根目录的 `web_replay.js` 已改为官方异步调用方式：

```powershell
$env:ADOBE2API_KEY="<service_api_key>"
$env:MODEL="sd2-4s-16x9-720p"
$env:GENERATE_AUDIO="true"
node .\web_replay.js "A paper boat moving through rain"
```

脚本会创建任务、每 2 秒查询一次，并在 `succeeded` 时输出完整任务 JSON。

## 10. 运行说明

- 任务状态保存在当前服务进程内存中，重启服务后旧任务 ID 的查询记录会清空。
- 已完成的 MP4 保存在 `data/generated`，文件清理由项目存储上限配置控制。
- 客户端轮询间隔建议 2 秒，整体超时建议 900 秒。
- 回调与轮询可以同时使用，最终结果以 `status=succeeded` 和
  `content.video_url` 为准。

## 11. 旧接口迁移

旧调用：

```text
POST /v1/chat/completions
model=sd2-4s-16x9-720p
messages=[...]
```

推荐调用：

```text
POST /api/v3/contents/generations/tasks
model=sd2-4s-16x9-720p
content=[...]
```

旧接口继续保留，方便已有 OpenAI SDK 客户端逐步迁移。

## 12. 官方资料

- BytePlus：[Create a video generation task](https://docs.byteplus.com/en/docs/ModelArk/1520757)
- BytePlus：[Retrieve a video generation task](https://docs.byteplus.com/en/docs/ModelArk/1521309)
- Adobe：[使用 Seedance 生成视频](https://helpx.adobe.com/cn/firefly/web/work-with-audio-and-video/work-with-video/generate-videos-using-seedance.html)
