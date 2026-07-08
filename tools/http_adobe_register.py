import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
CAPTURE_DIR = DATA_DIR / "http_register_captures"
CAPTURE_DIR.mkdir(exist_ok=True)
CONFIG_FILE = ROOT / "config" / "config.json"
RESULT_FILE = Path(
    os.environ.get("HTTP_REGISTER_RESULT_FILE")
    or str(DATA_DIR / "http_adobe_register_result.json")
)

AUTH_BASE = "https://auth.services.adobe.com"
RENGA_BASE = "https://adobeid-na1.services.adobe.com/renga-idprovider"
SIGNIN_BASE = "https://auth.services.adobe.com/signin"
DEFAULT_CLIENT_ID = "SunbreakWebUI1"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)


def now() -> int:
    return int(time.time())


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def log(stage: str, **data: Any) -> None:
    print(json.dumps({"stage": stage, **data}, ensure_ascii=True), flush=True)


def save_result(result: dict[str, Any]) -> None:
    RESULT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def proxy_from_config(cfg: dict[str, Any]) -> str:
    return str(cfg.get("proxy") or "").strip() if cfg.get("use_proxy") else ""


def create_session(proxy: str) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update(
        {
            "user-agent": USER_AGENT,
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
            "origin": AUTH_BASE,
            "referer": f"{AUTH_BASE}/en_US/index.html#/sign_up",
        }
    )
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def create_tempmail(cfg: dict[str, Any]) -> dict[str, str]:
    from core.tempmail_lol import TempMailLolClient

    client = TempMailLolClient(
        api_key=str(cfg.get("tempmail_lol_api_key") or ""),
        proxy=proxy_from_config(cfg),
        timeout=30,
    )
    preferred: list[dict[str, str]] = []
    fallback: list[dict[str, str]] = []
    for idx in range(20):
        inbox = client.create_inbox(prefix=f"adobehttp{idx}-{uuid.uuid4().hex[:5]}")
        domain = inbox["address"].split("@", 1)[-1].lower()
        if domain.endswith("gardianwaves.org") or domain.endswith("airfryersbg.com"):
            preferred.append(inbox)
            if domain.endswith("gardianwaves.org"):
                return inbox
        elif not domain.endswith("actionvspot.com") and not domain.endswith("icodetensor.com"):
            fallback.append(inbox)
    return (preferred or fallback or [inbox])[0]


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


def upsert_account(account: dict[str, Any]) -> None:
    try:
        session = api_session()
        base = session.base_url  # type: ignore[attr-defined]
        session.post(f"{base}/api/v1/adobe/accounts/import", json={"accounts": [account]}, timeout=20)
        session.put(f"{base}/api/v1/adobe/accounts/{account['id']}", json=account, timeout=20)
    except Exception as exc:
        log("local_account_upsert_failed", error=str(exc))


def decode_response(resp: requests.Response) -> Any:
    text = resp.text or ""
    try:
        return resp.json()
    except Exception:
        return {"text": text[:2000]}


def safe_step(name: str, fn) -> dict[str, Any]:
    started = time.time()
    try:
        resp = fn()
        body = decode_response(resp)
        out = {
            "stage": name,
            "ok": 200 <= resp.status_code < 300,
            "http_status": resp.status_code,
            "duration_sec": round(time.time() - started, 3),
            "headers": {
                k: v
                for k, v in resp.headers.items()
                if k.lower() in {"content-type", "x-request-id", "x-adobe-request-id", "x-ims-request-id"}
            },
            "response": body,
        }
        log(name, http_status=resp.status_code, ok=out["ok"])
        return out
    except Exception as exc:
        out = {"stage": name, "ok": False, "duration_sec": round(time.time() - started, 3), "error": str(exc)}
        log(name, ok=False, error=str(exc))
        return out


def mask_token(value: str) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    if len(token) <= 24:
        return token[:4] + "..." + token[-4:]
    return token[:12] + "..." + token[-8:]


