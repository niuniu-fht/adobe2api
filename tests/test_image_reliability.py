import json
import threading
import time
from io import BytesIO

import pytest
from PIL import Image
from fastapi.testclient import TestClient

from api.routes.generation import generate_with_reference_recovery
from core.adobe_client import (
    AdobeClient,
    AdobeRequestError,
    AuthError,
    ContentPolicyError,
    ImageStageTerminalError,
    RateLimitWaitExceededError,
    ReferenceImageRequiredError,
    SubmitRateLimitedError,
    UpstreamTemporaryError,
)
from core.image_queue import ImageTaskCancelled, ImageTaskCoordinator


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = headers or {"content-type": "application/json"}
        self.text = json.dumps(self._body)
        self.content = self.text.encode("utf-8")

    def json(self):
        return self._body


def _submit_success(job_id="job-1"):
    return FakeResponse(
        200,
        {
            "links": {
                "result": {
                    "href": f"https://example.test/jobs/{job_id}?sig=SECRET"
                }
            }
        },
    )


@pytest.mark.parametrize("unsafe_source", ["primary", "fallback"])
def test_submit_unsafe_stops_before_other_recovery(monkeypatch, unsafe_source):
    client = AdobeClient()
    calls = {"primary": 0, "fallback": 0}
    unsafe = FakeResponse(
        400,
        {"error": {"details": [{"code": "IMAGE_UNSAFE"}]}},
    )
    generic = FakeResponse(451, {"error_code": "temporary"})

    def primary(*args, **kwargs):
        calls["primary"] += 1
        return unsafe if unsafe_source == "primary" else generic

    def fallback(*args, **kwargs):
        calls["fallback"] += 1
        return unsafe

    monkeypatch.setattr(client, "_post_json", primary)
    monkeypatch.setattr(client, "_post_json_requests_once", fallback)

    with pytest.raises(ContentPolicyError, match="图片不安全"):
        client._post_image_json("https://example.test/submit", {}, {"seed": 7})

    assert calls["primary"] == 1
    assert calls["fallback"] == (0 if unsafe_source == "primary" else 1)


@pytest.mark.parametrize(
    ("status_code", "unsafe_body", "upstream_code"),
    [
        (200, {"result": {"error_code": "image_unsafe"}}, "image_unsafe"),
        (
            451,
            {
                "error_code": "prompt_unsafe",
                "message": (
                    "The provided prompt is considered unsafe and it cannot be used "
                    "to generate content."
                ),
            },
            "prompt_unsafe",
        ),
    ],
)
def test_poll_unsafe_stops_before_download(
    monkeypatch, status_code, unsafe_body, upstream_code
):
    client = AdobeClient()
    download_calls = []
    monkeypatch.setattr(
        client, "_build_payload_candidates", lambda **kwargs: [{"seed": 42}]
    )
    monkeypatch.setattr(client, "_post_image_json", lambda *args, **kwargs: _submit_success())
    monkeypatch.setattr(
        client,
        "_get",
        lambda *args, **kwargs: FakeResponse(status_code, unsafe_body),
    )
    monkeypatch.setattr(
        client,
        "_download_image_result",
        lambda **kwargs: download_calls.append(kwargs),
    )

    with pytest.raises(ContentPolicyError, match="图片不安全") as error_info:
        client._generate_once(token="TOKEN", prompt="draw", seed=42)

    assert error_info.value.upstream_code == upstream_code
    assert download_calls == []


def test_submit_auth_error_skips_candidate_and_transport_retries(monkeypatch):
    client = AdobeClient()
    submissions = []
    monkeypatch.setattr(
        client,
        "_build_payload_candidates",
        lambda **kwargs: [{"candidate": "first"}, {"candidate": "second"}],
    )

    def submit(*args, **kwargs):
        submissions.append(kwargs["payload"])
        return FakeResponse(401, {"message": "expired"})

    monkeypatch.setattr(client, "_post_image_json", submit)
    monkeypatch.setattr(
        client,
        "_wait_with_cancel",
        lambda *args, **kwargs: pytest.fail("auth error must not wait"),
    )

    with pytest.raises(AuthError, match="Token invalid or expired"):
        client._generate_once(token="TOKEN-A", prompt="draw", seed=42)

    assert submissions == [{"candidate": "first"}]


