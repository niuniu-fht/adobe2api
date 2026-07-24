import base64
import hashlib
import json
import logging
import os
import random
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote, urlparse

import requests

from core.config_mgr import config_manager
from core.models import build_image_payload_candidates, random_image_seed
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
    from PIL import Image
except Exception:
    Image = None


logger = logging.getLogger("adobe2api")

DEFAULT_GPT_IMAGE_MODEL_QUALITIES = {
    "gpt-image-2-high": "medium",
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
    prompt_prefix = str(prompt or "")[:256]
    if not user_id or not prompt_prefix:
        return ""
    nonce_input = f"{user_id}-{prompt_prefix}".encode("utf-8")
    return hashlib.sha256(nonce_input).hexdigest()


def _build_arp_session_id() -> str:
    now_ms = int(time.time() * 1000)
    ftr = f"{os.urandom(16).hex()}_{now_ms}_{os.getpid()}_dUAL43-mnts-ants-d4_31ck__tt"
    raw = json.dumps(
        {"sid": str(uuid.uuid4()), "ftr": ftr},
        separators=(",", ":"),
    )
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


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
    entity_api_base = "https://firefly-entity.adobe.io/api/entities/"
    platform_cs_index_url = "https://platform-cs-edge.adobe.io/index"
    platform_cs_base = "https://platform-cs-va6.adobe.io/composite/component/path"

    def __init__(self) -> None:
        self.api_key = "projectx_webapp"
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
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        self.sec_ch_ua = (
            '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"'
        )

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
    def _is_retryable_image_status(status_code: int) -> bool:
        normalized = int(status_code or 0)
        return normalized in {408, 425, 451} or 500 <= normalized <= 599

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

    def _requests_proxies(self) -> Optional[dict]:
        if not self.proxy:
            return None
        return {"http": self.proxy, "https": self.proxy}

    def _session(self):
        if CurlSession is None:
            return None
        kwargs = {"impersonate": self.impersonate, "timeout": 60}
        if self.proxy:
            kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
        return CurlSession(**kwargs)

    def _browser_headers(self) -> dict:
        return {
            "user-agent": self.user_agent,
            "origin": "https://new.express.adobe.com",
            "referer": "https://new.express.adobe.com/",
            "accept-language": "en-US,en;q=0.9",
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-site": "cross-site",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        }

    def _submit_headers(self, token: str, prompt: str = "") -> dict:
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

    def _poll_headers(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "accept": "*/*",
            "referer": "https://new.express.adobe.com/",
            "origin": "https://new.express.adobe.com",
            "user-agent": self.user_agent,
            "x-api-key": self.api_key,
            "content-type": "application/json",
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
    ):
        session = self._session()
        if session is None:
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

    def _post_image_json(self, url: str, headers: dict, payload: dict):
        primary_response = None
        primary_error: Optional[Exception] = None
        try:
            primary_response = self._post_json(
                url,
                headers,
                payload,
                legacy_451_fallback=False,
            )
        except ContentPolicyError:
            raise
        except Exception as exc:
            primary_error = exc

        if primary_response is not None:
            self._raise_if_image_unsafe(primary_response, param="prompt")
            if primary_response.status_code == 200:
                try:
                    primary_data = primary_response.json()
                except Exception:
                    primary_data = None
                if primary_data is not None and self._extract_result_link(
                    primary_response, primary_data
                ):
                    return primary_response

        logger.warning(
            "image submit primary transport failed; retrying with requests status=%s error=%s",
            getattr(primary_response, "status_code", None),
            str(primary_error or ""),
        )
        fallback_response = self._post_json_requests_once(url, headers, payload)
        self._raise_if_image_unsafe(fallback_response, param="prompt")
        return fallback_response

    def _post_bytes(self, url: str, headers: dict, payload: bytes):
        session = self._session()
        if session is None:
            try:
                return requests.post(
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

    def _get(self, url: str, headers: dict, timeout: int = 60):
        session = self._session()
        if session is None:
            try:
                return requests.get(
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
    ) -> int:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        try:
            with requests.get(
                url,
                headers=headers or {},
                timeout=timeout,
                proxies=self._requests_proxies(),
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
    ) -> str:
        resp = self._run_image_io(
            io_call,
            lambda: self._get(
                poll_url, headers=self._poll_headers(token), timeout=60
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
    ) -> Optional[bytes]:
        attempts = self._image_download_attempts()
        delays = (1.0, 2.0, 4.0, 8.0)
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
                    downloaded_size = self._run_image_io(
                        io_call,
                        lambda: self._download_to_file(
                            current_url,
                            headers=download_headers,
                            out_path=part_path,
                            timeout=30,
                        ),
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
                        current_url, headers=download_headers, timeout=30
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
                            poll_url, token, io_call=io_call
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
    ) -> str:
        if not image_bytes:
            raise AdobeRequestError("image is empty")

        headers = {
            "authorization": f"Bearer {token}",
            "x-api-key": self.api_key,
            "content-type": mime_type,
            "accept": "application/json",
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
        network_started: Optional[float] = None
        rate_limit_started: Optional[float] = None
        retry_count = 0
        while True:
            if cancel_check is not None:
                cancel_check()
            try:
                resp = self._run_image_io(
                    io_call,
                    lambda: self._post_bytes(
                        self.upload_url, headers=headers, payload=image_bytes
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
                now = time.time()
                rate_limit_started = rate_limit_started or now
                elapsed = now - rate_limit_started
                if elapsed >= self._image_rate_limit_wait_seconds():
                    if trace is not None:
                        trace.finish_stage(
                            trace_stage_id,
                            status="failed",
                            response=response_snapshot(resp),
                            error="rate limit wait exceeded",
                        )
                    raise RateLimitWaitExceededError()
                retry_count += 1
                delay = self._retry_delay(
                    retry_count,
                    rate_limited=True,
                    retry_after=self._response_retry_after(resp),
                )
                delay = min(
                    delay,
                    max(0.05, self._image_rate_limit_wait_seconds() - elapsed),
                )
                if progress_cb is not None:
                    progress_cb(
                        {
                            "task_status": "RATE_LIMITED",
                            "retry_after": int(round(delay)),
                            "retry_count": retry_count,
                            "rate_limit_wait_seconds": min(
                                self._image_rate_limit_wait_seconds(), elapsed + delay
                            ),
                            "error": resp.text[:300],
                        }
                    )
                self._wait_for_image_retry(
                    delay, cancel_check=cancel_check, wait_cb=wait_cb
                )
                continue
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
        if upstream_code == "image_unsafe":
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
    ) -> list[dict]:
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
            host = parsed.netloc
            path_parts = [p for p in parsed.path.split("/") if p]
            if not host or not path_parts:
                return raw_url
            if not host.startswith("firefly-epo"):
                return raw_url
            job_id = path_parts[-1]
            if not job_id:
                return raw_url
            host_suffix = host[len("firefly-epo") :].split(".", 1)[0]
            shard = host_suffix[:4].strip()
            if len(shard) != 4 or not shard.isdigit():
                return raw_url
            return f"https://bks-epo{shard}.adobe.io/v2/jobs/result/{job_id}?host={host}/"
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
            return poll_url

        links = submit_data.get("links") if isinstance(submit_data, dict) else {}
        if not isinstance(links, dict):
            links = {}

        result_link = links.get("result")
        if isinstance(result_link, str):
            return result_link.strip()
        if isinstance(result_link, dict):
            return str(result_link.get("href") or "").strip()
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

            if time.time() - start > timeout:
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
    ) -> tuple[Optional[bytes], dict]:
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
        )
        for candidate_index, payload in enumerate(payload_candidates, start=1):
            submit_headers = self._submit_headers(token, prompt=prompt)
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
            rate_limit_started: Optional[float] = None
            submit_retry_count = 0
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
                    if now - network_started >= self._image_network_retry_seconds():
                        if trace is not None:
                            trace.finish_stage(
                                submit_stage_id, status="failed", error=exc
                            )
                        raise ImageStageTerminalError(
                            str(exc), status_code=502, error_type="network"
                        ) from exc
                    submit_retry_count += 1
                    delay = self._retry_delay(
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
                is_rate_limited = (
                    submit_resp.status_code == 429
                    or self._is_rate_limited_response(submit_resp)
                )
                if is_rate_limited:
                    now = time.time()
                    rate_limit_started = rate_limit_started or now
                    elapsed = now - rate_limit_started
                    if elapsed >= self._image_rate_limit_wait_seconds():
                        if trace is not None:
                            trace.finish_stage(
                                submit_stage_id,
                                status="failed",
                                response=response_snapshot(submit_resp),
                                error="rate limit wait exceeded",
                            )
                        raise RateLimitWaitExceededError()
                    submit_retry_count += 1
                    delay = self._retry_delay(
                        submit_retry_count,
                        rate_limited=True,
                        retry_after=self._response_retry_after(submit_resp),
                    )
                    delay = min(
                        delay,
                        max(
                            0.05,
                            self._image_rate_limit_wait_seconds() - elapsed,
                        ),
                    )
                    if progress_cb is not None:
                        progress_cb(
                            {
                                "task_status": "RATE_LIMITED",
                                "retry_after": int(round(delay)),
                                "retry_count": submit_retry_count,
                                "rate_limit_wait_seconds": min(
                                    self._image_rate_limit_wait_seconds(), elapsed + delay
                                ),
                                "error": submit_resp.text[:300],
                            }
                        )
                    self._wait_for_image_retry(
                        delay, cancel_check=cancel_check, wait_cb=wait_cb
                    )
                    continue
                if self._is_retryable_image_status(submit_resp.status_code):
                    now = time.time()
                    network_started = network_started or now
                    if now - network_started >= self._image_network_retry_seconds():
                        break
                    submit_retry_count += 1
                    delay = self._retry_delay(
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

        start = time.time()
        latest = {}
        sleep_time = 3.0
        poll_network_started: Optional[float] = None
        poll_rate_limit_started: Optional[float] = None
        poll_retry_count = 0
        while True:
            if cancel_check is not None:
                cancel_check()
            poll_headers = self._poll_headers(token)
            poll_started = time.perf_counter()
            try:
                poll_resp = self._run_image_io(
                    io_call,
                    lambda: self._get(
                        poll_url, headers=poll_headers, timeout=60
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
                now = time.time()
                poll_rate_limit_started = poll_rate_limit_started or now
                elapsed = now - poll_rate_limit_started
                if elapsed >= self._image_rate_limit_wait_seconds():
                    raise RateLimitWaitExceededError()
                poll_retry_count += 1
                delay = self._retry_delay(
                    poll_retry_count,
                    rate_limited=True,
                    retry_after=self._response_retry_after(poll_resp),
                )
                delay = min(
                    delay,
                    max(0.05, self._image_rate_limit_wait_seconds() - elapsed),
                )
                if progress_cb is not None:
                    progress_cb(
                        {
                            "task_status": "RATE_LIMITED",
                            "upstream_job_id": upstream_job_id,
                            "retry_after": int(round(delay)),
                            "retry_count": poll_retry_count,
                            "rate_limit_wait_seconds": min(
                                self._image_rate_limit_wait_seconds(), elapsed + delay
                            ),
                            "error": poll_resp.text[:300],
                        }
                    )
                self._wait_for_image_retry(
                    delay, cancel_check=cancel_check, wait_cb=wait_cb
                )
                continue
            if poll_resp.status_code != 200:
                logger.error(
                    "poll failed status=%s body=%s",
                    poll_resp.status_code,
                    poll_resp.text[:500],
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
        )
