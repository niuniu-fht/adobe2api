import os
import time
from datetime import datetime
from typing import Any, Callable, Optional

from fastapi import APIRouter, Request


OPS_API_VERSION = 1
OPS_CAPABILITIES = [
    "snapshot",
    "cursor_logs",
    "tokens",
    "refresh_profiles",
    "config",
    "generated_media",
]


def build_ops_router(
    *,
    token_manager,
    refresh_manager,
    log_store,
    live_log_store,
    require_ops_auth: Callable[[Request], None],
    get_generated_storage_stats: Callable[[], dict[str, Any]],
    app_started_at: float,
    app_version: str,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/ops", tags=["ops"])

    @router.get("/snapshot")
    def snapshot(request: Request):
        require_ops_auth(request)
        now_ts = time.time()
        tokens = token_manager.list_all()
        status_counts: dict[str, int] = {}
        credits_total = 0.0
        credits_available = 0.0
        expiring_24h = 0
        for item in tokens:
            status = str(item.get("status") or "unknown").strip().lower()
            status_counts[status] = status_counts.get(status, 0) + 1
            try:
                if item.get("credits_total") is not None:
                    credits_total += float(item.get("credits_total") or 0)
                if item.get("credits_available") is not None:
                    credits_available += float(item.get("credits_available") or 0)
            except (TypeError, ValueError):
                pass
            try:
                remaining = float(item.get("remaining_seconds"))
            except (TypeError, ValueError):
                remaining = None
            if remaining is not None and 0 < remaining <= 86400:
                expiring_24h += 1

        profiles = refresh_manager.list_profiles()
        profile_failures = []
        for profile in profiles:
            state = profile.get("state") if isinstance(profile.get("state"), dict) else {}
            try:
                profile_failures.append(int(state.get("consecutive_failures") or 0))
            except (TypeError, ValueError):
                profile_failures.append(0)

        request_metrics = log_store.window_metrics(
            start_ts=now_ts - 300,
            end_ts=now_ts,
        )
        request_metrics["in_progress"] = live_log_store.count_in_progress()
        today_start = datetime.fromtimestamp(now_ts).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        ).timestamp()
        today_metrics = log_store.window_metrics(
            start_ts=today_start,
            end_ts=now_ts,
        )
        request_metrics["today"] = {
            "total": today_metrics["total"],
            "successful": today_metrics["successful"],
            "failed": today_metrics["failed"],
            "generated_images": today_metrics["generated_images"],
            "generated_videos": today_metrics["generated_videos"],
        }
        build_sha = str(os.getenv("ADOBE2API_BUILD_SHA") or "").strip()

        return {
            "ops_api_version": OPS_API_VERSION,
            "capabilities": OPS_CAPABILITIES,
            "measured_at": now_ts,
            "instance": {
                "service": "adobe2api",
                "version": app_version,
                "build_sha": build_sha or None,
                "started_at": app_started_at,
                "uptime_seconds": max(0, int(now_ts - app_started_at)),
            },
            "requests": request_metrics,
            "tokens": {
                "total": len(tokens),
                "active": status_counts.get("active", 0),
                "status_counts": status_counts,
                "expiring_24h": expiring_24h,
                "credits_total": round(credits_total, 2),
                "credits_available": round(credits_available, 2),
            },
            "refresh_profiles": {
                "total": len(profiles),
                "failing": sum(1 for value in profile_failures if value > 0),
                "consecutive_failures_max": max(profile_failures, default=0),
            },
            "storage": get_generated_storage_stats(),
        }

    @router.get("/logs")
    def cursor_logs(
        request: Request,
        before_ts: Optional[float] = None,
        limit: int = 100,
        prompt: str = "",
        errors_only: bool = False,
    ):
        require_ops_auth(request)
        items, next_before_ts = log_store.list_cursor(
            before_ts=before_ts,
            limit=limit,
            prompt=prompt,
            errors_only=errors_only,
        )
        return {
            "ops_api_version": OPS_API_VERSION,
            "items": items,
            "next_before_ts": next_before_ts,
        }

    return router
