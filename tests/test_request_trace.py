import gzip
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from core.adobe_client import AdobeClient, AdobeRequestError
from core.request_trace import (
    REDACTED,
    RequestTrace,
    RequestTraceStore,
    binary_summary,
    exception_snapshot,
    sanitize_headers,
    sanitize_trace_value,
    sanitize_url,
)
from core.stores import ErrorDetailStore, LiveRequestStore, RequestLogStore


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {"content-type": "application/json"}
        self.text = json.dumps(body) if body is not None else ""
        self.content = self.text.encode("utf-8")

    def json(self):
        return self._body


def test_trace_sanitizer_redacts_credentials_and_summarizes_binary():
    payload = sanitize_trace_value(
        {
            "Authorization": "Bearer SECRET",
            "cookie": "session=SECRET",
            "token_id": "safe-metadata-id",
            "nested": {
                "access_token": "SECRET",
                "b64_json": "aGVsbG8=",
            },
            "image": b"image-bytes",
        }
    )

    assert payload["Authorization"] == REDACTED
    assert payload["cookie"] == REDACTED
    assert payload["token_id"] == "safe-metadata-id"
    assert payload["nested"]["access_token"] == REDACTED
    assert payload["nested"]["b64_json"]["kind"] == "base64"
    assert payload["nested"]["b64_json"]["omitted"] is True
    assert payload["image"] == binary_summary(b"image-bytes")

    headers = sanitize_headers({"x-api-key": "SECRET", "accept": "*/*"})
    assert headers == {"x-api-key": REDACTED, "accept": "*/*"}

    safe_url = sanitize_url("https://example.test/result?a=SECRET&empty=")
    assert "SECRET" not in safe_url
    assert "a=%5BREDACTED%5D" in safe_url


def test_exception_snapshot_keeps_cause_chain_and_tracebacks():
    try:
        try:
            raise ValueError("inner failure")
        except ValueError as inner:
            raise RuntimeError("outer failure") from inner
    except RuntimeError as error:
        snapshot = exception_snapshot(error)

    assert snapshot["class"] == "RuntimeError"
    assert [item["relation"] for item in snapshot["chain"]] == ["raised", "cause"]
    assert snapshot["chain"][0]["class"] == "RuntimeError"
    assert snapshot["chain"][1]["class"] == "ValueError"
    assert "raise RuntimeError" in snapshot["chain"][0]["traceback"]


def test_trace_assigns_unique_sequences_across_threads():
    trace = RequestTrace(log_id="root", method="POST", path="/v1/images/generations")

    def add_stage(index):
        trace.add_stage(
            layer="service",
            kind="worker",
            name=f"worker-{index}",
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(add_stage, range(100)))

    payload = trace.finalize(outcome="failed", final_error="failure")
    sequences = [stage["seq"] for stage in payload["stages"]]
    ids = [stage["id"] for stage in payload["stages"]]
    assert sequences == list(range(1, 101))
    assert len(ids) == len(set(ids))


def test_consecutive_poll_responses_are_aggregated_by_status():
    trace = RequestTrace(log_id="root", method="POST", path="/v1/images/generations")
    request = {"method": "GET", "url": "https://example.test/jobs/1?sig=SECRET"}

    for progress in (10, 20, 30):
        trace.record_poll(
            parent_id="output-1",
            status_key="200|IN_PROGRESS|IN_PROGRESS",
            request=request,
            response={"status_code": 200, "body": {"status": "IN_PROGRESS", "progress": progress}},
            duration_ms=5,
        )
    trace.record_poll(
        parent_id="output-1",
        status_key="200|SUCCEEDED|SUCCEEDED",
        request=request,
        response={"status_code": 200, "body": {"status": "SUCCEEDED"}},
        duration_ms=7,
    )

    payload = trace.finalize(outcome="failed")
    polls = [stage for stage in payload["stages"] if stage["kind"] == "poll"]
    assert len(polls) == 2
    assert polls[0]["aggregate"]["count"] == 3
    assert polls[0]["aggregate"]["first_response"]["body"]["progress"] == 10
    assert polls[0]["aggregate"]["last_response"]["body"]["progress"] == 30
    assert polls[1]["aggregate"]["count"] == 1


def test_trace_store_round_trip_and_corrupt_file_fallback(tmp_path):
    store = RequestTraceStore(tmp_path / "traces", max_items=5000)
    payload = {"schema_version": 1, "log_id": "root", "stages": [{"id": "s1"}]}

    assert store.save("root", payload) is True
    assert store.get("root") == payload
    assert store.get("../outside") is None

    trace_path = tmp_path / "traces" / "root.json.gz"
    trace_path.write_bytes(b"not gzip")
    assert store.get("root") is None


