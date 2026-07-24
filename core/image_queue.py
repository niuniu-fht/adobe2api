from __future__ import annotations

import hashlib
import heapq
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from typing import Any, Callable, Iterable, Optional


TERMINAL_STATES = {"COMPLETED", "FAILED"}


class ImageTaskCancelled(RuntimeError):
    pass


class ImageTaskCoordinator:
    def __init__(self, *, io_workers: int = 32, retention_seconds: int = 60) -> None:
        self._lock = threading.RLock()
        self._io_executor = ThreadPoolExecutor(
            max_workers=max(4, int(io_workers)),
            thread_name_prefix="image-io",
        )
        self._requests: dict[str, dict[str, Any]] = {}
        self._token_semaphores: dict[str, tuple[int, threading.BoundedSemaphore]] = {}
        self._token_cooldowns: dict[str, float] = {}
        self._token_active: dict[str, int] = {}
        self._token_assigned: dict[str, int] = {}
        self._assignment_cursor = 0
        self._retention_seconds = max(10, int(retention_seconds))
        self._schedule_condition = threading.Condition()
        self._schedule_heap: list[tuple[float, int, threading.Event]] = []
        self._schedule_seq = 0
        self._scheduler = threading.Thread(
            target=self._run_scheduler,
            name="image-timer",
            daemon=True,
        )
        self._scheduler.start()

    def _run_scheduler(self) -> None:
        while True:
            with self._schedule_condition:
                while not self._schedule_heap:
                    self._schedule_condition.wait()
                deadline, _seq, event = self._schedule_heap[0]
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self._schedule_condition.wait(timeout=remaining)
                    continue
                heapq.heappop(self._schedule_heap)
            event.set()

    def wait(self, request_id: str, delay_seconds: float) -> None:
        delay = max(0.0, float(delay_seconds))
        if delay <= 0:
            self.raise_if_cancelled(request_id)
            return
        event = threading.Event()
        with self._schedule_condition:
            self._schedule_seq += 1
            heapq.heappush(
                self._schedule_heap,
                (time.monotonic() + delay, self._schedule_seq, event),
            )
            self._schedule_condition.notify()
        while not event.wait(timeout=0.25):
            self.raise_if_cancelled(request_id)
        self.raise_if_cancelled(request_id)

    def run_io(self, operation: Callable[[], Any]) -> Any:
        return self._io_executor.submit(operation).result()

    @staticmethod
    def token_id(token: str) -> str:
        value = str(token or "").encode("utf-8", errors="replace")
        return hashlib.sha256(value).hexdigest()[:12] if value else ""

    def register_request(
        self,
        *,
        log_id: str,
        path: str,
        model: str,
        prompt_preview: str,
        output_count: int,
    ) -> str:
        request_id = str(log_id or "").strip() or uuid.uuid4().hex[:12]
        now = time.time()
        with self._lock:
            self._requests[request_id] = {
                "id": request_id,
                "log_id": str(log_id or request_id),
                "path": str(path or ""),
                "model": str(model or ""),
                "prompt_preview": str(prompt_preview or "")[:180],
                "requested_count": max(0, int(output_count)),
                "state": "QUEUED",
                "created_at": now,
                "updated_at": now,
                "finished_at": None,
                "error": None,
                "cancelled": False,
                "outputs": [
                    {
                        "index": index,
                        "state": "QUEUED",
                        "token_id": None,
                        "account_name": None,
                        "upstream_job_id": None,
                        "retry_count": 0,
                        "next_run_at": None,
                        "rate_limit_wait_seconds": 0.0,
                        "download_attempt": 0,
                        "last_error": None,
                        "updated_at": now,
                    }
                    for index in range(max(0, int(output_count)))
                ],
            }
        return request_id

    def _request_locked(self, request_id: str) -> Optional[dict[str, Any]]:
        return self._requests.get(str(request_id or ""))

    def set_request_state(
        self,
        request_id: str,
        state: str,
        *,
        error: Any = None,
    ) -> None:
        with self._lock:
            item = self._request_locked(request_id)
            if item is None:
                return
            item["state"] = str(state or "QUEUED").upper()
            item["updated_at"] = time.time()
            if error is not None:
                item["error"] = str(error)

    def set_all_output_state(
        self,
        request_id: str,
        state: str,
        *,
        error: Any = None,
        only_nonterminal: bool = True,
        next_run_at: Optional[float] = None,
        rate_limit_wait_seconds: Optional[float] = None,
        retry_count: Optional[int] = None,
    ) -> None:
        now = time.time()
        normalized = str(state or "QUEUED").upper()
        with self._lock:
            item = self._request_locked(request_id)
            if item is None:
                return
            for output in item.get("outputs") or []:
                if only_nonterminal and str(output.get("state") or "").upper() in TERMINAL_STATES:
                    continue
                output["state"] = normalized
                output["updated_at"] = now
                output["next_run_at"] = (
                    float(next_run_at) if next_run_at else None
                )
                if rate_limit_wait_seconds is not None:
                    output["rate_limit_wait_seconds"] = round(
                        max(0.0, float(rate_limit_wait_seconds)), 3
                    )
                if retry_count is not None:
                    output["retry_count"] = max(0, int(retry_count))
                if error is not None:
                    output["last_error"] = str(error)[:500]
            item["state"] = normalized
            item["updated_at"] = now

    def update_output(
        self,
        request_id: str,
        output_index: int,
        *,
        state: Optional[str] = None,
        token: Optional[str] = None,
        account_name: Optional[str] = None,
        upstream_job_id: Optional[str] = None,
        retry_count: Optional[int] = None,
        next_run_at: Optional[float] = None,
        rate_limit_wait_seconds: Optional[float] = None,
        download_attempt: Optional[int] = None,
        error: Any = None,
    ) -> None:
        with self._lock:
            item = self._request_locked(request_id)
            if item is None:
                return
            outputs = item.get("outputs") or []
            if output_index < 0 or output_index >= len(outputs):
                return
            output = outputs[output_index]
            if state is not None:
                output["state"] = str(state).upper()
            if token is not None:
                output["token_id"] = self.token_id(token)
            if account_name is not None:
                output["account_name"] = str(account_name or "") or None
            if upstream_job_id is not None:
                output["upstream_job_id"] = str(upstream_job_id or "") or None
            if retry_count is not None:
                output["retry_count"] = max(0, int(retry_count))
            output["next_run_at"] = float(next_run_at) if next_run_at else None
            if rate_limit_wait_seconds is not None:
                output["rate_limit_wait_seconds"] = round(
                    max(0.0, float(rate_limit_wait_seconds)), 3
                )
            if download_attempt is not None:
                output["download_attempt"] = max(0, int(download_attempt))
            if error is not None:
                output["last_error"] = str(error)[:500]
            output["updated_at"] = time.time()
            item["updated_at"] = output["updated_at"]
            if item.get("state") not in TERMINAL_STATES:
                item["state"] = self._derive_request_state(outputs)

    @staticmethod
    def _derive_request_state(outputs: list[dict[str, Any]]) -> str:
        states = [str(item.get("state") or "QUEUED").upper() for item in outputs]
        for state in (
            "FAILED",
            "RATE_LIMITED",
            "DOWNLOAD_RETRY",
            "DOWNLOADING",
            "SUBMITTING",
            "UPLOADING",
            "WAITING_POLL",
        ):
            if state in states:
                return state
        if states and all(state == "COMPLETED" for state in states):
            return "COMPLETED"
        return "QUEUED"

    def finish_request(self, request_id: str, *, succeeded: bool, error: Any = None) -> None:
        now = time.time()
        with self._lock:
            item = self._request_locked(request_id)
            if item is None:
                return
            item["state"] = "COMPLETED" if succeeded else "FAILED"
            item["updated_at"] = now
            item["finished_at"] = now
            if error is not None:
                item["error"] = str(error)[:1000]

    def cancel_request(self, request_id: str, error: Any = None) -> None:
        with self._lock:
            item = self._request_locked(request_id)
            if item is None:
                return
            item["cancelled"] = True
            item["state"] = "FAILED"
            item["updated_at"] = time.time()
            if error is not None:
                item["error"] = str(error)[:1000]
            for output in item.get("outputs") or []:
                if str(output.get("state") or "").upper() in TERMINAL_STATES:
                    continue
                output["state"] = "FAILED"
                output["last_error"] = str(error or "request cancelled")[:500]
                output["next_run_at"] = None
                output["updated_at"] = item["updated_at"]

    def is_cancelled(self, request_id: str) -> bool:
        with self._lock:
            item = self._request_locked(request_id)
            return bool(item and item.get("cancelled"))

    def raise_if_cancelled(self, request_id: str) -> None:
        if self.is_cancelled(request_id):
            raise ImageTaskCancelled("image request cancelled")

    def note_token_cooldown(self, token: str, delay_seconds: float) -> None:
        key = self.token_id(token)
        if not key:
            return
        until = time.time() + max(0.0, float(delay_seconds))
        with self._lock:
            self._token_cooldowns[key] = max(
                until, float(self._token_cooldowns.get(key) or 0.0)
            )

    def token_cooldown_remaining(self, token: str) -> float:
        key = self.token_id(token)
        if not key:
            return 0.0
        with self._lock:
            remaining = float(self._token_cooldowns.get(key) or 0.0) - time.time()
            if remaining <= 0:
                self._token_cooldowns.pop(key, None)
                return 0.0
            return remaining

    def assign_token(
        self,
        candidates: Iterable[str],
        *,
        exclude: Optional[set[str]] = None,
    ) -> Optional[str]:
        excluded = exclude or set()
        values = [
            str(token or "").strip()
            for token in candidates
            if str(token or "").strip() and str(token or "").strip() not in excluded
        ]
        if not values:
            return None
        with self._lock:
            available = [
                token
                for token in values
                if float(self._token_cooldowns.get(self.token_id(token)) or 0.0)
                <= time.time()
            ]
            pool = available or values
            start = self._assignment_cursor % len(pool)
            ordered = pool[start:] + pool[:start]
            selected = min(
                ordered,
                key=lambda token: (
                    int(self._token_assigned.get(self.token_id(token)) or 0),
                    int(self._token_active.get(self.token_id(token)) or 0),
                ),
            )
            key = self.token_id(selected)
            self._token_assigned[key] = int(self._token_assigned.get(key) or 0) + 1
            self._assignment_cursor = (start + 1) % max(1, len(pool))
            return selected

    def release_token_assignment(self, token: str) -> None:
        key = self.token_id(token)
        if not key:
            return
        with self._lock:
            remaining = int(self._token_assigned.get(key) or 0) - 1
            if remaining > 0:
                self._token_assigned[key] = remaining
            else:
                self._token_assigned.pop(key, None)

    @contextmanager
    def token_slot(
        self,
        token: str,
        *,
        limit: int,
        request_id: str,
        output_index: int,
    ):
        safe_limit = max(1, int(limit))
        key = self.token_id(token)
        with self._lock:
            existing = self._token_semaphores.get(key)
            if existing is None or existing[0] != safe_limit:
                existing = (safe_limit, threading.BoundedSemaphore(safe_limit))
                self._token_semaphores[key] = existing
            semaphore = existing[1]

        acquired = False
        while not acquired:
            self.raise_if_cancelled(request_id)
            acquired = semaphore.acquire(timeout=0.25)
            if not acquired:
                self.update_output(
                    request_id,
                    output_index,
                    state="QUEUED",
                    token=token,
                )
        try:
            with self._lock:
                self._token_active[key] = int(self._token_active.get(key) or 0) + 1
            yield
        finally:
            with self._lock:
                remaining = int(self._token_active.get(key) or 0) - 1
                if remaining > 0:
                    self._token_active[key] = remaining
                else:
                    self._token_active.pop(key, None)
            semaphore.release()

    def run_indexed(
        self,
        *,
        request_id: str,
        indices: Iterable[int],
        worker: Callable[[int], Any],
        max_parallel: int,
    ) -> list[tuple[int, Any]]:
        pending = list(indices)
        if not pending:
            return []
        limit = max(1, int(max_parallel))
        active: dict[Future, int] = {}
        results: list[tuple[int, Any]] = []
        first_error: Optional[BaseException] = None

        with ThreadPoolExecutor(
            max_workers=limit,
            thread_name_prefix=f"image-state-{str(request_id)[:8]}",
        ) as state_executor:
            def submit_next() -> None:
                while pending and len(active) < limit and first_error is None:
                    index = pending.pop(0)
                    active[state_executor.submit(worker, index)] = index

            submit_next()
            while active:
                done, _ = wait(set(active), return_when=FIRST_COMPLETED)
                for future in done:
                    index = active.pop(future)
                    try:
                        results.append((index, future.result()))
                    except BaseException as exc:
                        is_priority_error = exc.__class__.__name__ == "ContentPolicyError"
                        current_is_priority = (
                            first_error is not None
                            and first_error.__class__.__name__ == "ContentPolicyError"
                        )
                        if first_error is None or (
                            is_priority_error and not current_is_priority
                        ):
                            first_error = exc
                submit_next()

        if first_error is not None:
            raise first_error
        return sorted(results, key=lambda pair: pair[0])

    def _prune_locked(self) -> None:
        cutoff = time.time() - self._retention_seconds
        stale = [
            key
            for key, item in self._requests.items()
            if item.get("finished_at") and float(item["finished_at"]) < cutoff
        ]
        for key in stale:
            self._requests.pop(key, None)

    def snapshot(self, *, limit: int = 200) -> dict[str, Any]:
        safe_limit = min(max(1, int(limit or 200)), 1000)
        with self._lock:
            self._prune_locked()
            items = []
            for raw in self._requests.values():
                item = {key: value for key, value in raw.items() if key != "cancelled"}
                item["outputs"] = [dict(output) for output in raw.get("outputs") or []]
                item["completed_count"] = sum(
                    1
                    for output in item["outputs"]
                    if output.get("state") == "COMPLETED"
                )
                item["elapsed_seconds"] = round(
                    max(
                        0.0,
                        float(item.get("finished_at") or time.time())
                        - float(item.get("created_at") or time.time()),
                    ),
                    3,
                )
                items.append(item)
            items.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
            items = items[:safe_limit]

        states = [
            str(output.get("state") or "QUEUED")
            for item in items
            for output in item.get("outputs") or []
        ]
        summary = {
            "requests": len(items),
            "outputs": len(states),
            "in_progress": sum(
                state
                in {
                    "UPLOADING",
                    "SUBMITTING",
                    "WAITING_POLL",
                    "DOWNLOADING",
                    "DOWNLOAD_RETRY",
                }
                for state in states
            ),
            "queued": states.count("QUEUED"),
            "waiting_poll": states.count("WAITING_POLL"),
            "rate_limited": states.count("RATE_LIMITED"),
            "download_retry": states.count("DOWNLOAD_RETRY"),
        }
        return {"summary": summary, "items": items}


image_task_coordinator = ImageTaskCoordinator()
