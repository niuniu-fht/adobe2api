import base64
import hashlib
import io
import json
import logging
import os
import random
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote, urlparse

import requests

from core.config_mgr import config_manager
from core.models import (
    build_image_payload_candidates,
    build_remote_adobe_image_payload_candidates,
    random_image_seed,
)
from core.request_trace import (
    RequestTrace,
    binary_summary,
    response_snapshot,
    sanitize_headers,
    sanitize_trace_value,
    sanitize_url,
)

try:
    from curl_cffi.requests import Session as CurlSession
except Exception:
    CurlSession = None

try:
    from PIL import Image, ImageChops
except Exception:
    Image = None
    ImageChops = None


logger = logging.getLogger("adobe2api")

ADOBE_REFRESH_URL = (
    "https://adobeid-na1.services.adobe.com/ims/check/v6/token"
    "?jslVersion=v2-v0.48.0-1-g1e322cb"
)
ADOBE_EXPRESS_CLIENT_ID = "projectx_webapp"
ADOBE_EXPRESS_SCOPE = "AdobeID,firefly_api,openid"
ADOBE_CLIO_CLIENT_ID = "clio-playground-web"
ADOBE_CLIO_SCOPE = (
    "AdobeID,firefly_api,openid,pps.read,pps.write,"
    "additional_info.projectedProductContext,additional_info.ownerOrg,"
    "uds_read,uds_write,ab.manage,read_organizations,additional_info.roles,"
    "account_cluster.read,creative_production,tk_platform,tk_platform_sync,profile"
)
ADOBE_CREDITS_URL = "https://firefly.adobe.io/v1/credits/balance"
ADOBE_CREDITS_API_KEY = "SunbreakWebUI1"
ADOBE_PROFILE_URLS = (
    "https://ims-na1.adobelogin.com/ims/profile/v1",
    "https://adobeid-na1.services.adobe.com/ims/profile/v1",
)