def load_signup_token_overrides(cfg: dict[str, Any]) -> dict[str, Any]:
    """Load optional Arkose/Genuine tokens captured from a real browser signup request.

    Adobe's frontend sends the Arkose captcha value as X-IMS-EntCaptcha-Response
    for /signin/v2/accounts. Newer flows may also send x-ims-arkose-captcha-token
    and x-ims-genuine-token. This function intentionally keeps the source explicit
    in the result logs so the operator can see whether the HTTP attempt had a token
    or was expected to stop at captcha_required.
    """
    source = "none"
    arkose = str(
        os.environ.get("HTTP_REGISTER_ARKOSE_TOKEN")
        or cfg.get("http_register_arkose_token")
        or ""
    ).strip()
    genuine = str(
        os.environ.get("HTTP_REGISTER_GENUINE_TOKEN")
        or cfg.get("http_register_genuine_token")
        or ""
    ).strip()
    entcaptcha = str(
        os.environ.get("HTTP_REGISTER_ENTCAPTCHA_RESPONSE")
        or cfg.get("http_register_entcaptcha_response")
        or ""
    ).strip()
    if arkose or genuine or entcaptcha:
        source = "env_or_config"

    token_file = str(
        os.environ.get("HTTP_REGISTER_TOKEN_FILE")
        or cfg.get("http_register_token_file")
        or ""
    ).strip()
    if not token_file:
        latest = CAPTURE_DIR / "latest_signup_tokens.json"
        if latest.exists():
            token_file = str(latest)
    if token_file and Path(token_file).exists() and not (arkose or genuine or entcaptcha):
        try:
            payload = json.loads(Path(token_file).read_text(encoding="utf-8"))
            tokens = payload.get("tokens") if isinstance(payload, dict) else {}
            headers = payload.get("headers") if isinstance(payload, dict) else {}
            if isinstance(tokens, dict):
                arkose = str(tokens.get("arkose_token") or "").strip()
                genuine = str(tokens.get("genuine_token") or "").strip()
                entcaptcha = str(tokens.get("entcaptcha_response") or "").strip()
            if isinstance(headers, dict):
                arkose = arkose or str(headers.get("x-ims-arkose-captcha-token") or "").strip()
                genuine = genuine or str(headers.get("x-ims-genuine-token") or "").strip()
                entcaptcha = entcaptcha or str(headers.get("x-ims-entcaptcha-response") or headers.get("X-IMS-EntCaptcha-Response") or "").strip()
            if arkose or genuine or entcaptcha:
                source = token_file
        except Exception as exc:
            source = f"token_file_error:{exc}"

    return {
        "source": source,
        "arkose_token": arkose,
        "genuine_token": genuine,
        "entcaptcha_response": entcaptcha,
        "has_arkose": bool(arkose),
        "has_genuine": bool(genuine),
        "has_entcaptcha": bool(entcaptcha),
        "masked": {
            "arkose_token": mask_token(arkose),
            "genuine_token": mask_token(genuine),
            "entcaptcha_response": mask_token(entcaptcha),
        },
    }


def apply_signup_token_headers(headers: dict[str, str], token_overrides: dict[str, Any]) -> dict[str, str]:
    patched = dict(headers)
    arkose = str(token_overrides.get("arkose_token") or "").strip()
    genuine = str(token_overrides.get("genuine_token") or "").strip()
    entcaptcha = str(token_overrides.get("entcaptcha_response") or "").strip()
    if entcaptcha or arkose:
        # SUSI v2 createAccountV2UsingPOST uses X-IMS-EntCaptcha-Response.
        patched["X-IMS-EntCaptcha-Response"] = entcaptcha or arkose
    if arkose:
        # Some newer flows use the explicit Arkose header.
        patched["x-ims-arkose-captcha-token"] = arkose
    if genuine:
        patched["x-ims-genuine-token"] = genuine
    return patched


def extract_interface_catalog(html: str, script_text: str) -> dict[str, Any]:
    scripts = re.findall(r"<script[^>]+src=[\"']([^\"']+)", html)
    endpoints = []
    for m in re.finditer(r'new URL\("(/[^"]+)",\$?J?P\)', script_text):
        path = m.group(1)
        if any(k in path.lower() for k in ("account", "user", "password", "captcha", "challenge", "proof", "email", "terms")):
            endpoints.append(path)
    endpoints = sorted(set(endpoints))
    catalog = {
        "auth_base": AUTH_BASE,
        "renga_base": RENGA_BASE,
        "scripts": scripts[:20],
        "registration_endpoints": [
            p for p in endpoints if p in {"/v2/users/accounts", "/v2/accounts", "/v4/accounts", "/v1/passwords/check", "/v1/captcha/encryptedData"}
        ],
        "all_relevant_endpoints": endpoints[:200],
    }
    return catalog


