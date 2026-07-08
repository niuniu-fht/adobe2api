import json
import base64
import os
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
SESSION_DIR = DATA_DIR / "adobe_sessions"
SESSION_DIR.mkdir(exist_ok=True)

CONFIG_FILE = ROOT / "config" / "config.json"
RESULT_FILE = Path(
    os.environ.get("CLOAK_REGISTER_RESULT_FILE")
    or str(DATA_DIR / "cloak_adobe_register_result.json")
)


def load_config() -> dict:
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def log(stage: str, **data) -> None:
    payload = {"stage": stage, **data}
    print(json.dumps(payload, ensure_ascii=True), flush=True)


def apply_proxy_env(cfg: dict) -> str:
    """Make every network layer default to the configured local proxy.

    CloakBrowser's binary downloader uses httpx and Playwright uses its own
    browser proxy option. Setting both env vars and launch(proxy=...) ensures:
    - cloakbrowser.dev / GitHub binary download goes through 127.0.0.1:7890
    - browser page traffic goes through the same proxy
    - local backend import stays direct via NO_PROXY
    """
    proxy = str(cfg.get("proxy") or "").strip() if cfg.get("use_proxy") else ""
    if proxy:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            os.environ[key] = proxy
        os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
        os.environ.setdefault("no_proxy", os.environ["NO_PROXY"])

    binary_path = str(cfg.get("cloak_browser_binary_path") or "").strip()
    if binary_path:
        os.environ["CLOAKBROWSER_BINARY_PATH"] = binary_path
    license_key = str(cfg.get("cloak_browser_license_key") or "").strip()
    if license_key:
        os.environ["CLOAKBROWSER_LICENSE_KEY"] = license_key
    version = str(cfg.get("cloak_browser_version") or "").strip()
    if version:
        os.environ["CLOAKBROWSER_VERSION"] = version
    return proxy


def create_tempmail(cfg: dict) -> dict:
    from core.tempmail_lol import TempMailLolClient

    client = TempMailLolClient(
        api_key=cfg["tempmail_lol_api_key"],
        proxy=cfg.get("proxy") if cfg.get("use_proxy") else "",
        timeout=30,
    )
    # Prefer domains Adobe accepted with lower friction in prior live testing.
    preferred = []
    fallback = []
    for idx in range(20):
        inbox = client.create_inbox(prefix=f"adobecloak{idx}")
        domain = inbox["address"].split("@", 1)[-1].lower()
        if domain.endswith("gardianwaves.org") or domain.endswith("airfryersbg.com"):
            preferred.append(inbox)
            # Return early for the exact family that already succeeded.
            if domain.endswith("gardianwaves.org"):
                return inbox
        elif not domain.endswith("actionvspot.com") and not domain.endswith("icodetensor.com"):
            fallback.append(inbox)
    if preferred:
        return preferred[0]
    if fallback:
        return fallback[0]
    return inbox


def api_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    base = "http://127.0.0.1:6001"
    session.base_url = base  # type: ignore[attr-defined]
    session.post(
        f"{base}/api/v1/auth/login",
        json={"username": "admin", "password": "admin"},
        timeout=10,
    )
    return session


def upsert_account(account: dict) -> None:
    try:
        session = api_session()
        base = session.base_url  # type: ignore[attr-defined]
        session.post(
            f"{base}/api/v1/adobe/accounts/import",
            json={"accounts": [account]},
            timeout=20,
        )
        session.put(
            f"{base}/api/v1/adobe/accounts/{account['id']}",
            json=account,
            timeout=20,
        )
    except Exception:
        pass


def safe_session_slug(email: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(email or "").strip())[:120] or "adobe"


def cookie_string(cookies: list[dict]) -> str:
    pairs = []
    for item in cookies:
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        domain = str(item.get("domain") or "").lower()
        if not name or not ("adobe" in domain or "adobelogin" in domain):
            continue
        pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def mask_token(value: str) -> str:
    token = str(value or "").strip()
    if len(token) <= 30:
        return "***" if token else ""
    return token[:15] + "..." + token[-10:]


