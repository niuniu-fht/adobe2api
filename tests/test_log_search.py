import json

from app import _extract_logging_fields
from core.stores import RequestLogStore


def _add_log(
    store: RequestLogStore,
    *,
    item_id: str,
    prompt: str,
    status_code: int,
    task_status: str,
) -> None:
    store.add_payload(
        {
            "id": item_id,
            "prompt_preview": prompt,
            "status_code": status_code,
            "task_status": task_status,
        }
    )


def test_log_prompt_search_is_case_insensitive_and_paginates_matches(tmp_path):
    store = RequestLogStore(tmp_path / "requests.jsonl", max_items=100)
    _add_log(
        store,
        item_id="first",
        prompt="A Red Banana on a table",
        status_code=200,
        task_status="COMPLETED",
    )
    _add_log(
        store,
        item_id="other",
        prompt="A blue mountain",
        status_code=200,
        task_status="COMPLETED",
    )
    _add_log(
        store,
        item_id="latest",
        prompt="RED BANANA in space",
        status_code=500,
        task_status="FAILED",
    )

    first_page, total = store.list(limit=1, page=1, prompt="red banana")
    second_page, second_total = store.list(limit=1, page=2, prompt="red banana")

    assert total == 2
    assert second_total == 2
    assert [item["id"] for item in first_page] == ["latest"]
    assert [item["id"] for item in second_page] == ["first"]


def test_log_error_filter_matches_http_and_failed_task_errors(tmp_path):
    store = RequestLogStore(tmp_path / "requests.jsonl", max_items=100)
    _add_log(
        store,
        item_id="success",
        prompt="banana success",
        status_code=200,
        task_status="COMPLETED",
    )
    _add_log(
        store,
        item_id="http-error",
        prompt="banana failed",
        status_code=429,
        task_status="COMPLETED",
    )
    _add_log(
        store,
        item_id="task-error",
        prompt="banana failed task",
        status_code=0,
        task_status="FAILED",
    )

    errors, total = store.list(
        limit=20,
        page=1,
        prompt="banana",
        errors_only=True,
    )

    assert total == 2
    assert [item["id"] for item in errors] == ["task-error", "http-error"]


def test_log_search_uses_full_prompt_beyond_preview(tmp_path):
    store = RequestLogStore(tmp_path / "requests.jsonl", max_items=100)
    full_prompt = f"{'A' * 220} hidden search phrase"
    store.add_payload(
        {
            "id": "full-prompt",
            "prompt": full_prompt,
            "prompt_preview": full_prompt[:180],
            "status_code": 200,
            "task_status": "COMPLETED",
        }
    )

    items, total = store.list(prompt="hidden search phrase")

    assert total == 1
    assert items[0]["id"] == "full-prompt"


def test_logging_fields_keep_full_prompt_and_build_short_preview():
    full_prompt = f"first line\n{'B' * 220}\nlast line"
    metadata = _extract_logging_fields(
        json.dumps(
            {
                "model": "gpt-image-gemini-3.1-flash-image",
                "prompt": full_prompt,
                "size": "1536x1024",
            }
        ).encode("utf-8")
    )

    assert metadata["prompt"] == full_prompt
    assert metadata["prompt_preview"] == full_prompt.replace("\n", " ")[:180]


def test_cursor_logs_return_newest_page_and_next_cursor(tmp_path):
    store = RequestLogStore(tmp_path / "requests.jsonl", max_items=100)
    for index in range(4):
        store.add_payload(
            {
                "id": f"log-{index}",
                "ts": 100.0 + index,
                "prompt": "fleet test",
                "status_code": 200,
                "task_status": "COMPLETED",
                "duration_sec": index,
            }
        )

    first, cursor = store.list_cursor(limit=2)
    second, second_cursor = store.list_cursor(limit=2, before_ts=cursor)

    assert [item["id"] for item in first] == ["log-3", "log-2"]
    assert cursor == 102.0
    assert [item["id"] for item in second] == ["log-1", "log-0"]
    assert second_cursor is None


def test_window_metrics_calculate_failures_and_percentiles(tmp_path):
    store = RequestLogStore(tmp_path / "requests.jsonl", max_items=100)
    for index, status in enumerate((200, 200, 500)):
        store.add_payload(
            {
                "id": f"metric-{index}",
                "ts": 200.0 + index,
                "status_code": status,
                "task_status": "FAILED" if status >= 400 else "COMPLETED",
                "duration_sec": index + 1,
                "preview_kind": "image",
            }
        )

    metrics = store.window_metrics(start_ts=199.0, end_ts=204.0)

    assert metrics["total"] == 3
    assert metrics["successful"] == 2
    assert metrics["failed"] == 1
    assert metrics["error_rate"] == 0.3333
    assert metrics["duration_p50_seconds"] == 2.0
    assert metrics["duration_p95_seconds"] == 2.9
    assert metrics["generated_images"] == 2
