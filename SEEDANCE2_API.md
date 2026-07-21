# Seedance 2.0 标准版本机调用文档

本文档对应 Adobe Firefly 的 Seedance 2.0 标准版接入。服务地址为
`http://127.0.0.1:6002`，新请求统一使用 `sd2-{时长}-{比例}-{分辨率}` 模型名。

新项目推荐使用官方兼容异步接口，详见
[`SEEDANCE_OFFICIAL_API.md`](SEEDANCE_OFFICIAL_API.md)。本文件中的
`/v1/chat/completions` 保留为旧客户端兼容入口。

## 模型映射

```text
前端模型：sd2-{4s..15s}-{16x9|9x16}-{720p|1080p}
Adobe modelId：seedance
Adobe modelVersion：seedance_2.0
Adobe 创建任务：POST https://firefly-3p.ff.adobe.io/v2/3p-videos/generate-async
```

Fast 使用 `sd2-fast-*`，对应上游版本 `seedance_2.0_fast`。

前端固定参数模型名：

```text
sd2-4s-16x9-720p
sd2-4s-16x9-1080p
sd2-6s-9x16-720p
sd2-8s-9x16-1080p
```

完整格式为 `sd2-{4s..15s}-{16x9|9x16}-{720p|1080p}`，共 48 个组合。
时长、比例和分辨率由模型名固定。请求体不接收时长或比例字段。

## 实测结果

2026-07-19 在 Chrome 中明确选择 `Seedance 2.0` 后，使用兼容别名验证 Adobe
上游链路（新接入请使用固定模型名）调用
本机 6001：

- 提示词：`A white ceramic cup rests on a wooden table as morning sunlight moves slowly across the surface, static camera.`
- 时长：4 秒
- 分辨率：480p
- 纵横比：16:9
- 音频：关闭
- seed：246810
- HTTP 状态：200
- 调用耗时：172.6 秒
- Adobe 任务状态：`COMPLETED`
- Adobe 上游任务 ID：`f10aa6bd-f2e7-46b1-9f45-ee8a3b183189`
- 输出：MP4，367,725 字节

实测输出：

`http://127.0.0.1:6002/generated/4fd3edb33c1e4b0da985accd1fc3e2cf.mp4`

成片内嵌 Adobe C2PA 清单：

```text
com.adobe.details = Seedance
com.adobe.modelId = seedance
com.adobe.modelVersion = seedance_2.0
claim_generator = Adobe_Firefly
```

成片中不含 `seedance_2.0_fast`，因此本次调用实际使用标准版。

## 接口

```text
POST /v1/chat/completions
Authorization: Bearer <service_api_key>
Content-Type: application/json
```

### 请求参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---:|---:|---|
| `model` | string | 必填 | `sd2-{4s..15s}-{16x9|9x16}-{720p|1080p}` |
| `messages` | array | 必填 | OpenAI Chat 格式，最后一条用户消息提供提示词和参考图 |
| `generate_audio` | boolean | `true` | 是否生成同步音频 |
| `seed` | integer | 随机 | 0 到 4294967295 |
| `video_reference_mode` | string | `frame` | `frame` 为首尾帧，`image` 为普通图像参考 |
| `stream` | boolean | `false` | 返回 Chat Completions JSON 或 SSE |

提示词最大 2500 个字符。Firefly 页面固定使用 24 FPS，Adobe 提交载荷中不传
`fps`。

### curl 示例

```bash
curl -X POST "http://127.0.0.1:6002/v1/chat/completions" \
  -H "Authorization: Bearer <service_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "sd2-4s-16x9-720p",
    "messages": [{
      "role": "user",
      "content": "A white ceramic cup rests on a wooden table as morning sunlight moves slowly across the surface, static camera."
    }],
    "generate_audio": false,
    "seed": 246810,
    "stream": false
  }'
```

接口等待 Adobe 异步任务完成后返回，调用端超时建议设置为 900 秒。

### 响应

```json
{
  "object": "chat.completion",
  "model": "sd2-4s-16x9-720p",
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "```html\n<video src='http://127.0.0.1:6002/generated/RESULT.mp4' controls></video>\n```"
    },
    "finish_reason": "stop"
  }]
}
```

## 参考图

最后一条用户消息可传一到两张图片；默认映射为首帧和尾帧：

```json
{
  "model": "sd2-8s-16x9-1080p",
  "messages": [{
    "role": "user",
    "content": [
      {"type": "text", "text": "Make a smooth transition."},
      {"type": "image_url", "image_url": {"url": "https://HOST/first.png"}},
      {"type": "image_url", "image_url": {"url": "https://HOST/last.png"}}
    ]
  }],
  "video_reference_mode": "frame"
}
```

`video_reference_mode: "image"` 支持最多 9 张普通图像参考。

## 标准版与 Fast

| 项目 | Seedance 2.0 | Seedance 2.0 Fast |
|---|---|---|
| 固定模型格式 | `sd2-{duration}-{ratio}-{resolution}` | `sd2-fast-{duration}-{ratio}-{resolution}` |
| Adobe 版本 | `seedance_2.0` | `seedance_2.0_fast` |
| Adobe 能力 | 480p/720p/1080p | 480p/720p |
| Firefly 480p/4 秒积分 | 160 | 120 |
| 时长、比例、音频、参考图 | 相同 | 相同 |

## 重放脚本

```powershell
$env:ADOBE2API_KEY="<service_api_key>"
$env:MODEL="sd2-4s-16x9-720p"
$env:GENERATE_AUDIO="false"
$env:SEED="246810"
node .\web_replay.js "A white ceramic cup on a wooden table"
```

## 官方资料

- Adobe Firefly：[使用 Seedance 生成视频](https://helpx.adobe.com/cn/firefly/web/work-with-audio-and-video/work-with-video/generate-videos-using-seedance.html)
- BytePlus ModelArk：[Seedance 2.0 API Reference](https://docs.byteplus.com/en/docs/ModelArk/1520757)
