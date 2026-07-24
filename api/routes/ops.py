import os
import re
import time
from datetime import datetime
from typing import Any, Callable, Optional

from fastapi import APIRouter, Query, Request


OPS_API_VERSION = 1
OPS_CAPABILITIES = [
    "snapshot",
    "cursor_logs",
    "tokens",
    "accounts",
    "refresh_profiles",
    "config",
    "generated_media",
    "image_queue",
]


def _credit_value(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_error(value: Any) -> str:
    text = str(value or "")[:1000]
    text = re.sub(
        r"(?i)(cookie|authorization|access_token|refresh_token)\s*[:=]\s*[^\s,;\"'}]+",
        r"\1=[redacted]",
        text,
    )
    text = re.sub(
        r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}(?:\.[A-Za-z0-9_-]{10,})?\b",
        "[redacted]",
        text,
    )
    text = re.sub(r"\b[A-Za-z0-9_=-]{64,}\b", "[redacted]", text)
    return text


def build_account_health(
    token_manager,
    refresh_manager,
    threshold: float,
    *,
    tokens: Optional[list[dict[str, Any]]] = None,
    profiles: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    safe_threshold = max(0.0, min(float(threshold), 1_000_000_000.0))
    tokens_by_profile: dict[str, dict[str, Any]] = {}
    token_items = tokens if tokens is not None else token_manager.list_all()
    profile_items = profiles if profiles is not None else refresh_manager.list_profiles()
    for token in token_items:
        profile_id = str(token.get("refresh_profile_id") or "").strip()
        if profile_id and bool(token.get("auto_refresh")):
            tokens_by_profile[profile_id] = token

    items: list[dict[str, Any]] = []
    for profile in profile_items:
        profile_id = str(profile.get("id") or "").strip()
        account = profile.get("account") if isinstance(profile.get("account"), dict) else {}
        state = profile.get("state") if isinstance(profile.get("state"), dict) else {}
        token = tokens_by_profile.get(profile_id)
        enabled = bool(profile.get("enabled", True))
        try:
            consecutive_failures = int(state.get("consecutive_failures") or 0)
        except (TypeError, ValueError):
            consecutive_failures = 0
        token_status = str((token or {}).get("status") or "missing").strip().lower()
        credits_available = _credit_value((token or {}).get("credits_available"))
        credits_total = _credit_value((token or {}).get("credits_total"))
        is_low_credit = (
            credits_available is not None and credits_available < safe_threshold
        )

        if not enabled:
            health = "disabled"
        elif consecutive_failures > 0:
            health = "refresh_failed"
        elif token_status in {"disabled", "exhausted", "invalid", "error"}:
            health = "credential_error"
        elif token is None or credits_available is None:
            health = "balance_unknown"
        elif is_low_credit:
            health = "low_credit"
        else:
            health = "healthy"

        items.append(
            {
                "id": profile_id,
                "name": str(profile.get("name") or profile_id),
                "display_name": str(account.get("display_name") or "").strip(),
                "email": str(account.get("email") or "").strip(),
                "user_id": str(account.get("user_id") or "").strip(),
                "enabled": enabled,
                "health": health,
                "low_credit": is_low_credit,
                "credits_available": credits_available,
                "credits_total": credits_total,
                "credits_updated_at": (token or {}).get("credits_updated_at"),
                "credential_status": token_status,
                "credential_expires_at": (token or {}).get("expires_at"),
                "consecutive_failures": consecutive_failures,
                "last_attempt_at": state.get("last_attempt_at"),
                "last_success_at": state.get("last_success_at"),
                "next_refresh_at": state.get("next_retry_at"),
                "last_error": _safe_error(state.get("last_error")),
                "imported_at": profile.get("imported_at"),
            }
        )

    items.sort(
        key=lambda item: (
            item["credits_available"] is None,
            item["credits_available"] if item["credits_available"] is not None else 0,
            str(item.get("name") or "").lower(),
        )
    )
    known_available = [item for item in items if item["credits_available"] is not None]
    known_total = [item for item in items if item["credits_total"] is not None]
    summary = {
        "total": len(items),
        "available": sum(
            1
            for item in items
            if item["enabled"] and item["credential_status"] == "active"
        ),
        "low_credit": sum(1 for item in items if item["low_credit"]),
        "balance_unknown": sum(1 for item in items if item["health"] == "balance_unknown"),
        "refresh_failing": sum(1 for item in items if item["health"] == "refresh_failed"),
        "credential_error": sum(1 for item in items if item["health"] == "credential_error"),
        "credits_available": round(
            sum(float(item["credits_available"]) for item in known_available), 2
        ),
        "credits_total": round(
            sum(float(item["credits_total"]) for item in known_total), 2
        ),
        "low_credit_threshold": safe_threshold,
    }
    return {"items": items, "summary": summary}


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
    def snapshot(
        request: Request,
        low_credit_threshold: float = Query(default=100.0, ge=0, le=1_000_000_000),
    ):
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
        account_health = build_account_health(
            token_manager,
            refresh_manager,
            low_credit_threshold,
            tokens=tokens,
            profiles=profiles,
        )
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
            "accounts": account_health["summary"],
            "storage": get_generated_storage_stats(),
        }

    @router.get("/accounts")
    def accounts(
        request: Request,
        low_credit_threshold: float = Query(default=100.0, ge=0, le=1_000_000_000),
    ):
        require_ops_auth(request)
        payload = build_account_health(
            token_manager, refresh_manager, low_credit_threshold
        )
        return {
            "ops_api_version": OPS_API_VERSION,
            "capabilities": OPS_CAPABILITIES,
            **payload,
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