def test_primary_submit_429_skips_requests_fallback(monkeypatch):
    client = AdobeClient()
    response = FakeResponse(429, {"error_code": "rate_limited"})
    monkeypatch.setattr(client, "_post_json", lambda *args, **kwargs: response)
    monkeypatch.setattr(
        client,
        "_post_json_requests_once",
        lambda *args, **kwargs: pytest.fail("429 must switch accounts immediately"),
    )

    assert client._post_image_json("https://example.test", {}, {}) is response


def test_candidate_unsafe_stops_before_later_candidates(monkeypatch):
    client = AdobeClient()
    submitted = []

    def submit(*args, **kwargs):
        submitted.append(dict(kwargs["payload"]))
        if len(submitted) == 1:
            return FakeResponse(400, {"error_code": "bad_request"})
        return FakeResponse(400, {"nested": {"code": "image_unsafe"}})

    monkeypatch.setattr(
        client,
        "_build_payload_candidates",
        lambda **kwargs: [
            {"candidate": "general"},
            {"candidate": "subject"},
            {"candidate": "unused"},
        ],
    )
    monkeypatch.setattr(client, "_post_image_json", submit)

    with pytest.raises(ContentPolicyError):
        client._generate_once(token="TOKEN", prompt="edit", seed=123)

    assert submitted == [
        {"candidate": "general"},
        {"candidate": "subject"},
    ]


def _patch_images_endpoint_token(monkeypatch, app_module):
    monkeypatch.setattr(
        app_module.token_manager,
        "list_active_account_tokens",
        lambda: [{"token": "TOKEN", "account_id": "account-1"}],
    )
    monkeypatch.setattr(
        app_module.token_manager, "get_available", lambda strategy=None: "TOKEN"
    )
    monkeypatch.setattr(
        app_module.token_manager,
        "get_meta_by_value",
        lambda token: {
            "token_id": "token-1",
            "token_account_id": "account-1",
            "token_account_name": "Test Account",
        },
    )


def test_images_endpoint_returns_exact_unsafe_contract(monkeypatch):
    import app as app_module

    _patch_images_endpoint_token(monkeypatch, app_module)
    monkeypatch.setattr(
        app_module.client,
        "generate",
        lambda **kwargs: (_ for _ in ()).throw(
            ContentPolicyError("unsafe", upstream_code="image_unsafe")
        ),
    )
    api_key = str(app_module.config_manager.get("api_key", "") or "")
    headers = {"X-API-Key": api_key} if api_key else {}

    response = TestClient(app_module.app).post(
        "/v1/images/generations",
        headers=headers,
        json={"model": "gpt-image-2", "prompt": "draw", "n": 3},
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "message": "内容审核未通过，请修改提示词后重试",
            "type": "invalid_request_error",
        }
    }


@pytest.mark.parametrize(
    "model_id",
    [
        "gpt-image-gemini-3.1-flash-image",
        "gpt-image-gemini-3-pro-image",
    ],
)
def test_gemini_images_edits_returns_gpt_image_unsafe_contract(
    monkeypatch, model_id
):
    import app as app_module

    _patch_images_endpoint_token(monkeypatch, app_module)
    monkeypatch.setattr(
        app_module.client,
        "upload_image",
        lambda *_args, **_kwargs: "reference-image-id",
    )
    monkeypatch.setattr(
        app_module.client,
        "generate",
        lambda **kwargs: (_ for _ in ()).throw(
            ContentPolicyError("unsafe", upstream_code="prompt_unsafe")
        ),
    )
    api_key = str(app_module.config_manager.get("api_key", "") or "")
    headers = {"X-API-Key": api_key} if api_key else {}

    response = TestClient(app_module.app).post(
        "/v1/images/edits",
        headers=headers,
        data={"model": model_id, "prompt": "edit"},
        files={"image": ("reference.png", _png_bytes(), "image/png")},
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "message": "内容审核未通过，请修改提示词后重试",
            "type": "invalid_request_error",
        }
    }


