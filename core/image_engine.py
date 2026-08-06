from __future__ import annotations

import heapq
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from core.config_mgr import config_manager
from core.executor_metrics import MetricsThreadPoolExecutor


def _conf_int(key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(config_manager.get(key, default) or default)
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


@dataclass
class ImageEngineJob:
    id: str
    token: str
    prompt: str
    aspect_ratio: str
    output_resolution: str
    upstream_model_id: str
    upstream_model_version: str
    quality_level: Optional[str]
    detail_level: Optional[int]
    seed: Optional[int]
    source_image_ids: Optional[list[str]]
    requested_size: Optional[dict]
    timeout: int
    out_path: Optional[Path]
    progress_cb: Optional[Callable[[dict], None]]
    trace: Any
    trace_parent_id: Optional[str]
    cancel_check: Optional[Callable[[], None]]
    event: threading.Event = field(default_factory=threading.Event)
    created_at: float = field(default_factory=time.time)
    submitted_at: float = 0.0
    poll_url: str = ""
    upstream_job_id: str = ""
    poll_state: dict[str, Any] = field(default_factory=dict)
    result: Optional[tuple[Optional[bytes], dict]] = None
    error: Optional[BaseException] = None
    stage: str = "QUEUED"


class ImageGenerationEngine:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._schedule_condition = threading.Condition(self._lock)
        self._schedule_heap: list[tuple[float, int, str]] = []
        self._schedule_seq = 0
        self._jobs: dict[str, ImageEngineJob] = {}
        self._global_semaphore = threading.BoundedSemaphore(self.global_limit)
        self._global_active = 0
        self._stage_counts: dict[str, int] = {}
        self._submit_executor = MetricsThreadPoolExecutor(
            name="submit",
            max_workers=self.submit_workers,
            thread_name_prefix="image-submit",
        )
        self._poll_executor = MetricsThreadPoolExecutor(
            name="poll",
            max_workers=self.poll_workers,
            thread_name_prefix="image-poll",
        )
        self._download_executor = MetricsThreadPoolExecutor(
            name="download",
            max_workers=self.download_workers,
            thread_name_prefix="image-download",
        )
        self._scheduler = threading.Thread(
            target=self._run_scheduler,
            name="image-poll-scheduler",
            daemon=True,
        )
        self._scheduler.start()

    @property
    def enabled(self) -> bool:
        return bool(config_manager.get("image_async_poll_enabled", True))

    @property
    def global_limit(self) -> int:
        return _conf_int("image_global_concurrency", 45, 1, 200)

    @property
    def submit_workers(self) -> int:
        return _conf_int("image_submit_workers", 12, 1, 100)

    @property
    def poll_workers(self) -> int:
        return _conf_int("image_poll_workers", 8, 1, 100)

    @property
    def download_workers(self) -> int:
        return _conf_int("image_download_workers", 12, 1, 100)

    def _set_stage(self, job: ImageEngineJob, stage: str) -> None:
        normalized = str(stage or "QUEUED").upper()
        with self._lock:
            old = str(job.stage or "").upper()
            if old:
                self._stage_counts[old] = max(0, int(self._stage_counts.get(old) or 0) - 1)
            self._stage_counts[normalized] = int(self._stage_counts.get(normalized) or 0) + 1
            job.stage = normalized

    def _finish_job(
        self,
        job: ImageEngineJob,
        *,
        result: Optional[tuple[Optional[bytes], dict]] = None,
        error: Optional[BaseException] = None,
    ) -> None:
        with self._lock:
            if job.id not in self._jobs:
                return
            if result is not None:
                job.result = result
                stage = "COMPLETED"
            else:
                job.error = error
                stage = "FAILED"
            old = str(job.stage or "").upper()
            if old:
                self._stage_counts[old] = max(0, int(self._stage_counts.get(old) or 0) - 1)
            job.stage = stage
            self._jobs.pop(job.id, None)
            self._global_active = max(0, self._global_active - 1)
            try:
                self._global_semaphore.release()
            except ValueError:
                pass
            job.event.set()

    def _run_scheduler(self) -> None:
        while True:
            with self._schedule_condition:
                while not self._schedule_heap:
                    self._schedule_condition.wait()
                run_at, _seq, job_id = self._schedule_heap[0]
                remaining = run_at - time.monotonic()
                if remaining > 0:
                    self._schedule_condition.wait(timeout=remaining)
                    continue
                heapq.heappop(self._schedule_heap)
                job = self._jobs.get(job_id)
            if job is not None and not job.event.is_set():
                self._poll_executor.submit(self._poll_job, job)

    def _schedule_poll(self, job: ImageEngineJob, delay: float) -> None:
        delay = max(0.0, float(delay or 0.0))
        with self._schedule_condition:
            self._schedule_seq += 1
            heapq.heappush(
                self._schedule_heap,
                (time.monotonic() + delay, self._schedule_seq, job.id),
            )
            self._schedule_condition.notify()

    def _submit_job(self, client: Any, job: ImageEngineJob) -> None:
        try:
            self._set_stage(job, "SUBMITTING")
            meta = client.submit_image_job(
                token=job.token,
                prompt=job.prompt,
                aspect_ratio=job.aspect_ratio,
                output_resolution=job.output_resolution,
                upstream_model_id=job.upstream_model_id,
                upstream_model_version=job.upstream_model_version,
                quality_level=job.quality_level,
                detail_level=job.detail_level,
                seed=job.seed,
                source_image_ids=job.source_image_ids,
                requested_size=job.requested_size,
                progress_cb=job.progress_cb,
                trace=job.trace,
                trace_parent_id=job.trace_parent_id,
                cancel_check=job.cancel_check,
            )
            job.poll_url = str(meta.get("poll_url") or "")
            job.upstream_job_id = str(meta.get("upstream_job_id") or "")
            job.submitted_at = float(meta.get("submitted_at") or time.time())
            job.poll_state = dict(meta)
            job.poll_state["_client"] = client
            self._set_stage(job, "WAITING_POLL")
            self._schedule_poll(job, 0.0)
        except BaseException as exc:
            self._finish_job(job, error=exc)

    def _poll_job(self, job: ImageEngineJob) -> None:
        try:
            if job.cancel_check is not None:
                job.cancel_check()
            self._set_stage(job, "POLLING")
            result = job.poll_state.get("_client").poll_image_job_once(
                token=job.token,
                poll_url=job.poll_url,
                state=job.poll_state,
                timeout=job.timeout,
                progress_cb=job.progress_cb,
                trace=job.trace,
                trace_parent_id=job.trace_parent_id,
                cancel_check=job.cancel_check,
            )
            status = str(result.get("status") or "").lower()
            if status == "completed":
                job.poll_state["latest"] = result.get("latest") or {}
                job.poll_state["image_url"] = result.get("image_url") or ""
                self._set_stage(job, "DOWNLOADING")
                self._download_executor.submit(self._download_job, job)
                return
            delay = float(result.get("retry_after") or 3.0)
            self._set_stage(job, "WAITING_POLL")
            self._schedule_poll(job, delay)
        except BaseException as exc:
            self._finish_job(job, error=exc)

    def _download_job(self, job: ImageEngineJob) -> None:
        try:
            client = job.poll_state.get("_client")
            image_bytes = client.download_image_result(
                image_url=str(job.poll_state.get("image_url") or ""),
                poll_url=job.poll_url,
                token=job.token,
                out_path=job.out_path,
                progress_cb=job.progress_cb,
                trace=job.trace,
                trace_parent_id=job.trace_parent_id,
                upstream_job_id=job.upstream_job_id,
                cancel_check=job.cancel_check,
            )
            latest = dict(job.poll_state.get("latest") or {})
            if job.progress_cb:
                try:
                    job.progress_cb(
                        {
                            "task_status": "COMPLETED",
                            "task_progress": 100.0,
                            "upstream_job_id": job.upstream_job_id,
                            "retry_after": None,
                        }
                    )
                except Exception:
                    pass
            self._finish_job(job, result=(image_bytes, latest))
        except BaseException as exc:
            self._finish_job(job, error=exc)

    def generate(self, client: Any, **kwargs: Any) -> tuple[Optional[bytes], dict]:
        while True:
            cancel_check = kwargs.get("cancel_check")
            if cancel_check is not None:
                cancel_check()
            acquired = self._global_semaphore.acquire(timeout=0.25)
            if acquired:
                break
        job = ImageEngineJob(
            id=uuid.uuid4().hex,
            token=str(kwargs.get("token") or ""),
            prompt=str(kwargs.get("prompt") or ""),
            aspect_ratio=str(kwargs.get("aspect_ratio") or "16:9"),
            output_resolution=str(kwargs.get("output_resolution") or "2K"),
            upstream_model_id=str(kwargs.get("upstream_model_id") or "gemini-flash"),
            upstream_model_version=str(kwargs.get("upstream_model_version") or "nano-banana-2"),
            quality_level=kwargs.get("quality_level"),
            detail_level=kwargs.get("detail_level"),
            seed=kwargs.get("seed"),
            source_image_ids=kwargs.get("source_image_ids"),
            requested_size=kwargs.get("requested_size"),
            timeout=int(kwargs.get("timeout") or 180),
            out_path=kwargs.get("out_path"),
            progress_cb=kwargs.get("progress_cb"),
            trace=kwargs.get("trace"),
            trace_parent_id=kwargs.get("trace_parent_id"),
            cancel_check=kwargs.get("cancel_check"),
        )
        job.poll_state["_client"] = client
        with self._lock:
            self._jobs[job.id] = job
            self._global_active += 1
            self._stage_counts["QUEUED"] = int(self._stage_counts.get("QUEUED") or 0) + 1
        self._submit_executor.submit(self._submit_job, client, job)
        job.event.wait()
        if job.error is not None:
            raise job.error
        return job.result or (None, {})

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            stages = dict(self._stage_counts)
            active = int(self._global_active)
        return {
            "summary": {
                "engine_submitting": stages.get("SUBMITTING", 0),
                "engine_waiting_poll": stages.get("WAITING_POLL", 0),
                "engine_polling": stages.get("POLLING", 0),
                "engine_downloading": stages.get("DOWNLOADING", 0),
            },
            "pools": {
                "submit": self._submit_executor.snapshot(),
                "poll": self._poll_executor.snapshot(),
                "download": self._download_executor.snapshot(),
            },
            "limits": {
                "global_active": active,
                "global_limit": self.global_limit,
                "per_token_limit": _conf_int("image_per_token_concurrency", 5, 1, 50),
            },
            "engine_stages": stages,
        }


image_generation_engine = ImageGenerationEngine()
