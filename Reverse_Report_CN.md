# Adobe Firefly Seedance 2.0 Fast 协议分析报告

## 1. 任务摘要

- Target：`https://firefly.adobe.com/generate/video`
- Objective：确认 Firefly Seedance 2.0 Fast 参数、重建 Adobe 提交载荷并接入本机 6001
- Depth：standard
- Active trigger：两次官方页面提交动作、四次本机真实生成
- Scope：模型发现、视频提交、任务轮询、结果下载

防护定级为 **T0**：Webpack 压缩 bundle，可直接静态阅读；未发现字符串加密、
自定义 VM、WASM 核心或前端签名算法。认证使用 Adobe Bearer token 与
`x-api-key`，请求体没有额外签名。

## 2. 核心发现

1. Adobe 上游标识为 `modelId=seedance`、
   `modelVersion=seedance_2.0_fast`。
2. 提交端点为 `POST /v2/3p-videos/generate-async`，结果地址来自
   `x-override-status-link` 或 `links.result.href`。
3. Fast 支持 480p/720p、4-15 秒、六种显式比例、音频开关和图像参考。
4. Firefly 页面固定显示 24 FPS，但转换器不会把 `fps` 放入 Seedance 请求体。
5. 本机 6001 同参数实测生成成功，任务状态 `COMPLETED`；成片 C2PA 清单明确
   标记 `modelId=seedance` 与 `modelVersion=seedance_2.0_fast`。
6. Seedance 2.0 标准版使用 `modelVersion=seedance_2.0`，支持额外的 1080p；
   本机同参数实测同样为 `COMPLETED`，成片 C2PA 排除了 Fast 版本。
7. 新增官方兼容异步入口 `/api/v3/contents/generations/tasks`，Fast 实测任务
   `b1d1a2ed2b08431ea0a5ba5d1065fd2a` 按 `running -> succeeded` 完成，成片
   C2PA 为 `seedance_2.0_fast`。
8. 视频参考 URL 实测任务 `7bb6c15ebaa34cb786eb0a62dabe6728` 成功；图片加
   WAV Base64 任务 `6916fe3ade074056b62d79fecb3c7ad8` 成功；视频 URL 加
   WAV Base64 混合任务 `1353e2e893a84a149778d4be8fdfb16f` 成功。三份成片
   C2PA 均确认 `seedance_2.0_fast`。

## 3. 证据表

| 结论 | 证据锚点 |
|---|---|
| 模型 ID 与 Fast 版本 | `chunk.617fc584220a637a.min.js` 偏移约 44,036；Adobe discovery 响应 `models[17]` |
| Adobe 页面能力范围 | Adobe HelpX 页面“使用 Seedance 生成视频”，更新日期 2026-07-10 |
| 动态模型 schema | `POST https://firefly-3p.ff.adobe.io/v2/models/discovery`，body `{"filters":{"resolveSchema":true}}` |
| 请求转换逻辑 | `chunk.617fc584220a637a.min.js` 偏移约 114,600 的 `to()` 与约 128,900 的 `ty()` |
| 视频/音频素材上传 | `bundle.7929f95504df55a9.min.js` 偏移约 817,000-821,500 的 `colligoUploadVideo`、`colligoUploadAudio` |
| Chrome 页面选择 | 模型下拉框 `Seedance 2.0 Fast [selected]`；480p、16:9、4 秒、音频关闭、seed 135790 |
| 本机成功结果 | `data/request_logs.jsonl`，上游任务 `4d1f5c29-9923-4c81-bbea-dc730772fa5d`，状态 `COMPLETED` |
| 输出文件 | `data/generated/41150c3af5034b03a7c67ed385680550.mp4`，375,513 字节 |
| 实际模型证明 | 输出 MP4 的 C2PA `c2pa.actions.v2`：`com.adobe.details=Seedance`、`com.adobe.modelId=seedance`、`com.adobe.modelVersion=seedance_2.0_fast` |
| 标准版 discovery | `modelId=seedance`、`modelVersion=seedance_2.0`、状态 `HEALTHY`，size schema 额外包含 1920x1080 |
| 标准版本机结果 | `data/request_logs.jsonl`，上游任务 `f10aa6bd-f2e7-46b1-9f45-ee8a3b183189`，状态 `COMPLETED` |
| 标准版实际模型证明 | `data/generated/4fd3edb33c1e4b0da985accd1fc3e2cf.mp4` 的 C2PA：`com.adobe.modelId=seedance`、`com.adobe.modelVersion=seedance_2.0`，且不含 `seedance_2.0_fast` |

