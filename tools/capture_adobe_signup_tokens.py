import argparse
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DATA_DIR = ROOT / "data"
CAPTURE_DIR = DATA_DIR / "http_register_captures"
CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = ROOT / "config" / "config.json"
AUTH_BASE = "https://auth.services.adobe.com"
SIGNUP_URL = f"{AUTH_BASE}/en_US/index.html?client_id=SunbreakWebUI1&api=authorize#/signup"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
SENSITIVE_HEADERS = {
    "x-ims-arkose-captcha-token",
    "x-ims-genuine-token",
    "x-ims-clientid",
    "x-request-id",
    "x-adobe-app-id",
    "x-ims-entcaptcha-response",
    "content-type",
    "origin",
    "referer",
}


def load_config() -> dict[str, Any]:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def cfg_proxy(cfg: dict[str, Any]) -> str:
    return str(cfg.get("proxy") or "").strip() if cfg.get("use_proxy") else ""


def log(stage: str, **data: Any) -> None:
    print(json.dumps({"stage": stage, **data}, ensure_ascii=True), flush=True)


def mask(value: str) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    if len(token) <= 24:
        return token[:4] + "..." + token[-4:]
    return token[:12] + "..." + token[-8:]


def create_tempmail(cfg: dict[str, Any]) -> dict[str, str]:
    from core.tempmail_lol import TempMailLolClient

    client = TempMailLolClient(
        api_key=str(cfg.get("tempmail_lol_api_key") or ""),
        proxy=cfg_proxy(cfg),
        timeout=30,
    )
    for idx in range(10):
        inbox = client.create_inbox(prefix=f"adobecaptcha{idx}-{uuid.uuid4().hex[:5]}")
        domain = inbox["address"].split("@", 1)[-1].lower()
        if domain.endswith("gardianwaves.org"):
            return inbox
    return inbox