def test_trace_size_limit_preserves_final_adobe_response_and_error(monkeypatch):
    import core.request_trace as trace_module

    monkeypatch.setattr(trace_module, "MAX_TRACE_BYTES", 20_000)
    trace = RequestTrace(log_id="root", method="POST", path="/v1/images/generations")
    for index in range(40):
        is_final = index == 39
        trace.add_stage(
            layer="adobe",
            kind="poll",
            name=f"poll-{index}",
            status="failed" if is_final else "succeeded",
            response={"status_code": 200, "body": {"payload": str(index) * 2000}},
            error=RuntimeError("final Adobe error") if is_final else None,
        )

    payload = trace.finalize(outcome="failed", final_error="final Adobe error")
    encoded_size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    assert payload["truncated"] is True
    assert encoded_size <= 20_000
    assert payload["stages"][-1]["response"]["body"]["payload"] == "39" * 2000
    assert payload["stages"][-1]["error"]["class"] == "RuntimeError"


def test_adobe_submit_and_poll_responses_are_attached_to_trace(monkeypatch):
    client = AdobeClient()
    trace = RequestTrace(log_id="root", method="POST", path="/v1/images/generations")
    submit_response = FakeResponse(
        200,
        {"links": {"result": {"href": "https://example.test/jobs/1?sig=SECRET"}}},
    )
    poll_responses = iter(
        [
            FakeResponse(200, {"status": "IN_PROGRESS", "progress": 0.1}),
            FakeResponse(200, {"status": "IN_PROGRESS", "progress": 0.2}),
            FakeResponse(200, {"status": "FAILED", "error": {"code": "UPSTREAM"}}),
        ]
    )
    monkeypatch.setattr(client, "_build_payload_candidates", lambda **kwargs: [{"modelId": "gpt-image"}])
    monkeypatch.setattr(client, "_post_json", lambda *args, **kwargs: submit_response)
    monkeypatch.setattr(client, "_get", lambda *args, **kwargs: next(poll_responses))
    monkeypatch.setattr("core.adobe_client.time.sleep", lambda *_args: None)

    with pytest.raises(AdobeRequestError, match="image job failed"):
        client._generate_once(
            token="TOP-SECRET-TOKEN",
            prompt="draw",
            upstream_model_id="gpt-image",
            upstream_model_version="2",
            trace=trace,
            trace_parent_id="seed-1",
        )

    payload = trace.finalize(outcome="failed")
    submits = [stage for stage in payload["stages"] if stage["kind"] == "submit"]
    polls = [stage for stage in payload["stages"] if stage["kind"] == "poll"]
    assert len(submits) == 1
    assert submits[0]["request"]["headers"]["Authorization"] == REDACTED
    assert submits[0]["response"]["status_code"] == 200
    assert len(polls) == 2
    assert polls[0]["aggregate"]["count"] == 2
    assert polls[1]["response"]["body"]["error"]["code"] == "UPSTREAM"
    assert "TOP-SECRET-TOKEN" not in json.dumps(payload, ensure_ascii=False)


def test_failed_generation_keeps_error_response_and_persists_trace(monkeypatch, tmp_path):
    import app as app_module

    trace_store = RequestTraceStore(tmp_path / "traces", max_items=5000)
    monkeypatch.setattr(app_module, "trace_store", trace_store)
    monkeypatch.setattr(
        app_module,
        "log_store",
        RequestLogStore(tmp_path / "request_logs.jsonl", max_items=5000),
    )
    monkeypatch.setattr(
        app_module,
        "error_store",
        ErrorDetailStore(tmp_path / "request_errors.jsonl", max_items=5000),
    )
    monkeypatch.setattr(app_module, "live_log_store", LiveRequestStore(max_items=2000))

    required_key = str(app_module.config_manager.get("api_key", "") or "").strip()
    headers = {"Authorization": f"Bearer {required_key}"} if required_key else {}
    with TestClient(app_module.app) as client:
        response = client.post(
            "/v1/images/generations",
            json={"model": "gpt-image-2"},
            headers=headers,
        )

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "message": "prompt is required",
            "type": "invalid_request_error",
        }
    }

    trace_files = list((tmp_path / "traces").glob("*.json.gz"))
    assert len(trace_files) == 1
    with gzip.open(trace_files[0], "rt", encoding="utf-8") as stream:
        trace = json.load(stream)
    assert trace["path"] == "/v1/images/generations"
    assert trace["outcome"] == "failed"
    assert any(stage["kind"] == "validation" for stage in trace["stages"])
    assert trace["stages"][-1]["kind"] == "response"
    serialized = json.dumps(trace, ensure_ascii=False)
    if required_key:
        assert required_key not in serialized