## 4. 调用链

```text
POST http://127.0.0.1:6002/v1/chat/completions
  -> 参数校验与 Adobe payload 映射
  -> POST https://firefly-3p.ff.adobe.io/v2/3p-videos/generate-async
  -> 读取 x-override-status-link / links.result.href
  -> GET Adobe job result，轮询到 COMPLETED
  -> 下载 outputs[0].video.presignedUrl
  -> 保存到 /generated/RESULT.mp4
  -> 返回 OpenAI Chat Completions JSON
```

## 5. FACTS

- Adobe discovery schema 的 Fast `size` 包含 1280x720 与 640x480 基准尺寸。
- Firefly 客户端还维护按比例展开的 480p/720p输出尺寸，并将比例写入
  `generationSettings.aspectRatio`。
- `generateAudio`、`duration`、`seeds`、`referenceBlobs` 都是 Adobe 请求字段。
- 首尾帧映射为 `usage=frame` 与 `order=1/2`；普通图像参考映射为
  `usage=style`。
- Adobe 页面分别使用 `/v2/storage/image`、`/v2/storage/video`、
  `/v2/storage/audio` 上传二进制素材；视频和音频响应素材 ID 位于
  `assets[0].id`，并以 `usage=source` 写入生成请求。bundle 中的转换器再以
  `mention.label` 的 `VideoN` / `AudioN` 前缀区分两类原生输入。
- Adobe 对该 `mention.id` 校验长度为 21 个字符；本项目生成
  `seedance-video-ref-NN` / `seedance-audio-ref-NN` 标识。
- Chrome 页面明确选择 Seedance 2.0 Fast 后已执行生成动作；页面创建了对应历史
  卡片，账号组织权限未创建上游网络任务。
- 项目 token 通过 6001 使用完全相同的参数完成真实任务，耗时 100.1 秒。
- Adobe 生成的 MP4 内嵌 C2PA 清单，模型标识与请求映射完全一致。
- Chrome 页面切换到 Seedance 2.0 标准版后显示 480p/720p/1080p，使用同一
  提示词和参数经 6001 完成真实任务，耗时 172.6 秒。

## 6. INFERENCES

- Adobe 的 `size` 主要决定分辨率档位，最终宽高还会结合
  `generationSettings.aspectRatio` 归一化。依据是请求档位为 480p，输出实际为
  864x496，与 BytePlus 官方 480p/16:9 尺寸表一致。
- Adobe 的 seed 是包装层扩展：Adobe schema 接受 `seeds`，而 BytePlus 原生文档
  明确标注 Seedance 2.0 系列不支持原生 `seed` 参数。

## 7. UNKNOWNS

- Seedance 2.0 标准版的 4k 能力存在于 BytePlus 原生接口，Adobe discovery
  当前只开放到 1080p。
- Adobe 账号 entitlement 的组织级开通规则未包含在公开页面 schema 中。

## 8. 复现步骤

1. 启动：`uvicorn app:app --host 127.0.0.1 --port 6001`
2. 设置：`$env:ADOBE2API_KEY="<service_api_key>"`
3. 执行：`node web_replay.js "A paper boat moving through rain"`
4. 检查响应中的 `/generated/*.mp4` URL。
5. 对照 `SEEDANCE2_FAST_API.md` 调整时长、比例、分辨率、音频和 seed。

## 9. 交付物

- `SEEDANCE2_FAST_API.md`
- `SEEDANCE2_API.md`
- `SEEDANCE_OFFICIAL_API.md`
- `web_replay.js`
- `Reverse_Report_CN.md`
- `tests/test_seedance2_fast.py`