def fetch_auth_assets(session: requests.Session) -> dict[str, Any]:
    html_resp = session.get(
        f"{AUTH_BASE}/en_US/index.html?client_id={DEFAULT_CLIENT_ID}&api=authorize#/signup",
        timeout=45,
    )
    html = html_resp.text or ""
    scripts = re.findall(r"<script[^>]+src=[\"']([^\"']+)", html)
    script_text = ""
    script_url = ""
    if scripts:
        script_url = scripts[0]
        if script_url.startswith("/"):
            script_url = AUTH_BASE + script_url
        script_resp = session.get(script_url, timeout=60)
        script_text = script_resp.text or ""
    catalog = extract_interface_catalog(html, script_text)
    stamp = now()
    html_path = CAPTURE_DIR / f"auth_signup_{stamp}.html"
    script_path = CAPTURE_DIR / f"auth_script_{stamp}.js"
    catalog_path = CAPTURE_DIR / f"auth_interface_catalog_{stamp}.json"
    html_path.write_text(html, encoding="utf-8")
    if script_text:
        script_path.write_text(script_text, encoding="utf-8")
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    log("auth_assets_captured", html_status=html_resp.status_code, script=script_url, catalog=str(catalog_path))
    return {
        "html_status": html_resp.status_code,
        "script_url": script_url,
        "html_path": str(html_path),
        "script_path": str(script_path) if script_text else "",
        "catalog_path": str(catalog_path),
        "catalog": catalog,
    }


def build_account_payload(email: str, password: str, *, terms_name: str = "ADOBE_MASTER") -> dict[str, Any]:
    return {
        "account": {
            "email": email,
            "password": password,
            "firstName": "Li",
            "lastName": "Ming",
            "countryCode": "US",
            "dateOfBirth": {"year": 1995, "month": 1, "day": 1},
            "type": "individual",
            "termsOfUseAcceptances": [
                {"name": terms_name or "ADOBE_MASTER", "language": "en_US", "accepted": True}
            ],
            "marketingConsent": {"accepted": False, "text": ""},
        },
        "clientRedirect": "",
        "redirectUri": "",
        "locale": "en_US",
    }


def classify_attempt(steps: list[dict[str, Any]]) -> tuple[str, str]:
    merged = json.dumps([s.get("response") or s.get("error") or "" for s in steps], ensure_ascii=False).lower()
    if any(s.get("stage") == "create_account_v2" and s.get("ok") for s in steps):
        return "registered", "http registration accepted"
    if "captcha_required" in merged:
        return "challenge", "Adobe direct HTTP create_account_v2 returned captcha_required; browserless flow needs a valid Arkose/EntCaptcha token"
    if "invalid_captcha" in merged or "invalid captcha" in merged:
        return "challenge", "Adobe rejected the supplied Arkose/EntCaptcha token"
    if "captcha" in merged or "arkose" in merged:
        return "challenge", "Adobe requires captcha/arkose token for direct HTTP registration"
    if "genuine" in merged or "ivt" in merged or "identity verification" in merged:
        return "challenge", "Adobe requires browser genuine/identity-verification token"
    if "already" in merged or "exists" in merged:
        return "blocked", "email already exists or account lookup blocked"
    if "403" in merged or "forbidden" in merged:
        return "blocked", "Adobe rejected browserless request with forbidden response"
    return "blocked", "direct HTTP registration did not return an accepted account response"


