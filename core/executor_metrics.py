from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable


class MetricsThreadPoolExecutor:
    """Small wrapper around ThreadPoolExecutor with cheap in-memory counters."""

    def __init__(self, *, name: str, max_workers: int, thread_name_prefix: str) -> None:
        self.name = str(name or "executor")
        self.max_workers = max(1, int(max_workers))
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix=thread_name_prefix,
        )
        self._lock = threading.Lock()
        self._active = 0
        self._queued = 0
        self._completed = 0
        self._failed = 0

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future:
        with self._lock:
            self._queued += 1

        def wrapped() -> Any:
            with self._lock:
                self._queued = max(0, self._queued - 1)
                self._active += 1
            try:
                result = fn(*args, **kwargs)
                with self._lock:
                    self._completed += 1
                return result
            except BaseException:
                with self._lock:
                    self._failed += 1
                raise
            finally:
                with self._lock:
                    self._active = max(0, self._active - 1)

        return self._executor.submit(wrapped)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)

    def snapshot(self) -> dict[str, int | str]:
        with self._lock:
            return {
                "name": self.name,
                "max_workers": self.max_workers,
                "active": int(self._active),
                "queued": int(self._queued),
                "completed": int(self._completed),
                "failed": int(self._failed),
            }