def test_images_endpoint_upscales_small_size_before_adobe_submit(monkeypatch):
    import app as app_module

    _patch_images_endpoint_token(monkeypatch, app_module)
    submitted_sizes = []

    def generate(**kwargs):
        submitted_sizes.append(kwargs["requested_size"])
        return _png_bytes(), {}

    monkeypatch.setattr(app_module.client, "generate", generate)
    api_key = str(app_module.config_manager.get("api_key", "") or "")
    headers = {"X-API-Key": api_key} if api_key else {}
    response = TestClient(app_module.app).post(
        "/v1/images/generations",
        headers=headers,
        json={
            "model": "gpt-image-2",
            "prompt": "draw",
            "size": "256x256",
            "response_format": "b64_json",
        },
    )

    assert response.status_code == 200
    assert submitted_sizes == [{"width": 816, "height": 816}]


def test_images_endpoint_auth_switches_to_a_different_account(monkeypatch):
    import app as app_module

    records = [
        {"token": "TOKEN-A1", "account_id": "account-a"},
        {"token": "TOKEN-A2", "account_id": "account-a"},
        {"token": "TOKEN-B", "account_id": "account-b"},
    ]
    active = {item["token"] for item in records}
    attempts = []

    monkeypatch.setattr(
        app_module.token_manager,
        "list_active_account_tokens",
        lambda: [item for item in records if item["token"] in active],
    )
    monkeypatch.setattr(
        app_module.token_manager,
        "get_available",
        lambda strategy=None: next(
            (item["token"] for item in records if item["token"] in active),
            None,
        ),
    )
    monkeypatch.setattr(
        app_module.token_manager,
        "get_meta_by_value",
        lambda token: {
            "token_id": token.lower(),
            "token_account_id": next(
                item["account_id"] for item in records if item["token"] == token
            ),
            "token_account_name": token,
        },
    )
    monkeypatch.setattr(
        app_module.token_manager,
        "report_invalid",
        lambda token: active.discard(token),
    )
    monkeypatch.setattr(app_module.token_manager, "report_success", lambda _token: None)

    monkeypatch.setattr(
        app_module.image_task_coordinator,
        "assign_token",
        lambda candidates, exclude=None: next(
            (token for token in candidates if token not in (exclude or set())),
            None,
        ),
    )

    def generate(**kwargs):
        token = kwargs["token"]
        attempts.append(token)
        if token == "TOKEN-A1":
            raise AuthError("Token invalid or expired")
        if token == "TOKEN-A2":
            pytest.fail("must switch accounts instead of using another token")
        return _png_bytes(), {}

    monkeypatch.setattr(app_module.client, "generate", generate)
    api_key = str(app_module.config_manager.get("api_key", "") or "")
    headers = {"X-API-Key": api_key} if api_key else {}
    response = TestClient(app_module.app).post(
        "/v1/images/generations",
        headers=headers,
        json={
            "model": "gpt-image-2",
            "prompt": "draw",
            "response_format": "b64_json",
        },
    )

    assert response.status_code == 200
    assert attempts == ["TOKEN-A1", "TOKEN-B"]
    assert response.json()["data"][0]["b64_json"]


