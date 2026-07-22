import json
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


@dataclass
class JobRecord:
    id: str
    prompt: str
    aspect_ratio: str
    status: str = "queued"
    progress: float = 0.0
    image_url: Optional[str] = None
    error: Optional[str] = None
    created_at: float = 0.0
    updated_at: float = 0.0


class JobStore:
    def __init__(self, max_items: int = 200) -> None:
        self._items: dict[str, JobRecord] = {}
        self._lock = threading.Lock()
        self._max_items = max_items

    def _cleanup(self):
        if len(self._items) > self._max_items:
            sorted_items = sorted(self._items.values(), key=lambda x: x.created_at)
            for item in sorted_items[:50]:
                self._items.pop(item.id, None)

    def create(self, prompt: str, aspect_ratio: str) -> JobRecord:
        now = time.time()
        item = JobRecord(
            id=uuid.uuid4().hex,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._cleanup()
            self._items[item.id] = item
        return item

    def get(self, job_id: str) -> Optional[JobRecord]:
        with self._lock:
            return self._items.get(job_id)

    def update(self, job_id: str, **kwargs) -> None:
        with self._lock:
            item = self._items.get(job_id)
            if not item:
                return
            for k, v in kwargs.items():
                setattr(item, k, v)
            item.updated_at = time.time()


@dataclass
class RequestLogRecord:
    id: str
    ts: float
    method: str
    path: str
    status_code: int
    duration_sec: int
    operation: str
    preview_url: Optional[str] = None
    preview_kind: Optional[str] = None
    model: Optional[str] = None
    prompt: Optional[str] = None
    prompt_preview: Optional[str] = None
    resolution: Optional[str] = None
    request_type: Optional[str] = None
    request_params: Optional[str] = None
    input_image_urls: Optional[list[str]] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    error_type: Optional[str] = None
    upstream_error_code: Optional[str] = None
    task_status: Optional[str] = None
    task_progress: Optional[float] = None
    upstream_job_id: Optional[str] = None
    retry_after: Optional[int] = None
    token_id: Optional[str] = None
    token_account_name: Optional[str] = None
    token_account_email: Optional[str] = None
    token_source: Optional[str] = None
    token_attempt: Optional[int] = None


class RequestLogStore:
    def __init__(self, file_path: Path, max_items: int = 500) -> None:
        self._file_path = file_path
        self._lock = threading.Lock()
        self._max_items = max_items
        self._append_since_truncate = 0
        self._truncate_check_interval = 200
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._file_path.exists():
            self._file_path.touch()

    def _truncate_to_max_locked(self) -> None:
        tail: deque[str] = deque(maxlen=self._max_items)
        total = 0
        with self._file_path.open("r", encoding="utf-8") as f:
            for line in f:
                total += 1
                tail.append(line)
        if total <= self._max_items:
            return
        with self._file_path.open("w", encoding="utf-8") as f:
            f.writelines(tail)

    def _append_payload_locked(self, payload: dict) -> None:
        with self._file_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._append_since_truncate += 1
        if self._append_since_truncate >= self._truncate_check_interval:
            self._truncate_to_max_locked()
            self._append_since_truncate = 0

    def add(self, item: RequestLogRecord) -> None:
        payload = asdict(item)
        self.add_payload(payload)

    def add_payload(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        with self._lock:
            self._append_payload_locked(payload)

    def upsert(self, item_id: str, payload: dict) -> None:
        if not item_id:
            return
        if not isinstance(payload, dict):
            return
        item = {"id": item_id}
        item.update(payload)
        with self._lock:
            self._append_payload_locked(item)

    @staticmethod
    def matches_filters(
        item: dict,
        *,
        prompt: str = "",
        errors_only: bool = False,
    ) -> bool:
        prompt_query = str(prompt or "").strip().casefold()
        if prompt_query:
            prompt_text = str(
                item.get("prompt") or item.get("prompt_preview") or ""
            ).casefold()
            if prompt_query not in prompt_text:
                return False
        if errors_only:
            try:
                status_code = int(item.get("status_code") or 0)
            except (TypeError, ValueError):
                status_code = 0
            task_status = str(item.get("task_status") or "").strip().upper()
            if status_code < 400 and task_status != "FAILED":
                return False
        return True

    def list(
        self,
        limit: int = 20,
        page: int = 1,
        *,
        prompt: str = "",
        errors_only: bool = False,
    ) -> tuple[list[dict], int]:
        safe_limit = min(max(int(limit or 20), 1), 100)
        safe_page = max(int(page or 1), 1)
        matching_items: list[dict] = []
        with self._lock:
            with self._file_path.open("r", encoding="utf-8") as f:
                for line in f:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        item = json.loads(raw)
                    except Exception:
                        continue
                    if not isinstance(item, dict):
                        continue
                    if self.matches_filters(
                        item,
                        prompt=prompt,
                        errors_only=errors_only,
                    ):
                        matching_items.append(item)

        total = len(matching_items)
        if total <= 0:
            return [], 0
        start_from_end = (safe_page - 1) * safe_limit
        if start_from_end >= total:
            return [], total
        end_idx = total - start_from_end
        start_idx = max(0, end_idx - safe_limit)
        return list(reversed(matching_items[start_idx:end_idx])), total

    def list_cursor(
        self,
        *,
        before_ts: Optional[float] = None,
        limit: int = 100,
        prompt: str = "",
        errors_only: bool = False,
    ) -> tuple[list[dict], Optional[float]]:
        """Return a newest-first page suitable for cross-instance log merging."""
        safe_limit = min(max(int(limit or 100), 1), 500)
        items: list[dict] = []
        with self._lock:
            with self._file_path.open("r", encoding="utf-8") as f:
                for line in f:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        item = json.loads(raw)
                    except Exception:
                        continue
                    if not isinstance(item, dict):
                        continue
                    try:
                        ts_value = float(item.get("ts") or 0)
                    except (TypeError, ValueError):
                        ts_value = 0.0
                    if before_ts is not None and ts_value >= float(before_ts):
                        continue
                    if not self.matches_filters(
                        item,
                        prompt=prompt,
                        errors_only=errors_only,
                    ):
                        continue
                    items.append(item)

        items.sort(key=lambda row: float(row.get("ts") or 0), reverse=True)
        page = items[:safe_limit]
        next_before_ts = None
        if len(items) > safe_limit and page:
            next_before_ts = float(page[-1].get("ts") or 0)
        return page, next_before_ts

    @staticmethod
    def _percentile(values: list[float], quantile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        if len(ordered) == 1:
            return float(ordered[0])
        position = max(0.0, min(1.0, quantile)) * (len(ordered) - 1)
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        fraction = position - lower
        return float(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction)

    def window_metrics(self, *, start_ts: float, end_ts: float) -> dict:
        total = 0
        failed = 0
        generated_images = 0
        generated_videos = 0
        durations: list[float] = []

        with self._lock:
            with self._file_path.open("r", encoding="utf-8") as f:
                for line in f:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        item = json.loads(raw)
                    except Exception:
                        continue
                    if not isinstance(item, dict):
                        continue
                    try:
                        ts_value = float(item.get("ts") or 0)
                    except (TypeError, ValueError):
                        continue
                    if ts_value < float(start_ts) or ts_value > float(end_ts):
                        continue

                    total += 1
                    try:
                        status_code = int(item.get("status_code") or 0)
                    except (TypeError, ValueError):
                        status_code = 0
                    task_status = str(item.get("task_status") or "").upper()
                    if status_code >= 400 or task_status == "FAILED":
                        failed += 1
                    try:
                        duration = float(item.get("duration_sec") or 0)
                    except (TypeError, ValueError):
                        duration = 0.0
                    if duration >= 0:
                        durations.append(duration)
                    if 200 <= status_code < 300 and task_status not in {
                        "FAILED",
                        "ERROR",
                        "CANCELLED",
                    }:
                        preview_kind = str(item.get("preview_kind") or "").lower()
                        if preview_kind == "image":
                            generated_images += 1
                        elif preview_kind == "video":
                            generated_videos += 1

        return {
            "window_seconds": max(0, int(end_ts - start_ts)),
            "total": total,
            "successful": max(0, total - failed),
            "failed": failed,
            "error_rate": round(failed / total, 4) if total else 0.0,
            "duration_p50_seconds": round(self._percentile(durations, 0.50), 3),
            "duration_p95_seconds": round(self._percentile(durations, 0.95), 3),
            "generated_images": generated_images,
            "generated_videos": generated_videos,
        }

    def stats(
        self,
        start_ts: Optional[float] = None,
        end_ts: Optional[float] = None,
    ) -> dict:
        total_requests = 0
        failed_requests = 0
        generated_images = 0
        generated_videos = 0
        in_progress_requests = 0

        with self._lock:
            with self._file_path.open("r", encoding="utf-8") as f:
                for line in f:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        item = json.loads(raw)
                    except Exception:
                        continue
                    if not isinstance(item, dict):
                        continue

                    try:
                        ts_val = float(item.get("ts") or 0)
                    except Exception:
                        ts_val = 0.0
                    if start_ts is not None and ts_val < float(start_ts):
                        continue
                    if end_ts is not None and ts_val > float(end_ts):
                        continue

                    total_requests += 1

                    try:
                        status_code = int(item.get("status_code") or 0)
                    except Exception:
                        status_code = 0
                    if status_code >= 400:
                        failed_requests += 1

                    task_status = str(item.get("task_status") or "").upper()
                    if task_status == "IN_PROGRESS":
                        in_progress_requests += 1

                    preview_kind = str(item.get("preview_kind") or "").strip().lower()
                    if 200 <= status_code < 300:
                        if preview_kind == "image":
                            generated_images += 1
                        elif preview_kind == "video":
                            generated_videos += 1

        return {
            "total_requests": total_requests,
            "failed_requests": failed_requests,
            "generated_images": generated_images,
            "generated_videos": generated_videos,
            "generated_total": generated_images + generated_videos,
            "in_progress_requests": in_progress_requests,
        }

    @staticmethod
    def _is_image_generation_request(item: dict) -> bool:
        path = str(item.get("path") or "").strip().lower()
        operation = str(item.get("operation") or "").strip().lower()
        request_type = str(item.get("request_type") or "").strip().lower()
        preview_kind = str(item.get("preview_kind") or "").strip().lower()
        model = str(item.get("model") or "").strip().lower()

        if preview_kind == "image":
            return True
        if path in {"/v1/images/generations", "/v1/images/edits"}:
            return True
        if operation in {"images.generations", "images.edits"}:
            return True
        if request_type in {"generation", "edits", "image", "images.generations", "images.edits"}:
            return True

        # /v1/chat/completions is used for both image and video generation.
        # Treat non-video generation models as image requests when they go
        # through the chat completion generation path.
        if path == "/v1/chat/completions" or operation == "chat.completions":
            video_prefixes = (
                "firefly-sora",
                "firefly-veo",
                "firefly-kling",
            )
            return bool(model) and not model.startswith(video_prefixes)

        return False

    @staticmethod
    def _is_successful_generation(item: dict) -> bool:
        try:
            status_code = int(item.get("status_code") or 0)
        except Exception:
            status_code = 0
        task_status = str(item.get("task_status") or "").strip().upper()
        if task_status in {"FAILED", "ERROR", "CANCELLED"}:
            return False
        return 200 <= status_code < 300

    @staticmethod
    def _is_image_unsafe_block(item: dict) -> bool:
        fields = [
            item.get("error"),
            item.get("error_code"),
            item.get("error_type"),
            item.get("upstream_error_code"),
        ]
        text = " ".join(str(x or "") for x in fields).lower()
        return "image_unsafe" in text or "content_policy_violation" in text

    def image_generation_stats_windows(
        self,
        windows: Optional[dict[str, int]] = None,
        now_ts: Optional[float] = None,
    ) -> dict:
        now = float(now_ts if now_ts is not None else time.time())
        window_map = windows or {"1h": 3600, "6h": 21600, "24h": 86400}
        stats = {
            key: {
                "seconds": int(seconds),
                "request_count": 0,
                "success_count": 0,
                "failed_count": 0,
                "image_unsafe_count": 0,
                "success_rate": 0.0,
            }
            for key, seconds in window_map.items()
        }

        with self._lock:
            with self._file_path.open("r", encoding="utf-8") as f:
                for line in f:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        item = json.loads(raw)
                    except Exception:
                        continue
                    if not isinstance(item, dict):
                        continue
                    if not self._is_image_generation_request(item):
                        continue

                    try:
                        ts_val = float(item.get("ts") or 0)
                    except Exception:
                        ts_val = 0.0
                    if ts_val <= 0:
                        continue

                    age = now - ts_val
                    if age < 0:
                        age = 0
                    is_success = self._is_successful_generation(item)
                    is_unsafe = self._is_image_unsafe_block(item)

                    for key, seconds in window_map.items():
                        if age > float(seconds):
                            continue
                        row = stats[key]
                        row["request_count"] += 1
                        if is_success:
                            row["success_count"] += 1
                        else:
                            row["failed_count"] += 1
                        if is_unsafe:
                            row["image_unsafe_count"] += 1

        for row in stats.values():
            total = int(row.get("request_count") or 0)
            success = int(row.get("success_count") or 0)
            row["success_rate"] = round((success * 100.0 / total), 2) if total else 0.0
        return stats

    def clear(self) -> None:
        with self._lock:
            with self._file_path.open("w", encoding="utf-8") as f:
                f.write("")
            self._append_since_truncate = 0


@dataclass
class ErrorDetailRecord:
    code: str
    ts: float
    message: str
    error_type: Optional[str] = None
    status_code: Optional[int] = None
    operation: Optional[str] = None
    method: Optional[str] = None
    path: Optional[str] = None
    log_id: Optional[str] = None
    model: Optional[str] = None
    prompt: Optional[str] = None
    prompt_preview: Optional[str] = None
    resolution: Optional[str] = None
    request_type: Optional[str] = None
    request_params: Optional[str] = None
    input_image_urls: Optional[list[str]] = None
    task_status: Optional[str] = None
    task_progress: Optional[float] = None
    upstream_job_id: Optional[str] = None
    token_id: Optional[str] = None
    token_account_name: Optional[str] = None
    token_account_email: Optional[str] = None
    token_source: Optional[str] = None
    token_attempt: Optional[int] = None
    exception_class: Optional[str] = None
    traceback: Optional[str] = None


class ErrorDetailStore:
    def __init__(self, file_path: Path, max_items: int = 5000) -> None:
        self._file_path = file_path
        self._lock = threading.Lock()
        self._max_items = max(200, int(max_items or 5000))
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._file_path.exists():
            self._file_path.touch()

    def _truncate_to_max_locked(self) -> None:
        with self._file_path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= self._max_items:
            return
        kept = lines[-self._max_items :]
        with self._file_path.open("w", encoding="utf-8") as f:
            f.writelines(kept)

    def add(self, item: ErrorDetailRecord) -> None:
        payload = asdict(item)
        with self._lock:
            with self._file_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._truncate_to_max_locked()

    def get(self, code: str) -> Optional[dict]:
        target = str(code or "").strip()
        if not target:
            return None
        with self._lock:
            with self._file_path.open("r", encoding="utf-8") as f:
                lines = f.readlines()

        for line in reversed(lines):
            raw = line.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except Exception:
                continue
            if isinstance(item, dict) and str(item.get("code") or "") == target:
                return item
        return None


class LiveRequestStore:
    def __init__(self, max_items: int = 2000) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, dict] = {}
        self._max_items = max(100, int(max_items or 2000))

    def upsert(self, item_id: str, payload: dict) -> None:
        iid = str(item_id or "").strip()
        if not iid or not isinstance(payload, dict):
            return
        with self._lock:
            old = self._items.get(iid, {})
            merged = dict(old)
            merged.update(payload)
            merged["id"] = iid
            if not merged.get("ts"):
                merged["ts"] = time.time()
            self._items[iid] = merged
            if len(self._items) > self._max_items:
                pairs = sorted(
                    self._items.items(),
                    key=lambda x: float((x[1] or {}).get("ts") or 0),
                )
                overflow = len(self._items) - self._max_items
                for key, _ in pairs[:overflow]:
                    self._items.pop(key, None)

    def remove(self, item_id: str) -> None:
        iid = str(item_id or "").strip()
        if not iid:
            return
        with self._lock:
            self._items.pop(iid, None)

    def list(self, limit: int = 200) -> list[dict]:
        safe_limit = min(max(int(limit or 200), 1), 1000)
        with self._lock:
            data = list(self._items.values())
        data.sort(key=lambda x: float((x or {}).get("ts") or 0), reverse=True)
        return data[:safe_limit]

    def count_in_progress(self) -> int:
        with self._lock:
            vals = list(self._items.values())
        total = 0
        for item in vals:
            status = str((item or {}).get("task_status") or "").upper()
            if status == "IN_PROGRESS":
                total += 1
        return total
