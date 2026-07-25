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


def test_poll_unsafe_stops_before_download(monkeypatch):
    client = AdobeClient()
    download_calls = []
    monkeypatch.setattr(
        client, "_build_payload_candidates", lambda **kwargs: [{"seed": 42}]
    )
    monkeypatch.setattr(client, "_post_image_json", lambda *args, **kwargs: _submit_success())
    monkeypatch.setattr(
        client,
        "_get",
        lambda *args, **kwargs: FakeResponse(
            200, {"result": {"error_code": "image_unsafe"}}
        ),
    )
    monkeypatch.setattr(
        client,
        "_download_image_result",
        lambda **kwargs: download_calls.append(kwargs),
    )

    with pytest.raises(ContentPolicyError, match="图片不安全"):
        client._generate_once(token="TOKEN", prompt="draw", seed=42)

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
            "message": "图片不安全",
            "type": "invalid_request_error",
        }
    }


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


def test_images_edits_auth_switch_reuploads_reference_with_next_account(monkeypatch):
    import app as app_module

    records = [
        {"token": "TOKEN-A1", "account_id": "account-a", "profile_id": "profile-a"},
        {"token": "TOKEN-A2", "account_id": "account-a", "profile_id": "profile-a"},
        {"token": "TOKEN-B", "account_id": "account-b", "profile_id": "profile-b"},
    ]
    active = {item["token"] for item in records}
    upload_attempts = []
    generation_attempts = []

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

    def upload_image(token, *_args, **_kwargs):
        upload_attempts.append(token)
        if token == "TOKEN-A1":
            raise AuthError("Token invalid or expired")
        if token == "TOKEN-A2":
            pytest.fail("must re-upload with a different account")
        return "reference-b"

    def generate(**kwargs):
        generation_attempts.append((kwargs["token"], kwargs["source_image_ids"]))
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
    assert upload_attempts == ["TOKEN-A1", "TOKEN-B"]
    assert generation_attempts == [("TOKEN-B", ["reference-b"])]
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


def test_rate_limited_json_reuses_frozen_submit_payload(monkeypatch):
    client = AdobeClient()
    payload_ids = []
    responses = iter(
        [
            FakeResponse(400, {"error_code": "rate_limited"}),
            _submit_success(),
        ]
    )

    def submit(*args, **kwargs):
        payload_ids.append(id(kwargs["payload"]))
        return next(responses)

    monkeypatch.setattr(
        client, "_build_payload_candidates", lambda **kwargs: [{"seed": 42}]
    )
    monkeypatch.setattr(client, "_post_image_json", submit)
    monkeypatch.setattr(client, "_wait_with_cancel", lambda *args, **kwargs: None)
    monkeypatch.setattr(client, "_submit_retry_delay", lambda *args, **kwargs: 2.0)
    monkeypatch.setattr(
        client,
        "_get",
        lambda *args, **kwargs: FakeResponse(
            200, {"nested": {"code": "IMAGE_UNSAFE"}}
        ),
    )

    with pytest.raises(ContentPolicyError):
        client._generate_once(token="TOKEN", prompt="draw", seed=42)

    assert len(payload_ids) == 2
    assert len(set(payload_ids)) == 1


def test_rate_limit_budget_returns_fixed_terminal_error(monkeypatch):
    client = AdobeClient()
    clock = [1000.0]
    payload_ids = []

    def submit(*args, **kwargs):
        payload_ids.append(id(kwargs["payload"]))
        return FakeResponse(429, {"error_code": "rate_limited"})

    def wait(delay, **kwargs):
        clock[0] += float(delay)

    monkeypatch.setattr(
        client, "_build_payload_candidates", lambda **kwargs: [{"seed": 99}]
    )
    monkeypatch.setattr(client, "_post_image_json", submit)
    monkeypatch.setattr(client, "_image_submit_rate_limit_wait_seconds", lambda: 180)
    monkeypatch.setattr(client, "_submit_retry_delay", lambda *args, **kwargs: 30.0)
    monkeypatch.setattr(client, "_wait_with_cancel", wait)
    monkeypatch.setattr("core.adobe_client.time.time", lambda: clock[0])

    with pytest.raises(
        RateLimitWaitExceededError,
        match="Too many requests. Please try again later.",
    ):
        client._generate_once(token="TOKEN", prompt="draw", seed=99)

    assert clock[0] == 1180.0
    assert len(payload_ids) == 7
    assert len(set(payload_ids)) == 1


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


def test_coordinator_uses_least_assigned_tokens_and_prioritizes_unsafe():
    coordinator = ImageTaskCoordinator(io_workers=4)
    first = coordinator.assign_token(["A", "B"])
    second = coordinator.assign_token(["A", "B"])
    assert {first, second} == {"A", "B"}
    coordinator.release_token_assignment(first or "")
    coordinator.release_token_assignment(second or "")

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