@pytest.mark.parametrize("failure_kind", ["upload_auth", "submit_rate_limit"])
def test_images_edits_switch_reuploads_reference_with_next_account(
    monkeypatch, failure_kind
):
    import app as app_module

    records = [
        {"token": "TOKEN-A1", "account_id": "account-a", "profile_id": "profile-a"},
        {"token": "TOKEN-A2", "account_id": "account-a", "profile_id": "profile-a"},
        {"token": "TOKEN-B", "account_id": "account-b", "profile_id": "profile-b"},
    ]
    active = {item["token"] for item in records}
    upload_attempts = []
    generation_attempts = []
    selection_attempts = []

    monkeypatch.setattr(
        app_module.token_manager,
        "list_active_account_tokens",
        lambda: [item for item in records if item["token"] in active],
    )
    monkeypatch.setattr(
        app_module.token_manager,
        "get_available",
        lambda strategy=None: next(
            (item["token"] for item in records if item["token"] in active),
            None,
        ),
    )
    monkeypatch.setattr(
        app_module.token_manager,
        "get_meta_by_value",
        lambda token: {
            "token_id": token.lower(),
            "token_account_id": next(
                item["account_id"] for item in records if item["token"] == token
            ),
            "refresh_profile_id": next(
                item["profile_id"] for item in records if item["token"] == token
            ),
            "token_account_name": token,
        },
    )
    monkeypatch.setattr(
        app_module.token_manager,
        "report_invalid",
        lambda token: active.discard(token),
    )
    monkeypatch.setattr(app_module.token_manager, "report_success", lambda _token: None)

    def assign_edit_token(candidates, exclude=None):
        selected = next(
            (token for token in candidates if token not in (exclude or set())),
            None,
        )
        selection_attempts.append(selected)
        return selected

    monkeypatch.setattr(
        app_module.image_task_coordinator,
        "assign_token",
        assign_edit_token,
    )

    def upload_image(token, *_args, **_kwargs):
        upload_attempts.append(token)
        if token == "TOKEN-A1" and failure_kind == "upload_auth":
            raise AuthError("Token invalid or expired")
        if token == "TOKEN-A2":
            pytest.fail("must re-upload with a different account")
        return "reference-a" if token == "TOKEN-A1" else "reference-b"

    def generate(**kwargs):
        generation_attempts.append((kwargs["token"], kwargs["source_image_ids"]))
        if kwargs["token"] == "TOKEN-A1" and failure_kind == "submit_rate_limit":
            raise SubmitRateLimitedError()
        return _png_bytes(), {}

    monkeypatch.setattr(app_module.client, "upload_image", upload_image)
    monkeypatch.setattr(app_module.client, "generate", generate)
    api_key = str(app_module.config_manager.get("api_key", "") or "")
    headers = {"X-API-Key": api_key} if api_key else {}
    response = TestClient(app_module.app).post(
        "/v1/images/edits",
        headers=headers,
        data={
            "model": "gpt-image-2",
            "prompt": "edit",
            "response_format": "b64_json",
        },
        files={"image": ("reference.png", _png_bytes(), "image/png")},
    )

    assert response.status_code == 200
    assert selection_attempts == ["TOKEN-A1", "TOKEN-B"]
    assert upload_attempts == ["TOKEN-A1", "TOKEN-B"]
    if failure_kind == "upload_auth":
        assert generation_attempts == [("TOKEN-B", ["reference-b"])]
    else:
        assert generation_attempts == [
            ("TOKEN-A1", ["reference-a"]),
            ("TOKEN-B", ["reference-b"]),
        ]
    assert response.json()["data"][0]["b64_json"]


def test_images_endpoint_returns_fixed_rate_limit_contract(monkeypatch):
    import app as app_module

    _patch_images_endpoint_token(monkeypatch, app_module)
    monkeypatch.setattr(
        app_module.client,
        "generate",
        lambda **kwargs: (_ for _ in ()).throw(RateLimitWaitExceededError()),
    )
    api_key = str(app_module.config_manager.get("api_key", "") or "")
    headers = {"X-API-Key": api_key} if api_key else {}

    response = TestClient(app_module.app).post(
        "/v1/images/generations",
        headers=headers,
        json={"model": "gpt-image-2", "prompt": "draw"},
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "message": "Too many requests. Please try again later.",
            "type": "invalid_request_error",
        }
    }