def write_capture(payload: dict[str, Any]) -> str:
    stamp = int(payload.get("captured_at") or time.time())
    path = CAPTURE_DIR / f"signup_tokens_{stamp}.json"
    latest = CAPTURE_DIR / "latest_signup_tokens.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    latest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture Adobe signup Arkose/Genuine request tokens from a real signup page request.")
    parser.add_argument("--headed", action="store_true", help="Show the browser so a human can solve Arkose if it appears.")
    parser.add_argument("--wait-seconds", type=int, default=180, help="How long to wait for a create-account request.")
    parser.add_argument("--email", default="", help="Email to fill; defaults to a TempMail inbox.")
    parser.add_argument("--password", default="", help="Password to fill; defaults to a generated password.")
    parser.add_argument("--output", default="", help="Optional output json path.")
    args = parser.parse_args()

    cfg = load_config()
    proxy = cfg_proxy(cfg)
    email = str(args.email or "").strip()
    mail_token = ""
    if not email:
        inbox = create_tempmail(cfg)
        email = inbox["address"]
        mail_token = inbox["token"]
    password = str(args.password or f"Aa{uuid.uuid4().hex[:10]}!{int(time.time()) % 1000}").strip()

    log("capture_signup_tokens_start", headed=bool(args.headed), proxy=proxy or None, email=email)

    from playwright.sync_api import sync_playwright

    captured: dict[str, Any] = {
        "status": "pending",
        "captured_at": int(time.time()),
        "email": email,
        "password": password,
        "mail_token": mail_token,
        "url": SIGNUP_URL,
        "used_proxy": bool(proxy),
        "proxy": proxy,
        "request": {},
        "response": {},
        "headers": {},
        "tokens": {
            "arkose_token": "",
            "genuine_token": "",
            "entcaptcha_response": "",
        },
        "tokens_masked": {},
        "screenshot": "",
        "detail": "",
    }

    out_path = Path(args.output) if args.output else None
    screenshot = CAPTURE_DIR / f"signup_token_capture_{int(time.time())}.png"
    with sync_playwright() as p:
        launch_kwargs: dict[str, Any] = {"headless": not args.headed}
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}
        browser = p.chromium.launch(**launch_kwargs)
        page = browser.new_page(viewport={"width": 1365, "height": 900}, user_agent=USER_AGENT)

        def on_request(req):
            try:
                url = str(req.url or "")
                if "/signin/v2/accounts" not in url and "/signin/v4/accounts" not in url:
                    return
                headers = {k.lower(): v for k, v in req.headers.items() if k.lower() in SENSITIVE_HEADERS}
                post_data = req.post_data or ""
                captured["request"] = {
                    "method": req.method,
                    "url": url,
                    "headers": headers,
                    "post_data_preview": post_data[:4000],
                }
                captured["headers"] = headers
                captured["tokens"] = {
                    "arkose_token": headers.get("x-ims-arkose-captcha-token") or "",
                    "genuine_token": headers.get("x-ims-genuine-token") or "",
                    "entcaptcha_response": headers.get("x-ims-entcaptcha-response") or "",
                }
                captured["tokens_masked"] = {k: mask(v) for k, v in captured["tokens"].items()}
                log("create_account_request_seen", url=url, tokens=captured["tokens_masked"])
            except Exception as exc:
                log("request_capture_error", error=str(exc))

        def on_response(resp):
            try:
                url = str(resp.url or "")
                if "/signin/v2/accounts" not in url and "/signin/v4/accounts" not in url:
                    return
                body = ""
                try:
                    body = resp.text(timeout=3000)  # type: ignore[call-arg]
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
                    "body": body[:4000],
                }
                log("create_account_response_seen", status=resp.status, body=body[:240])
            except Exception as exc:
                log("response_capture_error", error=str(exc))

        page.on("request", on_request)
        page.on("response", on_response)
        page.goto(SIGNUP_URL, wait_until="networkidle", timeout=90000)
        page.wait_for_timeout(4000)

        # Try the standard signup fields. If Arkose appears in headed mode, the user can solve it during wait_seconds.
        for selector, value in (
            ("#Signup-EmailField", email),
            ("input[type='email']", email),
            ("input[name='email']", email),
            ("#Signup-PasswordField", password),
            ("input[type='password']", password),
            ("input[name='password']", password),
        ):
            try:
                loc = page.locator(selector).first
                if loc.count():
                    loc.fill(value, timeout=2500)
            except Exception:
                pass
        for label in ("Continue", "Create account", "Sign up with email", "继续", "创建账户"):
            try:
                page.get_by_role("button", name=label).click(timeout=3500)
                page.wait_for_timeout(5000)
            except Exception:
                pass
        for selector, value in (
            ("#Signup-LastNameField", "Ming"),
            ("#Signup-FirstNameField", "Li"),
            ("#Signup-DateOfBirthChooser-Year", "1995"),
            ("#Signup-DateOfBirthChooser-Month", "January"),
            ("input[name='lastName']", "Ming"),
            ("input[name='firstName']", "Li"),
            ("input[name='year']", "1995"),
        ):
            try:
                loc = page.locator(selector).first
                if loc.count():
                    loc.fill(value, timeout=2500)
            except Exception:
                pass
        for label, value in (("Month", "January"), ("Country/Region", "United States")):
            try:
                loc = page.get_by_label(label).first
                loc.click(timeout=2500)
                page.keyboard.press("Control+A")
                page.keyboard.type(value, delay=10)
                page.keyboard.press("Enter")
                page.wait_for_timeout(800)
            except Exception:
                try:
                    page.get_by_text(value, exact=True).first.click(timeout=2500)
                    page.wait_for_timeout(800)
                except Exception:
                    pass
        try:
            page.locator("#Signup-DateOfBirthChooser-Month").click(timeout=2500)
            page.get_by_role("option", name="January").first.click(timeout=2500)
            page.wait_for_timeout(500)
        except Exception:
            try:
                page.get_by_text("January", exact=True).last.click(timeout=2500)
                page.wait_for_timeout(500)
            except Exception:
                pass
        try:
            page.evaluate(
                """() => {
                    const s = document.querySelector("select[name='month']");
                    if (!s) return false;
                    let idx = [...s.options].findIndex(o => (o.textContent || '').trim() === 'January' || String(o.value) === '1' || String(o.value).toLowerCase() === 'january');
                    if (idx < 0) idx = s.options.length > 1 ? 1 : 0;
                    s.selectedIndex = idx;
                    s.value = s.options[idx].value;
                    s.dispatchEvent(new Event('input', {bubbles:true}));
                    s.dispatchEvent(new Event('change', {bubbles:true}));
                    return true;
                }"""
            )
            page.wait_for_timeout(500)
        except Exception:
            pass
        try:
            page.locator("select[name='countryCode']").select_option(label="United States", timeout=2500)
            page.dispatch_event("select[name='countryCode']", "change")
            page.wait_for_timeout(500)
        except Exception:
            pass
        for label in ("Create account", "Continue", "完成", "创建账户"):
            try:
                page.get_by_role("button", name=label).click(timeout=3500)
                page.wait_for_timeout(5000)
            except Exception:
                pass
        try:
            page.locator("button[type='submit']").last.click(timeout=3500)
            page.wait_for_timeout(8000)
        except Exception:
            pass

        deadline = time.time() + max(10, int(args.wait_seconds or 180))
        while time.time() < deadline:
            if captured.get("request"):
                # Wait briefly for the response too.
                if captured.get("response"):
                    break
            page.wait_for_timeout(1000)
        try:
            page.screenshot(path=str(screenshot), full_page=True)
            captured["screenshot"] = str(screenshot)
        except Exception:
            pass
        try:
            captured["final_url"] = page.url
            captured["body_preview"] = page.locator("body").inner_text(timeout=5000)[:4000]
            captured["form_summary"] = page.evaluate(
                """() => [...document.querySelectorAll('input,button,select,textarea,[role="combobox"]')].slice(0,140).map(e=>({
                    tag:e.tagName,id:e.id||'',name:e.name||'',type:e.type||'',role:e.getAttribute('role')||'',
                    text:(e.innerText||e.textContent||'').trim().slice(0,120),
                    aria:e.getAttribute('aria-label')||'',placeholder:e.getAttribute('placeholder')||'',value:e.value||''
                }))"""
            )
        except Exception:
            pass
        browser.close()

    has_any_token = any(str(v or "").strip() for v in (captured.get("tokens") or {}).values())
    captured["status"] = "ok" if has_any_token else ("request_seen_no_token" if captured.get("request") else "not_captured")
    if captured["status"] == "not_captured":
        captured["detail"] = "No /signin/v2/accounts request was captured. Run with --headed and solve any visible Arkose challenge."
    elif captured["status"] == "request_seen_no_token":
        captured["detail"] = "Create-account request was captured but no Arkose/Genuine token headers were present."
    else:
        captured["detail"] = "Captured signup token headers. These can be read by tools/http_adobe_register.py."

    saved = str(out_path) if out_path else write_capture(captured)
    if out_path:
        out_path.write_text(json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8")
    log("capture_signup_tokens_done", status=captured["status"], path=saved, screenshot=captured.get("screenshot"), tokens=captured.get("tokens_masked"))
    return 0 if captured["status"] in {"ok", "request_seen_no_token"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
