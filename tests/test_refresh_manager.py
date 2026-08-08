import pytest

import base64
import hashlib
import json

from api.schemas import RefreshCookieBatchImportItem, RefreshCookieImportRequest
import core.adobe_client as adobe_client_module
from core.adobe_client import (
    AdobeClient,
    fetch_adobe_account_profile,
    fetch_adobe_credits_balance,
    _go_json_bytes,
    _normalize_arp_session_region,
    exchange_adobe_cookie,
)
from core.refresh_mgr import RefreshManager


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Cookie: sid=one; auth=two", "sid=one; auth=two"),
        (
            [{"name": "sid", "value": "one"}, {"name": "auth", "value": "two"}],
            "sid=one; auth=two",
        ),
        ({"cookies": [{"name": "sid", "value": "one"}]}, "sid=one"),
        ({"cookie": "sid=one; auth=two"}, "sid=one; auth=two"),
        ({"cookie": {"cookie": "sid=one"}}, "sid=one"),
    ],
)
def test_cookie_string_input_formats(value, expected):
    assert RefreshManager._cookie_string_from_input(value) == expected


def test_structured_move_bundle_preserves_cookie_and_firefly_header():
    bundle = {
        "cookie": "sid=one; auth=two",
        "headers": {"X-Arp-Session-Id": "arp-session"},
    }

    assert RefreshManager._cookie_string_from_input(bundle) == "sid=one; auth=two"
    assert RefreshManager._firefly_headers_from_input(bundle) == {
        "x-arp-session-id": "arp-session"
    }


def test_batch_cookie_import_item_preserves_firefly_header():
    item = RefreshCookieBatchImportItem(
        cookie="sid=one; auth=two",
        name="child@example.com",
        headers={"x-arp-session-id": "arp-session"},
    )

    assert item.headers == {"x-arp-session-id": "arp-session"}


def test_single_cookie_import_request_preserves_firefly_header():
    req = RefreshCookieImportRequest(
        cookie="sid=one; auth=two",
        name="child@example.com",
        headers={"x-arp-session-id": "arp-session"},
    )

    assert req.headers == {"x-arp-session-id": "arp-session"}