def test_admin_image_queue_endpoint_exposes_read_only_snapshot(monkeypatch):
    import app as app_module

    monkeypatch.setenv("ADOBE2API_OPS_KEY", "QUEUE_TEST_KEY")
    response = TestClient(app_module.app).get(
        "/api/v1/image-queue?limit=200",
        headers={"X-Adobe2API-Ops-Key": "QUEUE_TEST_KEY"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"summary", "items"}
    assert set(payload["summary"]) == {
        "requests",
        "outputs",
        "in_progress",
        "queued",
        "waiting_poll",
        "rate_limited",
        "download_retry",
    }
    assert isinstance(payload["items"], list)


def test_rate_limited_submit_switches_account_without_same_account_retry(monkeypatch):
    client = AdobeClient()
    payload_ids = []

    def submit(*args, **kwargs):
        payload_ids.append(id(kwargs["payload"]))
        return FakeResponse(400, {"error_code": "rate_limited"})

    monkeypatch.setattr(
        client, "_build_payload_candidates", lambda **kwargs: [{"seed": 42}]
    )
    monkeypatch.setattr(client, "_post_image_json", submit)
    wait_calls = []
    monkeypatch.setattr(
        client,
        "_wait_with_cancel",
        lambda delay, **kwargs: wait_calls.append(delay),
    )

    with pytest.raises(SubmitRateLimitedError):
        client._generate_once(token="TOKEN", prompt="draw", seed=42)

    assert len(payload_ids) == 1
    assert wait_calls == []


def test_submit_rate_limit_reports_switch_delay_without_local_wait(monkeypatch):
    client = AdobeClient()
    progress = []

    def submit(*args, **kwargs):
        return FakeResponse(
            429,
            {"error_code": "rate_limited"},
            headers={"retry-after": "17"},
        )

    monkeypatch.setattr(
        client, "_build_payload_candidates", lambda **kwargs: [{"seed": 99}]
    )
    monkeypatch.setattr(client, "_post_image_json", submit)
    wait_calls = []
    monkeypatch.setattr(
        client,
        "_wait_with_cancel",
        lambda delay, **kwargs: wait_calls.append(delay),
    )

    with pytest.raises(SubmitRateLimitedError):
        client._generate_once(
            token="TOKEN",
            prompt="draw",
            seed=99,
            progress_cb=progress.append,
        )

    retry_updates = [item for item in progress if "retry_after" in item]
    assert wait_calls == []
    assert retry_updates[0]["task_status"] == "SUBMITTING"
    assert retry_updates[0]["retry_after"] == 5
    assert retry_updates[0]["rate_limit_wait_seconds"] == 5.0




def test_invalid_image_size_aspect_retries_once_with_auto_payload(monkeypatch):
    client = AdobeClient()
    submissions = []

    initial_payload = {
        "modelId": "gpt-image",
        "modelVersion": "2",
        "prompt": "draw",
        "size": {"width": 208, "height": 3840},
        "outputResolution": "4K",
        "modelSpecificPayload": {"size": "208x3840"},
    }

    def submit(*args, **kwargs):
        submissions.append(dict(kwargs["payload"]))
        if len(submissions) == 1:
            return FakeResponse(
                400,
                {
                    "error_code": "bad_request",
                    "message": "Invalid image size 208x3840: aspect ratio must not exceed 3:1 (got 18.46:1)",
                },
            )
        return _submit_success("job-auto")

    monkeypatch.setattr(client, "_build_payload_candidates", lambda **kwargs: [initial_payload])
    monkeypatch.setattr(client, "_post_image_json", submit)
    monkeypatch.setattr(
        client,
        "_get",
        lambda *args, **kwargs: FakeResponse(
            200,
            {"outputs": [{"image": {"presignedUrl": "https://example.test/image.png"}}]},
        ),
    )
    monkeypatch.setattr(client, "_download_image_result", lambda **kwargs: _png_bytes())

    image_bytes, _meta = client._generate_once(token="TOKEN", prompt="draw", seed=1)

    assert image_bytes == _png_bytes()
    assert len(submissions) == 2
    assert submissions[0]["size"] == {"width": 208, "height": 3840}
    assert "size" not in submissions[1]
    assert "outputResolution" not in submissions[1]
    assert submissions[1]["modelSpecificPayload"] == {"size": "auto"}

def test_poll_rate_limit_switches_account_without_same_account_retry(monkeypatch):
    client = AdobeClient()
    waits = []
    poll_calls = []

    monkeypatch.setattr(client, "_build_payload_candidates", lambda **kwargs: [{"seed": 1}])
    monkeypatch.setattr(client, "_post_image_json", lambda *args, **kwargs: _submit_success("job-rl"))

    def poll(*args, **kwargs):
        poll_calls.append(args[0] if args else kwargs.get("url"))
        return FakeResponse(429, {"error_code": "rate_limited"})

    monkeypatch.setattr(client, "_get", poll)
    monkeypatch.setattr(client, "_wait_with_cancel", lambda delay, **kwargs: waits.append(delay))

    with pytest.raises(SubmitRateLimitedError):
        client._generate_once(token="TOKEN", prompt="draw", seed=1)

    assert len(poll_calls) == 1
    assert waits == []


def test_upload_rate_limit_switches_account_without_same_account_retry(monkeypatch):
    client = AdobeClient()
    waits = []
    upload_calls = []

    def upload(*args, **kwargs):
        upload_calls.append(kwargs.get("payload"))
        return FakeResponse(429, {"error_code": "rate_limited"})

    monkeypatch.setattr(client, "_post_bytes", upload)
    monkeypatch.setattr(client, "_wait_with_cancel", lambda delay, **kwargs: waits.append(delay))

    with pytest.raises(SubmitRateLimitedError):
        client.upload_image("TOKEN", _png_bytes(), "image/png")

    assert len(upload_calls) == 1
    assert waits == []

def test_image_submit_retry_delay_uses_capped_schedule(monkeypatch):
    client = AdobeClient()
    monkeypatch.setattr("core.adobe_client.random.uniform", lambda _low, _high: 1.0)

    expected = [2.0, 4.0, 4.0, 4.0, 4.0, 4.0]
    assert [
        client._submit_retry_delay(attempt, rate_limited=False)
        for attempt in range(1, 7)
    ] == expected
    assert [
        client._submit_retry_delay(attempt, rate_limited=True)
        for attempt in range(1, 7)
    ] == expected
    assert client._submit_retry_delay(1, rate_limited=True, retry_after=9.0) == 9.0


def test_image_submit_retry_budgets_default_to_60_seconds():
    client = AdobeClient()

    assert client._image_submit_network_retry_seconds() == 60
    assert client._image_submit_rate_limit_wait_seconds() == 60
    assert client._image_network_retry_seconds() == 180
    assert client._image_rate_limit_wait_seconds() == 180


def _png_bytes():
    output = BytesIO()
    Image.new("RGB", (2, 2), (20, 120, 220)).save(output, format="PNG")
    return output.getvalue()


def test_download_retries_four_times_then_atomically_succeeds(monkeypatch, tmp_path):
    client = AdobeClient()
    attempts = []
    target = tmp_path / "result.png"

    def download(url, headers, out_path, timeout):
        attempts.append(url)
        if len(attempts) < 5:
            raise UpstreamTemporaryError("connection reset", error_type="connection")
        out_path.write_bytes(_png_bytes())
        return out_path.stat().st_size

    monkeypatch.setattr(client, "_image_download_attempts", lambda: 5)
    monkeypatch.setattr(client, "_download_to_file", download)
    monkeypatch.setattr(client, "_wait_with_cancel", lambda *args, **kwargs: None)

    result = client._download_image_result(
        image_url="https://example.test/image.png",
        poll_url="https://example.test/jobs/1",
        token="TOKEN",
        out_path=target,
        progress_cb=None,
        trace=None,
        trace_parent_id=None,
        upstream_job_id="job-1",
        cancel_check=None,
    )

    assert result is None
    assert len(attempts) == 5
    assert target.read_bytes() == _png_bytes()
    assert not (tmp_path / "result.png.part").exists()


def test_expired_download_url_refreshes_same_job(monkeypatch, tmp_path):
    client = AdobeClient()
    urls = []
    refresh_calls = []
    target = tmp_path / "result.png"

    def download(url, headers, out_path, timeout):
        urls.append(url)
        if len(urls) == 1:
            raise UpstreamTemporaryError(
                "presigned URL expired", status_code=403, error_type="download_http"
            )
        out_path.write_bytes(_png_bytes())
        return out_path.stat().st_size

    monkeypatch.setattr(client, "_image_download_attempts", lambda: 5)
    monkeypatch.setattr(client, "_download_to_file", download)
    monkeypatch.setattr(client, "_wait_with_cancel", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        client,
        "_refresh_image_result_url",
        lambda poll_url, token, **kwargs: refresh_calls.append((poll_url, token))
        or "https://example.test/refreshed.png",
    )

    client._download_image_result(
        image_url="https://example.test/expired.png",
        poll_url="https://example.test/jobs/1",
        token="TOKEN",
        out_path=target,
        progress_cb=None,
        trace=None,
        trace_parent_id=None,
        upstream_job_id="job-1",
        cancel_check=None,
    )

    assert urls == [
        "https://example.test/expired.png",
        "https://example.test/refreshed.png",
    ]
    assert refresh_calls == [("https://example.test/jobs/1", "TOKEN")]


def test_download_exhaustion_is_terminal_not_token_retryable(monkeypatch, tmp_path):
    client = AdobeClient()
    monkeypatch.setattr(client, "_image_download_attempts", lambda: 2)
    monkeypatch.setattr(
        client,
        "_download_to_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            UpstreamTemporaryError("offline", error_type="connection")
        ),
    )
    monkeypatch.setattr(client, "_wait_with_cancel", lambda *args, **kwargs: None)

    with pytest.raises(ImageStageTerminalError) as error_info:
        client._download_image_result(
            image_url="https://example.test/image.png",
            poll_url="https://example.test/jobs/1",
            token="TOKEN",
            out_path=tmp_path / "result.png",
            progress_cb=None,
            trace=None,
            trace_parent_id=None,
            upstream_job_id="job-1",
            cancel_check=None,
        )

    assert error_info.value.status_code == 502
    assert not isinstance(error_info.value, UpstreamTemporaryError)