def decode_jwt_payload(value: str) -> dict:
    token = str(value or "").strip()
    if token.startswith("Bearer "):
        token = token[7:].strip()
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8", errors="ignore"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def persist_browser_state(page, account: dict, arp_headers: dict) -> dict:
    slug = safe_session_slug(account.get("email", ""))
    storage_path = SESSION_DIR / f"{slug}.storage.json"
    cookies_path = SESSION_DIR / f"{slug}.cookies.json"
    try:
        page.context.storage_state(path=str(storage_path))
    except Exception:
        pass
    try:
        cookies = page.context.cookies()
    except Exception:
        cookies = []

    payload = {
        "email": account.get("email"),
        "saved_at": int(time.time()),
        "storage_state_path": str(storage_path),
        "cookies": cookies,
        "cookie": cookie_string(cookies),
        "headers": {k: v for k, v in (arp_headers or {}).items() if v},
    }
    cookies_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    account["session_state_path"] = str(storage_path)
    return payload


def import_refresh_profile(account: dict, state_payload: dict) -> dict:
    result = {"status": "skipped", "detail": "no cookie"}
    cookie = str(state_payload.get("cookie") or "").strip()
    if not cookie:
        return result
    try:
        session = api_session()
        base = session.base_url  # type: ignore[attr-defined]
        cookie_input = {
            "cookie": cookie,
            "cookies": state_payload.get("cookies") or [],
            "headers": state_payload.get("headers") or {},
            "email": account.get("email"),
            "display_name": "Li Ming",
        }
        resp = session.post(
            f"{base}/api/v1/refresh-profiles/import-cookie",
            json={"cookie": cookie_input, "name": account.get("email")},
            timeout=90,
        )
        try:
            result = resp.json()
        except Exception:
            result = {"status": "failed", "http_status": resp.status_code, "text": resp.text[:1000]}
        if resp.status_code >= 400:
            result.setdefault("status", "failed")
        profile = result.get("profile") if isinstance(result, dict) else None
        if isinstance(profile, dict) and profile.get("id"):
            account["cookie_profile_id"] = str(profile.get("id"))
        refresh_result = result.get("refresh_result") if isinstance(result, dict) else None
        if isinstance(refresh_result, dict) and refresh_result.get("status") == "ok":
            account["token_status"] = "active"
        else:
            account["token_status"] = "refresh_failed"
        return result
    except Exception as exc:
        account["token_status"] = "refresh_exception"
        return {"status": "failed", "detail": str(exc)}


def import_clio_token(account: dict, clio_token: str) -> dict:
    token_value = str(clio_token or "").strip()
    if token_value.startswith("Bearer "):
        token_value = token_value[7:].strip()
    if not token_value:
        return {"status": "skipped", "detail": "no clio token"}
    payload = decode_jwt_payload(token_value)
    account_id = str(
        payload.get("user_id") or payload.get("aa_id") or payload.get("sub") or ""
    ).strip()
    try:
        session = api_session()
        base = session.base_url  # type: ignore[attr-defined]
        resp = session.post(
            f"{base}/api/v1/tokens",
            json={
                "token": token_value,
                "source": "clio_browser_auto",
                "refresh_profile_id": str(account.get("cookie_profile_id") or ""),
                "refresh_profile_name": "Li Ming",
                "refresh_profile_email": str(account.get("email") or ""),
                "refresh_client_id": "clio-playground-web",
                "account_id": account_id,
                "auto_refresh": True,
            },
            timeout=30,
        )
        try:
            data = resp.json()
        except Exception:
            data = {"text": resp.text[:1000]}
        if resp.status_code >= 400:
            account["token_status"] = "clio_import_failed"
            return {"status": "failed", "http_status": resp.status_code, "response": data}
        account["token_status"] = "clio_active"
        return {
            "status": "ok",
            "http_status": resp.status_code,
            "client_id": str(payload.get("client_id") or ""),
            "account_id": account_id,
            "token": mask_token(token_value),
            "response": data,
        }
    except Exception as exc:
        account["token_status"] = "clio_import_exception"
        return {"status": "failed", "detail": str(exc)}