def test_arp_session_region_is_normalized_for_firefly_submit():
    raw = {
        "sid": "session-id",
        "ark": "abc|r=us-west-2|meta=3|rid=1",
        "bfp": "fingerprint",
        "ftr": "trace",
    }
    encoded = base64.b64encode(
        json.dumps(raw, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")

    normalized = _normalize_arp_session_region(encoded, "ap-southeast-1")
    decoded = json.loads(base64.b64decode(normalized.encode("ascii")).decode("utf-8"))

    assert decoded["sid"] == raw["sid"]
    assert decoded["bfp"] == raw["bfp"]
    assert decoded["ftr"] == raw["ftr"]
    assert "|r=ap-southeast-1|" in decoded["ark"]


def test_cookie_input_stops_excessive_nesting():
    value = {"cookie": {"cookie": {"cookie": {"cookie": {"cookie": "sid=one"}}}}}

    assert RefreshManager._cookie_string_from_input(value) == ""


def _jwt_with_user_id(user_id: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"user_id": user_id}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"e30.{payload}.sig"


class _ExchangeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class _ExchangeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def post(self, url, *, headers, data, **kwargs):
        self.calls.append(
            {"url": url, "headers": headers, "data": data, "kwargs": kwargs}
        )
        return self.responses.pop(0)

    def close(self):
        self.closed = True


class _GetSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, *, headers, **kwargs):
        self.calls.append({"url": url, "headers": headers, "kwargs": kwargs})
        return self.responses.pop(0)

    def close(self):
        self.closed = True


def test_adobe_fingerprint_pool_only_uses_supported_impersonations():
    from curl_cffi.requests import BrowserType

    supported = {browser.value for browser in BrowserType}
    for fingerprint in adobe_client_module._ADOBE_FINGERPRINTS:
        assert fingerprint["impersonate"] in supported
        assert f'v="{fingerprint["major"]}"' in fingerprint["sec_ch_ua"]


def test_credits_request_uses_browser_tls_fingerprint_and_proxy(monkeypatch):
    session = _GetSession(
        [
            _ExchangeResponse(
                200,
                {
                    "total": {
                        "quota": {"total": 100, "used": 25, "available": 75},
                        "availableUntil": "RESET",
                        "planCap": "premium",
                    }
                },
            )
        ]
    )
    session_kwargs = {}

    def make_session(**kwargs):
        session_kwargs.update(kwargs)
        return session

    monkeypatch.setattr(adobe_client_module, "CurlSession", make_session)
    monkeypatch.setattr(
        adobe_client_module,
        "_select_adobe_fingerprint",
        lambda: {
            "impersonate": "chrome146",
            "user_agent": "Chrome/146",
        },
    )

    result = fetch_adobe_credits_balance(
        "ACCESS-TOKEN",
        "ACCOUNT-ID",
        proxy="http://proxy.test",
    )

    assert session_kwargs == {
        "impersonate": "chrome146",
        "timeout": 60,
        "proxies": {
            "http": "http://proxy.test",
            "https": "http://proxy.test",
        },
    }
    assert session.calls[0]["headers"] == {
        "authorization": "Bearer ACCESS-TOKEN",
        "x-api-key": "SunbreakWebUI1",
        "x-account-id": "ACCOUNT-ID",
        "accept": "application/json",
        "content-type": "application/json",
        "user-agent": "Chrome/146",
    }
    assert result["available"] == 75
    assert result["plan"] == "premium"
    assert session.closed is True


def test_account_profile_uses_direct_browser_tls_session(monkeypatch):
    session = _GetSession(
        [_ExchangeResponse(200, {"email": "user@example.com", "userId": "USER"})]
    )
    session_kwargs = {}

    def make_session(**kwargs):
        session_kwargs.update(kwargs)
        return session

    monkeypatch.setattr(adobe_client_module, "CurlSession", make_session)
    monkeypatch.setattr(
        adobe_client_module,
        "_select_adobe_fingerprint",
        lambda: {
            "impersonate": "chrome146",
            "user_agent": "Chrome/146",
        },
    )

    result = fetch_adobe_account_profile("ACCESS-TOKEN")

    assert session_kwargs == {"impersonate": "chrome146", "timeout": 60}
    assert session.calls[0]["headers"] == {
        "authorization": "Bearer ACCESS-TOKEN",
        "accept": "application/json",
        "user-agent": "Chrome/146",
    }
    assert result["email"] == "user@example.com"
    assert session.closed is True


def test_cookie_exchange_uses_express_then_clio(monkeypatch):
    express_token = _jwt_with_user_id("USER-ID")
    session = _ExchangeSession(
        [
            _ExchangeResponse(200, {"access_token": express_token, "expires_in": 10}),
            _ExchangeResponse(200, {"access_token": "CLIO-TOKEN", "expires_in": 20}),
        ]
    )
    session_kwargs = {}

    def make_session(**kwargs):
        session_kwargs.update(kwargs)
        return session

    monkeypatch.setattr(adobe_client_module, "CurlSession", make_session)
    monkeypatch.setattr(
        adobe_client_module,
        "_select_adobe_fingerprint",
        lambda: {
            "impersonate": "chrome146",
            "user_agent": "Chrome/146",
            "sec_ch_ua": '"Chromium";v="146"',
            "platform": '"Windows"',
        },
    )

    result = exchange_adobe_cookie("Cookie: sid=one", proxy="http://proxy.test")

    assert result["access_token"] == "CLIO-TOKEN"
    assert result["used_clio"] is True
    assert session_kwargs["impersonate"] == "chrome146"
    assert "proxies" not in session_kwargs
    assert session.calls[0]["kwargs"]["allow_redirects"] is False
    assert session.calls[0]["data"] == {
        "client_id": "projectx_webapp",
        "guest_allowed": "true",
        "scope": "AdobeID,firefly_api,openid",
    }
    assert session.calls[0]["headers"]["origin"] == "https://new.express.adobe.com"
    assert session.calls[1]["data"]["client_id"] == "clio-playground-web"
    assert session.calls[1]["data"]["user_id"] == "USER-ID"
    assert "guest_allowed" not in session.calls[1]["data"]
    assert session.calls[1]["headers"]["origin"] == "https://firefly.adobe.com"
    assert session.closed is True


def test_cookie_exchange_falls_back_to_express_token(monkeypatch):
    express_token = _jwt_with_user_id("USER-ID")
    session = _ExchangeSession(
        [
            _ExchangeResponse(200, {"access_token": express_token, "expires_in": 10}),
            _ExchangeResponse(500, {"error": "temporary"}),
        ]
    )
    monkeypatch.setattr(adobe_client_module, "CurlSession", lambda **_kwargs: session)

    result = exchange_adobe_cookie("sid=one")

    assert result["access_token"] == express_token
    assert result["used_clio"] is False


def test_remote_submit_headers_use_fresh_arp_and_stable_pid(monkeypatch):
    token = _jwt_with_user_id("USER-ID")
    client = AdobeClient()
    monkeypatch.setattr(
        adobe_client_module,
        "_select_adobe_fingerprint",
        lambda: {
            "impersonate": "chrome146",
            "major": 146,
            "platform": '"Windows"',
            "os": "Windows NT 10.0; Win64; x64",
            "user_agent": "Mozilla/5.0 Chrome/146.0.0.0 Safari/537.36",
            "sec_ch_ua": '"Chromium";v="146", "Google Chrome";v="146"',
        },
    )

    first = client._submit_headers(token, prompt=" draw ", protocol_profile="remote_adobe")
    second = client._submit_headers(token, prompt=" draw ", protocol_profile="remote_adobe")
    first_arp = json.loads(base64.b64decode(first["x-arp-session-id"]))
    second_arp = json.loads(base64.b64decode(second["x-arp-session-id"]))

    assert first["x-api-key"] == "projectx_webapp"
    assert first["origin"] == "https://new.express.adobe.com"
    assert first["referer"] == "https://new.express.adobe.com/"
    assert first["user-agent"].endswith("Safari/537.36")
    assert first["x-nonce"] == hashlib.sha256(b"USER-ID-draw").hexdigest()
    assert list(first) == [
        "authorization",
        "x-api-key",
        "x-arp-session-id",
        "content-type",
        "accept",
        "origin",
        "referer",
        "accept-language",
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
        "sec-fetch-site",
        "sec-fetch-mode",
        "sec-fetch-dest",
        "user-agent",
        "x-nonce",
    ]
    assert first_arp["sid"] != second_arp["sid"]
    assert "|r=ap-southeast-1|" in first_arp["ark"]
    assert first_arp["ftr"].split("_")[2] == second_arp["ftr"].split("_")[2]
    assert first_arp["ftr"].endswith("_UDF43-m4_31ck__tt")


def test_remote_nonce_truncates_utf8_prompt_to_256_bytes(monkeypatch):
    token = _jwt_with_user_id("USER-ID")
    client = AdobeClient()
    monkeypatch.setattr(
        adobe_client_module,
        "_select_adobe_fingerprint",
        lambda: {
            "impersonate": "chrome146",
            "user_agent": "Chrome/146",
            "sec_ch_ua": '"Chromium";v="146"',
            "platform": '"Windows"',
        },
    )

    prompt = "图" * 200
    headers = client._submit_headers(
        token,
        prompt=prompt,
        protocol_profile="remote_adobe",
    )

    expected = hashlib.sha256(
        b"USER-ID-" + prompt.encode("utf-8")[:256]
    ).hexdigest()
    assert headers["x-nonce"] == expected


def test_remote_submit_json_matches_go_marshal_shape():
    assert _go_json_bytes(
        {"z": "图<&\u2028", "a": {"d": True, "b": 1}}
    ) == (
        '{"a":{"b":1,"d":true},"z":"图\\u003c\\u0026\\u2028"}'
    ).encode("utf-8")