def test_reference_recovery_retries_ids_then_reuploads_once():
    attempts = []
    sleeps = []
    upload_calls = []

    def generate(ids):
        attempts.append(list(ids))
        if ids == ["old-1", "old-2"]:
            raise ReferenceImageRequiredError()
        return [{"url": "ok"}]

    result, final_ids = generate_with_reference_recovery(
        source_image_ids=["old-1", "old-2"],
        expected_image_count=2,
        generate_with_ids=generate,
        reupload_all=lambda: upload_calls.append(True) or ["new-1", "new-2"],
        cancel_check=lambda: None,
        sleep=sleeps.append,
    )

    assert result == [{"url": "ok"}]
    assert final_ids == ["new-1", "new-2"]
    assert attempts == [["old-1", "old-2"]] * 4 + [["new-1", "new-2"]]
    assert sleeps == [0.5, 1.0, 2.0]
    assert upload_calls == [True]


def test_reference_recovery_rejects_partial_reupload_before_generate():
    attempts = []

    def generate(ids):
        attempts.append(list(ids))
        raise ReferenceImageRequiredError()

    with pytest.raises(AdobeRequestError, match="re-upload incomplete"):
        generate_with_reference_recovery(
            source_image_ids=["old-1", "old-2"],
            expected_image_count=2,
            generate_with_ids=generate,
            reupload_all=lambda: ["new-1", ""],
            cancel_check=lambda: None,
            sleep=lambda _delay: None,
        )

    assert attempts == [["old-1", "old-2"]] * 4


