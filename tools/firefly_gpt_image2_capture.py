from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any

import requests
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SESSION_DIR = DATA_DIR / "adobe_sessions"
CAPTURE_DIR = DATA_DIR / "firefly_web_captures"
CAPTURE_DIR.mkdir(parents=True, exist_ok=True)


def log(message: str, **extra: Any) -> None:
    tail = ""
    if extra:
        tail = " " + " ".join(f"{k}={v}" for k, v in extra.items())
    print(f"[{time.strftime('%H:%M:%S')}] {message}{tail}", flush=True)


def load_config() -> dict:
    path = ROOT / "config" / "config.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def latest_storage_state() -> Path:
    files = sorted(
        SESSION_DIR.glob("*.storage.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        raise FileNotFoundError(f"no *.storage.json found in {SESSION_DIR}")
    return files[0]


def mask_headers(headers: dict[str, str]) -> dict[str, str]:
    out = dict(headers or {})
    for key in list(out.keys()):
        if key.lower() == "authorization":
            val = str(out.get(key) or "")
            if len(val) > 32:
                out[key] = val[:24] + "..." + val[-10:]
            else:
                out[key] = "***"
    return out


def safe_json_loads(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return text


def looks_like_gpt_image2(payload_text: str) -> bool:
    text = str(payload_text or "")
    return bool(
        re.search(r'"modelId"\s*:\s*"gpt-image"', text)
        or re.search(r'"modelVersion"\s*:\s*"?2"?', text)
        or "gpt-image" in text.lower()
    )


def extract_poll_url(resp: requests.Response, data: Any) -> str:
    header_url = resp.headers.get("x-override-status-link") or ""
    if header_url:
        return header_url
    if isinstance(data, dict):
        try:
            return str(data["links"]["result"]["href"] or "")
        except Exception:
            pass
    return ""


def replay_capture(capture: dict, *, timeout: int = 300, proxy: str = "") -> dict:
    request_info = capture.get("request") or {}
    url = str(request_info.get("url") or "")
    headers = dict(request_info.get("headers") or {})
    post_data = str(request_info.get("post_data") or "")
    if not url or not headers or not post_data:
        return {"status": "failed", "detail": "missing captured url/headers/post_data"}

    # Let requests compute transport-specific headers.
    for h in ("host", "content-length", "accept-encoding", "connection"):
        headers.pop(h, None)
        headers.pop(h.title(), None)

    proxies = None
    if proxy:
        proxies = {"http": proxy, "https": proxy}
    session = requests.Session()
    session.trust_env = False
    resp = session.post(url, headers=headers, data=post_data.encode("utf-8"), timeout=60, proxies=proxies)
    try:
        data = resp.json()
    except Exception:
        data = {"text": resp.text[:2000]}
    result = {
        "status": "accepted" if resp.status_code in (200, 201) else "failed",
        "http_status": resp.status_code,
        "headers": dict(resp.headers),
        "response": data,
    }
    if resp.status_code not in (200, 201):
        return result

    poll_url = extract_poll_url(resp, data)
    result["poll_url"] = poll_url
    if not poll_url:
        result["status"] = "failed"
        result["detail"] = "accepted but no poll url"
        return result

    deadline = time.time() + timeout
    latest: Any = {}
    while time.time() < deadline:
        poll_resp = session.get(poll_url, headers=headers, timeout=60, proxies=proxies)
        try:
            latest = poll_resp.json()
        except Exception:
            latest = {"text": poll_resp.text[:2000]}
        status_header = str(poll_resp.headers.get("x-task-status") or "").upper()
        status_val = str((latest or {}).get("status") or "").upper() if isinstance(latest, dict) else ""
        log("REPLAY_POLL", http=poll_resp.status_code, status=status_val or status_header or "-")
        if poll_resp.status_code in (408, 429, 500, 502, 503, 504):
            time.sleep(5)
            continue
        if poll_resp.status_code >= 400:
            result.update({"status": "failed", "poll_http_status": poll_resp.status_code, "poll_response": latest})
            return result
        outputs = latest.get("outputs") if isinstance(latest, dict) else None
        if outputs:
            image_url = ((outputs[0] or {}).get("image") or {}).get("presignedUrl") or ""
            result.update({"status": "ok", "image_url": image_url, "poll_response": latest})
            return result
        if status_val in {"FAILED", "CANCELLED", "ERROR"}:
            result.update({"status": "failed", "poll_response": latest})
            return result
        time.sleep(5)
    result.update({"status": "failed", "detail": "replay poll timeout", "poll_response": latest})
    return result


def local_6001_test(base_url: str, api_key: str, prompt: str, size: str, timeout: int) -> dict:
    payload = {
        "model": "gpt-image-2",
        "prompt": prompt,
        "n": 1,
        "size": size,
        "response_format": "url",
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    resp = requests.post(
        base_url.rstrip("/") + "/v1/images/generations",
        headers=headers,
        json=payload,
        timeout=timeout,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"text": resp.text[:2000]}
    return {"http_status": resp.status_code, "response": data}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture real Firefly web gpt-image-2 generate request and compare replay/6001."
    )
    parser.add_argument("--storage", default="", help="Playwright storage_state JSON; default uses newest data/adobe_sessions/*.storage.json")
    parser.add_argument("--url", default="https://firefly.adobe.com/")
    parser.add_argument("--prompt", default="a tiny blue crystal cube icon on a clean white background")
    parser.add_argument("--base-url", default="http://127.0.0.1:6001")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--size", default="1024x1024")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--no-replay", action="store_true")
    parser.add_argument("--no-local", action="store_true")
    parser.add_argument("--save-auth", action="store_true", help="Save full Authorization header in capture file")
    args = parser.parse_args()

    cfg = load_config()
    storage = Path(args.storage) if args.storage else latest_storage_state()
    api_key = args.api_key or str(cfg.get("api_key") or "your-api-key")
    proxy = str(cfg.get("proxy") or "").strip() if bool(cfg.get("use_proxy")) else ""

    captured: dict[str, Any] = {}
    response_body: Any = None
    response_headers: dict[str, str] = {}

    log("OPEN_BROWSER", storage=storage)
    with sync_playwright() as p:
        launch_kwargs: dict[str, Any] = {"headless": False}
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(storage_state=str(storage))
        page = context.new_page()

        def on_request(req):
            nonlocal captured
            if "generate-async" not in req.url:
                return
            post_data = req.post_data or ""
            if not looks_like_gpt_image2(post_data):
                log("WEB_GENERATE_SEEN_NON_GPT2", url=req.url)
                return
            captured = {
                "captured_at": int(time.time()),
                "request": {
                    "url": req.url,
                    "method": req.method,
                    "headers": req.headers,
                    "post_data": post_data,
                    "json": safe_json_loads(post_data),
                },
            }
            log("GPT_IMAGE2_REQUEST_CAPTURED", url=req.url)

        def on_response(resp):
            nonlocal response_body, response_headers
            if not captured:
                return
            try:
                if resp.request.url != captured.get("request", {}).get("url"):
                    return
                response_headers = dict(resp.headers)
                try:
                    response_body = resp.json()
                except Exception:
                    response_body = resp.text()[:2000]
                captured["response"] = {
                    "status": resp.status,
                    "headers": response_headers,
                    "body": response_body,
                }
                log("GPT_IMAGE2_RESPONSE_CAPTURED", status=resp.status)
            except Exception as exc:
                log("RESPONSE_CAPTURE_ERROR", error=exc)

        page.on("request", on_request)
        page.on("response", on_response)
        page.goto(args.url, wait_until="domcontentloaded", timeout=90000)
        log("ACTION_REQUIRED", note="在打开的 Firefly 页面选择 gpt-image-2，输入提示词并点击生成；捕获到请求后窗口会自动关闭。")
        log("SUGGESTED_PROMPT", prompt=args.prompt)

        deadline = time.time() + args.timeout
        while time.time() < deadline:
            if captured and captured.get("response"):
                break
            page.wait_for_timeout(1000)
        try:
            context.storage_state(path=str(storage))
        except Exception:
            pass
        browser.close()

    if not captured:
        log("CAPTURE_FAILED", detail="timeout without gpt-image-2 generate-async request")
        return 2

    save_payload = json.loads(json.dumps(captured, ensure_ascii=False, default=str))
    if not args.save_auth:
        save_payload["request"]["headers"] = mask_headers(save_payload["request"].get("headers") or {})
    out = CAPTURE_DIR / f"gpt_image2_capture_{int(time.time())}.json"
    out.write_text(json.dumps(save_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log("CAPTURE_SAVED", path=out)

    if not args.no_replay:
        log("REPLAY_START")
        replay_result = replay_capture(captured, timeout=args.timeout, proxy=proxy)
        replay_out = CAPTURE_DIR / f"gpt_image2_replay_{int(time.time())}.json"
        replay_out.write_text(json.dumps(replay_result, ensure_ascii=False, indent=2), encoding="utf-8")
        log("REPLAY_DONE", status=replay_result.get("status"), http=replay_result.get("http_status"), path=replay_out)

    if not args.no_local:
        log("LOCAL_6001_START")
        local_result = local_6001_test(args.base_url, api_key, args.prompt, args.size, args.timeout + 60)
        local_out = CAPTURE_DIR / f"gpt_image2_6001_{int(time.time())}.json"
        local_out.write_text(json.dumps(local_result, ensure_ascii=False, indent=2), encoding="utf-8")
        log("LOCAL_6001_DONE", http=local_result.get("http_status"), path=local_out)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
