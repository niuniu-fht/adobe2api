import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from typing import Any, Callable, Dict, List, Optional


class TempMailLolError(RuntimeError):
    pass


class TempMailLolClient:
    """Small TempMail.lol v2 API client.

    Uses curl_cffi when available because the public API is behind Cloudflare.
    Falls back to urllib for environments where direct API access is allowed.
    """

    BASE_URL = "https://api.tempmail.lol/v2"

    def __init__(
        self,
        api_key: str = "",
        *,
        base_url: str = BASE_URL,
        timeout: int = 30,
        proxy: str = "",
    ) -> None:
        self.api_key = (
            str(api_key or "").strip()
            or os.getenv("TEMPMAIL_LOL_API_KEY", "").strip()
            or os.getenv("TEMPMAIL_API_KEY", "").strip()
        )
        self.base_url = str(base_url or self.BASE_URL).rstrip("/")
        self.timeout = max(5, int(timeout or 30))
        self.proxy = str(proxy or os.getenv("TEMPMAIL_LOL_PROXY", "") or "").strip()

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        method = method.upper()

        try:
            from curl_cffi import requests as curl_requests  # type: ignore

            response = curl_requests.request(
                method,
                url,
                json=payload if payload is not None else None,
                headers=self._headers(),
                timeout=self.timeout,
                impersonate="chrome120",
                proxies={"http": self.proxy, "https": self.proxy} if self.proxy else None,
            )
            text = response.text or ""
            if response.status_code >= 400:
                raise TempMailLolError(self._format_http_error(response.status_code, text))
            return self._decode_json(text)
        except ImportError:
            return self._urllib_request(method, url, payload)
        except TempMailLolError:
            raise
        except Exception as exc:
            raise TempMailLolError(str(exc)) from exc

    def _urllib_request(
        self, method: str, url: str, payload: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers=self._headers(),
        )
        try:
            if self.proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy})
                )
                response_ctx = opener.open(req, timeout=self.timeout)
            else:
                response_ctx = urllib.request.urlopen(req, timeout=self.timeout)
            with response_ctx as response:
                return self._decode_json(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            raise TempMailLolError(self._format_http_error(exc.code, text)) from exc
        except urllib.error.URLError as exc:
            raise TempMailLolError(str(exc.reason)) from exc

    @staticmethod
    def _decode_json(text: str) -> Dict[str, Any]:
        try:
            data = json.loads(text or "{}")
        except json.JSONDecodeError as exc:
            raise TempMailLolError(f"invalid json response: {text[:200]}") from exc
        if not isinstance(data, dict):
            raise TempMailLolError("unexpected response payload")
        if data.get("error"):
            raise TempMailLolError(str(data.get("error")))
        return data

    @staticmethod
    def _format_http_error(status: int, text: str) -> str:
        try:
            data = json.loads(text or "{}")
            detail = data.get("error") or data.get("message") or text
        except Exception:
            detail = text
        detail = str(detail or "").strip()
        return f"HTTP {status}: {detail[:300]}"

    def create_inbox(
        self, *, prefix: Optional[str] = None, domain: Optional[str] = None
    ) -> Dict[str, str]:
        payload: Dict[str, Any] = {}
        if prefix:
            payload["prefix"] = self._clean_prefix(prefix)
        if domain:
            payload["domain"] = str(domain).strip().lower().lstrip("@")
        data = self._request("POST", "/inbox/create", payload=payload)
        address = str(data.get("address") or "").strip()
        token = str(data.get("token") or "").strip()
        if not address or not token:
            raise TempMailLolError("create inbox response missing address/token")
        return {"address": address, "token": token}

    def fetch_inbox(self, token: str) -> Dict[str, Any]:
        token = str(token or "").strip()
        if not token:
            raise TempMailLolError("token is required")
        data = self._request("GET", "/inbox", query={"token": token})
        emails = data.get("emails")
        if emails is None:
            data["emails"] = []
        elif not isinstance(emails, list):
            raise TempMailLolError("inbox response emails is not a list")
        return data

    def wait_for_email(
        self,
        token: str,
        *,
        timeout: int = 180,
        interval: float = 5.0,
        matcher: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> Optional[Dict[str, Any]]:
        deadline = time.time() + max(1, int(timeout or 180))
        interval = max(1.0, float(interval or 5.0))
        seen: set[str] = set()
        while time.time() < deadline:
            inbox = self.fetch_inbox(token)
            for email in inbox.get("emails") or []:
                if not isinstance(email, dict):
                    continue
                key = json.dumps(email, sort_keys=True, ensure_ascii=False)
                if key in seen:
                    continue
                seen.add(key)
                if matcher is None or matcher(email):
                    return email
            if inbox.get("expired"):
                return None
            time.sleep(interval)
        return None

    @staticmethod
    def extract_verification(email: Dict[str, Any]) -> Dict[str, str]:
        text = " ".join(
            str(email.get(key) or "")
            for key in ("subject", "body", "html", "text", "from")
        )
        text = unescape(re.sub(r"<[^>]+>", " ", text))
        links = re.findall(r"https?://[^\s'\"<>]+", text)
        code_match = re.search(r"(?<!\d)(\d{6,8})(?!\d)", text)
        return {
            "code": code_match.group(1) if code_match else "",
            "link": links[0] if links else "",
        }

    @staticmethod
    def _clean_prefix(prefix: str) -> str:
        value = str(prefix or "").strip().lower()
        cleaned = "".join(ch for ch in value if ch.isalnum() or ch in {"-", "_", "."})
        cleaned = cleaned.strip("-_.")
        return cleaned[:48] or "adobe2api"