def test_coordinator_enforces_token_limit_and_preserves_output_order():
    coordinator = ImageTaskCoordinator(io_workers=8)
    request_id = coordinator.register_request(
        log_id="log-1",
        path="/v1/images/generations",
        model="gpt-image-2",
        prompt_preview="draw",
        output_count=6,
    )
    lock = threading.Lock()
    active = 0
    maximum_active = 0

    def worker(index):
        nonlocal active, maximum_active
        with coordinator.token_slot(
            "TOKEN", limit=3, request_id=request_id, output_index=index
        ):
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.02)
            with lock:
                active -= 1
        coordinator.update_output(request_id, index, state="COMPLETED")
        return index

    results = coordinator.run_indexed(
        request_id=request_id,
        indices=range(6),
        worker=worker,
        max_parallel=6,
    )

    assert maximum_active == 3
    assert results == [(index, index) for index in range(6)]


def test_coordinator_timed_queue_wakes_by_deadline():
    coordinator = ImageTaskCoordinator(io_workers=4)
    request_id = coordinator.register_request(
        log_id="log-timer",
        path="/v1/images/generations",
        model="gpt-image-2",
        prompt_preview="draw",
        output_count=2,
    )
    wake_order = []

    def wait_then_record(label, delay):
        coordinator.wait(request_id, delay)
        wake_order.append(label)

    slow = threading.Thread(target=wait_then_record, args=("slow", 0.08))
    fast = threading.Thread(target=wait_then_record, args=("fast", 0.01))
    slow.start()
    fast.start()
    slow.join(timeout=1)
    fast.join(timeout=1)

    assert wake_order == ["fast", "slow"]