_ADOBE_FINGERPRINTS = (
    {
        "impersonate": "chrome146",
        "major": 146,
        "platform": '"Windows"',
        "os": "Windows NT 10.0; Win64; x64",
        "sec_ch_ua": '"Chromium";v="146", "Google Chrome";v="146", "Not?A_Brand";v="24"',
    },
    {
        "impersonate": "chrome146",
        "major": 146,
        "platform": '"macOS"',
        "os": "Macintosh; Intel Mac OS X 10_15_7",
        "sec_ch_ua": '"Chromium";v="146", "Google Chrome";v="146", "Not?A_Brand";v="24"',
    },
    {
        "impersonate": "chrome145",
        "major": 145,
        "platform": '"Windows"',
        "os": "Windows NT 10.0; Win64; x64",
        "sec_ch_ua": '"Chromium";v="145", "Google Chrome";v="145", "Not?A_Brand";v="24"',
    },
    {
        "impersonate": "chrome145",
        "major": 145,
        "platform": '"macOS"',
        "os": "Macintosh; Intel Mac OS X 10_15_7",
        "sec_ch_ua": '"Chromium";v="145", "Google Chrome";v="145", "Not?A_Brand";v="24"',
    },
    {
        "impersonate": "chrome133a",
        "major": 133,
        "platform": '"Windows"',
        "os": "Windows NT 10.0; Win64; x64",
        "sec_ch_ua": '"Not(A:Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"',
    },
    {
        "impersonate": "chrome131",
        "major": 131,
        "platform": '"macOS"',
        "os": "Macintosh; Intel Mac OS X 10_15_7",
        "sec_ch_ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    },
)


def _go_json_bytes(value: Any) -> bytes:
    text = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    text = (
        text.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    return text.encode("utf-8")


def _select_adobe_fingerprint() -> dict[str, Any]:
    fingerprint = dict(random.choice(_ADOBE_FINGERPRINTS))
    fingerprint["user_agent"] = (
        f"Mozilla/5.0 ({fingerprint['os']}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{fingerprint['major']}.0.0.0 Safari/537.36"
    )
    return fingerprint


class _BorrowedSession:
    def __init__(self, session: Any):
        self._session = session

    def __enter__(self):
        return self._session

    def __exit__(self, _exc_type, _exc, _tb):
        return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    def get(self, *args, **kwargs):
        kwargs.setdefault("allow_redirects", False)
        return self._session.get(*args, **kwargs)

    def post(self, *args, **kwargs):
        kwargs.setdefault("allow_redirects", False)
        return self._session.post(*args, **kwargs)


def exchange_adobe_cookie(cookie: str, *, proxy: str = "") -> dict[str, Any]:
    cookie_value = str(cookie or "").strip()
    if cookie_value.lower().startswith("cookie:"):
        cookie_value = cookie_value.split(":", 1)[1].strip()
    if not cookie_value:
        raise ValueError("cookie is empty")

    fingerprint = _select_adobe_fingerprint()
    session = None
    if CurlSession is not None:
        kwargs: dict[str, Any] = {
            "impersonate": fingerprint["impersonate"],
            "timeout": 60,
        }
        session = CurlSession(**kwargs)

    def exchange(form: dict[str, str], origin: str) -> dict[str, Any]:
        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
            "cookie": cookie_value,
            "origin": origin,
            "referer": f"{origin}/",
            "user-agent": fingerprint["user_agent"],
        }
        if session is not None:
            response = session.post(
                ADOBE_REFRESH_URL,
                headers=headers,
                data=form,
                allow_redirects=False,
            )
        else:
            response = requests.post(
                ADOBE_REFRESH_URL,
                headers=headers,
                data=form,
                timeout=60,
                allow_redirects=False,
            )
        if response.status_code != 200:
            raise RuntimeError(
                f"refresh request failed: {response.status_code} {response.text[:200]}"
            )
        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError("refresh response is not valid json") from exc
        if not isinstance(payload, dict) or not str(payload.get("access_token") or "").strip():
            raise RuntimeError("refresh response missing access_token")
        return payload

    try:
        express = exchange(
            {
                "client_id": ADOBE_EXPRESS_CLIENT_ID,
                "guest_allowed": "true",
                "scope": ADOBE_EXPRESS_SCOPE,
            },
            "https://new.express.adobe.com",
        )
        selected = express
        used_clio = False
        account_id = str(
            _decode_jwt_payload(str(express.get("access_token") or "")).get("user_id")
            or _decode_jwt_payload(str(express.get("access_token") or "")).get("aa_id")
            or _decode_jwt_payload(str(express.get("access_token") or "")).get("sub")
            or ""
        ).strip()
        if account_id:
            try:
                selected = exchange(
                    {
                        "client_id": ADOBE_CLIO_CLIENT_ID,
                        "scope": ADOBE_CLIO_SCOPE,
                        "user_id": account_id,
                    },
                    "https://firefly.adobe.com",
                )
                used_clio = True
            except Exception as exc:
                logger.warning("Adobe Clio token exchange failed; using Express token: %s", exc)
        return {
            "access_token": str(selected.get("access_token") or "").strip(),
            "expires_in": selected.get("expires_in"),
            "raw": selected,
            "express_raw": express,
            "used_clio": used_clio,
        }
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


def fetch_adobe_account_profile(access_token: str) -> dict[str, Any]:
    token = str(access_token or "").strip()
    if not token:
        return {}

    fingerprint = _select_adobe_fingerprint()
    session = None
    if CurlSession is not None:
        session = CurlSession(
            impersonate=fingerprint["impersonate"],
            timeout=60,
        )
    headers = {
        "authorization": f"Bearer {token}",
        "accept": "application/json",
        "user-agent": fingerprint["user_agent"],
    }
    try:
        for url in ADOBE_PROFILE_URLS:
            try:
                if session is not None:
                    response = session.get(
                        url,
                        headers=headers,
                        allow_redirects=False,
                    )
                else:
                    response = requests.get(
                        url,
                        headers=headers,
                        timeout=60,
                        allow_redirects=False,
                    )
            except Exception:
                continue
            if response.status_code != 200:
                continue
            try:
                payload = response.json()
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            display_name = str(
                payload.get("displayName")
                or payload.get("name")
                or payload.get("fullName")
                or ""
            ).strip()
            email = str(payload.get("email") or "").strip()
            user_id = str(
                payload.get("userId") or payload.get("authId") or ""
            ).strip()
            if display_name or email or user_id:
                return {
                    "display_name": display_name,
                    "email": email,
                    "user_id": user_id,
                    "source": "ims_profile_v1",
                    "updated_at": int(time.time()),
                }
        return {}
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


def fetch_adobe_credits_balance(
    access_token: str, account_id: str, *, proxy: str = ""
) -> dict[str, Any]:
    token = str(access_token or "").strip()
    aid = str(account_id or "").strip()
    unknown = {
        "total": None,
        "used": None,
        "available": None,
        "available_until": None,
        "plan": "",
        "unknown": True,
        "updated_at": int(time.time()),
    }
    if not token:
        return {**unknown, "error": "empty token"}
    if not aid:
        return {**unknown, "error": "no account id"}

    fingerprint = _select_adobe_fingerprint()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    session = None
    if CurlSession is not None:
        kwargs: dict[str, Any] = {
            "impersonate": fingerprint["impersonate"],
            "timeout": 60,
        }
        if proxies:
            kwargs["proxies"] = proxies
        session = CurlSession(**kwargs)
    headers = {
        "authorization": f"Bearer {token}",
        "x-api-key": ADOBE_CREDITS_API_KEY,
        "x-account-id": aid,
        "accept": "application/json",
        "content-type": "application/json",
        "user-agent": fingerprint["user_agent"],
    }
    try:
        try:
            if session is not None:
                response = session.get(
                    ADOBE_CREDITS_URL,
                    headers=headers,
                    allow_redirects=False,
                )
            else:
                response = requests.get(
                    ADOBE_CREDITS_URL,
                    headers=headers,
                    timeout=60,
                    proxies=proxies,
                    allow_redirects=False,
                )
        except Exception as exc:
            return {**unknown, "error": f"network: {exc}"}
        if response.status_code == 401:
            raise AuthError(
                "Adobe credits auth failed",
                status_code=401,
                error_type="auth",
            )
        if response.status_code != 200:
            return {
                **unknown,
                "error": f"http {response.status_code}: {response.text[:160]}",
            }
        try:
            payload = response.json()
        except Exception:
            return {**unknown, "error": "non-json"}
        total_info = payload.get("total", {}) if isinstance(payload, dict) else {}
        quota = total_info.get("quota", {}) if isinstance(total_info, dict) else {}
        return {
            "total": quota.get("total"),
            "used": quota.get("used"),
            "available": quota.get("available"),
            "available_until": total_info.get("availableUntil"),
            "plan": total_info.get("planCap"),
            "unknown": False,
            "error": None,
            "updated_at": int(time.time()),
        }
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

_generated_arp_cache_lock = threading.Lock()
_generated_arp_cache: dict[str, tuple[str, float]] = {}
_GENERATED_ARP_TTL_SECONDS = 6 * 60 * 60

DEFAULT_GPT_IMAGE_MODEL_QUALITIES = {
    "gpt-image-2-high": "high",
    "gpt-image-2-higher": "high",
    "gpt-image-2-clarity": "low",
    "gpt-image-2-**clarity": "low",
    "gpt-image-2-clarity-free": "low",
}


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    raw_token = str(token or "").strip()
    if not raw_token:
        return {}
    parts = raw_token.split(".")
    if len(parts) < 2:
        return {}

    payload_part = parts[1].strip()
    if not payload_part:
        return {}

    padding = (-len(payload_part)) % 4
    if padding:
        payload_part += "=" * padding

    try:
        decoded = base64.urlsafe_b64decode(payload_part.encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _build_submit_nonce(token: str, prompt: str) -> str:
    claims = _decode_jwt_payload(token)
    user_id = str(
        claims.get("user_id")
        or claims.get("aa_id")
        or claims.get("sub")
        or ""
    ).strip()
    prompt_bytes = str(prompt or "").strip().encode("utf-8")[:256]
    if not user_id or not prompt_bytes:
        return ""
    nonce_input = user_id.encode("utf-8") + b"-" + prompt_bytes
    return hashlib.sha256(nonce_input).hexdigest()


def _build_arp_session_id() -> str:
    now_ms = int(time.time() * 1000)
    ftr = (
        f"{os.urandom(16).hex()}_{now_ms}_{random.randint(10000, 99999)}"
        f"_UDF43-m4_31ck_{secrets.token_urlsafe(9)}-{random.randint(1000, 9999)}-v2_tt"
    )
    ark = (
        f"{random.randint(1, 9)}{secrets.token_hex(8)}.{random.randint(1000000000, 9999999999)}"
        "|r=ap-southeast-1"
        "|meta=3"
        "|metabgclr=transparent"
        "|metaiconclr=%23757575"
        "|guitextcolor=%23000000"
        "|pk=BBCC314C-4937-4CCD-B0A3-FDF0F0F7603C"
        "|at=40"
        "|sup=1"
        f"|rid={random.randint(1, 99)}"
        "|ag=101"
        "|cdn_url=https%3A%2F%2Farks-client.adobe.com%2Fcdn%2Ffc"
        "|surl=https%3A%2F%2Farks-client.adobe.com"
        "|smurl=https%3A%2F%2Farks-client.adobe.com%2Fcdn%2Ffc%2Fassets%2Fstyle-manager"
    )
    raw = json.dumps(
        {"sid": str(uuid.uuid4()), "ark": ark, "ftr": ftr},
        separators=(",", ":"),
    )
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


_remote_arp_pid_lock = threading.Lock()
_remote_arp_token_pid: dict[str, int] = {}
_remote_arp_used_pids: set[int] = set()


def _remote_arp_pid(token: str) -> int:
    key = str(token or "").strip() or "default"
    with _remote_arp_pid_lock:
        if key in _remote_arp_token_pid:
            return _remote_arp_token_pid[key]
        while True:
            pid = 1000 + secrets.randbelow(99000)
            if pid not in _remote_arp_used_pids:
                _remote_arp_used_pids.add(pid)
                _remote_arp_token_pid[key] = pid
                return pid


def _build_remote_arp_session_id(token: str) -> str:
    now_ms = int(time.time() * 1000)
    ark = (
        f"{os.urandom(9).hex()[:17]}.{1000000000 + secrets.randbelow(9000000000)}"
        "|r=ap-southeast-1|meta=3|metabgclr=transparent"
        "|metaiconclr=%23757575|guitextcolor=%23000000"
        "|pk=BBCC314C-4937-4CCD-B0A3-FDF0F0F7603C|at=40|sup=1"
        f"|rid={1 + secrets.randbelow(99)}|ag=101"
        "|cdn_url=https%3A%2F%2Farks-client.adobe.com%2Fcdn%2Ffc"
        "|surl=https%3A%2F%2Farks-client.adobe.com"
        "|smurl=https%3A%2F%2Farks-client.adobe.com%2Fcdn%2Ffc%2Fassets%2Fstyle-manager"
    )
    ftr = (
        f"{os.urandom(16).hex()}_{now_ms}_{_remote_arp_pid(token)}"
        "_UDF43-m4_31ck__tt"
    )
    raw = json.dumps(
        {"ark": ark, "ftr": ftr, "sid": str(uuid.uuid4())},
        separators=(",", ":"),
    )
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def _generated_arp_cache_key(token: str) -> str:
    claims = _decode_jwt_payload(token)
    return str(
        claims.get("user_id")
        or claims.get("aa_id")
        or claims.get("sub")
        or claims.get("sid")
        or token[:24]
        or "default"
    ).strip()


def _generated_arp_session_id_for_token(token: str) -> str:
    cache_key = _generated_arp_cache_key(token)
    now = time.time()
    with _generated_arp_cache_lock:
        cached = _generated_arp_cache.get(cache_key)
        if cached and cached[1] > now and _looks_like_firefly_arp_session_id(cached[0]):
            return cached[0]
        value = _build_arp_session_id()
        _generated_arp_cache[cache_key] = (
            value,
            now + _GENERATED_ARP_TTL_SECONDS,
        )
        return value


def _arp_session_id_for_token(token: str) -> str:
    try:
        from core.refresh_mgr import refresh_manager
        from core.token_mgr import token_manager

        meta = token_manager.get_meta_by_value(token)
        profile_id = str(meta.get("refresh_profile_id") or "").strip()
        if not profile_id:
            return ""
        firefly_headers = refresh_manager.get_firefly_headers_for_profile(profile_id)
        return str(firefly_headers.get("x-arp-session-id") or "").strip()
    except Exception:
        return ""


def _configured_arp_session_id() -> str:
    return str(
        os.getenv("ADOBE_X_ARP_SESSION_ID")
        or config_manager.get("firefly_x_arp_session_id", "")
        or ""
    ).strip()


def _preferred_arp_region() -> str:
    return str(os.getenv("ADOBE_FIREFLY_ARP_REGION") or "ap-southeast-1").strip()


def _normalize_arp_session_region(value: str, region: Optional[str] = None) -> str:
    raw_value = str(value or "").strip()
    target_region = str(region if region is not None else _preferred_arp_region()).strip()
    if not raw_value or not target_region:
        return raw_value
    try:
        padding = (-len(raw_value)) % 4
        decoded = base64.b64decode((raw_value + ("=" * padding)).encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
        if not isinstance(data, dict):
            return raw_value
        ark = str(data.get("ark") or "")
        marker = "|r="
        if marker not in ark:
            return raw_value
        prefix, tail = ark.split(marker, 1)
        _, sep, suffix = tail.partition("|")
        data["ark"] = f"{prefix}{marker}{target_region}{sep}{suffix}"
        return base64.b64encode(
            json.dumps(data, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
    except Exception:
        return raw_value


def _looks_like_firefly_arp_session_id(value: str) -> bool:
    raw_value = str(value or "").strip()
    if not raw_value:
        return False
    try:
        padding = (-len(raw_value)) % 4
        decoded = base64.b64decode((raw_value + ("=" * padding)).encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    return bool(
        str(data.get("sid") or "").strip()
        and str(data.get("ark") or "").strip()
        and str(data.get("ftr") or "").strip()
    )


class AdobeRequestError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        error_type: str = "",
        user_message: str = "",
    ):
        super().__init__(message)
        self.status_code = status_code
        self.error_type = str(error_type or "").strip().lower()
        self.user_message = (
            str(user_message or "").strip() or str(message or "").strip()
        )


class QuotaExhaustedError(AdobeRequestError):
    pass


class AuthError(AdobeRequestError):
    pass


class ContentPolicyError(AdobeRequestError):
    def __init__(
        self,
        message: str,
        *,
        upstream_code: str = "",
        param: str = "prompt",
    ):
        super().__init__(
            "图片不安全",
            status_code=400,
            error_type="content_policy_violation",
            user_message="图片不安全",
        )
        self.error_code = "content_policy_violation"
        self.upstream_code = str(upstream_code or "").strip()
        self.param = str(param or "").strip() or "prompt"


class UpstreamTemporaryError(AdobeRequestError):
    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        error_type: str = "",
    ):
        super().__init__(message)
        self.status_code = status_code
        self.error_type = str(error_type or "").strip().lower()


class ReferenceImageRequiredError(AdobeRequestError):
    def __init__(self, message: str = "Image edit use case requires a reference image"):
        super().__init__(
            message,
            status_code=400,
            error_type="invalid_request_error",
            user_message=message,
        )


class RateLimitWaitExceededError(AdobeRequestError):
    def __init__(self):
        super().__init__(
            "Too many requests. Please try again later.",
            status_code=400,
            error_type="invalid_request_error",
            user_message="Too many requests. Please try again later.",
        )


class SubmitRateLimitedError(AdobeRequestError):
    def __init__(self):
        super().__init__(
            "Adobe image submit rate limited; switch account",
            status_code=429,
            error_type="rate_limit_error",
            user_message="Too many requests. Please try again later.",
        )


class PollNanobananaTimeoutError(AdobeRequestError):
    def __init__(self, message: str):
        super().__init__(
            message,
            status_code=408,
            error_type="status",
            user_message=message,
        )


class ImageStageTerminalError(AdobeRequestError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        error_type: str = "server_error",
    ):
        super().__init__(
            message,
            status_code=status_code,
            error_type=error_type,
            user_message=message,
        )


class AdobeClient:
    submit_url = "https://firefly-3p.ff.adobe.io/v2/3p-images/generate-async"
    video_submit_url = "https://firefly-3p.ff.adobe.io/v2/3p-videos/generate-async"
    upload_url = "https://firefly-3p.ff.adobe.io/v2/storage/image"
    upload_video_url = "https://firefly-3p.ff.adobe.io/v2/storage/video"
    upload_audio_url = "https://firefly-3p.ff.adobe.io/v2/storage/audio"
    firefly_video_upload_url = "https://video-v1.ff.adobe.io/v2/storage/image"
    select_subject_url = "https://di-imaging.ff.adobe.io/v1/masking/select-subject"
    entity_api_base = "https://firefly-entity.adobe.io/api/entities/"
    platform_cs_index_url = "https://platform-cs-edge.adobe.io/index"
    platform_cs_base = "https://platform-cs-va6.adobe.io/composite/component/path"

    def __init__(self) -> None:
        self.api_key = "clio-playground-web"
        self.impersonate = "chrome124"
        self.proxy = ""
        self.generate_timeout = 300
        self.retry_enabled = True
        self.retry_max_attempts = 3
        self.retry_backoff_seconds = 1.0
        self.retry_on_status_codes = [429, 451, 500, 502, 503, 504]
        self.retry_on_error_types = {"timeout", "connection", "proxy"}
        self.token_rotation_strategy = "round_robin"
        self.gpt_image_quality = "low"
        self.gpt_image_model_qualities: dict[str, str] = {}
        self.masking_api_key = "clio-playground-web"
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0"
        self.sec_ch_ua = (
            '"Not=A?Brand";v="99", "Microsoft Edge";v="151", "Chromium";v="151"'
        )
        self._fingerprint_local = threading.local()

        self.apply_config(config_manager.get_all())

        env_api_key = os.getenv("ADOBE_API_KEY")
        env_impersonate = os.getenv("ADOBE_IMPERSONATE")
        env_proxy = os.getenv("ADOBE_PROXY")
        env_user_agent = os.getenv("ADOBE_USER_AGENT")
        env_sec_ch_ua = os.getenv("ADOBE_SEC_CH_UA")
        env_generate_timeout = os.getenv("ADOBE_GENERATE_TIMEOUT")

        if env_api_key:
            self.api_key = env_api_key.strip() or self.api_key
        if env_impersonate:
            self.impersonate = env_impersonate.strip() or self.impersonate
        if env_proxy is not None:
            self.proxy = env_proxy.strip()
        if env_user_agent:
            self.user_agent = env_user_agent.strip() or self.user_agent
        if env_sec_ch_ua:
            self.sec_ch_ua = env_sec_ch_ua.strip() or self.sec_ch_ua
        if env_generate_timeout:
            try:
                self.generate_timeout = int(env_generate_timeout)
                if self.generate_timeout <= 0:
                    self.generate_timeout = 300
            except Exception:
                pass

    def apply_config(self, cfg: dict) -> None:
        proxy = str(cfg.get("proxy", "")).strip()
        use_proxy = bool(cfg.get("use_proxy", False))
        timeout_val = cfg.get("generate_timeout", 300)
        try:
            timeout_val = int(timeout_val)
        except Exception:
            timeout_val = 300
        self.generate_timeout = timeout_val if timeout_val > 0 else 300
        self.proxy = proxy if use_proxy and proxy else ""
        self.retry_enabled = bool(cfg.get("retry_enabled", True))
        gpt_quality = str(cfg.get("gpt_image_quality", "low") or "low").strip().lower()
        if gpt_quality not in {"low", "medium", "high"}:
            gpt_quality = "low"
        self.gpt_image_quality = gpt_quality
        self.masking_api_key = (
            str(
                cfg.get("masking_api_key")
                or cfg.get("di_imaging_api_key")
                or "clio-playground-web"
            ).strip()
            or "clio-playground-web"
        )
        model_qualities = cfg.get("gpt_image_model_qualities", {})
        if not isinstance(model_qualities, dict):
            model_qualities = {}
        normalized_model_qualities: dict[str, str] = dict(
            DEFAULT_GPT_IMAGE_MODEL_QUALITIES
        )
        for raw_model_id, raw_quality in model_qualities.items():
            model_id = str(raw_model_id or "").strip()
            quality = str(raw_quality or "").strip().lower()
            if not model_id or quality not in {"low", "medium", "high"}:
                continue
            normalized_model_qualities[model_id] = quality
        self.gpt_image_model_qualities = normalized_model_qualities
        try:
            attempts = int(cfg.get("retry_max_attempts", 3))
        except Exception:
            attempts = 3
        self.retry_max_attempts = max(1, min(attempts, 10))

        try:
            backoff = float(cfg.get("retry_backoff_seconds", 1.0))
        except Exception:
            backoff = 1.0
        self.retry_backoff_seconds = max(0.0, min(backoff, 30.0))

        status_codes_raw = cfg.get(
            "retry_on_status_codes", [429, 451, 500, 502, 503, 504]
        )
        parsed_status_codes: list[int] = []
        if isinstance(status_codes_raw, list):
            for item in status_codes_raw:
                try:
                    val = int(item)
                except Exception:
                    continue
                if 100 <= val <= 599:
                    parsed_status_codes.append(val)
        self.retry_on_status_codes = sorted(set(parsed_status_codes)) or [
            429,
            451,
            500,
            502,
            503,
            504,
        ]

        error_types_raw = cfg.get(
            "retry_on_error_types", ["timeout", "connection", "proxy"]
        )
        parsed_error_types: set[str] = set()
        if isinstance(error_types_raw, list):
            for item in error_types_raw:
                txt = str(item or "").strip().lower()
                if txt:
                    parsed_error_types.add(txt)
        self.retry_on_error_types = parsed_error_types or {
            "timeout",
            "connection",
            "proxy",
        }

        strategy = (
            str(cfg.get("token_rotation_strategy", "round_robin") or "round_robin")
            .strip()
            .lower()
        )
        if strategy not in {"round_robin", "random"}:
            strategy = "round_robin"
        self.token_rotation_strategy = strategy
        if self.proxy:
            logger.warning("proxy enabled for upstream requests: %s", self.proxy)
        else:
            logger.warning("proxy disabled for upstream requests")

    def is_gpt_image_model_alias(self, model_id: Optional[str]) -> bool:
        model_id = str(model_id or "").strip()
        return bool(model_id and model_id in self.gpt_image_model_qualities)

    def get_gpt_image_quality(self, model_id: Optional[str] = None) -> str:
        model_id = str(model_id or "").strip()
        if model_id == "gpt-image-2":
            return self.gpt_image_quality
        if model_id and model_id in self.gpt_image_model_qualities:
            return self.gpt_image_model_qualities[model_id]
        return self.gpt_image_quality

    def _retry_delay_for_attempt(self, attempt: int) -> float:
        base = float(self.retry_backoff_seconds or 0.0)
        if base <= 0:
            return 0.0
        safe_attempt = max(1, int(attempt))
        return min(30.0, base * (2 ** (safe_attempt - 1)))

    def should_retry_temporary_error(self, exc: UpstreamTemporaryError) -> bool:
        if not self.retry_enabled:
            return False
        if isinstance(exc, UpstreamTemporaryError):
            if exc.error_type in {
                "adobe_temporary",
                "adobe_dead_upstream",
                "adobe_download",
            }:
                return True
            if exc.status_code is not None:
                try:
                    return int(exc.status_code) in set(self.retry_on_status_codes)
                except Exception:
                    return False
            if exc.error_type:
                return exc.error_type in set(self.retry_on_error_types)
        return False

    @staticmethod
    def _config_int(key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(config_manager.get(key, default) or default)
        except Exception:
            value = default
        return max(minimum, min(maximum, value))

    def _image_network_retry_seconds(self) -> int:
        return self._config_int("image_network_retry_seconds", 180, 30, 1800)

    def _image_rate_limit_wait_seconds(self) -> int:
        return self._config_int("image_rate_limit_wait_seconds", 180, 30, 1800)

    def _image_submit_network_retry_seconds(self) -> int:
        return self._config_int("image_submit_network_retry_seconds", 60, 30, 1800)

    def _image_submit_rate_limit_wait_seconds(self) -> int:
        return self._config_int("image_submit_rate_limit_wait_seconds", 60, 30, 1800)

    def _image_rate_limit_single_retry_seconds(self) -> float:
        return 3.0

    def _image_download_attempts(self) -> int:
        return self._config_int("image_download_attempts", 5, 1, 10)

    @staticmethod
    def _response_retry_after(response: Any) -> float:
        try:
            headers = getattr(response, "headers", {}) or {}
            return max(
                0.0,
                float(
                    headers.get("retry-after")
                    or headers.get("Retry-After")
                    or 0.0
                ),
            )
        except Exception:
            return 0.0

    @staticmethod
    def _retry_delay(attempt: int, *, rate_limited: bool, retry_after: float = 0.0) -> float:
        schedule = (2.0, 4.0, 8.0, 15.0, 30.0) if rate_limited else (
            1.0,
            2.0,
            4.0,
            8.0,
            15.0,
        )
        base = schedule[min(max(1, int(attempt)) - 1, len(schedule) - 1)]
        delay = max(base, float(retry_after or 0.0))
        return max(0.05, delay * random.uniform(0.8, 1.2))

    @staticmethod
    def _submit_retry_delay(
        attempt: int, *, rate_limited: bool, retry_after: float = 0.0
    ) -> float:
        if rate_limited and float(retry_after or 0.0) > 0:
            return max(0.05, float(retry_after))
        schedule = (2.0, 4.0, 4.0, 4.0, 4.0)
        base = schedule[min(max(1, int(attempt)) - 1, len(schedule) - 1)]
        return max(0.05, base * random.uniform(0.8, 1.2))

    @staticmethod
    def _is_retryable_image_status(status_code: int) -> bool:
        normalized = int(status_code or 0)
        return normalized in {408, 425, 451} or 500 <= normalized <= 599

    @staticmethod
    def _is_fal_nanobanana_timeout_response(resp) -> bool:
        if int(getattr(resp, "status_code", 0) or 0) != 408:
            return False
        text = str(getattr(resp, "text", "") or "").lower()
        body = None
        try:
            body = resp.json()
        except Exception:
            body = None
        if isinstance(body, dict):
            text = f"{text} {json.dumps(body, ensure_ascii=False).lower()}"
        return (
            "fal-nanobanana" in text
            and "timeout" in text
            and "request timed out" in text
        )

    @staticmethod
    def _is_invalid_image_size_aspect_response(resp: Any) -> bool:
        if int(getattr(resp, "status_code", 0) or 0) != 400:
            return False
        text = str(getattr(resp, "text", "") or "").lower()
        return "invalid image size" in text and "aspect ratio must not exceed" in text

    @staticmethod
    def _auto_size_fallback_payload(payload: dict) -> dict:
        fallback = dict(payload or {})
        model_specific = dict(fallback.get("modelSpecificPayload") or {})
        model_id = str(fallback.get("modelId") or "").strip().lower()
        if model_id != "gpt-image":
            return fallback
        model_specific.pop("aspectRatio", None)
        model_specific["size"] = "auto"
        fallback.pop("size", None)
        fallback.pop("outputResolution", None)
        fallback["modelSpecificPayload"] = model_specific
        return fallback

    @staticmethod
    def _wait_with_cancel(
        delay: float,
        *,
        cancel_check: Optional[Callable[[], None]] = None,
    ) -> None:
        deadline = time.time() + max(0.0, float(delay))
        while True:
            if cancel_check is not None:
                cancel_check()
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            time.sleep(min(0.25, remaining))

    @staticmethod
    def _run_image_io(
        io_call: Optional[Callable[[Callable[[], Any]], Any]],
        operation: Callable[[], Any],
    ) -> Any:
        return io_call(operation) if io_call is not None else operation()

    def _wait_for_image_retry(
        self,
        delay: float,
        *,
        cancel_check: Optional[Callable[[], None]],
        wait_cb: Optional[Callable[[float], None]],
    ) -> None:
        if cancel_check is not None:
            cancel_check()
        if wait_cb is not None:
            wait_cb(max(0.0, float(delay)))
            if cancel_check is not None:
                cancel_check()
            return
        self._wait_with_cancel(delay, cancel_check=cancel_check)

    @staticmethod
    def _classify_network_error_type(exc: Exception) -> str:
        text = str(exc or "").strip().lower()
        if "timed out" in text or "timeout" in text:
            return "timeout"
        if "proxy" in text:
            return "proxy"
        if (
            "connection" in text
            or "dns" in text
            or "resolve" in text
            or "refused" in text
            or "reset" in text
            or "unreachable" in text
        ):
            return "connection"
        return "network"

    def _requests_proxies(self, *, use_proxy: bool = True) -> Optional[dict]:
        if not use_proxy or not self.proxy:
            return None
        return {"http": self.proxy, "https": self.proxy}

    def _session(
        self, *, use_proxy: bool = True, headers: Optional[dict] = None
    ):
        borrowed = getattr(self._fingerprint_local, "session_override", None)
        if borrowed is not None:
            return _BorrowedSession(borrowed)
        if CurlSession is None:
            return None
        fingerprint = getattr(self._fingerprint_local, "current", None)
        user_agent = str((headers or {}).get("user-agent") or (headers or {}).get("User-Agent") or "")
        if user_agent and "Edg/" not in user_agent:
            for candidate in _ADOBE_FINGERPRINTS:
                if f"Chrome/{candidate['major']}." in user_agent:
                    fingerprint = candidate
                    break
        impersonate = (
            str(fingerprint.get("impersonate") or "")
            if isinstance(fingerprint, dict)
            else ""
        )
        kwargs = {"impersonate": impersonate or self.impersonate, "timeout": 60}
        if use_proxy and self.proxy:
            kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
        return CurlSession(**kwargs)

    def _new_remote_adobe_session(
        self,
        fingerprint: dict[str, Any],
        *,
        use_proxy: bool,
    ) -> Any:
        if CurlSession is None:
            return None
        kwargs: dict[str, Any] = {
            "impersonate": str(fingerprint.get("impersonate") or self.impersonate),
            "timeout": 60,
        }
        if use_proxy and self.proxy:
            kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
        return CurlSession(**kwargs)

    def _set_session_override(self, session: Any) -> None:
        self._fingerprint_local.session_override = session

    def _clear_session_override(self) -> None:
        self._fingerprint_local.session_override = None

    def _browser_headers(
        self,
        *,
        remote_profile: bool = False,
        fingerprint: Optional[dict[str, Any]] = None,
    ) -> dict:
        if remote_profile:
            fingerprint = fingerprint or _select_adobe_fingerprint()
            self._fingerprint_local.current = fingerprint
        else:
            self._fingerprint_local.current = None
        return {
            "user-agent": (
                fingerprint["user_agent"] if fingerprint else self.user_agent
            ),
            "origin": "https://new.express.adobe.com",
            "referer": "https://new.express.adobe.com/",
            "accept-language": "en-US,en;q=0.9",
            "sec-ch-ua": fingerprint["sec_ch_ua"] if fingerprint else self.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": (
                fingerprint["platform"] if fingerprint else '"Windows"'
            ),
            "sec-fetch-site": "cross-site",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        }

    def _submit_headers(
        self,
        token: str,
        prompt: str = "",
        *,
        protocol_profile: str = "",
        fingerprint: Optional[dict[str, Any]] = None,
    ) -> dict:
        is_remote = protocol_profile == "remote_adobe"
        if is_remote:
            fingerprint = fingerprint or _select_adobe_fingerprint()
            self._fingerprint_local.current = fingerprint
            arp_session_id = _build_remote_arp_session_id(token)
            headers = {
                "authorization": f"Bearer {token}",
                "x-api-key": ADOBE_EXPRESS_CLIENT_ID,
                "x-arp-session-id": arp_session_id,
                "content-type": "application/json",
                "accept": "*/*",
                "origin": "https://new.express.adobe.com",
                "referer": "https://new.express.adobe.com/",
                "accept-language": "en-US,en;q=0.9",
                "sec-ch-ua": fingerprint["sec_ch_ua"],
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": fingerprint["platform"],
                "sec-fetch-site": "cross-site",
                "sec-fetch-mode": "cors",
                "sec-fetch-dest": "empty",
                "user-agent": fingerprint["user_agent"],
            }
            nonce = _build_submit_nonce(token, prompt)
            if nonce:
                headers["x-nonce"] = nonce
            return headers

        headers = self._browser_headers(
            remote_profile=is_remote,
            fingerprint=fingerprint,
        )
        if not is_remote:
            headers.update(
                {
                    "origin": "https://firefly.adobe.com",
                    "referer": "https://firefly.adobe.com/",
                    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
                }
            )
        headers.update(
            {
                "Authorization": f"Bearer {token}",
                "x-api-key": (
                    ADOBE_EXPRESS_CLIENT_ID if is_remote else self.api_key
                ),
                "content-type": "application/json",
                "accept": "*/*",
            }
        )
        if not is_remote:
            headers.update(
                {
                    "cache-control": "no-cache",
                    "pragma": "no-cache",
                    "priority": "u=1, i",
                }
            )
        nonce = _build_submit_nonce(token, prompt)
        if nonce:
            headers["x-nonce"] = nonce
        arp_session_id = (
            _arp_session_id_for_token(token)
            or _configured_arp_session_id()
            or _generated_arp_session_id_for_token(token)
        )
        arp_session_id = _normalize_arp_session_region(arp_session_id)
        if _looks_like_firefly_arp_session_id(arp_session_id):
            headers["x-arp-session-id"] = arp_session_id
        return headers

    def _submit_headers_minimal(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "x-api-key": self.api_key,
            "content-type": "application/json",
            "accept": "*/*",
        }

    def _video_submit_headers(self, token: str) -> dict:
        headers = self._browser_headers()
        headers.update(
            {
                "Authorization": f"Bearer {token}",
                "x-api-key": self.api_key,
                "content-type": "application/json",
                "accept": "*/*",
            }
        )
        return headers

    def _poll_headers(
        self,
        token: str,
        *,
        protocol_profile: str = "",
        fingerprint: Optional[dict[str, Any]] = None,
    ) -> dict:
        if protocol_profile == "remote_adobe":
            fingerprint = fingerprint or _select_adobe_fingerprint()
            self._fingerprint_local.current = fingerprint
            return {
                "authorization": f"Bearer {token}",
                "accept": "*/*",
                "origin": "https://new.express.adobe.com",
                "referer": "https://new.express.adobe.com/",
                "user-agent": fingerprint["user_agent"],
            }
        return {
            "Authorization": f"Bearer {token}",
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
            "cache-control": "no-cache",
            "origin": "https://firefly.adobe.com",
            "pragma": "no-cache",
            "priority": "u=1, i",
            "referer": "https://firefly.adobe.com/",
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
            "user-agent": self.user_agent,
        }

    def _select_subject_headers(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "accept": "application/json",
            "content-type": "application/json",
            "origin": "https://firefly.adobe.com",
            "referer": "https://firefly.adobe.com/",
            "user-agent": self.user_agent,
            "x-api-key": self.masking_api_key,
        }

    def _entity_headers(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "x-api-key": self.api_key,
            "content-type": "application/json",
            "accept": "application/json",
        }

    def _post_json(
        self,
        url: str,
        headers: dict,
        payload: dict,
        *,
        legacy_451_fallback: bool = True,
        go_json: bool = False,
    ):
        session = self._session(headers=headers)
        encoded_payload = _go_json_bytes(payload) if go_json else None
        if session is None:
            try:
                return requests.post(
                    url,
                    headers=headers,
                    json=None if go_json else payload,
                    data=encoded_payload,
                    timeout=60,
                    proxies=self._requests_proxies(),
                )
            except requests.Timeout as exc:
                raise UpstreamTemporaryError(
                    f"upstream timeout: {exc}", error_type="timeout"
                )
            except requests.exceptions.ProxyError as exc:
                raise UpstreamTemporaryError(
                    f"upstream proxy error: {exc}", error_type="proxy"
                )
            except requests.ConnectionError as exc:
                raise UpstreamTemporaryError(
                    f"upstream connection error: {exc}", error_type="connection"
                )
            except requests.RequestException as exc:
                raise UpstreamTemporaryError(
                    f"upstream request error: {exc}", error_type="network"
                )
        try:
            with session:
                if go_json:
                    resp = session.post(url, headers=headers, data=encoded_payload)
                else:
                    resp = session.post(url, headers=headers, json=payload)
        except Exception as exc:
            raise UpstreamTemporaryError(
                f"upstream session error: {exc}",
                error_type=self._classify_network_error_type(exc),
            )
        if resp.status_code == 451 and legacy_451_fallback:
            try:
                return requests.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=60,
                    proxies=self._requests_proxies(),
                )
            except requests.Timeout as exc:
                raise UpstreamTemporaryError(
                    f"upstream timeout: {exc}", status_code=451, error_type="timeout"
                )
            except requests.exceptions.ProxyError as exc:
                raise UpstreamTemporaryError(
                    f"upstream proxy error: {exc}", status_code=451, error_type="proxy"
                )
            except requests.ConnectionError as exc:
                raise UpstreamTemporaryError(
                    f"upstream connection error: {exc}",
                    status_code=451,
                    error_type="connection",
                )
            except requests.RequestException as exc:
                raise UpstreamTemporaryError(
                    f"upstream request error: {exc}",
                    status_code=451,
                    error_type="network",
                )
        return resp

    def _post_json_requests_once(self, url: str, headers: dict, payload: dict):
        try:
            return requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=60,
                proxies=self._requests_proxies(),
            )
        except requests.Timeout as exc:
            raise UpstreamTemporaryError(
                f"upstream timeout: {exc}", error_type="timeout"
            ) from exc
        except requests.exceptions.ProxyError as exc:
            raise UpstreamTemporaryError(
                f"upstream proxy error: {exc}", error_type="proxy"
            ) from exc
        except requests.ConnectionError as exc:
            raise UpstreamTemporaryError(
                f"upstream connection error: {exc}", error_type="connection"
            ) from exc
        except requests.RequestException as exc:
            raise UpstreamTemporaryError(
                f"upstream request error: {exc}", error_type="network"
            ) from exc

    def _post_image_json(
        self, url: str, headers: dict, payload: dict, *, strict_transport: bool = False
    ):
        primary_response = None
        primary_error: Optional[Exception] = None
        try:
            primary_response = self._post_json(
                url,
                headers,
                payload,
                legacy_451_fallback=False,
                go_json=strict_transport,
            )
        except ContentPolicyError:
            raise
        except Exception as exc:
            primary_error = exc

        if strict_transport and primary_response is not None:
            return primary_response

        if primary_response is not None:
            self._raise_if_image_unsafe(primary_response, param="prompt")
            if (
                primary_response.status_code == 429
                or self._is_rate_limited_response(primary_response)
            ):
                return primary_response
            if primary_response.status_code == 200:
                try:
                    primary_data = primary_response.json()
                except Exception:
                    primary_data = None
                if primary_data is not None and self._extract_result_link(
                    primary_response, primary_data
                ):
                    return primary_response

        if strict_transport:
            if primary_response is not None:
                return primary_response
            if primary_error is not None:
                raise primary_error
            raise UpstreamTemporaryError(
                "upstream session returned no response", error_type="network"
            )

        logger.warning(
            "image submit primary transport failed; retrying with requests status=%s error=%s",
            getattr(primary_response, "status_code", None),
            str(primary_error or ""),
        )
        fallback_response = self._post_json_requests_once(url, headers, payload)
        self._raise_if_image_unsafe(fallback_response, param="prompt")
        return fallback_response

    def _post_bytes(
        self, url: str, headers: dict, payload: bytes, *, use_proxy: bool = True
    ):
        session = self._session(use_proxy=use_proxy, headers=headers)
        if session is None:
            try:
                return requests.post(
                    url,
                    headers=headers,
                    data=payload,
                    timeout=60,
                    proxies=self._requests_proxies(use_proxy=use_proxy),
                )
            except requests.Timeout as exc:
                raise UpstreamTemporaryError(
                    f"upstream timeout: {exc}", error_type="timeout"
                )
            except requests.exceptions.ProxyError as exc:
                raise UpstreamTemporaryError(
                    f"upstream proxy error: {exc}", error_type="proxy"
                )
            except requests.ConnectionError as exc:
                raise UpstreamTemporaryError(
                    f"upstream connection error: {exc}", error_type="connection"
                )
            except requests.RequestException as exc:
                raise UpstreamTemporaryError(
                    f"upstream request error: {exc}", error_type="network"
                )
        try:
            with session:
                resp = session.post(url, headers=headers, data=payload)
        except Exception as exc:
            raise UpstreamTemporaryError(
                f"upstream session error: {exc}",
                error_type=self._classify_network_error_type(exc),
            )
        return resp

    def _put_bytes(self, url: str, headers: dict, payload: bytes):
        session = self._session()
        if session is None:
            try:
                return requests.put(
                    url,
                    headers=headers,
                    data=payload,
                    timeout=60,
                    proxies=self._requests_proxies(),
                )
            except requests.Timeout as exc:
                raise UpstreamTemporaryError(
                    f"upstream timeout: {exc}", error_type="timeout"
                )
            except requests.exceptions.ProxyError as exc:
                raise UpstreamTemporaryError(
                    f"upstream proxy error: {exc}", error_type="proxy"
                )
            except requests.ConnectionError as exc:
                raise UpstreamTemporaryError(
                    f"upstream connection error: {exc}", error_type="connection"
                )
            except requests.RequestException as exc:
                raise UpstreamTemporaryError(
                    f"upstream request error: {exc}", error_type="network"
                )
        try:
            with session:
                resp = session.put(url, headers=headers, data=payload)
        except Exception as exc:
            raise UpstreamTemporaryError(
                f"upstream session error: {exc}",
                error_type=self._classify_network_error_type(exc),
            )
        return resp

    def _get(
        self, url: str, headers: dict, timeout: int = 60, *, use_proxy: bool = True
    ):
        session = self._session(use_proxy=use_proxy, headers=headers)
        if session is None:
            try:
                return requests.get(
                    url,
                    headers=headers,
                    timeout=timeout,
                    proxies=self._requests_proxies(use_proxy=use_proxy),
                )
            except requests.Timeout as exc:
                raise UpstreamTemporaryError(
                    f"upstream timeout: {exc}", error_type="timeout"
                )
            except requests.exceptions.ProxyError as exc:
                raise UpstreamTemporaryError(
                    f"upstream proxy error: {exc}", error_type="proxy"
                )
            except requests.ConnectionError as exc:
                raise UpstreamTemporaryError(
                    f"upstream connection error: {exc}", error_type="connection"
                )
            except requests.RequestException as exc:
                raise UpstreamTemporaryError(
                    f"upstream request error: {exc}", error_type="network"
                )
        try:
            with session:
                resp = session.get(url, headers=headers)
        except Exception as exc:
            raise UpstreamTemporaryError(
                f"upstream session error: {exc}",
                error_type=self._classify_network_error_type(exc),
            )
        return resp

    def _delete(self, url: str, headers: dict, timeout: int = 60):
        session = self._session()
        if session is None:
            try:
                return requests.delete(
                    url,
                    headers=headers,
                    timeout=timeout,
                    proxies=self._requests_proxies(),
                )
            except requests.Timeout as exc:
                raise UpstreamTemporaryError(
                    f"upstream timeout: {exc}", error_type="timeout"
                )
            except requests.exceptions.ProxyError as exc:
                raise UpstreamTemporaryError(
                    f"upstream proxy error: {exc}", error_type="proxy"
                )
            except requests.ConnectionError as exc:
                raise UpstreamTemporaryError(
                    f"upstream connection error: {exc}", error_type="connection"
                )
            except requests.RequestException as exc:
                raise UpstreamTemporaryError(
                    f"upstream request error: {exc}", error_type="network"
                )
        try:
            with session:
                resp = session.delete(url, headers=headers)
        except Exception as exc:
            raise UpstreamTemporaryError(
                f"upstream session error: {exc}",
                error_type=self._classify_network_error_type(exc),
            )
        return resp

    def _get_json(self, url: str, headers: dict, timeout: int = 60) -> Any:
        resp = self._get(url, headers=headers, timeout=timeout)
        if resp.status_code in (401, 403):
            raise AuthError("Token invalid or expired")
        if resp.status_code != 200:
            if resp.status_code in (429, 451) or resp.status_code >= 500:
                raise UpstreamTemporaryError(
                    f"upstream get failed: {resp.status_code} {resp.text[:300]}",
                    status_code=resp.status_code,
                    error_type="status",
                )
            raise AdobeRequestError(
                f"upstream get failed: {resp.status_code} {resp.text[:300]}"
            )
        try:
            return resp.json()
        except Exception:
            raise AdobeRequestError("upstream get failed: invalid response")

    def _download_to_file(
        self,
        url: str,
        headers: Optional[dict],
        out_path: Path,
        timeout: int = 60,
        chunk_size: int = 1024 * 1024,
        use_proxy: bool = True,
    ) -> int:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        try:
            with requests.get(
                url,
                headers=headers or {},
                timeout=timeout,
                proxies=self._requests_proxies(use_proxy=use_proxy),
                stream=True,
            ) as resp:
                resp.raise_for_status()
                with out_path.open("wb") as f:
                    for chunk in resp.iter_content(chunk_size=chunk_size):
                        if not chunk:
                            continue
                        f.write(chunk)
                        total += len(chunk)
        except requests.Timeout as exc:
            raise UpstreamTemporaryError(f"upstream timeout: {exc}", error_type="timeout")
        except requests.exceptions.ProxyError as exc:
            raise UpstreamTemporaryError(
                f"upstream proxy error: {exc}", error_type="proxy"
            )
        except requests.ConnectionError as exc:
            raise UpstreamTemporaryError(
                f"upstream connection error: {exc}", error_type="connection"
            )
        except requests.HTTPError as exc:
            response = getattr(exc, "response", None)
            status_code = int(getattr(response, "status_code", 0) or 0)
            body = str(getattr(response, "text", "") or "")[:300]
            raise UpstreamTemporaryError(
                f"upstream download failed: {status_code or '?'} {body}",
                status_code=status_code or None,
                error_type="download_http",
            ) from exc
        except requests.RequestException as exc:
            raise UpstreamTemporaryError(f"upstream request error: {exc}", error_type="network")
        return total

    @staticmethod
    def _validate_downloaded_image(
        *, image_bytes: Optional[bytes] = None, image_path: Optional[Path] = None
    ) -> None:
        if image_path is not None:
            if not image_path.exists() or image_path.stat().st_size <= 0:
                raise AdobeRequestError("downloaded image is empty")
            if Image is None:
                return
            try:
                with Image.open(image_path) as image:
                    image.verify()
            except Exception as exc:
                raise AdobeRequestError(f"downloaded file is not a valid image: {exc}") from exc
            return
        if not image_bytes:
            raise AdobeRequestError("downloaded image is empty")
        if Image is None:
            return
        try:
            import io

            with Image.open(io.BytesIO(image_bytes)) as image:
                image.verify()
        except Exception as exc:
            raise AdobeRequestError(f"downloaded body is not a valid image: {exc}") from exc

    def _refresh_image_result_url(
        self,
        poll_url: str,
        token: str,
        *,
        io_call: Optional[Callable[[Callable[[], Any]], Any]] = None,
        protocol_profile: str = "",
        fingerprint: Optional[dict[str, Any]] = None,
    ) -> str:
        resp = self._run_image_io(
            io_call,
            lambda: self._get(
                poll_url,
                headers=self._poll_headers(
                    token,
                    protocol_profile=protocol_profile,
                    fingerprint=fingerprint,
                ),
                timeout=60,
                use_proxy=(protocol_profile != "remote_adobe"),
            ),
        )
        self._raise_if_image_unsafe(resp, param="prompt")
        if resp.status_code != 200:
            raise UpstreamTemporaryError(
                f"refresh result url failed: {resp.status_code} {resp.text[:300]}",
                status_code=resp.status_code,
                error_type="status",
            )
        data = resp.json()
        self._raise_if_image_unsafe_data(data, param="prompt")
        outputs = data.get("outputs") or []
        return str((((outputs[0] or {}).get("image") or {}).get("presignedUrl")) or "")

    def _download_image_result(
        self,
        *,
        image_url: str,
        poll_url: str,
        token: str,
        out_path: Optional[Path],
        progress_cb: Optional[Callable[[dict], None]],
        trace: Optional[RequestTrace],
        trace_parent_id: Optional[str],
        upstream_job_id: str,
        cancel_check: Optional[Callable[[], None]],
        io_call: Optional[Callable[[Callable[[], Any]], Any]] = None,
        wait_cb: Optional[Callable[[float], None]] = None,
        protocol_profile: str = "",
        fingerprint: Optional[dict[str, Any]] = None,
        session: Any = None,
    ) -> Optional[bytes]:
        if protocol_profile == "remote_adobe":
            download_fingerprint = fingerprint or _select_adobe_fingerprint()
            self._fingerprint_local.current = download_fingerprint
            headers = {
                "accept": "*/*",
                "user-agent": download_fingerprint["user_agent"],
            }
            deadline = time.monotonic() + 180.0
            last_error: Optional[Exception] = None
            waits = (0.0, 1.0, 2.0, 5.0, 10.0)
            for attempt, delay in enumerate(waits, start=1):
                if delay:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(delay, remaining))
                if time.monotonic() >= deadline:
                    break
                response = None
                try:
                    self._set_session_override(session)
                    response = self._run_image_io(
                        io_call,
                        lambda: self._get(
                            image_url,
                            headers=headers,
                            timeout=max(
                                1,
                                min(60, int(deadline - time.monotonic())),
                            ),
                            use_proxy=False,
                        ),
                    )
                except UpstreamTemporaryError as exc:
                    last_error = exc
                    continue
                finally:
                    self._clear_session_override()
                status_code = int(getattr(response, "status_code", 0) or 0)
                if status_code != 200:
                    last_error = AdobeRequestError(
                        f"adobe download failed: {status_code} {response.text[:200]}",
                        status_code=status_code,
                        error_type="download",
                    )
                    if status_code >= 500:
                        continue
                    raise last_error
                image_bytes = bytes(response.content or b"")
                if out_path is not None:
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path.write_bytes(image_bytes)
                    return None
                return image_bytes
            raise UpstreamTemporaryError(
                f"download failed after {len(waits)} attempts: {last_error}",
                status_code=502,
                error_type="adobe_download",
            )

        download_fingerprint = None
        if protocol_profile == "remote_adobe":
            download_fingerprint = fingerprint or _select_adobe_fingerprint()
            self._fingerprint_local.current = download_fingerprint
        attempts = self._image_download_attempts()
        delays = (
            (1.0, 2.0, 5.0, 10.0)
            if protocol_profile == "remote_adobe"
            else (1.0, 2.0, 4.0, 8.0)
        )
        current_url = str(image_url or "")
        last_error: Optional[Exception] = None
        part_path = out_path.with_name(f"{out_path.name}.part") if out_path else None

        for attempt in range(1, attempts + 1):
            if cancel_check is not None:
                cancel_check()
            if progress_cb is not None:
                progress_cb(
                    {
                        "task_status": "DOWNLOADING" if attempt == 1 else "DOWNLOAD_RETRY",
                        "upstream_job_id": upstream_job_id,
                        "download_attempt": attempt,
                        "retry_after": None,
                    }
                )
            download_headers = {"accept": "*/*"}
            if download_fingerprint is not None:
                download_headers["user-agent"] = download_fingerprint["user_agent"]
            download_stage_id = None
            if trace is not None:
                download_stage_id = trace.start_stage(
                    layer="adobe",
                    kind="download",
                    name="下载生成结果",
                    parent_id=trace_parent_id,
                    attempt={"number": attempt, "max_attempts": attempts},
                    request={
                        "method": "GET",
                        "url": sanitize_url(current_url),
                        "headers": sanitize_headers(download_headers),
                    },
                )
            response = None
            try:
                if part_path is not None:
                    part_path.unlink(missing_ok=True)
                    if protocol_profile == "remote_adobe":
                        download_file = lambda: self._download_to_file(
                            current_url,
                            headers=download_headers,
                            out_path=part_path,
                            timeout=(60 if protocol_profile == "remote_adobe" else 30),
                            use_proxy=False,
                        )
                    else:
                        download_file = lambda: self._download_to_file(
                            current_url,
                            headers=download_headers,
                            out_path=part_path,
                            timeout=30,
                        )
                    downloaded_size = self._run_image_io(
                        io_call,
                        download_file,
                    )
                    self._validate_downloaded_image(image_path=part_path)
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(part_path, out_path)
                    if trace is not None:
                        trace.finish_stage(
                            download_stage_id,
                            status="succeeded",
                            response={
                                "file": binary_summary(
                                    out_path.read_bytes(), filename=out_path.name
                                ),
                                "size_bytes": downloaded_size,
                            },
                        )
                    return None

                response = self._run_image_io(
                    io_call,
                    lambda: self._get(
                        current_url,
                        headers=download_headers,
                        timeout=(60 if protocol_profile == "remote_adobe" else 30),
                        use_proxy=(protocol_profile != "remote_adobe"),
                    ),
                )
                response.raise_for_status()
                image_bytes = response.content
                self._validate_downloaded_image(image_bytes=image_bytes)
                if trace is not None:
                    trace.finish_stage(
                        download_stage_id,
                        status="succeeded",
                        response={
                            **response_snapshot(response, include_body=False),
                            "body": binary_summary(
                                image_bytes,
                                content_type=response.headers.get("content-type"),
                            ),
                        },
                    )
                return image_bytes
            except ContentPolicyError:
                if trace is not None:
                    trace.finish_stage(
                        download_stage_id, status="failed", error="图片不安全"
                    )
                raise
            except Exception as exc:
                last_error = exc
                if part_path is not None:
                    part_path.unlink(missing_ok=True)
                if trace is not None:
                    trace.finish_stage(
                        download_stage_id,
                        status="failed",
                        response=(
                            response_snapshot(response) if response is not None else None
                        ),
                        error=exc,
                    )
                if attempt >= attempts:
                    break
                delay = delays[min(attempt - 1, len(delays) - 1)]
                if progress_cb is not None:
                    progress_cb(
                        {
                            "task_status": "DOWNLOAD_RETRY",
                            "upstream_job_id": upstream_job_id,
                            "download_attempt": attempt,
                            "retry_after": int(delay),
                            "error": str(exc),
                        }
                    )
                self._wait_for_image_retry(
                    delay,
                    cancel_check=cancel_check,
                    wait_cb=wait_cb,
                )
                status_code = int(getattr(exc, "status_code", 0) or 0)
                error_text = str(exc or "").lower()
                should_refresh_url = status_code in {401, 403, 404} or any(
                    marker in error_text
                    for marker in ("expired", "signature", "presigned")
                )
                if should_refresh_url:
                    try:
                        refreshed_url = self._refresh_image_result_url(
                            poll_url,
                            token,
                            io_call=io_call,
                            protocol_profile=protocol_profile,
                            fingerprint=download_fingerprint,
                        )
                        if refreshed_url:
                            current_url = refreshed_url
                    except ContentPolicyError:
                        raise
                    except Exception as refresh_exc:
                        logger.warning(
                            "failed to refresh generated image url attempt=%s error=%s",
                            attempt,
                            refresh_exc,
                        )

        if part_path is not None:
            part_path.unlink(missing_ok=True)
        raise ImageStageTerminalError(
            f"download failed after {attempts} attempts: {last_error}",
            status_code=502,
            error_type="download",
        )

    def upload_image(
        self,
        token: str,
        image_bytes: bytes,
        mime_type: str = "image/jpeg",
        *,
        trace: Optional[RequestTrace] = None,
        trace_parent_id: Optional[str] = None,
        progress_cb: Optional[Callable[[dict], None]] = None,
        cancel_check: Optional[Callable[[], None]] = None,
        io_call: Optional[Callable[[Callable[[], Any]], Any]] = None,
        wait_cb: Optional[Callable[[float], None]] = None,
        protocol_profile: str = "",
        engine: str = "",
    ) -> str:
        if not image_bytes:
            raise AdobeRequestError("image is empty")

        if protocol_profile == "remote_adobe":
            endpoint = self.upload_url
            if str(engine or "").strip() == "firefly-video":
                endpoint = self.firefly_video_upload_url
            elif str(mime_type or "").lower().startswith("video/"):
                endpoint = self.upload_video_url
            elif str(mime_type or "").lower().startswith("audio/"):
                endpoint = self.upload_audio_url

            last_error: Optional[Exception] = None
            last_response = None
            for attempt in range(6):
                if cancel_check is not None:
                    cancel_check()
                fingerprint = _select_adobe_fingerprint()
                self._fingerprint_local.current = fingerprint
                upload_session = self._new_remote_adobe_session(
                    fingerprint,
                    use_proxy=False,
                )
                headers = {
                    "authorization": f"Bearer {token}",
                    "x-api-key": ADOBE_EXPRESS_CLIENT_ID,
                    "content-type": mime_type or "image/png",
                    "accept": "*/*",
                    "user-agent": fingerprint["user_agent"],
                }
                try:
                    self._set_session_override(upload_session)
                    last_response = self._run_image_io(
                        io_call,
                        lambda: self._post_bytes(
                            endpoint,
                            headers=headers,
                            payload=image_bytes,
                            use_proxy=False,
                        ),
                    )
                except UpstreamTemporaryError as exc:
                    last_error = exc
                    if attempt < 5:
                        continue
                    raise AdobeRequestError(f"adobe upload request: {exc}") from exc
                finally:
                    self._clear_session_override()
                    if upload_session is not None:
                        upload_session.close()

                status_code = int(last_response.status_code or 0)
                if status_code in (401, 403):
                    raise AuthError(
                        f"Adobe upload auth failed: {status_code} "
                        f"{last_response.headers.get('x-access-error') or ''} "
                        f"{last_response.text[:300]}",
                        status_code=status_code,
                        error_type="auth",
                    )
                if status_code == 429:
                    raise SubmitRateLimitedError()
                if status_code == 451 or status_code >= 500:
                    last_error = AdobeRequestError(
                        f"adobe upload failed: {status_code} {last_response.text[:300]}",
                        status_code=status_code,
                        error_type="status",
                    )
                    if attempt < 5:
                        continue
                    raise last_error
                if status_code != 200:
                    raise AdobeRequestError(
                        f"adobe upload failed: {status_code} {last_response.text[:300]}",
                        status_code=status_code,
                        error_type="status",
                    )

                try:
                    data = last_response.json()
                except Exception as exc:
                    raise AdobeRequestError("adobe upload bad response") from exc
                image_id = str(data.get("id") or "").strip()
                if not image_id:
                    for key in (
                        "images",
                        "videos",
                        "audios",
                        "audio",
                        "assets",
                        "files",
                    ):
                        items = data.get(key)
                        if isinstance(items, list) and items and isinstance(items[0], dict):
                            image_id = str(items[0].get("id") or "").strip()
                            if image_id:
                                break
                if not image_id:
                    raise AdobeRequestError(
                        f"adobe upload missing blob id: {last_response.text[:300]}"
                    )
                return image_id

            raise AdobeRequestError(f"adobe upload failed: {last_error}")

        headers = {
            "authorization": f"Bearer {token}",
            "x-api-key": (
                ADOBE_EXPRESS_CLIENT_ID
                if protocol_profile == "remote_adobe"
                else self.api_key
            ),
            "content-type": mime_type,
            "accept": "*/*" if protocol_profile == "remote_adobe" else "application/json",
        }
        trace_stage_id = None
        if trace is not None:
            trace_stage_id = trace.start_stage(
                layer="adobe",
                kind="upload",
                name="上传编辑参考图",
                parent_id=trace_parent_id,
                request={
                    "method": "POST",
                    "url": sanitize_url(self.upload_url),
                    "headers": sanitize_headers(headers),
                    "body": binary_summary(
                        image_bytes,
                        content_type=mime_type,
                    ),
                },
            )
        logger.info(
            "image upload start token=%s bytes=%s mime=%s",
            str(token or "")[:8],
            len(image_bytes or b""),
            mime_type,
        )
        network_started: Optional[float] = None
        rate_limit_started: Optional[float] = None
        rate_limit_retry_used = False
        retry_count = 0
        while True:
            if cancel_check is not None:
                cancel_check()
            if protocol_profile == "remote_adobe":
                upload_fingerprint = _select_adobe_fingerprint()
                self._fingerprint_local.current = upload_fingerprint
                headers["user-agent"] = upload_fingerprint["user_agent"]
            try:
                resp = self._run_image_io(
                    io_call,
                    lambda: self._post_bytes(
                        self.upload_url,
                        headers=headers,
                        payload=image_bytes,
                        use_proxy=(protocol_profile != "remote_adobe"),
                    ),
                )
            except UpstreamTemporaryError as exc:
                now = time.time()
                network_started = network_started or now
                if now - network_started >= self._image_network_retry_seconds():
                    if trace is not None:
                        trace.finish_stage(trace_stage_id, status="failed", error=exc)
                    raise ImageStageTerminalError(
                        str(exc), status_code=502, error_type="network"
                    ) from exc
                retry_count += 1
                delay = self._retry_delay(retry_count, rate_limited=False)
                if progress_cb is not None:
                    progress_cb(
                        {
                            "task_status": "UPLOADING",
                            "retry_after": int(round(delay)),
                            "retry_count": retry_count,
                            "error": str(exc),
                        }
                    )
                self._wait_for_image_retry(
                    delay, cancel_check=cancel_check, wait_cb=wait_cb
                )
                continue
            try:
                self._raise_if_image_unsafe(resp, param="image")
            except ContentPolicyError as exc:
                if trace is not None:
                    trace.finish_stage(
                        trace_stage_id,
                        status="failed",
                        response=response_snapshot(resp),
                        error=exc,
                    )
                raise
            is_rate_limited = (
                resp.status_code == 429 or self._is_rate_limited_response(resp)
            )
            if is_rate_limited:
                delay = self._image_rate_limit_single_retry_seconds()
                logger.warning(
                    "image upload rate_limited token=%s action=switch_account_after_delay delay=%s status=%s body=%s",
                    str(token or "")[:8],
                    delay,
                    getattr(resp, "status_code", None),
                    str(getattr(resp, "text", "") or "")[:300],
                )
                if progress_cb is not None:
                    progress_cb(
                        {
                            "task_status": "RATE_LIMITED",
                            "retry_after": int(round(delay)),
                            "retry_count": retry_count,
                            "rate_limit_wait_seconds": delay,
                            "error": resp.text[:300],
                        }
                    )
                if trace is not None:
                    trace.finish_stage(
                        trace_stage_id,
                        status="failed",
                        response=response_snapshot(resp),
                        error="upload rate limited; switch account",
                    )
                raise SubmitRateLimitedError()
            if self._is_retryable_image_status(resp.status_code):
                now = time.time()
                network_started = network_started or now
                if now - network_started >= self._image_network_retry_seconds():
                    break
                retry_count += 1
                delay = self._retry_delay(retry_count, rate_limited=False)
                if progress_cb is not None:
                    progress_cb(
                        {
                            "task_status": "UPLOADING",
                            "retry_after": int(round(delay)),
                            "retry_count": retry_count,
                            "error": resp.text[:300],
                        }
                    )
                self._wait_for_image_retry(
                    delay, cancel_check=cancel_check, wait_cb=wait_cb
                )
                continue
            break
        if trace is not None:
            trace.finish_stage(
                trace_stage_id,
                status="succeeded" if resp.status_code == 200 else "failed",
                response=response_snapshot(resp),
            )

        if resp.status_code in (401, 403):
            raise AuthError("Token invalid or expired")
        if resp.status_code != 200:
            if resp.status_code in (429, 451) or resp.status_code >= 500:
                raise ImageStageTerminalError(
                    f"upload image failed: {resp.status_code} {resp.text[:300]}",
                    status_code=resp.status_code,
                    error_type="status",
                )
            raise AdobeRequestError(
                f"upload image failed: {resp.status_code} {resp.text[:300]}"
            )

        try:
            data = resp.json()
        except Exception:
            raise AdobeRequestError("upload image failed: invalid response")

        image_id = (((data.get("images") or [{}])[0]) or {}).get("id")
        if not image_id:
            raise AdobeRequestError("upload image succeeded but no image id returned")
        return str(image_id)

    def select_subject_mask(
        self,
        token: str,
        image_id: str,
        *,
        soft_mask: bool = True,
        trace: Optional[RequestTrace] = None,
        trace_parent_id: Optional[str] = None,
        io_call: Optional[Callable[[Callable[[], Any]], Any]] = None,
    ) -> dict:
        image_id = str(image_id or "").strip()
        if not image_id:
            raise AdobeRequestError("masking image id is required")
        headers = self._select_subject_headers(token)
        payload = {"image": {"id": image_id}, "softMask": bool(soft_mask)}
        trace_stage_id = None
        if trace is not None:
            trace_stage_id = trace.start_stage(
                layer="adobe",
                kind="mask",
                name="Select Subject 生成透明蒙版",
                parent_id=trace_parent_id,
                request={
                    "method": "POST",
                    "url": sanitize_url(self.select_subject_url),
                    "headers": sanitize_headers(headers),
                    "body": sanitize_trace_value(payload),
                },
            )
        resp = self._run_image_io(
            io_call,
            lambda: self._post_json(
                self.select_subject_url,
                headers=headers,
                payload=payload,
                legacy_451_fallback=False,
            ),
        )
        if trace is not None:
            trace.finish_stage(
                trace_stage_id,
                status="succeeded" if resp.status_code == 200 else "failed",
                response=response_snapshot(resp),
            )
        if resp.status_code in (401, 403):
            raise AuthError("Token invalid or expired")
        if resp.status_code != 200:
            if resp.status_code == 429 or self._is_rate_limited_response(resp):
                raise SubmitRateLimitedError()
            if resp.status_code in (429, 451) or resp.status_code >= 500:
                raise ImageStageTerminalError(
                    f"select subject mask failed: {resp.status_code} {resp.text[:300]}",
                    status_code=resp.status_code,
                    error_type="mask",
                )
            raise AdobeRequestError(
                f"select subject mask failed: {resp.status_code} {resp.text[:300]}"
            )
        data = self._json_or_empty(resp)
        if not isinstance(data, dict):
            raise AdobeRequestError("select subject mask failed: invalid response")
        masks = data.get("masks") or []
        mask = (masks[0] or {}) if masks else {}
        mask_url = str(mask.get("presignedUrl") or "").strip()
        if not mask_url:
            raise AdobeRequestError(
                "select subject mask succeeded but no mask url returned"
            )
        return data

    def _download_mask_bytes(
        self,
        mask_url: str,
        *,
        trace: Optional[RequestTrace] = None,
        trace_parent_id: Optional[str] = None,
        io_call: Optional[Callable[[Callable[[], Any]], Any]] = None,
    ) -> bytes:
        headers = {"accept": "*/*"}
        trace_stage_id = None
        if trace is not None:
            trace_stage_id = trace.start_stage(
                layer="adobe",
                kind="download",
                name="下载 Select Subject 蒙版",
                parent_id=trace_parent_id,
                request={
                    "method": "GET",
                    "url": sanitize_url(mask_url),
                    "headers": sanitize_headers(headers),
                },
            )
        resp = self._run_image_io(
            io_call,
            lambda: self._get(mask_url, headers=headers, timeout=30),
        )
        if trace is not None:
            trace.finish_stage(
                trace_stage_id,
                status="succeeded" if resp.status_code == 200 else "failed",
                response=response_snapshot(resp, include_body=False),
            )
        if resp.status_code != 200:
            raise ImageStageTerminalError(
                f"download mask failed: {resp.status_code} {resp.text[:300]}",
                status_code=resp.status_code,
                error_type="mask_download",
            )
        mask_bytes = bytes(resp.content or b"")
        self._validate_downloaded_image(image_bytes=mask_bytes)
        return mask_bytes

    @staticmethod
    def apply_mask_alpha(image_bytes: bytes, mask_bytes: bytes) -> bytes:
        if Image is None or ImageChops is None:
            raise AdobeRequestError("Pillow is required for transparent masking")
        with Image.open(io.BytesIO(image_bytes)) as src, Image.open(
            io.BytesIO(mask_bytes)
        ) as mask_src:
            image = src.convert("RGBA")
            mask = mask_src.convert("L")
            if mask.size != image.size:
                mask = mask.resize(image.size, Image.Resampling.LANCZOS)
            original_alpha = image.getchannel("A")
            combined_alpha = ImageChops.multiply(original_alpha, mask)
            image.putalpha(combined_alpha)
            out = io.BytesIO()
            image.save(out, format="PNG")
            return out.getvalue()

    def make_transparent_subject(
        self,
        token: str,
        image_bytes: bytes,
        *,
        mime_type: str = "image/png",
        trace: Optional[RequestTrace] = None,
        trace_parent_id: Optional[str] = None,
        progress_cb: Optional[Callable[[dict], None]] = None,
        cancel_check: Optional[Callable[[], None]] = None,
        io_call: Optional[Callable[[Callable[[], Any]], Any]] = None,
        wait_cb: Optional[Callable[[float], None]] = None,
    ) -> tuple[bytes, dict]:
        if cancel_check is not None:
            cancel_check()
        if progress_cb is not None:
            progress_cb({"task_status": "MASKING"})
        image_id = self.upload_image(
            token,
            image_bytes,
            mime_type,
            trace=trace,
            trace_parent_id=trace_parent_id,
            progress_cb=progress_cb,
            cancel_check=cancel_check,
            io_call=io_call,
            wait_cb=wait_cb,
        )
        mask_data = self.select_subject_mask(
            token,
            image_id,
            soft_mask=True,
            trace=trace,
            trace_parent_id=trace_parent_id,
            io_call=io_call,
        )
        mask = ((mask_data.get("masks") or [{}])[0] or {})
        mask_bytes = self._download_mask_bytes(
            str(mask.get("presignedUrl") or ""),
            trace=trace,
            trace_parent_id=trace_parent_id,
            io_call=io_call,
        )
        transparent_bytes = self.apply_mask_alpha(image_bytes, mask_bytes)
        if progress_cb is not None:
            progress_cb({"task_status": "MASKED"})
        return transparent_bytes, mask_data

    @staticmethod
    def _json_or_empty(resp) -> Any:
        if not str(getattr(resp, "text", "") or "").strip():
            return {}
        try:
            return resp.json()
        except Exception:
            return {}

    @staticmethod
    def _raise_if_image_unsafe_data(data: Any, *, param: str = "prompt") -> None:
        if isinstance(data, list):
            for item in data:
                AdobeClient._raise_if_image_unsafe_data(item, param=param)
            return
        if not isinstance(data, dict):
            return
        upstream_code = (
            str(data.get("error_code") or data.get("code") or "").strip().lower()
        )
        if upstream_code in {"image_unsafe", "prompt_unsafe"}:
            raise ContentPolicyError(
                "图片不安全", upstream_code=upstream_code, param=param
            )
        for value in data.values():
            if isinstance(value, (dict, list)):
                AdobeClient._raise_if_image_unsafe_data(value, param=param)

    @staticmethod
    def _raise_if_image_unsafe(resp, *, param: str = "prompt") -> None:
        try:
            data = resp.json()
        except Exception:
            data = {}
        AdobeClient._raise_if_image_unsafe_data(data, param=param)

    @staticmethod
    def _is_rate_limited_data(data: Any) -> bool:
        if isinstance(data, list):
            return any(AdobeClient._is_rate_limited_data(item) for item in data)
        if not isinstance(data, dict):
            return False
        code = str(data.get("error_code") or data.get("code") or "").strip().lower()
        if code == "rate_limited":
            return True
        return any(
            AdobeClient._is_rate_limited_data(value)
            for value in data.values()
            if isinstance(value, (dict, list))
        )

    @staticmethod
    def _is_rate_limited_response(resp: Any) -> bool:
        return AdobeClient._is_rate_limited_data(AdobeClient._json_or_empty(resp))

    @staticmethod
    def _is_reference_image_required_data(data: Any) -> bool:
        if isinstance(data, list):
            return any(AdobeClient._is_reference_image_required_data(item) for item in data)
        if not isinstance(data, dict):
            return False
        code = str(data.get("error_code") or data.get("code") or "").strip().lower()
        message = str(data.get("message") or "").strip().lower()
        if code == "bad_request" and "requires a reference image" in message:
            return True
        return any(
            AdobeClient._is_reference_image_required_data(value)
            for value in data.values()
            if isinstance(value, (dict, list))
        )

    @staticmethod
    def _raise_if_reference_image_required(resp) -> None:
        try:
            data = resp.json()
        except Exception:
            data = {}
        if AdobeClient._is_reference_image_required_data(data):
            raise ReferenceImageRequiredError()

    @staticmethod
    def _entity_urn_from_data(data: Any) -> str:
        if isinstance(data, dict):
            for key in ("id", "urn", "entityId", "entityUrn"):
                val = str(data.get(key) or "").strip()
                if val:
                    return val
            entity = data.get("entity")
            if isinstance(entity, dict):
                return AdobeClient._entity_urn_from_data(entity)
        return ""

    def create_entity(
        self,
        token: str,
        display_name: str,
        entity_type: str = "character",
        description: str = "",
    ) -> dict:
        name = str(display_name or "").strip()
        if not name:
            raise AdobeRequestError("entity displayName is required")
        payload = {
            "entityType": str(entity_type or "character").strip() or "character",
            "entityValue": {
                "displayName": name,
                "description": str(description or ""),
                "metaAttrs": None,
            },
        }
        resp = self._post_json(self.entity_api_base, self._entity_headers(token), payload)
        if resp.status_code in (401, 403):
            raise AuthError("Token invalid or expired")
        if resp.status_code not in (200, 201):
            if resp.status_code in (429, 451) or resp.status_code >= 500:
                raise UpstreamTemporaryError(
                    f"create entity failed: {resp.status_code} {resp.text[:300]}",
                    status_code=resp.status_code,
                    error_type="status",
                )
            raise AdobeRequestError(
                f"create entity failed: {resp.status_code} {resp.text[:300]}"
            )
        data = self._json_or_empty(resp)
        if isinstance(data, dict):
            urn = self._entity_urn_from_data(data)
            if urn and "id" not in data:
                data = {**data, "id": urn}
            return data
        return {}

    def upload_entity_image(
        self,
        token: str,
        repo_urn: str,
        entity_name: str,
        image_bytes: bytes,
        mime_type: str = "image/png",
        component_upload_href: Optional[str] = None,
    ) -> dict:
        if not image_bytes:
            raise AdobeRequestError("entity image is empty")
        repo = str(repo_urn or "").strip()
        name = str(entity_name or "").strip()
        if not repo:
            raise AdobeRequestError("Adobe repository is required for entity image upload")
        if not name:
            raise AdobeRequestError("entity name is required for entity image upload")
        component_id = str(uuid.uuid4())
        upload_href = str(component_upload_href or "").strip()
        if upload_href:
            url = upload_href.split("{", 1)[0]
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}component_id={component_id}"
        else:
            url = (
                f"{self.platform_cs_base}/{quote(repo, safe='')}/"
                f"appassets/firefly/entities/{quote(name, safe='')}?component_id={component_id}"
            )
        headers = {
            "Authorization": f"Bearer {token}",
            "x-api-key": self.api_key,
            "content-type": mime_type,
            "accept": "application/json",
        }
        resp = self._put_bytes(url, headers=headers, payload=image_bytes)
        if resp.status_code in (401, 403):
            raise AuthError("Token invalid or expired")
        if resp.status_code not in (200, 201):
            if resp.status_code in (429, 451) or resp.status_code >= 500:
                raise UpstreamTemporaryError(
                    f"upload entity image failed: {resp.status_code} {resp.text[:300]}",
                    status_code=resp.status_code,
                    error_type="status",
                )
            raise AdobeRequestError(
                f"upload entity image failed: {resp.status_code} {resp.text[:300]}"
            )

        def header_val(*names: str) -> str:
            for name_key in names:
                val = str(resp.headers.get(name_key) or "").strip()
                if val:
                    return val
            return ""

        length_raw = header_val("resource-length", "content-length")
        try:
            length = int(length_raw)
        except Exception:
            length = len(image_bytes)
        return {
            "component_id": component_id,
            "etag": header_val("etag"),
            "version": header_val("revision", "x-revision"),
            "md5": header_val("content-md5", "x-content-md5"),
            "length": length,
            "type": mime_type,
        }

    @staticmethod
    def entity_component_upload_href(entity_data: dict) -> str:
        upload_links = entity_data.get("uploadLinks") if isinstance(entity_data, dict) else {}
        if not isinstance(upload_links, dict):
            return ""
        links = upload_links.get("http://ns.adobe.com/adobecloud/rel/component")
        if not isinstance(links, list):
            return ""
        for item in links:
            if isinstance(item, dict):
                href = str(item.get("href") or "").strip()
                if href:
                    return href
        return ""

    def register_entity_base_resources(
        self, token: str, entity_urn: str, components: list[dict]
    ) -> Any:
        urn = str(entity_urn or "").strip()
        if not urn:
            raise AdobeRequestError("entity urn is required")
        if not components:
            raise AdobeRequestError("entity components are required")
        url = f"{self.entity_api_base}{quote(urn, safe='')}/base-resources/"
        body = []
        for idx, comp in enumerate(components):
            entry = {
                "component": {
                    "id": comp["component_id"],
                    "type": comp["type"],
                    "length": comp["length"],
                    "etag": comp["etag"],
                    "version": comp["version"],
                    "md5": comp["md5"],
                }
            }
            if idx == 0:
                entry["is_primary"] = True
            body.append(entry)
        resp = self._post_json(url, self._entity_headers(token), body)
        if resp.status_code in (401, 403):
            raise AuthError("Token invalid or expired")
        if resp.status_code not in (200, 201):
            if resp.status_code in (429, 451) or resp.status_code >= 500:
                raise UpstreamTemporaryError(
                    f"register entity resources failed: {resp.status_code} {resp.text[:300]}",
                    status_code=resp.status_code,
                    error_type="status",
                )
            raise AdobeRequestError(
                f"register entity resources failed: {resp.status_code} {resp.text[:300]}"
            )
        return self._json_or_empty(resp)

    def list_entities(self, token: str, limit: int = 50) -> list[dict]:
        safe_limit = max(1, min(int(limit or 50), 100))
        data = self._get_json(
            f"{self.entity_api_base}?limit={safe_limit}", self._entity_headers(token)
        )
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("entities", "items", "data", "results"):
                items = data.get(key)
                if isinstance(items, list):
                    return [item for item in items if isinstance(item, dict)]
        return []

    def resolve_repo_urn(self, token: str) -> str:
        headers = self._submit_headers_minimal(token)
        headers["accept"] = "*/*"
        data = self._get_json(self.platform_cs_index_url, headers=headers)
        if not isinstance(data, dict):
            raise AdobeRequestError("unable to resolve Adobe repository: invalid index response")

        candidates: list[dict] = []

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                repo_id = str(value.get("repo:repositoryId") or "").strip()
                if repo_id:
                    candidates.append(value)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(data.get("children") or [])

        def score(item: dict) -> tuple[int, int]:
            return (
                1 if str(item.get("repo:state") or "").upper() == "ACTIVE" else 0,
                1 if str(item.get("storage:directoryType") or "") == "assigned" else 0,
            )

        candidates.sort(key=score, reverse=True)
        for item in candidates:
            repo_id = str(item.get("repo:repositoryId") or "").strip()
            if repo_id:
                return repo_id
        raise AdobeRequestError("unable to resolve Adobe repository for current token")

    def delete_entity(self, token: str, entity_urn: str) -> bool:
        urn = str(entity_urn or "").strip()
        if not urn:
            raise AdobeRequestError("entity urn is required")
        resp = self._delete(
            f"{self.entity_api_base}{quote(urn, safe='')}/",
            self._entity_headers(token),
        )
        if resp.status_code in (401, 403):
            raise AuthError("Token invalid or expired")
        if resp.status_code in (200, 202, 204):
            return True
        if resp.status_code in (429, 451) or resp.status_code >= 500:
            raise UpstreamTemporaryError(
                f"delete entity failed: {resp.status_code} {resp.text[:300]}",
                status_code=resp.status_code,
                error_type="status",
            )
        raise AdobeRequestError(
            f"delete entity failed: {resp.status_code} {resp.text[:300]}"
        )

    def _build_payload_candidates(
        self,
        prompt: str,
        aspect_ratio: str,
        output_resolution: str,
        upstream_model_id: str,
        upstream_model_version: str,
        quality_level: Optional[str] = None,
        detail_level: Optional[int] = None,
        seed: Optional[int] = None,
        source_image_ids: Optional[list[str]] = None,
        requested_size: Optional[dict] = None,
        protocol_profile: str = "",
    ) -> list[dict]:
        if protocol_profile == "remote_adobe":
            return build_remote_adobe_image_payload_candidates(
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                output_resolution=output_resolution,
                upstream_model_id=upstream_model_id,
                upstream_model_version=upstream_model_version,
                seed=seed,
                source_image_ids=source_image_ids,
                requested_size=requested_size,
            )
        return build_image_payload_candidates(
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            output_resolution=output_resolution,
            upstream_model_id=upstream_model_id,
            upstream_model_version=upstream_model_version,
            quality_level=quality_level,
            detail_level=detail_level,
            seed=seed,
            source_image_ids=source_image_ids,
            requested_size=requested_size,
        )

    @staticmethod
    def _video_size(aspect_ratio: str, resolution: str = "720p") -> dict:
        res = str(resolution or "720p").lower()
        if res == "1080p":
            if aspect_ratio == "16:9":
                return {"width": 1920, "height": 1080}
            return {"width": 1080, "height": 1920}
        if aspect_ratio == "16:9":
            return {"width": 1280, "height": 720}
        return {"width": 720, "height": 1280}

    @staticmethod
    def _coerce_progress_percent(value: Any) -> Optional[float]:
        if value is None:
            return None

        val: Optional[float] = None
        if isinstance(value, (int, float)):
            val = float(value)
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            if text.endswith("%"):
                text = text[:-1].strip()
            try:
                val = float(text)
            except Exception:
                return None
        elif isinstance(value, dict):
            for key in (
                "progress",
                "percentage",
                "percent",
                "task_progress",
                "taskProgress",
                "value",
            ):
                nested = AdobeClient._coerce_progress_percent(value.get(key))
                if nested is not None:
                    return nested
            return None
        else:
            return None

        if val <= 1.0:
            val = val * 100.0
        if val < 0:
            return 0.0
        if val > 100:
            return 100.0
        return val

    @staticmethod
    def _is_in_progress_status(status_val: str) -> bool:
        return str(status_val or "").upper() in {
            "IN_PROGRESS",
            "RUNNING",
            "PROCESSING",
            "PENDING",
            "QUEUED",
            "STARTED",
        }

    def _extract_progress_percent(self, latest: dict, poll_resp) -> Optional[float]:
        if not isinstance(latest, dict):
            latest = {}

        task_obj = latest.get("task") if isinstance(latest.get("task"), dict) else {}
        result_obj = (
            latest.get("result") if isinstance(latest.get("result"), dict) else {}
        )
        meta_obj = latest.get("meta") if isinstance(latest.get("meta"), dict) else {}
        metadata_obj = (
            latest.get("metadata") if isinstance(latest.get("metadata"), dict) else {}
        )

        candidates: list[Any] = [
            latest.get("progress"),
            latest.get("percentage"),
            latest.get("percent"),
            latest.get("task_progress"),
            latest.get("taskProgress"),
            task_obj.get("progress"),
            task_obj.get("percentage"),
            result_obj.get("progress"),
            result_obj.get("percentage"),
            meta_obj.get("progress"),
            metadata_obj.get("progress"),
            poll_resp.headers.get("x-task-progress"),
            poll_resp.headers.get("x-progress"),
            poll_resp.headers.get("progress"),
        ]

        for raw in candidates:
            parsed = self._coerce_progress_percent(raw)
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    def _normalize_video_poll_url(raw_url: str) -> str:
        if not raw_url:
            return raw_url
        try:
            parsed = urlparse(raw_url)
            host = str(parsed.hostname or "")
            path_parts = [p for p in parsed.path.split("/") if p]
            if not host or not path_parts:
                return raw_url
            if not host.startswith("firefly-epo"):
                return raw_url
            job_id = path_parts[-1]
            if not job_id:
                return raw_url
            host_suffix = host[len("firefly-epo") :].split(".", 1)[0]
            shard = host_suffix.strip()
            if len(shard) != 4 or not shard.isdigit():
                return raw_url
            return (
                f"https://bks-epo{shard}.adobe.io/v2/jobs/result/{job_id}"
                f"?host={parsed.netloc}/"
            )
        except Exception:
            return raw_url

    @staticmethod
    def _extract_job_id(raw_url: str) -> str:
        try:
            parsed = urlparse(str(raw_url or ""))
            path_parts = [p for p in parsed.path.split("/") if p]
            if path_parts:
                return path_parts[-1]
        except Exception:
            pass
        return ""

    @staticmethod
    def _extract_result_link(submit_resp, submit_data: Any) -> str:
        poll_url = str(submit_resp.headers.get("x-override-status-link") or "").strip()
        if poll_url:
            return AdobeClient._normalize_video_poll_url(poll_url)

        links = submit_data.get("links") if isinstance(submit_data, dict) else {}
        if not isinstance(links, dict):
            links = {}

        result_link = links.get("result")
        if isinstance(result_link, str):
            return AdobeClient._normalize_video_poll_url(result_link.strip())
        if isinstance(result_link, dict):
            return AdobeClient._normalize_video_poll_url(
                str(result_link.get("href") or "").strip()
            )
        return ""

    @staticmethod
    def _build_video_prompt_json(
        prompt: str, duration: int, negative_prompt: str = ""
    ) -> str:
        payload = {
            "id": 1,
            "duration_sec": int(duration),
            "prompt_text": prompt,
        }
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
        return json.dumps(payload, ensure_ascii=False)

    def _build_video_payload(
        self,
        video_conf: dict,
        prompt: str,
        aspect_ratio: str,
        duration: int,
        source_image_ids: Optional[list[str]] = None,
        entity_refs: Optional[list[dict]] = None,
        negative_prompt: str = "",
        generate_audio: bool = True,
        reference_mode: str = "frame",
    ) -> dict:
        seed_val = int(time.time()) % 999999
        engine = str(video_conf.get("engine") or "sora2")
        upstream_model = str(
            video_conf.get("upstream_model") or "openai:firefly:colligo:sora2"
        )
        resolution = str(video_conf.get("resolution") or "720p")
        if engine in {"veo31-fast", "veo31-standard"}:
            model_version = (
                "3.1-fast-generate" if engine == "veo31-fast" else "3.1-generate"
            )
            payload = {
                "n": 1,
                "seeds": [seed_val],
                "modelId": "veo",
                "modelVersion": model_version,
                "output": {"storeInputs": True},
                "prompt": prompt,
                "size": self._video_size(aspect_ratio, resolution),
                "generateAudio": bool(generate_audio),
                "referenceBlobs": [],
                "generationMetadata": {"module": "text2video"},
                "modelSpecificPayload": {
                    "parameters": {
                        "durationSeconds": int(duration),
                        "aspectRatio": aspect_ratio,
                        "addWaterMark": False,
                    }
                },
            }
            if source_image_ids:
                if engine == "veo31-standard" and str(reference_mode) == "image":
                    for image_id in source_image_ids[:3]:
                        payload["referenceBlobs"].append(
                            {
                                "id": str(image_id),
                                "usage": "asset",
                            }
                        )
                else:
                    for idx, image_id in enumerate(source_image_ids[:2], start=1):
                        payload["referenceBlobs"].append(
                            {
                                "id": str(image_id),
                                "usage": "general",
                                "promptReference": idx,
                            }
                        )
            return payload

        if engine == "kling-o3":
            payload = {
                "n": 1,
                "seeds": [seed_val],
                "modelId": "kling",
                "modelVersion": "kling_o3_pro_reference_to_video",
                "output": {"storeInputs": True},
                "prompt": prompt,
                "size": self._video_size(aspect_ratio, resolution),
                "generateAudio": bool(generate_audio),
                "generationMetadata": {
                    "module": "image2video" if source_image_ids else "text2video"
                },
                "duration": int(duration),
                "generationSettings": {"aspectRatio": aspect_ratio},
                "referenceBlobs": [],
            }
            if source_image_ids:
                for idx, image_id in enumerate(source_image_ids[:2], start=1):
                    payload["referenceBlobs"].append(
                        {"id": str(image_id), "usage": "frame", "order": idx}
                    )
            if entity_refs:
                for ref in entity_refs:
                    urn = str(ref.get("urn") or ref.get("id") or "").strip()
                    mention_id = str(ref.get("mention_id") or "").strip()
                    if not urn or not mention_id:
                        continue
                    payload["referenceBlobs"].append(
                        {
                            "usage": "element",
                            "creativeCloudFileId": urn,
                            "mention": {"id": mention_id},
                        }
                    )
            return payload

        if engine == "kling3":
            payload = {
                "n": 1,
                "seeds": [seed_val],
                "modelId": "kling",
                "modelVersion": "kling_v3_standard_i2v",
                "output": {"storeInputs": True},
                "prompt": prompt,
                "size": self._video_size(aspect_ratio, resolution),
                "generateAudio": bool(generate_audio),
                "generationMetadata": {
                    "module": "image2video" if source_image_ids else "text2video"
                },
                "duration": int(duration),
                "generationSettings": {"aspectRatio": aspect_ratio},
                "referenceBlobs": [],
            }
            if source_image_ids:
                for idx, image_id in enumerate(source_image_ids[:2], start=1):
                    payload["referenceBlobs"].append(
                        {"id": str(image_id), "usage": "frame", "order": idx}
                    )
            return payload

        payload = {
            "n": 1,
            "seeds": [seed_val],
            "modelId": "sora",
            "modelVersion": "sora-2",
            "size": self._video_size(aspect_ratio, resolution),
            "duration": int(duration),
            "fps": 24,
            "prompt": self._build_video_prompt_json(
                prompt=prompt, duration=duration, negative_prompt=negative_prompt
            ),
            "generationMetadata": {"module": "text2video"},
            "model": upstream_model,
            "generateAudio": bool(generate_audio),
            "generateLoop": False,
            "transparentBackground": False,
            "seed": str(seed_val),
            "locale": "en-US",
            "camera": {
                "angle": "none",
                "shotSize": "none",
                "motion": None,
                "promptStyle": None,
            },
            "negativePrompt": negative_prompt or "",
            "jobMode": "standard",
            "debugGenerationEndpoint": "",
            "referenceBlobs": [],
            "referenceFrames": [],
            "referenceVideo": None,
            "cameraMotionReferenceVideo": None,
            "characterReference": None,
            "editReferenceVideo": None,
            "output": {"storeInputs": True},
        }
        if source_image_ids:
            first_id = str(source_image_ids[0])
            payload["referenceBlobs"] = [
                {"id": first_id, "usage": "general", "promptReference": 1}
            ]
            reference_frames = [{"localBlobRef": first_id}, None]
            if engine == "veo31-fast" and len(source_image_ids) > 1:
                last_id = str(source_image_ids[1])
                payload["referenceBlobs"].append(
                    {"id": last_id, "usage": "general", "promptReference": 2}
                )
                reference_frames[1] = {"localBlobRef": last_id}
            payload["referenceFrames"] = reference_frames
        return payload

    def generate_video(
        self,
        token: str,
        video_conf: dict,
        prompt: str,
        aspect_ratio: str = "9:16",
        duration: int = 12,
        source_image_ids: Optional[list[str]] = None,
        entity_refs: Optional[list[dict]] = None,
        timeout: int = 600,
        negative_prompt: str = "",
        generate_audio: bool = True,
        reference_mode: str = "frame",
        out_path: Optional[Path] = None,
        progress_cb: Optional[Callable[[dict], None]] = None,
    ) -> tuple[Optional[bytes], dict]:
        payload = self._build_video_payload(
            video_conf=video_conf,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            duration=duration,
            source_image_ids=source_image_ids,
            entity_refs=entity_refs,
            negative_prompt=negative_prompt,
            generate_audio=generate_audio,
            reference_mode=reference_mode,
        )
        submit_resp = self._post_json(
            self.video_submit_url,
            headers=self._video_submit_headers(token),
            payload=payload,
        )

        if submit_resp.status_code in (401, 403):
            access_error = submit_resp.headers.get("x-access-error")
            if access_error == "taste_exhausted":
                raise QuotaExhaustedError("Adobe quota exhausted for this account")
            raise AuthError("Token invalid or expired")

        if submit_resp.status_code != 200:
            if submit_resp.status_code in (429, 451) or submit_resp.status_code >= 500:
                raise UpstreamTemporaryError(
                    f"video submit failed: {submit_resp.status_code} {submit_resp.text[:300]}",
                    status_code=submit_resp.status_code,
                    error_type="status",
                )
            raise AdobeRequestError(
                f"video submit failed: {submit_resp.status_code} {submit_resp.text[:300]}"
            )

        submit_data = submit_resp.json()
        poll_url = self._extract_result_link(submit_resp, submit_data)
        if not poll_url:
            raise AdobeRequestError("video submit succeeded but no poll url returned")
        poll_url = self._normalize_video_poll_url(str(poll_url))
        upstream_job_id = self._extract_job_id(poll_url)
        if progress_cb:
            try:
                progress_cb(
                    {
                        "task_status": "IN_PROGRESS",
                        "task_progress": 0.0,
                        "upstream_job_id": upstream_job_id,
                        "retry_after": int(submit_resp.headers.get("retry-after") or 0)
                        or None,
                    }
                )
            except Exception:
                pass

        start = time.time()
        while True:
            poll_resp = self._get(
                poll_url, headers=self._poll_headers(token), timeout=60
            )
            if poll_resp.status_code in (401, 403):
                raise AuthError("Token invalid or expired")
            if poll_resp.status_code != 200:
                if poll_resp.status_code in (429, 451) or poll_resp.status_code >= 500:
                    raise UpstreamTemporaryError(
                        f"video poll failed: {poll_resp.status_code} {poll_resp.text[:300]}",
                        status_code=poll_resp.status_code,
                        error_type="status",
                    )
                raise AdobeRequestError(
                    f"video poll failed: {poll_resp.status_code} {poll_resp.text[:300]}"
                )

            latest = poll_resp.json()
            status_header = str(poll_resp.headers.get("x-task-status") or "").upper()
            status_val = str(latest.get("status") or "").upper() or status_header
            progress_val = self._extract_progress_percent(latest, poll_resp)

            if progress_cb and self._is_in_progress_status(status_val):
                try:
                    progress_cb(
                        {
                            "task_status": "IN_PROGRESS",
                            "task_progress": progress_val
                            if progress_val is not None
                            else 0.0,
                            "upstream_job_id": upstream_job_id,
                            "retry_after": int(
                                poll_resp.headers.get("retry-after") or 0
                            )
                            or None,
                        }
                    )
                except Exception:
                    pass

            outputs = latest.get("outputs") or []
            if outputs:
                video_url = ((outputs[0] or {}).get("video") or {}).get("presignedUrl")
                if not video_url:
                    raise AdobeRequestError("video job finished without video url")
                if out_path is not None:
                    self._download_to_file(
                        video_url,
                        headers={"accept": "*/*"},
                        out_path=out_path,
                        timeout=60,
                    )
                    video_bytes = None
                else:
                    video_resp = self._get(video_url, headers={"accept": "*/*"}, timeout=60)
                    video_resp.raise_for_status()
                    video_bytes = video_resp.content
                if progress_cb:
                    try:
                        progress_cb(
                            {
                                "task_status": "COMPLETED",
                                "task_progress": 100.0,
                                "upstream_job_id": upstream_job_id,
                                "retry_after": None,
                            }
                        )
                    except Exception:
                        pass
                return video_bytes, latest

            if status_val in {"FAILED", "CANCELLED", "ERROR"}:
                if progress_cb:
                    try:
                        progress_cb(
                            {
                                "task_status": "FAILED",
                                "task_progress": progress_val
                                if progress_val is not None
                                else 0.0,
                                "upstream_job_id": upstream_job_id,
                                "retry_after": None,
                                "error": f"video job failed: {latest}",
                            }
                        )
                    except Exception:
                        pass
                raise AdobeRequestError(f"video job failed: {latest}")

            if time.time() - start > timeout and not self._is_in_progress_status(status_val):
                if progress_cb:
                    try:
                        progress_cb(
                            {
                                "task_status": "FAILED",
                                "task_progress": progress_val
                                if "progress_val" in locals()
                                and progress_val is not None
                                else 0.0,
                                "upstream_job_id": upstream_job_id,
                                "retry_after": None,
                                "error": "video generation timed out",
                            }
                        )
                    except Exception:
                        pass
                raise AdobeRequestError("video generation timed out")
            time.sleep(3.0)

    @staticmethod
    def _is_adobe_overload_text(value: Any) -> bool:
        text = str(value or "").lower()
        return "system under load" in text or "timeout_error" in text

    @staticmethod
    def _is_adobe_content_rejection(status_code: int, value: Any) -> bool:
        if int(status_code or 0) != 451:
            return False
        text = str(value or "").lower()
        return "unsafe" in text or "privacy_error" in text

    def _submit_remote_adobe_image_job(
        self,
        *,
        token: str,
        prompt: str,
        aspect_ratio: str,
        output_resolution: str,
        upstream_model_id: str,
        upstream_model_version: str,
        quality_level: Optional[str],
        detail_level: Optional[int],
        seed: Optional[int],
        source_image_ids: Optional[list[str]],
        requested_size: Optional[dict],
        progress_cb: Optional[Callable[[dict], None]],
        trace: Optional[RequestTrace],
        trace_parent_id: Optional[str],
        cancel_check: Optional[Callable[[], None]],
    ) -> dict:
        payload_candidates = self._build_payload_candidates(
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            output_resolution=output_resolution,
            upstream_model_id=upstream_model_id,
            upstream_model_version=upstream_model_version,
            quality_level=quality_level,
            detail_level=detail_level,
            seed=seed,
            source_image_ids=source_image_ids,
            requested_size=requested_size,
            protocol_profile="remote_adobe",
        )
        submit_fingerprint = _select_adobe_fingerprint()
        direct_fingerprint = _select_adobe_fingerprint()
        submit_session = self._new_remote_adobe_session(
            submit_fingerprint,
            use_proxy=True,
        )
        direct_session = self._new_remote_adobe_session(
            direct_fingerprint,
            use_proxy=False,
        )
        last_error: Optional[Exception] = None

        try:
            for candidate_index, payload in enumerate(payload_candidates, start=1):
                if cancel_check is not None:
                    cancel_check()
                if progress_cb is not None:
                    progress_cb({"task_status": "SUBMITTING"})
                headers = self._submit_headers(
                    token,
                    prompt=prompt,
                    protocol_profile="remote_adobe",
                    fingerprint=submit_fingerprint,
                )
                stage_id = None
                if trace is not None:
                    stage_id = trace.start_stage(
                        layer="adobe",
                        kind="submit",
                        name="提交 Adobe 图片任务",
                        parent_id=trace_parent_id,
                        attempt={
                            "candidate": candidate_index,
                            "candidate_count": len(payload_candidates),
                        },
                        request={
                            "method": "POST",
                            "url": sanitize_url(self.submit_url),
                            "headers": sanitize_headers(headers),
                            "body": sanitize_trace_value(payload),
                        },
                    )
                try:
                    self._set_session_override(submit_session)
                    response = self._post_image_json(
                        self.submit_url,
                        headers=headers,
                        payload=payload,
                        strict_transport=True,
                    )
                except UpstreamTemporaryError as exc:
                    last_error = exc
                    if trace is not None:
                        trace.finish_stage(stage_id, status="failed", error=exc)
                    continue
                finally:
                    self._clear_session_override()

                body = str(getattr(response, "text", "") or "")
                status_code = int(getattr(response, "status_code", 0) or 0)
                if trace is not None:
                    trace.finish_stage(
                        stage_id,
                        status="succeeded" if status_code == 200 else "failed",
                        response=response_snapshot(response),
                    )
                if status_code in (401, 403):
                    access_error = str(response.headers.get("x-access-error") or "")
                    if access_error.lower() == "taste_exhausted":
                        raise QuotaExhaustedError("Adobe quota exhausted for this account")
                    raise AuthError(
                        f"Adobe submit auth failed: {status_code} {access_error} {body[:300]}",
                        status_code=status_code,
                        error_type=(
                            "user_not_entitled"
                            if access_error.lower() == "user_not_entitled"
                            else "auth"
                        ),
                    )
                if self._is_adobe_overload_text(body):
                    last_error = UpstreamTemporaryError(
                        f"Adobe upstream temporary error: {body[:300]}",
                        status_code=status_code or None,
                        error_type="adobe_temporary",
                    )
                    continue
                if self._is_adobe_content_rejection(status_code, body):
                    raise ContentPolicyError(body, param="prompt")
                if status_code == 429 or status_code == 451 or status_code >= 500:
                    last_error = UpstreamTemporaryError(
                        f"submit failed: {status_code} {body[:300]}",
                        status_code=status_code,
                        error_type="adobe_dead_upstream",
                    )
                    continue
                if status_code != 200:
                    last_error = AdobeRequestError(
                        f"submit rejected: {status_code} {body[:300]}",
                        status_code=status_code,
                        error_type="status",
                    )
                    continue
                try:
                    submit_data = response.json()
                except Exception:
                    last_error = AdobeRequestError("submit response is not valid json")
                    continue
                poll_url = self._extract_result_link(response, submit_data)
                if not poll_url:
                    last_error = AdobeRequestError("submit ok but no poll url")
                    continue
                upstream_job_id = self._extract_job_id(poll_url)
                if progress_cb is not None:
                    progress_cb(
                        {
                            "task_status": "IN_PROGRESS",
                            "task_progress": 0.0,
                            "upstream_job_id": upstream_job_id,
                            "retry_after": None,
                        }
                    )
                return {
                    "poll_url": poll_url,
                    "upstream_job_id": upstream_job_id,
                    "latest": submit_data,
                    "submitted_at": time.time(),
                    "sleep_time": 3.0,
                    "protocol_profile": "remote_adobe",
                    "direct_fingerprint": direct_fingerprint,
                    "_direct_session": direct_session,
                }

            if last_error is not None:
                raise last_error
            raise AdobeRequestError("submit failed: no response")
        except BaseException:
            if direct_session is not None:
                direct_session.close()
            raise
        finally:
            self._clear_session_override()
            if submit_session is not None:
                submit_session.close()

    def submit_image_job(
        self,
        *,
        token: str,
        prompt: str,
        aspect_ratio: str = "16:9",
        output_resolution: str = "2K",
        upstream_model_id: str = "gemini-flash",
        upstream_model_version: str = "nano-banana-2",
        quality_level: Optional[str] = None,
        detail_level: Optional[int] = None,
        seed: Optional[int] = None,
        source_image_ids: Optional[list[str]] = None,
        requested_size: Optional[dict] = None,
        progress_cb: Optional[Callable[[dict], None]] = None,
        trace: Optional[RequestTrace] = None,
        trace_parent_id: Optional[str] = None,
        cancel_check: Optional[Callable[[], None]] = None,
        protocol_profile: str = "",
    ) -> dict:
        """Submit an image task and return poll metadata without entering poll wait."""
        if protocol_profile == "remote_adobe":
            return self._submit_remote_adobe_image_job(
                token=token,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                output_resolution=output_resolution,
                upstream_model_id=upstream_model_id,
                upstream_model_version=upstream_model_version,
                quality_level=quality_level,
                detail_level=detail_level,
                seed=seed,
                source_image_ids=source_image_ids,
                requested_size=requested_size,
                progress_cb=progress_cb,
                trace=trace,
                trace_parent_id=trace_parent_id,
                cancel_check=cancel_check,
            )
        submit_resp = None
        first_error = ""
        first_error_status: Optional[int] = None
        payload_candidates = self._build_payload_candidates(
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            output_resolution=output_resolution,
            upstream_model_id=upstream_model_id,
            upstream_model_version=upstream_model_version,
            quality_level=quality_level,
            detail_level=detail_level,
            seed=seed,
            source_image_ids=source_image_ids,
            requested_size=requested_size,
            protocol_profile=protocol_profile,
        )
        for candidate_index, payload in enumerate(payload_candidates, start=1):
            submit_headers = self._submit_headers(
                token, prompt=prompt, protocol_profile=protocol_profile
            )
            submit_stage_id = None
            if trace is not None:
                submit_stage_id = trace.start_stage(
                    layer="adobe",
                    kind="submit",
                    name="提交 Adobe GPT Image 任务",
                    parent_id=trace_parent_id,
                    attempt={
                        "candidate": candidate_index,
                        "candidate_count": len(payload_candidates),
                    },
                    request={
                        "method": "POST",
                        "url": sanitize_url(self.submit_url),
                        "headers": sanitize_headers(submit_headers),
                        "body": sanitize_trace_value(payload),
                    },
                )
            network_started: Optional[float] = None
            invalid_size_auto_retry_used = False
            submit_retry_count = 0
            logger.info(
                "image async submit start token=%s candidate=%s/%s model=%s version=%s ratio=%s resolution=%s source_images=%s",
                str(token or "")[:8],
                candidate_index,
                len(payload_candidates),
                upstream_model_id,
                upstream_model_version,
                aspect_ratio,
                output_resolution,
                len(source_image_ids or []),
            )
            while True:
                if cancel_check is not None:
                    cancel_check()
                if progress_cb is not None:
                    progress_cb({"task_status": "SUBMITTING"})
                try:
                    submit_resp = self._post_image_json(
                        self.submit_url,
                        headers=submit_headers,
                        payload=payload,
                        strict_transport=(protocol_profile == "remote_adobe"),
                    )
                except ContentPolicyError:
                    if trace is not None:
                        trace.finish_stage(
                            submit_stage_id,
                            status="failed",
                            error="图片不安全",
                        )
                    raise
                except UpstreamTemporaryError as exc:
                    now = time.time()
                    network_started = network_started or now
                    if now - network_started >= self._image_submit_network_retry_seconds():
                        if trace is not None:
                            trace.finish_stage(submit_stage_id, status="failed", error=exc)
                        raise ImageStageTerminalError(
                            str(exc), status_code=502, error_type="network"
                        ) from exc
                    submit_retry_count += 1
                    delay = self._submit_retry_delay(
                        submit_retry_count, rate_limited=False
                    )
                    if progress_cb is not None:
                        progress_cb(
                            {
                                "task_status": "SUBMITTING",
                                "retry_after": int(round(delay)),
                                "retry_count": submit_retry_count,
                                "error": str(exc),
                            }
                        )
                    self._wait_for_image_retry(
                        delay, cancel_check=cancel_check, wait_cb=None
                    )
                    continue

                try:
                    self._raise_if_image_unsafe(submit_resp, param="prompt")
                    self._raise_if_reference_image_required(submit_resp)
                except (ContentPolicyError, ReferenceImageRequiredError) as exc:
                    if trace is not None:
                        trace.finish_stage(
                            submit_stage_id,
                            status="failed",
                            response=response_snapshot(submit_resp),
                            error=exc,
                        )
                    raise
                if (
                    str(payload.get("modelId") or "").strip().lower() == "gpt-image"
                    and not invalid_size_auto_retry_used
                    and self._is_invalid_image_size_aspect_response(submit_resp)
                ):
                    invalid_size_auto_retry_used = True
                    logger.warning(
                        "image async submit invalid_size_aspect auto_retry token=%s status=%s body=%s",
                        str(token or "")[:8],
                        getattr(submit_resp, "status_code", None),
                        str(getattr(submit_resp, "text", "") or "")[:300],
                    )
                    if trace is not None:
                        trace.add_stage(
                            layer="adobe",
                            kind="submit_retry",
                            name="Invalid image size，改用 auto 尺寸重试",
                            status="succeeded",
                            parent_id=trace_parent_id,
                            response=response_snapshot(submit_resp),
                            details={"fallback": "auto_size_no_aspect_ratio"},
                        )
                    payload = self._auto_size_fallback_payload(payload)
                    submit_headers = self._submit_headers(
                        token, prompt=prompt, protocol_profile=protocol_profile
                    )
                    continue
                is_rate_limited = (
                    submit_resp.status_code == 429
                    or self._is_rate_limited_response(submit_resp)
                )
                if is_rate_limited:
                    delay = self._image_rate_limit_single_retry_seconds()
                    logger.warning(
                        "image async submit rate_limited token=%s action=switch_account_after_delay delay=%s status=%s body=%s",
                        str(token or "")[:8],
                        delay,
                        getattr(submit_resp, "status_code", None),
                        str(getattr(submit_resp, "text", "") or "")[:300],
                    )
                    if progress_cb is not None:
                        progress_cb(
                            {
                                "task_status": "SUBMITTING",
                                "retry_after": int(round(delay)),
                                "retry_count": submit_retry_count,
                                "rate_limit_wait_seconds": delay,
                                "error": submit_resp.text[:300],
                            }
                        )
                    if trace is not None:
                        trace.finish_stage(
                            submit_stage_id,
                            status="failed",
                            response=response_snapshot(submit_resp),
                            error="submit rate limited; switch account",
                        )
                    raise SubmitRateLimitedError()
                if self._is_retryable_image_status(submit_resp.status_code):
                    now = time.time()
                    network_started = network_started or now
                    if now - network_started >= self._image_submit_network_retry_seconds():
                        break
                    submit_retry_count += 1
                    delay = self._submit_retry_delay(
                        submit_retry_count, rate_limited=False
                    )
                    if progress_cb is not None:
                        progress_cb(
                            {
                                "task_status": "SUBMITTING",
                                "retry_after": int(round(delay)),
                                "retry_count": submit_retry_count,
                                "error": submit_resp.text[:300],
                            }
                        )
                    self._wait_for_image_retry(
                        delay, cancel_check=cancel_check, wait_cb=None
                    )
                    continue
                break
            if trace is not None:
                trace.finish_stage(
                    submit_stage_id,
                    status="succeeded" if submit_resp.status_code == 200 else "failed",
                    response=response_snapshot(submit_resp),
                )
            if submit_resp.status_code == 200:
                break
            if submit_resp.status_code in (401, 403):
                break
            self._raise_if_image_unsafe(submit_resp, param="prompt")
            if not first_error:
                first_error = submit_resp.text[:300]
                first_error_status = submit_resp.status_code

        if submit_resp is None:
            raise AdobeRequestError("submit failed: no response")
        if submit_resp.status_code in (401, 403):
            access_error = submit_resp.headers.get("x-access-error")
            logger.warning(
                "submit auth failed status=%s access_error=%s body=%s",
                submit_resp.status_code,
                access_error,
                submit_resp.text[:300],
            )
            if access_error == "taste_exhausted":
                raise QuotaExhaustedError("Adobe quota exhausted for this account")
            raise AuthError("Token invalid or expired")
        if submit_resp.status_code != 200:
            logger.error("submit failed status=%s body=%s", submit_resp.status_code, submit_resp.text[:500])
            self._raise_if_image_unsafe(submit_resp, param="prompt")
            if submit_resp.status_code in (429, 451) or submit_resp.status_code >= 500:
                raise ImageStageTerminalError(
                    f"submit failed: {submit_resp.status_code} {submit_resp.text[:300]}",
                    status_code=submit_resp.status_code,
                    error_type="status",
                )
            if first_error:
                raise AdobeRequestError(
                    f"submit failed: {first_error_status or submit_resp.status_code} {first_error}"
                )
            raise AdobeRequestError(
                f"submit failed: {submit_resp.status_code} {submit_resp.text[:300]}"
            )

        submit_data = submit_resp.json()
        poll_url = self._extract_result_link(submit_resp, submit_data)
        logger.info(
            "image async submit success token=%s status=%s poll_url=%s",
            str(token or "")[:8],
            getattr(submit_resp, "status_code", None),
            sanitize_url(poll_url),
        )
        if not poll_url:
            raise AdobeRequestError("submit succeeded but no poll url returned")
        upstream_job_id = self._extract_job_id(poll_url)
        if progress_cb:
            try:
                progress_cb(
                    {
                        "task_status": "IN_PROGRESS",
                        "task_progress": 0.0,
                        "upstream_job_id": upstream_job_id,
                        "retry_after": int(submit_resp.headers.get("retry-after") or 0)
                        or None,
                    }
                )
            except Exception:
                pass
        return {
            "poll_url": poll_url,
            "upstream_job_id": upstream_job_id,
            "latest": submit_data,
            "submitted_at": time.time(),
            "sleep_time": 3.0,
            "poll_network_started": None,
            "poll_retry_count": 0,
            "protocol_profile": protocol_profile,
            "direct_fingerprint": (
                _select_adobe_fingerprint()
                if protocol_profile == "remote_adobe"
                else None
            ),
        }

    def _poll_remote_adobe_image_job_once(
        self,
        *,
        token: str,
        poll_url: str,
        state: dict,
        timeout: int,
        progress_cb: Optional[Callable[[dict], None]],
        trace: Optional[RequestTrace],
        trace_parent_id: Optional[str],
        cancel_check: Optional[Callable[[], None]],
    ) -> dict:
        if cancel_check is not None:
            cancel_check()
        submitted_at = float(state.get("submitted_at") or time.time())
        if time.time() - submitted_at > timeout:
            raise AdobeRequestError("generation timed out")
        upstream_job_id = str(
            state.get("upstream_job_id") or self._extract_job_id(poll_url)
        )
        fingerprint = state.get("direct_fingerprint")
        if not isinstance(fingerprint, dict):
            fingerprint = _select_adobe_fingerprint()
            state["direct_fingerprint"] = fingerprint
        headers = self._poll_headers(
            token,
            protocol_profile="remote_adobe",
            fingerprint=fingerprint,
        )
        started = time.perf_counter()
        try:
            self._set_session_override(state.get("_direct_session"))
            response = self._get(
                poll_url,
                headers=headers,
                timeout=60,
                use_proxy=False,
            )
        except UpstreamTemporaryError:
            raise
        finally:
            self._clear_session_override()
        snapshot = response_snapshot(response)
        body = str(getattr(response, "text", "") or "")
        status_code = int(getattr(response, "status_code", 0) or 0)
        if trace is not None:
            trace.record_poll(
                parent_id=trace_parent_id,
                status_key=str(status_code),
                request={
                    "method": "GET",
                    "url": sanitize_url(poll_url),
                    "headers": sanitize_headers(headers),
                },
                response=snapshot,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                failed=status_code != 200,
            )
        if self._is_adobe_overload_text(body):
            raise UpstreamTemporaryError(
                f"Adobe upstream temporary error: {body[:300]}",
                status_code=status_code or None,
                error_type="adobe_temporary",
            )
        if self._is_adobe_content_rejection(status_code, body):
            raise ContentPolicyError(body, param="prompt")
        if status_code == 429 or status_code == 451 or status_code >= 500:
            raise UpstreamTemporaryError(
                f"poll failed: {status_code} {body[:300]}",
                status_code=status_code,
                error_type="adobe_dead_upstream",
            )
        if status_code != 200:
            raise AdobeRequestError(
                f"poll failed: {status_code} {body[:300]}",
                status_code=status_code,
                error_type="status",
            )
        try:
            latest = response.json()
        except Exception as exc:
            raise AdobeRequestError("poll response is not valid json") from exc
        state["latest"] = latest
        outputs = latest.get("outputs") if isinstance(latest, dict) else None
        if isinstance(outputs, list) and outputs:
            first = outputs[0] if isinstance(outputs[0], dict) else {}
            image = first.get("image") if isinstance(first.get("image"), dict) else {}
            image_url = str(image.get("presignedUrl") or "").strip()
            if image_url:
                return {
                    "status": "completed",
                    "image_url": image_url,
                    "latest": latest,
                    "upstream_job_id": upstream_job_id,
                }
        status = str(latest.get("status") or "").strip().upper()
        if status in {"FAILED", "CANCELLED", "ERROR"}:
            raise AdobeRequestError(f"image job failed: {body[:300]}")
        progress = self._extract_progress_percent(latest, response)
        if progress_cb is not None:
            progress_cb(
                {
                    "task_status": "WAITING_POLL",
                    "task_progress": progress if progress is not None else 0.0,
                    "upstream_job_id": upstream_job_id,
                    "retry_after": 3,
                }
            )
        return {"status": "pending", "retry_after": 3.0, "latest": latest}

    def poll_image_job_once(
        self,
        *,
        token: str,
        poll_url: str,
        state: dict,
        timeout: int,
        progress_cb: Optional[Callable[[dict], None]] = None,
        trace: Optional[RequestTrace] = None,
        trace_parent_id: Optional[str] = None,
        cancel_check: Optional[Callable[[], None]] = None,
    ) -> dict:
        if str(state.get("protocol_profile") or "") == "remote_adobe":
            return self._poll_remote_adobe_image_job_once(
                token=token,
                poll_url=poll_url,
                state=state,
                timeout=timeout,
                progress_cb=progress_cb,
                trace=trace,
                trace_parent_id=trace_parent_id,
                cancel_check=cancel_check,
            )
        if cancel_check is not None:
            cancel_check()
        upstream_job_id = str(state.get("upstream_job_id") or self._extract_job_id(poll_url))
        started_at = float(state.get("submitted_at") or time.time())
        protocol_profile = str(state.get("protocol_profile") or "")
        direct_fingerprint = state.get("direct_fingerprint")
        poll_headers = self._poll_headers(
            token,
            protocol_profile=protocol_profile,
            fingerprint=(
                direct_fingerprint if isinstance(direct_fingerprint, dict) else None
            ),
        )
        poll_started = time.perf_counter()
        try:
            poll_resp = self._get(
                poll_url,
                headers=poll_headers,
                timeout=60,
                use_proxy=(protocol_profile != "remote_adobe"),
            )
        except UpstreamTemporaryError as exc:
            if trace is not None:
                trace.add_stage(
                    layer="adobe",
                    kind="poll",
                    name="Adobe task poll",
                    status="failed",
                    parent_id=trace_parent_id,
                    request={
                        "method": "GET",
                        "url": sanitize_url(poll_url),
                        "headers": sanitize_headers(poll_headers),
                    },
                    error=exc,
                )
            now = time.time()
            network_started = state.get("poll_network_started") or now
            state["poll_network_started"] = network_started
            if now - float(network_started) >= self._image_network_retry_seconds():
                raise ImageStageTerminalError(str(exc), status_code=502, error_type="network") from exc
            state["poll_retry_count"] = int(state.get("poll_retry_count") or 0) + 1
            delay = self._retry_delay(int(state["poll_retry_count"]), rate_limited=False)
            if progress_cb is not None:
                progress_cb(
                    {
                        "task_status": "WAITING_POLL",
                        "upstream_job_id": upstream_job_id,
                        "retry_after": int(round(delay)),
                        "retry_count": int(state["poll_retry_count"]),
                        "error": str(exc),
                    }
                )
            return {"status": "retry", "retry_after": delay, "latest": state.get("latest") or {}}

        poll_duration_ms = (time.perf_counter() - poll_started) * 1000.0
        poll_snapshot = response_snapshot(poll_resp)
        poll_body = poll_snapshot.get("body")
        body_status = (
            str(poll_body.get("status") or "") if isinstance(poll_body, dict) else ""
        )
        poll_status_key = "|".join(
            [
                str(poll_resp.status_code),
                str(poll_resp.headers.get("x-task-status") or "").upper(),
                body_status.upper(),
            ]
        )
        logger.info(
            "image async poll response token=%s upstream_job_id=%s status=%s task_status=%s body_status=%s retry_after=%s",
            str(token or "")[:8],
            upstream_job_id,
            getattr(poll_resp, "status_code", None),
            str(poll_resp.headers.get("x-task-status") or ""),
            body_status,
            poll_resp.headers.get("retry-after") or poll_resp.headers.get("Retry-After"),
        )
        if trace is not None:
            trace.record_poll(
                parent_id=trace_parent_id,
                status_key=poll_status_key,
                request={
                    "method": "GET",
                    "url": sanitize_url(poll_url),
                    "headers": sanitize_headers(poll_headers),
                },
                response=poll_snapshot,
                duration_ms=poll_duration_ms,
                failed=(
                    poll_resp.status_code != 200
                    or body_status.upper() in {"FAILED", "CANCELLED", "ERROR"}
                ),
            )
        self._raise_if_image_unsafe(poll_resp, param="prompt")
        is_rate_limited = (
            poll_resp.status_code == 429 or self._is_rate_limited_response(poll_resp)
        )
        if is_rate_limited:
            delay = self._image_rate_limit_single_retry_seconds()
            if progress_cb is not None:
                progress_cb(
                    {
                        "task_status": "RATE_LIMITED",
                        "upstream_job_id": upstream_job_id,
                        "retry_after": int(round(delay)),
                        "retry_count": int(state.get("poll_retry_count") or 0),
                        "rate_limit_wait_seconds": delay,
                        "error": poll_resp.text[:300],
                    }
                )
            raise SubmitRateLimitedError()
        if poll_resp.status_code != 200:
            logger.error("poll failed status=%s body=%s", poll_resp.status_code, poll_resp.text[:500])
            if self._is_fal_nanobanana_timeout_response(poll_resp):
                raise PollNanobananaTimeoutError(
                    f"poll failed: {poll_resp.status_code} {poll_resp.text[:300]}"
                )
            if self._is_retryable_image_status(poll_resp.status_code):
                now = time.time()
                network_started = state.get("poll_network_started") or now
                state["poll_network_started"] = network_started
                if now - float(network_started) < self._image_network_retry_seconds():
                    state["poll_retry_count"] = int(state.get("poll_retry_count") or 0) + 1
                    delay = self._retry_delay(int(state["poll_retry_count"]), rate_limited=False)
                    if progress_cb is not None:
                        progress_cb(
                            {
                                "task_status": "WAITING_POLL",
                                "upstream_job_id": upstream_job_id,
                                "retry_after": int(round(delay)),
                                "retry_count": int(state["poll_retry_count"]),
                                "error": poll_resp.text[:300],
                            }
                        )
                    return {"status": "retry", "retry_after": delay, "latest": state.get("latest") or {}}
                raise ImageStageTerminalError(
                    f"poll failed: {poll_resp.status_code} {poll_resp.text[:300]}",
                    status_code=poll_resp.status_code,
                    error_type="status",
                )
            raise AdobeRequestError(
                f"poll failed: {poll_resp.status_code} {poll_resp.text[:300]}"
            )

        latest = poll_resp.json()
        state["latest"] = latest
        self._raise_if_image_unsafe_data(latest, param="prompt")
        status_header = str(poll_resp.headers.get("x-task-status") or "").upper()
        status_val = str(latest.get("status") or "").upper() or status_header
        progress_val = self._extract_progress_percent(latest, poll_resp)
        if progress_cb and self._is_in_progress_status(status_val):
            try:
                progress_cb(
                    {
                        "task_status": "IN_PROGRESS",
                        "task_progress": progress_val if progress_val is not None else 0.0,
                        "upstream_job_id": upstream_job_id,
                        "retry_after": int(poll_resp.headers.get("retry-after") or 0) or None,
                    }
                )
            except Exception:
                pass
        outputs = latest.get("outputs") or []
        if outputs:
            image_url = ((outputs[0] or {}).get("image") or {}).get("presignedUrl")
            if not image_url:
                raise AdobeRequestError("job finished without image url")
            return {
                "status": "completed",
                "image_url": image_url,
                "latest": latest,
                "upstream_job_id": upstream_job_id,
            }
        if status_val in {"FAILED", "CANCELLED", "ERROR"}:
            if progress_cb:
                try:
                    progress_cb(
                        {
                            "task_status": "FAILED",
                            "task_progress": progress_val if progress_val is not None else 0.0,
                            "upstream_job_id": upstream_job_id,
                            "retry_after": None,
                            "error": f"image job failed: {latest}",
                        }
                    )
                except Exception:
                    pass
            raise AdobeRequestError(f"image job failed: {latest}")
        if time.time() - started_at > timeout:
            if progress_cb:
                try:
                    progress_cb(
                        {
                            "task_status": "FAILED",
                            "task_progress": progress_val if progress_val is not None else 0.0,
                            "upstream_job_id": upstream_job_id,
                            "retry_after": None,
                            "error": "image generation timed out",
                        }
                    )
                except Exception:
                    pass
            raise AdobeRequestError("generation timed out")
        poll_delay = self._response_retry_after(poll_resp) or float(state.get("sleep_time") or 3.0)
        if progress_cb is not None:
            try:
                progress_cb(
                    {
                        "task_status": "WAITING_POLL",
                        "task_progress": progress_val if progress_val is not None else 0.0,
                        "upstream_job_id": upstream_job_id,
                        "retry_after": int(poll_delay),
                    }
                )
            except Exception:
                pass
        return {"status": "pending", "retry_after": poll_delay, "latest": latest}

    def download_image_result(
        self,
        *,
        image_url: str,
        poll_url: str,
        token: str,
        out_path: Optional[Path],
        progress_cb: Optional[Callable[[dict], None]],
        trace: Optional[RequestTrace],
        trace_parent_id: Optional[str],
        upstream_job_id: str,
        cancel_check: Optional[Callable[[], None]],
        protocol_profile: str = "",
        fingerprint: Optional[dict[str, Any]] = None,
        session: Any = None,
    ) -> Optional[bytes]:
        return self._download_image_result(
            image_url=image_url,
            poll_url=poll_url,
            token=token,
            out_path=out_path,
            progress_cb=progress_cb,
            trace=trace,
            trace_parent_id=trace_parent_id,
            upstream_job_id=upstream_job_id,
            cancel_check=cancel_check,
            io_call=None,
            wait_cb=None,
            protocol_profile=protocol_profile,
            fingerprint=fingerprint,
            session=session,
        )

    def _generate_once(
        self,
        token: str,
        prompt: str,
        aspect_ratio: str = "16:9",
        output_resolution: str = "2K",
        upstream_model_id: str = "gemini-flash",
        upstream_model_version: str = "nano-banana-2",
        quality_level: Optional[str] = None,
        detail_level: Optional[int] = None,
        seed: Optional[int] = None,
        source_image_ids: Optional[list[str]] = None,
        requested_size: Optional[dict] = None,
        timeout: int = 180,
        out_path: Optional[Path] = None,
        progress_cb: Optional[Callable[[dict], None]] = None,
        trace: Optional[RequestTrace] = None,
        trace_parent_id: Optional[str] = None,
        cancel_check: Optional[Callable[[], None]] = None,
        io_call: Optional[Callable[[Callable[[], Any]], Any]] = None,
        wait_cb: Optional[Callable[[float], None]] = None,
        protocol_profile: str = "",
        download_result: bool = True,
    ) -> tuple[Optional[bytes], dict]:
        if protocol_profile == "remote_adobe":
            state = self.submit_image_job(
                token=token,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                output_resolution=output_resolution,
                upstream_model_id=upstream_model_id,
                upstream_model_version=upstream_model_version,
                quality_level=quality_level,
                detail_level=detail_level,
                seed=seed,
                source_image_ids=source_image_ids,
                requested_size=requested_size,
                progress_cb=progress_cb,
                trace=trace,
                trace_parent_id=trace_parent_id,
                cancel_check=cancel_check,
                protocol_profile=protocol_profile,
            )
            while True:
                result = self.poll_image_job_once(
                    token=token,
                    poll_url=str(state["poll_url"]),
                    state=state,
                    timeout=timeout,
                    progress_cb=progress_cb,
                    trace=trace,
                    trace_parent_id=trace_parent_id,
                    cancel_check=cancel_check,
                )
                if result.get("status") == "completed":
                    latest = result.get("latest") or {}
                    image_url = str(result.get("image_url") or "")
                    latest["image_url"] = image_url
                    if not download_result:
                        direct_session = state.get("_direct_session")
                        if direct_session is not None:
                            direct_session.close()
                        return None, latest
                    data = self._download_image_result(
                        image_url=image_url,
                        poll_url=str(state["poll_url"]),
                        token=token,
                        out_path=out_path,
                        progress_cb=progress_cb,
                        trace=trace,
                        trace_parent_id=trace_parent_id,
                        upstream_job_id=str(state.get("upstream_job_id") or ""),
                        cancel_check=cancel_check,
                        io_call=io_call,
                        wait_cb=wait_cb,
                        protocol_profile=protocol_profile,
                        fingerprint=state.get("direct_fingerprint"),
                        session=state.get("_direct_session"),
                    )
                    direct_session = state.get("_direct_session")
                    if direct_session is not None:
                        direct_session.close()
                    return data, latest
                time.sleep(3.0)

        submit_resp = None
        first_error = ""
        first_error_status: Optional[int] = None
        payload_candidates = self._build_payload_candidates(
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            output_resolution=output_resolution,
            upstream_model_id=upstream_model_id,
            upstream_model_version=upstream_model_version,
            quality_level=quality_level,
            detail_level=detail_level,
            seed=seed,
            source_image_ids=source_image_ids,
            requested_size=requested_size,
            protocol_profile=protocol_profile,
        )
        for candidate_index, payload in enumerate(payload_candidates, start=1):
            submit_headers = self._submit_headers(
                token, prompt=prompt, protocol_profile=protocol_profile
            )
            submit_stage_id = None
            if trace is not None:
                submit_stage_id = trace.start_stage(
                    layer="adobe",
                    kind="submit",
                    name="提交 Adobe GPT Image 任务",
                    parent_id=trace_parent_id,
                    attempt={
                        "candidate": candidate_index,
                        "candidate_count": len(payload_candidates),
                    },
                    request={
                        "method": "POST",
                        "url": sanitize_url(self.submit_url),
                        "headers": sanitize_headers(submit_headers),
                        "body": sanitize_trace_value(payload),
                    },
                )
            network_started: Optional[float] = None
            submit_rate_limit_retry_used = False
            invalid_size_auto_retry_used = False
            submit_retry_count = 0
            logger.info(
                "image submit start token=%s candidate=%s/%s model=%s version=%s ratio=%s resolution=%s source_images=%s",
                str(token or "")[:8],
                candidate_index,
                len(payload_candidates),
                upstream_model_id,
                upstream_model_version,
                aspect_ratio,
                output_resolution,
                len(source_image_ids or []),
            )
            while True:
                if cancel_check is not None:
                    cancel_check()
                if progress_cb is not None:
                    progress_cb({"task_status": "SUBMITTING"})
                try:
                    submit_resp = self._run_image_io(
                        io_call,
                        lambda: self._post_image_json(
                            self.submit_url,
                            headers=submit_headers,
                            payload=payload,
                            strict_transport=(protocol_profile == "remote_adobe"),
                        ),
                    )
                except ContentPolicyError:
                    if trace is not None:
                        trace.finish_stage(
                            submit_stage_id,
                            status="failed",
                            error="图片不安全",
                        )
                    raise
                except UpstreamTemporaryError as exc:
                    now = time.time()
                    network_started = network_started or now
                    if now - network_started >= self._image_submit_network_retry_seconds():
                        if trace is not None:
                            trace.finish_stage(
                                submit_stage_id, status="failed", error=exc
                            )
                        raise ImageStageTerminalError(
                            str(exc), status_code=502, error_type="network"
                        ) from exc
                    submit_retry_count += 1
                    delay = self._submit_retry_delay(
                        submit_retry_count, rate_limited=False
                    )
                    if progress_cb is not None:
                        progress_cb(
                            {
                                "task_status": "SUBMITTING",
                                "retry_after": int(round(delay)),
                                "retry_count": submit_retry_count,
                                "error": str(exc),
                            }
                        )
                    self._wait_for_image_retry(
                        delay, cancel_check=cancel_check, wait_cb=wait_cb
                    )
                    continue

                try:
                    self._raise_if_image_unsafe(submit_resp, param="prompt")
                    self._raise_if_reference_image_required(submit_resp)
                except (ContentPolicyError, ReferenceImageRequiredError) as exc:
                    if trace is not None:
                        trace.finish_stage(
                            submit_stage_id,
                            status="failed",
                            response=response_snapshot(submit_resp),
                            error=exc,
                        )
                    raise
                if (
                    str(payload.get("modelId") or "").strip().lower() == "gpt-image"
                    and not invalid_size_auto_retry_used
                    and self._is_invalid_image_size_aspect_response(submit_resp)
                ):
                    invalid_size_auto_retry_used = True
                    logger.warning(
                        "image submit invalid_size_aspect auto_retry token=%s status=%s body=%s",
                        str(token or "")[:8],
                        getattr(submit_resp, "status_code", None),
                        str(getattr(submit_resp, "text", "") or "")[:300],
                    )
                    if trace is not None:
                        trace.add_stage(
                            layer="adobe",
                            kind="submit_retry",
                            name="Invalid image size，改用 auto 尺寸重试",
                            status="succeeded",
                            parent_id=trace_parent_id,
                            response=response_snapshot(submit_resp),
                            details={"fallback": "auto_size_no_aspect_ratio"},
                        )
                    payload = self._auto_size_fallback_payload(payload)
                    submit_headers = self._submit_headers(
                        token, prompt=prompt, protocol_profile=protocol_profile
                    )
                    continue
                is_rate_limited = (
                    submit_resp.status_code == 429
                    or self._is_rate_limited_response(submit_resp)
                )
                if is_rate_limited:
                    delay = self._image_rate_limit_single_retry_seconds()
                    logger.warning(
                        "image submit rate_limited token=%s action=switch_account_after_delay delay=%s status=%s body=%s",
                        str(token or "")[:8],
                        delay,
                        getattr(submit_resp, "status_code", None),
                        str(getattr(submit_resp, "text", "") or "")[:300],
                    )
                    if progress_cb is not None:
                        progress_cb(
                            {
                                "task_status": "SUBMITTING",
                                "retry_after": int(round(delay)),
                                "retry_count": submit_retry_count,
                                "rate_limit_wait_seconds": delay,
                                "error": submit_resp.text[:300],
                            }
                        )
                    if trace is not None:
                        trace.finish_stage(
                            submit_stage_id,
                            status="failed",
                            response=response_snapshot(submit_resp),
                            error="submit rate limited; switch account",
                        )
                    raise SubmitRateLimitedError()
                if self._is_retryable_image_status(submit_resp.status_code):
                    now = time.time()
                    network_started = network_started or now
                    if now - network_started >= self._image_submit_network_retry_seconds():
                        break
                    submit_retry_count += 1
                    delay = self._submit_retry_delay(
                        submit_retry_count, rate_limited=False
                    )
                    if progress_cb is not None:
                        progress_cb(
                            {
                                "task_status": "SUBMITTING",
                                "retry_after": int(round(delay)),
                                "retry_count": submit_retry_count,
                                "error": submit_resp.text[:300],
                            }
                        )
                    self._wait_for_image_retry(
                        delay, cancel_check=cancel_check, wait_cb=wait_cb
                    )
                    continue
                break
            if trace is not None:
                trace.finish_stage(
                    submit_stage_id,
                    status="succeeded" if submit_resp.status_code == 200 else "failed",
                    response=response_snapshot(submit_resp),
                )
            if submit_resp.status_code == 200:
                break

            if submit_resp.status_code in (401, 403):
                break

            self._raise_if_image_unsafe(submit_resp, param="prompt")
            if not first_error:
                first_error = submit_resp.text[:300]
                first_error_status = submit_resp.status_code

        if submit_resp is None:
            raise AdobeRequestError("submit failed: no response")

        if submit_resp.status_code in (401, 403):
            access_error = submit_resp.headers.get("x-access-error")
            logger.warning(
                "submit auth failed status=%s access_error=%s body=%s",
                submit_resp.status_code,
                access_error,
                submit_resp.text[:300],
            )
            if access_error == "taste_exhausted":
                raise QuotaExhaustedError("Adobe quota exhausted for this account")
            raise AuthError("Token invalid or expired")

        if submit_resp.status_code != 200:
            logger.error(
                "submit failed status=%s body=%s",
                submit_resp.status_code,
                submit_resp.text[:500],
            )
            self._raise_if_image_unsafe(submit_resp, param="prompt")
            if submit_resp.status_code in (429, 451) or submit_resp.status_code >= 500:
                raise ImageStageTerminalError(
                    f"submit failed: {submit_resp.status_code} {submit_resp.text[:300]}",
                    status_code=submit_resp.status_code,
                    error_type="status",
                )
            if first_error:
                raise AdobeRequestError(
                    f"submit failed: {first_error_status or submit_resp.status_code} {first_error}"
                )
            raise AdobeRequestError(
                f"submit failed: {submit_resp.status_code} {submit_resp.text[:300]}"
            )

        submit_data = submit_resp.json()
        poll_url = self._extract_result_link(submit_resp, submit_data)
        logger.info(
            "image submit success token=%s status=%s poll_url=%s",
            str(token or "")[:8],
            getattr(submit_resp, "status_code", None),
            sanitize_url(poll_url),
        )
        if not poll_url:
            raise AdobeRequestError("submit succeeded but no poll url returned")

        upstream_job_id = self._extract_job_id(poll_url)
        logger.info(
            "image poll start token=%s upstream_job_id=%s poll_url=%s",
            str(token or "")[:8],
            upstream_job_id,
            sanitize_url(poll_url),
        )
        if progress_cb:
            try:
                progress_cb(
                    {
                        "task_status": "IN_PROGRESS",
                        "task_progress": 0.0,
                        "upstream_job_id": upstream_job_id,
                        "retry_after": int(submit_resp.headers.get("retry-after") or 0)
                        or None,
                    }
                )
            except Exception:
                pass

        start = time.time()
        latest = {}
        sleep_time = 3.0
        poll_network_started: Optional[float] = None
        poll_rate_limit_started: Optional[float] = None
        poll_rate_limit_retry_used = False
        poll_retry_count = 0
        direct_fingerprint = (
            _select_adobe_fingerprint()
            if protocol_profile == "remote_adobe"
            else None
        )
        while True:
            if cancel_check is not None:
                cancel_check()
            poll_headers = self._poll_headers(
                token,
                protocol_profile=protocol_profile,
                fingerprint=direct_fingerprint,
            )
            poll_started = time.perf_counter()
            try:
                poll_resp = self._run_image_io(
                    io_call,
                    lambda: self._get(
                        poll_url,
                        headers=poll_headers,
                        timeout=60,
                        use_proxy=(protocol_profile != "remote_adobe"),
                    ),
                )
            except UpstreamTemporaryError as exc:
                if trace is not None:
                    trace.add_stage(
                        layer="adobe",
                        kind="poll",
                        name="Adobe task poll",
                        status="failed",
                        parent_id=trace_parent_id,
                        request={
                            "method": "GET",
                            "url": sanitize_url(poll_url),
                            "headers": sanitize_headers(poll_headers),
                        },
                        error=exc,
                    )
                now = time.time()
                poll_network_started = poll_network_started or now
                if now - poll_network_started >= self._image_network_retry_seconds():
                    raise ImageStageTerminalError(
                        str(exc), status_code=502, error_type="network"
                    ) from exc
                poll_retry_count += 1
                delay = self._retry_delay(poll_retry_count, rate_limited=False)
                if progress_cb is not None:
                    progress_cb(
                        {
                            "task_status": "WAITING_POLL",
                            "upstream_job_id": upstream_job_id,
                            "retry_after": int(round(delay)),
                            "retry_count": poll_retry_count,
                            "error": str(exc),
                        }
                    )
                self._wait_for_image_retry(
                    delay, cancel_check=cancel_check, wait_cb=wait_cb
                )
                continue
            poll_duration_ms = (time.perf_counter() - poll_started) * 1000.0
            poll_snapshot = response_snapshot(poll_resp)
            poll_body = poll_snapshot.get("body")
            body_status = (
                str(poll_body.get("status") or "")
                if isinstance(poll_body, dict)
                else ""
            )
            poll_status_key = "|".join(
                [
                    str(poll_resp.status_code),
                    str(poll_resp.headers.get("x-task-status") or "").upper(),
                    body_status.upper(),
                ]
            )
            logger.info(
                "image poll response token=%s upstream_job_id=%s status=%s task_status=%s body_status=%s retry_after=%s",
                str(token or "")[:8],
                upstream_job_id,
                getattr(poll_resp, "status_code", None),
                str(poll_resp.headers.get("x-task-status") or ""),
                body_status,
                poll_resp.headers.get("retry-after") or poll_resp.headers.get("Retry-After"),
            )
            if trace is not None:
                trace.record_poll(
                    parent_id=trace_parent_id,
                    status_key=poll_status_key,
                    request={
                        "method": "GET",
                        "url": sanitize_url(poll_url),
                        "headers": sanitize_headers(poll_headers),
                    },
                    response=poll_snapshot,
                    duration_ms=poll_duration_ms,
                    failed=(
                        poll_resp.status_code != 200
                        or body_status.upper() in {"FAILED", "CANCELLED", "ERROR"}
                    ),
                )
            self._raise_if_image_unsafe(poll_resp, param="prompt")
            is_rate_limited = (
                poll_resp.status_code == 429
                or self._is_rate_limited_response(poll_resp)
            )
            if is_rate_limited:
                delay = self._image_rate_limit_single_retry_seconds()
                logger.warning(
                    "image poll rate_limited token=%s upstream_job_id=%s action=switch_account_after_delay delay=%s status=%s body=%s",
                    str(token or "")[:8],
                    upstream_job_id,
                    delay,
                    getattr(poll_resp, "status_code", None),
                    str(getattr(poll_resp, "text", "") or "")[:300],
                )
                if progress_cb is not None:
                    progress_cb(
                        {
                            "task_status": "RATE_LIMITED",
                            "upstream_job_id": upstream_job_id,
                            "retry_after": int(round(delay)),
                            "retry_count": poll_retry_count,
                            "rate_limit_wait_seconds": delay,
                            "error": poll_resp.text[:300],
                        }
                    )
                raise SubmitRateLimitedError()
            if poll_resp.status_code != 200:
                logger.error(
                    "poll failed status=%s body=%s",
                    poll_resp.status_code,
                    poll_resp.text[:500],
                )
                if self._is_fal_nanobanana_timeout_response(poll_resp):
                    raise PollNanobananaTimeoutError(
                        f"poll failed: {poll_resp.status_code} {poll_resp.text[:300]}"
                    )
                if self._is_retryable_image_status(poll_resp.status_code):
                    now = time.time()
                    poll_network_started = poll_network_started or now
                    if now - poll_network_started < self._image_network_retry_seconds():
                        poll_retry_count += 1
                        delay = self._retry_delay(
                            poll_retry_count, rate_limited=False
                        )
                        if progress_cb is not None:
                            progress_cb(
                                {
                                    "task_status": "WAITING_POLL",
                                    "upstream_job_id": upstream_job_id,
                                    "retry_after": int(round(delay)),
                                    "retry_count": poll_retry_count,
                                    "error": poll_resp.text[:300],
                                }
                            )
                        self._wait_for_image_retry(
                            delay,
                            cancel_check=cancel_check,
                            wait_cb=wait_cb,
                        )
                        continue
                    raise ImageStageTerminalError(
                        f"poll failed: {poll_resp.status_code} {poll_resp.text[:300]}",
                        status_code=poll_resp.status_code,
                        error_type="status",
                    )
                raise AdobeRequestError(
                    f"poll failed: {poll_resp.status_code} {poll_resp.text[:300]}"
                )

            latest = poll_resp.json()
            self._raise_if_image_unsafe_data(latest, param="prompt")
            status_header = str(poll_resp.headers.get("x-task-status") or "").upper()
            status_val = str(latest.get("status") or "").upper() or status_header
            progress_val = self._extract_progress_percent(latest, poll_resp)

            if progress_cb and self._is_in_progress_status(status_val):
                try:
                    progress_cb(
                        {
                            "task_status": "IN_PROGRESS",
                            "task_progress": progress_val
                            if progress_val is not None
                            else 0.0,
                            "upstream_job_id": upstream_job_id,
                            "retry_after": int(
                                poll_resp.headers.get("retry-after") or 0
                            )
                            or None,
                        }
                    )
                except Exception:
                    pass

            outputs = latest.get("outputs") or []
            if outputs:
                image_url = ((outputs[0] or {}).get("image") or {}).get("presignedUrl")
                if not image_url:
                    raise AdobeRequestError("job finished without image url")
                latest["image_url"] = image_url
                if not download_result:
                    if progress_cb:
                        progress_cb(
                            {
                                "task_status": "COMPLETED",
                                "task_progress": 100.0,
                                "upstream_job_id": upstream_job_id,
                                "retry_after": None,
                            }
                        )
                    return None, latest
                image_bytes = self._download_image_result(
                    image_url=image_url,
                    poll_url=poll_url,
                    token=token,
                    out_path=out_path,
                    progress_cb=progress_cb,
                    trace=trace,
                    trace_parent_id=trace_parent_id,
                    upstream_job_id=upstream_job_id,
                    cancel_check=cancel_check,
                    io_call=io_call,
                    wait_cb=wait_cb,
                    protocol_profile=protocol_profile,
                    fingerprint=direct_fingerprint,
                )
                if progress_cb:
                    try:
                        progress_cb(
                            {
                                "task_status": "COMPLETED",
                                "task_progress": 100.0,
                                "upstream_job_id": upstream_job_id,
                                "retry_after": None,
                            }
                        )
                    except Exception:
                        pass
                logger.info(
                    "image generation success token=%s upstream_job_id=%s bytes=%s",
                    str(token or "")[:8],
                    upstream_job_id,
                    len(image_bytes or b""),
                )
                return image_bytes, latest

            if status_val in {"FAILED", "CANCELLED", "ERROR"}:
                if progress_cb:
                    try:
                        progress_cb(
                            {
                                "task_status": "FAILED",
                                "task_progress": progress_val
                                if progress_val is not None
                                else 0.0,
                                "upstream_job_id": upstream_job_id,
                                "retry_after": None,
                                "error": f"image job failed: {latest}",
                            }
                        )
                    except Exception:
                        pass
                raise AdobeRequestError(f"image job failed: {latest}")

            if time.time() - start > timeout:
                if progress_cb:
                    try:
                        progress_cb(
                            {
                                "task_status": "FAILED",
                                "task_progress": progress_val
                                if progress_val is not None
                                else 0.0,
                                "upstream_job_id": upstream_job_id,
                                "retry_after": None,
                                "error": "image generation timed out",
                            }
                        )
                    except Exception:
                        pass
                raise AdobeRequestError("generation timed out")
            if progress_cb is not None:
                try:
                    progress_cb(
                        {
                            "task_status": "WAITING_POLL",
                            "task_progress": progress_val
                            if progress_val is not None
                            else 0.0,
                            "upstream_job_id": upstream_job_id,
                            "retry_after": int(
                                poll_resp.headers.get("retry-after") or sleep_time
                            ),
                        }
                    )
                except Exception:
                    pass
            poll_delay = self._response_retry_after(poll_resp) or sleep_time
            self._wait_for_image_retry(
                poll_delay, cancel_check=cancel_check, wait_cb=wait_cb
            )

    def generate(
        self,
        token: str,
        prompt: str,
        aspect_ratio: str = "16:9",
        output_resolution: str = "2K",
        upstream_model_id: str = "gemini-flash",
        upstream_model_version: str = "nano-banana-2",
        quality_level: Optional[str] = None,
        detail_level: Optional[int] = None,
        seed: Optional[int] = None,
        source_image_ids: Optional[list[str]] = None,
        requested_size: Optional[dict] = None,
        timeout: int = 180,
        out_path: Optional[Path] = None,
        progress_cb: Optional[Callable[[dict], None]] = None,
        trace: Optional[RequestTrace] = None,
        trace_parent_id: Optional[str] = None,
        cancel_check: Optional[Callable[[], None]] = None,
        io_call: Optional[Callable[[Callable[[], Any]], Any]] = None,
        wait_cb: Optional[Callable[[float], None]] = None,
        protocol_profile: str = "",
        download_result: bool = True,
    ) -> tuple[Optional[bytes], dict]:
        is_gpt_image = str(upstream_model_id or "").strip().lower() == "gpt-image"
        fixed_seed = (
            int(seed)
            if is_gpt_image and seed is not None
            else (random_image_seed() if is_gpt_image else None)
        )
        return self._generate_once(
            token=token,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            output_resolution=output_resolution,
            upstream_model_id=upstream_model_id,
            upstream_model_version=upstream_model_version,
            quality_level=quality_level,
            detail_level=detail_level,
            seed=fixed_seed,
            source_image_ids=source_image_ids,
            requested_size=requested_size,
            timeout=timeout,
            out_path=out_path,
            progress_cb=progress_cb,
            trace=trace,
            trace_parent_id=trace_parent_id,
            cancel_check=cancel_check,
            io_call=io_call,
            wait_cb=wait_cb,
            protocol_profile=protocol_profile,
            download_result=download_result,
        )