def poll_clio_web_generation(cfg: dict, clio_token: str, poll_url: str) -> dict:
    token = str(clio_token or "").strip()
    url = str(poll_url or "").strip()
    if not token or not url:
        return {"status": "skipped", "detail": "missing token or poll url"}
    session = requests.Session()
    session.trust_env = False
    proxies = None
    if cfg.get("use_proxy") and str(cfg.get("proxy") or "").strip():
        proxies = {"http": str(cfg.get("proxy")).strip(), "https": str(cfg.get("proxy")).strip()}
    headers = {
        "Authorization": f"Bearer {token}",
        "x-api-key": "clio-playground-web",
        "accept": "application/json",
        "origin": "https://firefly.adobe.com",
        "referer": "https://firefly.adobe.com/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    }
    deadline = time.time() + 240
    latest = {}
    last_status = ""
    while time.time() < deadline:
        try:
            resp = session.get(url, headers=headers, timeout=60, proxies=proxies)
            try:
                data = resp.json()
            except Exception:
                data = {"text": resp.text[:1000]}
            latest = data if isinstance(data, dict) else {"data": data}
            status_header = str(resp.headers.get("x-task-status") or "").upper()
            status_val = str(latest.get("status") or "").upper() or status_header
            last_status = status_val or last_status
            log("web_image_poll", http_status=resp.status_code, task_status=status_val or "-", retry_after=resp.headers.get("retry-after"))
            if resp.status_code in (408, 429, 500, 502, 503, 504):
                time.sleep(min(max(float(resp.headers.get("retry-after") or 5), 3.0), 12.0))
                continue
            if resp.status_code >= 400:
                return {"status": "failed", "http_status": resp.status_code, "response": latest}
            outputs = latest.get("outputs") or []
            if outputs:
                image_url = ((outputs[0] or {}).get("image") or {}).get("presignedUrl") or ""
                return {
                    "status": "ok",
                    "http_status": resp.status_code,
                    "task_status": status_val or "COMPLETED",
                    "image_url": image_url,
                    "response": latest,
                }
            if status_val in {"FAILED", "CANCELLED", "ERROR"}:
                return {"status": "failed", "http_status": resp.status_code, "task_status": status_val, "response": latest}
            time.sleep(min(max(float(resp.headers.get("retry-after") or 5), 3.0), 12.0))
        except Exception as exc:
            latest = {"detail": str(exc)}
            log("web_image_poll_error", error=str(exc))
            time.sleep(5)
    return {"status": "failed", "detail": f"web generation timed out status={last_status or 'unknown'}", "response": latest}


def capture_clio_token(page, cfg: dict, account: dict) -> dict:
    """Use the logged-in Firefly browser once to capture the native Clio token.

    The captured token is immediately added to this project's token pool so the
    following /v1/images/generations test uses the same request path as the web UI.
    """
    slug = safe_session_slug(account.get("email", ""))
    capture_path = SESSION_DIR / f"{slug}.clio.json"
    result: dict = {
        "status": "pending",
        "token": "",
        "token_masked": "",
        "request_seen": False,
        "response_status": None,
        "import": {},
        "web_generation": {},
    }
    captured: dict = {"token": "", "request": {}, "response": {}, "poll_url": ""}

    def on_request(req):
        try:
            url = str(req.url or "")
            if "firefly-clio-imaging.adobe.io/v2/images/generate-async" not in url:
                return
            captured["request"] = {
                "url": url,
                "method": req.method,
                "headers": {
                    k: v
                    for k, v in dict(req.headers).items()
                    if k.lower() in {"x-api-key", "x-request-id", "content-type", "referer"}
                },
                "post_data": (req.post_data or "")[:2000],
            }
            auth = str(req.headers.get("authorization") or "").strip()
            if auth.lower().startswith("bearer "):
                captured["token"] = auth[7:].strip()
            result["request_seen"] = True
            log("clio_generate_request_seen", has_token=bool(captured.get("token")))
        except Exception as exc:
            log("clio_request_capture_error", error=str(exc))

    def on_response(resp):
        try:
            url = str(resp.url or "")
            if "firefly-clio-imaging.adobe.io/v2/images/generate-async" not in url:
                return
            body = ""
            try:
                body = resp.text(timeout=5000)  # type: ignore[call-arg]
            except TypeError:
                try:
                    body = resp.text()
                except Exception:
                    body = ""
            except Exception:
                body = ""
            captured["response"] = {
                "url": url,
                "status": resp.status,
                "headers": {
                    k: v
                    for k, v in dict(resp.headers).items()
                    if k.lower()
                    in {
                        "content-type",
                        "x-override-status-link",
                        "retry-after",
                        "x-request-id",
                    }
                },
                "body": body[:2000],
            }
            poll_url = str(resp.headers.get("x-override-status-link") or "").strip()
            if poll_url:
                captured["poll_url"] = poll_url
            result["response_status"] = resp.status
            log("clio_generate_response_seen", status=resp.status, has_poll_url=bool(captured.get("poll_url")))
        except Exception as exc:
            log("clio_response_capture_error", error=str(exc))

    try:
        page.on("request", on_request)
        page.on("response", on_response)
    except Exception:
        pass

    try:
        log("capture_clio_goto_firefly", url="https://firefly.adobe.com/generate/images")
        page.goto("https://firefly.adobe.com/generate/images", wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(12000)

        for label in ("开始", "Start", "继续", "Continue", "稍后", "Not now"):
            try:
                page.get_by_role("button", name=label).click(timeout=2500)
                page.wait_for_timeout(1200)
            except Exception:
                pass

        prompt = (
            "small clean product photo of a blue glass cup on white table, "
            "soft daylight, minimal background"
        )
        filled = False
        for selector in (
            "textarea",
            'input[type="text"]',
            '[contenteditable="true"]',
            'sp-textfield textarea',
            'sp-textfield input',
        ):
            try:
                loc = page.locator(selector).first
                loc.click(timeout=2500)
                page.keyboard.press("Control+A")
                page.keyboard.type(prompt, delay=15)
                filled = True
                break
            except Exception:
                pass
        if not filled:
            try:
                page.mouse.click(690, 680)
                page.keyboard.press("Control+A")
                page.keyboard.type(prompt, delay=15)
                filled = True
            except Exception:
                pass
        log("capture_clio_prompt_filled", filled=filled)

        clicked = False
        for label in ("生成", "Generate"):
            try:
                page.get_by_role("button", name=label).click(timeout=5000)
                clicked = True
                break
            except Exception:
                pass
            try:
                page.locator("sp-button").filter(has_text=label).last.click(timeout=5000)
                clicked = True
                break
            except Exception:
                pass
        if not clicked:
            try:
                size = page.viewport_size or {"width": 1280, "height": 900}
                page.mouse.click(max(100, int(size["width"]) - 130), max(100, int(size["height"]) - 220))
                clicked = True
            except Exception:
                pass
        log("capture_clio_generate_clicked", clicked=clicked)

        deadline = time.time() + 120
        while time.time() < deadline:
            if captured.get("token"):
                break
            page.wait_for_timeout(1000)

        clio_token = str(captured.get("token") or "").strip()
        if clio_token:
            result["status"] = "ok"
            result["token"] = clio_token
            result["token_masked"] = mask_token(clio_token)
            account["last_action"] = "CloakBrowser 注册成功，已捕获 Firefly Clio token"
            log("capture_clio_token_ok", token=result["token_masked"])
            if captured.get("poll_url"):
                log("web_image_generation_poll_start")
                result["web_generation"] = poll_clio_web_generation(
                    cfg, clio_token, str(captured.get("poll_url") or "")
                )
                if result["web_generation"].get("status") == "ok":
                    account["web_image_status"] = "passed"
                    account["web_image_test_url"] = str(result["web_generation"].get("image_url") or "")[:1000]
                    log("web_image_generation_ok", image_url=account.get("web_image_test_url"))
                else:
                    account["web_image_status"] = "failed"
                    account["web_image_test_error"] = json.dumps(result["web_generation"], ensure_ascii=False)[:1000]
                    log("web_image_generation_failed", detail=account.get("web_image_test_error", "")[:300])
            else:
                result["web_generation"] = {"status": "skipped", "detail": "generate response did not include poll url"}
                account["web_image_status"] = "skipped"
                account["web_image_test_error"] = "generate response did not include poll url"
                log("web_image_generation_skipped", detail=account.get("web_image_test_error"))
            result["import"] = import_clio_token(account, clio_token)
        else:
            result["status"] = "failed"
            result["detail"] = "generate-async request token not captured"
            account["token_status"] = account.get("token_status") or "clio_capture_failed"
            account["web_image_status"] = "failed"
            account["web_image_test_error"] = "generate-async request token not captured"
            log("capture_clio_token_failed", request_seen=bool(result.get("request_seen")))

        safe_payload = {
            "saved_at": int(time.time()),
            "email": account.get("email"),
            "status": result.get("status"),
            "token": result.get("token_masked"),
            "request": captured.get("request") or {},
            "response": captured.get("response") or {},
            "poll_url": captured.get("poll_url") or "",
            "web_generation": {
                k: v
                for k, v in (result.get("web_generation") or {}).items()
                if k != "response"
            },
            "import": result.get("import") or {},
        }
        capture_path.write_text(json.dumps(safe_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        result["capture_path"] = str(capture_path)
        return result
    except Exception as exc:
        result["status"] = "failed"
        result["detail"] = str(exc)
        account["token_status"] = account.get("token_status") or "clio_capture_exception"
        return result


def test_image_generation(cfg: dict, account: dict) -> dict:
    if not bool(cfg.get("cloak_register_test_image", True)):
        return {"status": "skipped", "detail": "cloak_register_test_image=false"}
    try:
        session = requests.Session()
        session.trust_env = False
        base = "http://127.0.0.1:6001"
        prompt = (
            "small clean product photo of a red ceramic mug on white table, "
            "soft daylight, minimal background"
        )
        payload = {
            "model": str(cfg.get("cloak_register_test_model") or "firefly-nano-banana-1k-1x1"),
            "prompt": prompt,
            "size": "1024x1024",
            "response_format": "url",
        }
        resp = session.post(
            f"{base}/v1/images/generations",
            headers={"Authorization": f"Bearer {cfg.get('api_key') or 'projectx_webapp'}"},
            json=payload,
            timeout=int(cfg.get("cloak_register_image_timeout_seconds") or 360),
        )
        try:
            data = resp.json()
        except Exception:
            data = {"text": resp.text[:1500]}
        if resp.status_code == 200:
            image_url = ""
            items = data.get("data") if isinstance(data, dict) else None
            if isinstance(items, list) and items:
                first = items[0] if isinstance(items[0], dict) else {}
                image_url = str(first.get("url") or first.get("b64_json") or "")
            account["image_status"] = "passed"
            account["image_test_url"] = image_url[:1000]
            account["image_test_error"] = ""
            account["last_action"] = "CloakBrowser 注册成功，cookie 已维护，出图测试成功"
            return {"status": "ok", "http_status": resp.status_code, "image_url": image_url, "response": data}
        account["image_status"] = "failed"
        account["image_test_error"] = json.dumps(data, ensure_ascii=False)[:1000]
        account["last_action"] = "CloakBrowser 注册成功，cookie 已保存，但出图测试失败"
        return {"status": "failed", "http_status": resp.status_code, "response": data}
    except Exception as exc:
        account["image_status"] = "failed"
        account["image_test_error"] = str(exc)[:1000]
        account["last_action"] = "CloakBrowser 注册成功，cookie 已保存，但出图测试异常"
        return {"status": "failed", "detail": str(exc)}


def save_result(result: dict) -> None:
    RESULT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def visible_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=5000)
    except Exception:
        return ""


def input_summary(page) -> list[dict]:
    try:
        return page.evaluate(
            """() => [...document.querySelectorAll('input,select,button,iframe')].slice(0,80).map(e => ({
                tag:e.tagName, id:e.id, name:e.name || '', type:e.type || '',
                text:(e.textContent||'').trim().slice(0,100),
                aria:e.getAttribute('aria-label') || '',
                title:e.title || '', src:e.src || '', value:e.value || ''
            }))"""
        )
    except Exception:
        return []


def extract_codes_from_email(item: dict) -> list[str]:
    body = f"{item.get('body') or ''}\n{item.get('html') or ''}"
    return re.findall(r"(?<!\d)(\d{6})(?!\d)", body)


def wait_for_new_verification_code(cfg: dict, account: dict, after_ms: int) -> str:
    from core.tempmail_lol import TempMailLolClient

    client = TempMailLolClient(
        api_key=cfg["tempmail_lol_api_key"],
        proxy=cfg.get("proxy") if cfg.get("use_proxy") else "",
        timeout=30,
    )
    token = str(account.get("mail_token") or "").strip()
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            inbox = client.fetch_inbox(token)
            emails = inbox.get("emails") or []
        except Exception:
            emails = []
        candidates = []
        for item in emails:
            try:
                date_ms = int(item.get("date") or 0)
            except Exception:
                date_ms = 0
            if date_ms + 3000 < after_ms:
                continue
            codes = extract_codes_from_email(item)
            for code in codes:
                candidates.append((date_ms, code))
        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][1]
        time.sleep(8)
    return ""


def complete_firefly_email_verification(page, cfg: dict, account: dict) -> dict:
    result = {"required": False, "verified": False, "code": ""}
    try:
        page.goto("https://firefly.adobe.com/generate/images", wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(10000)
    except Exception as exc:
        result["error"] = str(exc)
        return result

    if "email-verification" not in str(page.url):
        result["verified"] = True
        return result

    result["required"] = True
    after_ms = int(time.time() * 1000)
    try:
        page.get_by_role("button", name="重新发送代码").click(timeout=5000)
        page.wait_for_timeout(2000)
    except Exception:
        pass
    code = wait_for_new_verification_code(cfg, account, after_ms=after_ms)
    result["code"] = code
    if not code:
        result["error"] = "verification code not received"
        return result

    for attempt in range(2):
        try:
            inputs = page.locator('input[type="number"]')
            if inputs.count() < 6:
                page.wait_for_timeout(3000)
                inputs = page.locator('input[type="number"]')
            if inputs.count() >= 6:
                for idx in range(inputs.count()):
                    try:
                        inputs.nth(idx).fill("", timeout=1000)
                    except Exception:
                        pass
                inputs.first.click(timeout=5000)
                page.keyboard.type(code, delay=220)
                page.wait_for_timeout(12000)
                if "email-verification" not in str(page.url):
                    result["verified"] = True
                    break
                page.keyboard.press("Enter")
                page.wait_for_timeout(12000)
                if "email-verification" not in str(page.url):
                    result["verified"] = True
                    break
        except Exception as exc:
            result["error"] = str(exc)
        if attempt == 0 and not result["verified"]:
            after_ms = int(time.time() * 1000)
            try:
                page.get_by_role("button", name="重新发送代码").click(timeout=5000)
            except Exception:
                pass
            new_code = wait_for_new_verification_code(cfg, account, after_ms=after_ms)
            if new_code:
                code = new_code
                result["code"] = code

    if result["verified"]:
        account["mail_status"] = "firefly_email_verified"
        try:
            page.goto("https://firefly.adobe.com/generate/images", wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(15000)
        except Exception:
            pass
    return result


def main() -> None:
    cfg = load_config()
    proxy = apply_proxy_env(cfg)
    log("proxy_configured", use_proxy=bool(proxy), proxy=proxy)

    inbox = create_tempmail(cfg)
    email = inbox["address"]
    token = inbox["token"]
    password = "Aa" + re.sub(r"[^A-Za-z0-9]", "", token)[:10] + "!9"
    account = {
        "id": token[:32],
        "email": email,
        "password": password,
        "status": "registering",
        "eligibility": "unknown",
        "plan": "-",
        "image_status": "untested",
        "ip": "127.0.0.1",
        "last_action": "CloakBrowser 注册中",
        "email_provider": "tempmail_lol",
        "mail_token": token,
        "mail_status": "inbox_created",
    }
    save_result({"stage": "created_inbox", "account": account})
    log("created_inbox", email=email, token=token[:8] + "...")

    log("cloakbrowser_importing")
    from cloakbrowser import launch
    try:
        from cloakbrowser.download import binary_info

        info = binary_info()
        log("cloakbrowser_binary", **(info if isinstance(info, dict) else {"info": str(info)}))
    except Exception as exc:
        log("cloakbrowser_binary_info_failed", error=str(exc))

    log("cloakbrowser_launching", proxy=proxy or None)
    browser = launch(
        headless=bool(cfg.get("cloak_browser_headless", False)),
        proxy=proxy or None,
        locale="zh-Hans-CN",
        timezone="Asia/Tokyo",
        humanize=True,
        args=[
            "--window-size=1280,900",
            *( [f"--proxy-server={proxy}"] if proxy else [] ),
        ],
    )
    try:
        page = browser.new_page()
        arp_headers: dict[str, str] = {}

        def capture_request(req):
            try:
                headers = req.headers
                value = str(headers.get("x-arp-session-id") or "").strip()
                if value:
                    arp_headers["x-arp-session-id"] = value
            except Exception:
                pass

        try:
            page.on("request", capture_request)
        except Exception:
            pass

        log("goto", url="https://account.adobe.com/")
        page.goto("https://account.adobe.com/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)

        log("fill_email", email=email)
        page.locator("#EmailPage-EmailField").fill(email, timeout=20000)
        page.get_by_role("button", name="继续").click(timeout=20000)
        page.wait_for_timeout(5000)

        txt = visible_text(page)
        if "创建帐户" in txt:
            log("click_create_account_link")
            page.get_by_role("link", name="创建帐户").click(timeout=20000)
            page.wait_for_timeout(5000)

        log("signup_step1")
        page.locator("#Signup-EmailField").fill(email, timeout=20000)
        page.locator("#Signup-PasswordField").fill(password, timeout=20000)
        page.wait_for_timeout(5000)
        page.get_by_role("button", name="继续").click(timeout=20000)
        page.wait_for_timeout(10000)

        txt = visible_text(page)
        if "#/signup/2" in page.url or "第 2 步" in txt:
            log("signup_step2")
            page.locator("#Signup-LastNameField").fill("Li", timeout=20000)
            page.locator("#Signup-FirstNameField").fill("Ming", timeout=20000)
            page.locator("#Signup-DateOfBirthChooser-Year").fill("1995", timeout=20000)
            page.locator('select[name="month"]').select_option("0", timeout=20000)
            page.wait_for_timeout(1500)
            page.get_by_role("button", name="创建帐户").click(timeout=20000)
            page.wait_for_timeout(20000)

        final_text = visible_text(page)
        summary = input_summary(page)
        has_challenge = any(
            "challenge" in str(item.get("title", "")).lower()
            or "arks-client" in str(item.get("src", "")).lower()
            for item in summary
        )
        success = "account.adobe.com" in page.url and "auth.services.adobe.com" not in page.url
        if has_challenge:
            account["status"] = "need_human"
            account["mail_status"] = "cloak_verification_challenge"
            account["last_action"] = "CloakBrowser 仍触发 Adobe Verification challenge"
        elif success:
            account["status"] = "registered"
            account["mail_status"] = "adobe_registered_or_redirected"
            account["last_action"] = "CloakBrowser 注册后跳转成功"
        else:
            account["status"] = "unknown"
            account["mail_status"] = "cloak_flow_stopped"
            account["last_action"] = "CloakBrowser 流程停止在 Adobe 页面"

        state_payload = {}
        refresh_profile_result = {}
        clio_token_result = {}
        image_test_result = {}
        if success and not has_challenge:
            log("firefly_email_verification")
            email_verification_result = complete_firefly_email_verification(page, cfg, account)
            if email_verification_result.get("code"):
                account["verification_code"] = str(email_verification_result.get("code") or "")
            if email_verification_result.get("required") and not email_verification_result.get("verified"):
                account["mail_status"] = "firefly_email_verification_failed"
                account["last_action"] = "CloakBrowser 注册成功，但 Firefly 邮箱验证未完成"
            elif email_verification_result.get("verified"):
                account["last_action"] = "CloakBrowser 注册成功，Firefly 邮箱验证完成"

            try:
                log("goto_express_for_session", url="https://new.express.adobe.com/")
                page.goto("https://new.express.adobe.com/", wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(15000)
            except Exception as exc:
                log("goto_express_failed", error=str(exc))

            log("persist_browser_state")
            state_payload = persist_browser_state(page, account, arp_headers)
            account["last_action"] = "CloakBrowser 注册成功，cookie/storage 已保存"
            upsert_account(account)

            log("import_refresh_profile")
            refresh_profile_result = import_refresh_profile(account, state_payload)
            if account.get("token_status") == "active":
                account["last_action"] = "CloakBrowser 注册成功，cookie 已维护并刷新 token"
            upsert_account(account)

            log("capture_clio_token")
            clio_token_result = capture_clio_token(page, cfg, account)
            if clio_token_result.get("status") == "ok":
                account["last_action"] = "CloakBrowser 注册成功，cookie 已保存，Clio token 已加入池"
            upsert_account(account)

            log("image_generation_test")
            image_test_result = test_image_generation(cfg, account)
            upsert_account(account)
        else:
            upsert_account(account)

        result = {
            "stage": "final",
            "success": success,
            "has_challenge": has_challenge,
            "url": page.url,
            "title": page.title(),
            "text_excerpt": final_text[:2000],
            "summary": summary,
            "session": {
                "storage_state_path": account.get("session_state_path", ""),
                "cookie_count": len(state_payload.get("cookies") or []) if isinstance(state_payload, dict) else 0,
                "has_cookie": bool(state_payload.get("cookie")) if isinstance(state_payload, dict) else False,
                "headers": state_payload.get("headers", {}) if isinstance(state_payload, dict) else {},
            },
            "email_verification": locals().get("email_verification_result", {}),
            "refresh_profile": refresh_profile_result,
            "clio_token": {
                **{
                    k: v
                    for k, v in (clio_token_result or {}).items()
                    if k != "token"
                },
                "token": mask_token(str((clio_token_result or {}).get("token") or "")),
            },
            "image_test": image_test_result,
            "account": account,
        }
        save_result(result)
        upsert_account(account)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        time.sleep(5)
    finally:
        try:
            browser.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