def test_coordinator_uses_one_round_robin_for_new_requests_and_429_retries():
    coordinator = ImageTaskCoordinator(io_workers=4)
    candidates = ["A", "B", "C"]

    first_request = coordinator.assign_token(candidates)
    submit_429_retry = coordinator.assign_token(candidates, exclude={"A"})
    next_request = coordinator.assign_token(candidates)
    following_request = coordinator.assign_token(candidates)

    assert [first_request, submit_429_retry, next_request, following_request] == [
        "A",
        "B",
        "C",
        "A",
    ]


def test_coordinator_round_robin_is_even_across_concurrent_requests():
    coordinator = ImageTaskCoordinator(io_workers=4)
    candidates = ["A", "B", "C", "D"]
    barrier = threading.Barrier(12)
    selected = []
    selected_lock = threading.Lock()

    def choose_token():
        barrier.wait()
        token = coordinator.assign_token(candidates)
        with selected_lock:
            selected.append(token)

    threads = [threading.Thread(target=choose_token) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)

    assert len(selected) == 12
    assert {token: selected.count(token) for token in candidates} == {
        "A": 3,
        "B": 3,
        "C": 3,
        "D": 3,
    }


def test_coordinator_prioritizes_unsafe():
    coordinator = ImageTaskCoordinator(io_workers=4)

    request_id = coordinator.register_request(
        log_id="log-unsafe",
        path="/v1/images/generations",
        model="gpt-image-2",
        prompt_preview="draw",
        output_count=2,
    )
    barrier = threading.Barrier(2)

    def worker(index):
        barrier.wait()
        if index == 0:
            coordinator.cancel_request(request_id, "图片不安全")
            raise ContentPolicyError("unsafe", upstream_code="image_unsafe")
        while not coordinator.is_cancelled(request_id):
            time.sleep(0.001)
        raise ImageTaskCancelled("cancelled")

    with pytest.raises(ContentPolicyError):
        coordinator.run_indexed(
            request_id=request_id,
            indices=[0, 1],
            worker=worker,
            max_parallel=2,
        )

    snapshot = coordinator.snapshot()
    assert snapshot["items"][0]["state"] == "FAILED"
    assert all(
        output["state"] == "FAILED"
        for output in snapshot["items"][0]["outputs"]
    )
