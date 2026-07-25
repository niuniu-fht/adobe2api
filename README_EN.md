# adobe2api

---

### ✨ Ad Spot (o゜▽゜)o☆

This is my independently built and actively maintained personal website: [**Pixelle Labs**](https://www.pixellelabs.com/)

I share **AI creative tools**, image/video mini-products, and fun experiments here. You are welcome to explore, try everything out, and play around (๑•̀ㅂ•́)و✧. Feedback, ideas, and collaboration discussions are always appreciated! ヾ(≧▽≦*)o

---

Adobe Firefly/OpenAI-compatible gateway service.

Chinese README: `README.md`


Current design:

- External unified entry: `/v1/chat/completions` (image + video)
- Optional image-only endpoint: `/v1/images/generations`
- Token pool management (manual token + auto-refresh token)
- Admin web UI: token/config/logs/refresh profile import

## 1) Deployment

### A. Local Run

1. **Install dependencies**:

```bash
pip install -r requirements.txt
```

2. **Start service** (run in `adobe2api/`):

```bash
uvicorn app:app --host 0.0.0.0 --port 6002 --reload
```

3. **Access Admin UI**:

- URL: `http://127.0.0.1:6002/`
- Default login: `admin / admin`
- You can change credentials in "系统配置" (System Config) or edit `config/config.json`

### B. Docker Deployment (Recommended)

This project provides Docker support. It is recommended to use Docker Compose for one-click deployment:

```bash
docker compose up -d --build
```

The video branch Compose stack uses host port `6002` and persists state in
`video-data/` and `video-config/`. Override the host port with `VIDEO_PORT`; the
container continues to listen on `6001`, so this stack can run beside the main branch.

## 2) Auth to this service

Service API key is configured in `config/config.json` (`api_key`). With this
branch's Compose stack, the host-side file is `video-config/config.json`.

- If set, call with either:
  - `Authorization: Bearer <api_key>`
  - `X-API-Key: <api_key>`

Admin UI and admin APIs require login session cookie via `/api/v1/auth/login`.

## 3) External API usage

### 3.0 Supported model families

Current supported model families are:

- `nano-banana-*` (image, maps to upstream `nano-banana-2`)
- `nano-banana2-*` (image, maps to upstream `nano-banana-3`)
- `nano-banana-pro-*` (image)
- `gpt-image-*` (image, maps to upstream `gpt-image:2`)
- `seedance2` / `seedance2-fast` (ByteDance video)
- `sora2` / `sora2-pro` (OpenAI video)
- `veo31` / `veo31-ref` / `veo31-fast` (Google video)
- `kling3` / `kling-o3` (Kling video)

Nano Banana image models (`nano-banana-2`):

- Pattern: `nano-banana-{resolution}-{ratio}`
- Resolution: `1k` / `2k` / `4k`
- Ratio suffix: `1x1` / `16x9` / `9x16` / `4x3` / `3x4`
- Current implementation supports `1K` / `2K` / `4K`
- Examples:
  - `nano-banana-2k-16x9`
  - `nano-banana-4k-1x1`

Nano Banana 2 image models (`nano-banana-3`):

- Pattern: `nano-banana2-{resolution}-{ratio}`
- Resolution: `1k` / `2k` / `4k`
- Ratio suffix: `1x1` / `16x9` / `9x16` / `4x3` / `3x4` / `1x8` / `1x4` / `4x1` / `8x1`
- Nano Banana 2 additionally supports ultra-wide/tall ratios: `1:8` / `1:4` / `4:1` / `8:1`
- Current implementation supports `1K` / `2K` / `4K`
- Examples:
  - `nano-banana2-2k-16x9`
  - `nano-banana2-4k-1x1`
  - `nano-banana2-2k-1x8`
  - `nano-banana2-2k-8x1`

Nano Banana Pro image models (legacy-compatible):

- Pattern: `nano-banana-pro-{resolution}-{ratio}`
- Resolution: `1k` / `2k` / `4k`
- Ratio suffix: `1x1` / `16x9` / `9x16` / `4x3` / `3x4`
- Does not include Nano Banana 2's extra `1:8` / `1:4` / `4:1` / `8:1` ratios
- Current implementation supports `1K` / `2K` / `4K`
- Examples:
  - `nano-banana-pro-2k-16x9`
  - `nano-banana-pro-4k-1x1`

GPT Image models (experimental):

- Pattern: `gpt-image-{resolution}-{ratio}`
- Resolution: `1k` / `2k` / `4k`
- Ratio suffix: `1x1` / `5x4` / `9x16` / `21x9` / `16x9` / `4x3` / `3x2` / `4x5` / `3x4` / `2x3`
- The implementation sends both `outputResolution` and the mapped pixel `size`
- GPT Image quality is controlled by system config `gpt_image_quality`: `low` / `medium` / `high`, default `low`
- Examples:
  - `gpt-image-2k-16x9`
  - `gpt-image-4k-1x1`
  - `gpt-image-2k-21x9`

About `auto`:

- Current implementation does **not** support `aspect_ratio=auto`
- If `auto` is sent, the service falls back to `1:1`
- Prefer sending an explicit ratio or using a model ID with a ratio suffix

Video models now use one public ID per model family. Duration, ratio, and
resolution are request parameters. Legacy combination IDs remain available as
hidden compatibility aliases and are omitted from `/v1/models`.

| Model ID | Duration (seconds) | Ratios | Resolutions | References |
| --- | --- | --- | --- | --- |
| `seedance2` | `4-15` | `adaptive`, `21:9`, `16:9`, `4:3`, `1:1`, `3:4`, `9:16` | `480p`, `720p`, `1080p` | 9 images, 3 videos, 3 audios; 9 total |
| `seedance2-fast` | `4-15` | Same as above | `480p`, `720p` | 9 images, 3 videos, 3 audios; 9 total |
| `sora2` / `sora2-pro` | `4`, `8`, `12` | `16:9`, `9:16` | `720p` | 1 image |
| `veo31` / `veo31-fast` | `4`, `6`, `8` | `16:9`, `9:16` | `720p`, `1080p` | Up to 2 frame images |
| `veo31-ref` | `4`, `6`, `8` | `16:9`, `9:16` | `720p`, `1080p` | Up to 3 reference images |
| `kling3` | `5`, `10`, `15` | `16:9`, `9:16` | `720p` | Up to 2 frame images |
| `kling-o3` | `5`, `15` | `16:9`, `9:16` | `1080p` | Up to 2 frame images |

Unified asynchronous video protocol:

```text
POST /v1/video/generations
GET  /v1/video/generations/{task_id}
GET  /v1/videos/{task_id}/content
```

Create a video:

```bash
curl -X POST "http://127.0.0.1:6002/v1/video/generations" \
  -H "Authorization: Bearer <service_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "seedance2",
    "prompt": "@Image1 is the main character; keep the character consistent",
    "images": ["https://example.com/character.jpg"],
    "videos": [],
    "audios": [],
    "ratio": "16:9",
    "duration": 15,
    "resolution": "720p",
    "watermark": false
  }'
```

The create response includes `task_id`. Query status is `queued`, `processing`,
`completed`, or `failed`. After completion, play or download the content:

```bash
curl "http://127.0.0.1:6002/v1/video/generations/<task_id>" \
  -H "Authorization: Bearer <service_api_key>"

curl -L "http://127.0.0.1:6002/v1/videos/<task_id>/content" \
  -H "Authorization: Bearer <service_api_key>" \
  -o result.mp4
```

Array order maps `images`, `videos`, and `audios` to `@Image1`, `@Video1`, and
`@Audio1` in the prompt. This endpoint accepts directly accessible HTTP(S)
material URLs. Existing Seedance official and xAI Grok compatibility routes remain available.

### 3.1 List models

```bash
curl -X GET "http://127.0.0.1:6002/v1/models" \
  -H "Authorization: Bearer <service_api_key>"
```

### 3.2 Unified endpoint: `/v1/chat/completions`

Text-to-image:

```bash
curl -X POST "http://127.0.0.1:6002/v1/chat/completions" \
  -H "Authorization: Bearer <service_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nano-banana-pro-2k-16x9",
    "messages": [{"role":"user","content":"a cinematic mountain sunrise"}]
  }'
```

Image-to-image (pass image in latest user message):

```bash
curl -X POST "http://127.0.0.1:6002/v1/chat/completions" \
  -H "Authorization: Bearer <service_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nano-banana-pro-2k-16x9",
    "messages": [{
      "role":"user",
      "content":[
        {"type":"text","text":"turn this photo into watercolor style"},
        {"type":"image_url","image_url":{"url":"https://example.com/input.jpg"}}
      ]
    }]
  }'
```

Text-to-video:

```bash
curl -X POST "http://127.0.0.1:6002/v1/chat/completions" \
  -H "Authorization: Bearer <service_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "sora2",
    "duration": 4,
    "ratio": "16:9",
    "resolution": "720p",
    "messages": [{"role":"user","content":"a drone shot over snowy forest"}]
  }'
```

Veo31 single-image semantics:

- `veo31` / `veo31-fast`: frame mode
  - 1 image => first frame
  - 2 images => first frame + last frame
- `veo31-ref`: reference-image mode
  - 1~3 images => reference images

Image-to-video:

```bash
curl -X POST "http://127.0.0.1:6002/v1/chat/completions" \
  -H "Authorization: Bearer <service_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "sora2",
    "duration": 8,
    "ratio": "9:16",
    "resolution": "720p",
    "messages": [{
      "role":"user",
      "content":[
        {"type":"text","text":"animate this character walking forward"},
        {"type":"image_url","image_url":{"url":"https://example.com/character.png"}}
      ]
    }]
  }'
```

### 3.3 Image endpoint: `/v1/images/generations`

```bash
curl -X POST "http://127.0.0.1:6002/v1/images/generations" \
  -H "Authorization: Bearer <service_api_key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nano-banana-pro-4k-16x9",
    "prompt": "futuristic city skyline at dusk"
  }'
```

## 4) Cookie Import

### Step 1: Export using the Browser Extension (Recommended)

This project includes a companion browser extension to help you easily export required cookies from the Adobe Firefly page.

- Extension source: `browser-cookie-exporter/`
- Exports a minimal `cookie_*.json` (containing only the `cookie` field)
- Detailed instructions: `browser-cookie-exporter/README.md`

**Installation & Usage:**

1. Open Chrome or Edge extension management: `chrome://extensions`
2. Enable "Developer mode" in the top right
3. Click "Load unpacked" and select the `browser-cookie-exporter/` directory from this project
4. Log in to [Adobe Firefly](https://firefly.adobe.com/) as usual
5. Click the extension icon in your browser toolbar and select the export scope
6. Click "Export Minimal JSON" and save the file

### Step 2: Import into the Project

Once you have the exported JSON file, follow these steps to import it:

1. Access and log in to the admin UI (default `http://127.0.0.1:6002/`)
2. Navigate to the "Token 管理" (Token Management) tab
3. Click the "导入 Cookie" (Import Cookie) button
4. **Option A:** Paste the JSON content into the text box; **Option B:** Upload the exported `.json` file directly
5. Click "Confirm Import" (the service will verify the cookies and run an initial refresh)
6. Upon success, the token will appear in the list with `自动刷新` (Auto Refresh) set to "Yes"

**Batch Import:** The import dialog supports uploading multiple files at once or pasting a JSON array containing multiple account credentials.

## 5) Storage Paths

- Generated media: `video-data/generated/`
- Request logs: `video-data/request_logs.jsonl`
- Token pool: `video-config/tokens.json`
- Service config: `video-config/config.json`
- Refresh profile (local private): `video-config/refresh_profile.json`

Generated media retention policy:

- Files under `video-data/generated/` are preserved and served via `/generated/*`
- Auto-prune is enabled by size threshold (oldest files first)
  - `generated_max_size_mb` (default `1024`)
  - `generated_prune_size_mb` (default `200`)
- When total generated file size exceeds `generated_max_size_mb`, service deletes old files until at least `generated_prune_size_mb` is reclaimed and total size falls back under threshold

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=leik1000/adobe2api&type=Date)](https://star-history.com/#leik1000/adobe2api&Date)