def run() -> dict[str, Any]:
    cfg = load_config()
    proxy = proxy_from_config(cfg)
    client_id = str(cfg.get("http_register_client_id") or DEFAULT_CLIENT_ID).strip() or DEFAULT_CLIENT_ID
    token_overrides = load_signup_token_overrides(cfg)
    session = create_session(proxy)
    result: dict[str, Any] = {
        "success": False,
        "mode": "http_request",
        "started_at": now(),
        "used_proxy": bool(proxy),
        "proxy": proxy,
        "client_id": client_id,
        "signup_token_overrides": {
            "source": token_overrides.get("source"),
            "has_arkose": token_overrides.get("has_arkose"),
            "has_genuine": token_overrides.get("has_genuine"),
            "has_entcaptcha": token_overrides.get("has_entcaptcha"),
            "masked": token_overrides.get("masked"),
        },
        "account": {},
        "steps": [],
        "assets": {},
        "interface_catalog": {},
        "error": "",
        "classification": "",
    }
    save_result(result)

    log(
        "http_register_start",
        proxy=proxy or None,
        token_source=token_overrides.get("source"),
        has_arkose=bool(token_overrides.get("has_arkose")),
        has_entcaptcha=bool(token_overrides.get("has_entcaptcha")),
        has_genuine=bool(token_overrides.get("has_genuine")),
    )
    inbox = create_tempmail(cfg)
    email = inbox["address"]
    password = f"Aa{uuid.uuid4().hex[:10]}!{int(time.time()) % 1000}"
    account = {
        "id": uuid.uuid4().hex,
        "email": email,
        "password": password,
        "status": "http_registering",
        "eligibility": "unknown",
        "plan": "-",
        "image_status": "untested",
        "email_provider": "tempmail_lol",
        "mail_token": inbox["token"],
        "mail_status": "inbox_created",
        "last_action": "HTTP 请求注册流程启动",
        "ip": "",
    }
    result["account"] = account
    upsert_account(account)
    log("tempmail_created", email=email)

    try:
        assets = fetch_auth_assets(session)
        result["assets"] = assets
        result["interface_catalog"] = assets.get("catalog") or {}

        headers = {
            "content-type": "application/json",
            "x-request-id": str(uuid.uuid4()),
            "x-adobe-app-id": "auth-webapp",
            "x-ims-clientid": client_id,
        }
        create_headers = apply_signup_token_headers(headers, token_overrides)
        steps: list[dict[str, Any]] = []
        config_step = safe_step(
            "get_client_configuration",
            lambda: session.get(f"{SIGNIN_BASE}/v2/configurations/{client_id}", headers=headers, timeout=45),
        )
        steps.append(config_step)
        terms_name = "ADOBE_MASTER"
        if isinstance(config_step.get("response"), dict):
            terms_name = str((config_step.get("response") or {}).get("termsOfUseName") or terms_name)
        steps.append(
            safe_step(
                "get_captcha_encrypted_data",
                lambda: session.get(f"{SIGNIN_BASE}/v1/captcha/encryptedData", headers=headers, timeout=45),
            )
        )
        steps.append(
            safe_step(
                "lookup_user_accounts",
                lambda: session.post(
                    f"{SIGNIN_BASE}/v2/users/accounts",
                    headers=headers,
                    json={"username": email, "usernameType": "EMAIL"},
                    timeout=45,
                ),
            )
        )
        steps.append(
            safe_step(
                "check_password",
                lambda: session.post(
                    f"{SIGNIN_BASE}/v1/passwords/check",
                    headers=headers,
                    json={"password": password, "email": email, "username": email},
                    timeout=45,
                ),
            )
        )
        steps.append(
            safe_step(
                "create_account_v2",
                lambda: session.post(
                    f"{SIGNIN_BASE}/v2/accounts",
                    headers=create_headers,
                    json=build_account_payload(email, password, terms_name=terms_name),
                    timeout=60,
                ),
            )
        )
        result["steps"] = steps
        status, detail = classify_attempt(steps)
        result["classification"] = status
        result["error"] = "" if status == "registered" else detail
        result["success"] = status == "registered"
        account["status"] = "registered" if status == "registered" else f"http_{status}"
        account["mail_status"] = "http_registered" if status == "registered" else "http_no_verification_email"
        account["last_action"] = "HTTP 请求注册成功" if status == "registered" else f"HTTP 请求注册未成功：{detail}"
        upsert_account(account)
        log("http_register_done", success=result["success"], classification=status, detail=detail)
        return result
    except Exception as exc:
        result["success"] = False
        result["classification"] = "exception"
        result["error"] = str(exc)
        account["status"] = "http_exception"
        account["last_action"] = f"HTTP 请求注册异常：{str(exc)[:300]}"
        upsert_account(account)
        log("http_register_exception", error=str(exc))
        return result
    finally:
        result["finished_at"] = now()
        save_result(result)


if __name__ == "__main__":
    save_result(run())
