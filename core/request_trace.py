from __future__ import annotations

import copy
import gzip
import hashlib
import json
import os
import re
import threading
import time
import traceback
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACE_SCHEMA_VERSION = 1
MAX_TRACE_BYTES = 4 * 1024 * 1024
MAX_FIELD_BYTES = 256 * 1024
MAX_TRACE_STAGES = 1000
MAX_COLLECTION_ITEMS = 1000
MAX_NESTING_DEPTH = 20
REDACTED = "[REDACTED]"

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_SENSITIVE_KEYS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api-key",
    "apikey",
    "access-token",
    "access_token",
    "refresh-token",
    "refresh_token",
    "client-secret",
    "client_secret",
    "password",
    "secret",
    "token",
}
_BINARY_TEXT_KEYS = {
    "b64_json",
    "base64",
    "image_base64",
    "imagebase64",
}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _truncated_text(value: str) -> dict[str, Any]:
    encoded = value.encode("utf-8", errors="replace")
    return {
        "truncated": True,
        "kind": "text",
        "original_size_bytes": len(encoded),
        "sha256": _sha256_bytes(encoded),
        "preview": encoded[:MAX_FIELD_BYTES].decode("utf-8", errors="replace"),
    }


def binary_summary(
    value: bytes | bytearray | memoryview,
    *,
    content_type: Optional[str] = None,
    filename: Optional[str] = None,
) -> dict[str, Any]:
    raw = bytes(value)
    payload: dict[str, Any] = {
        "kind": "binary",
        "size_bytes": len(raw),
        "sha256": _sha256_bytes(raw),
        "omitted": True,
    }
    if content_type:
        payload["content_type"] = str(content_type)
    if filename:
        payload["filename"] = str(filename)
    return payload


def sanitize_url(value: Any) -> str:
    raw = str(value or "")
    if not raw:
        return raw
    try:
        parsed = urlsplit(raw)
        if not parsed.query:
            return raw
        query = urlencode(
            [(key, REDACTED if query_value else "") for key, query_value in parse_qsl(parsed.query, keep_blank_values=True)]
        )
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))
    except Exception:
        return raw


def _is_sensitive_key(key: Any) -> bool:
    normalized = str(key or "").strip().lower()
    return normalized in _SENSITIVE_KEYS


def _looks_like_data_image(value: str) -> bool:
    return value[:64].lower().startswith("data:image/") and ";base64," in value[:256].lower()


def sanitize_trace_value(
    value: Any,
    *,
    key: Optional[str] = None,
    depth: int = 0,
) -> Any:
    if _is_sensitive_key(key):
        return REDACTED
    if depth >= MAX_NESTING_DEPTH:
        encoded = repr(value).encode("utf-8", errors="replace")
        return {
            "truncated": True,
            "reason": "max_depth",
            "sha256": _sha256_bytes(encoded),
        }
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return binary_summary(value)
    if isinstance(value, str):
        normalized_key = str(key or "").strip().lower()
        if normalized_key in _BINARY_TEXT_KEYS or _looks_like_data_image(value):
            raw = value.encode("utf-8", errors="replace")
            return {
                "kind": "base64",
                "size_chars": len(value),
                "size_bytes": len(raw),
                "sha256": _sha256_bytes(raw),
                "omitted": True,
            }
        if len(value.encode("utf-8", errors="replace")) > MAX_FIELD_BYTES:
            return _truncated_text(value)
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        items = list(value.items())
        for item_key, item_value in items[:MAX_COLLECTION_ITEMS]:
            text_key = str(item_key)
            result[text_key] = sanitize_trace_value(
                item_value,
                key=text_key,
                depth=depth + 1,
            )
        if len(items) > MAX_COLLECTION_ITEMS:
            result["__truncated_items__"] = {
                "truncated": True,
                "original_count": len(items),
                "kept_count": MAX_COLLECTION_ITEMS,
            }
        return result
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        result = [
            sanitize_trace_value(item, depth=depth + 1)
            for item in items[:MAX_COLLECTION_ITEMS]
        ]
        if len(items) > MAX_COLLECTION_ITEMS:
            result.append(
                {
                    "truncated": True,
                    "original_count": len(items),
                    "kept_count": MAX_COLLECTION_ITEMS,
                }
            )
        return result
    return sanitize_trace_value(str(value), key=key, depth=depth + 1)


def sanitize_headers(headers: Any) -> dict[str, Any]:
    if headers is None:
        return {}
    try:
        values = dict(headers)
    except Exception:
        return {"value": sanitize_trace_value(str(headers))}
    return sanitize_trace_value(values)


def response_snapshot(response: Any, *, include_body: bool = True) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "status_code": int(getattr(response, "status_code", 0) or 0),
        "headers": sanitize_headers(getattr(response, "headers", {})),
    }
    if not include_body:
        return snapshot

    content = getattr(response, "content", None)
    text_value = str(getattr(response, "text", "") or "")
    try:
        parsed = response.json()
    except Exception:
        parsed = None
    if parsed is not None:
        snapshot["body"] = sanitize_trace_value(parsed)
    elif text_value:
        snapshot["body"] = sanitize_trace_value(text_value)
    elif isinstance(content, (bytes, bytearray, memoryview)) and content:
        snapshot["body"] = binary_summary(
            content,
            content_type=str(snapshot["headers"].get("content-type") or "") or None,
        )
    else:
        snapshot["body"] = None
    return snapshot


def exception_snapshot(error: BaseException | str) -> dict[str, Any]:
    if not isinstance(error, BaseException):
        return {"message": str(error or "")}

    chain: list[dict[str, Any]] = []
    seen: set[int] = set()
    current: Optional[BaseException] = error
    relation = "raised"
    while current is not None and id(current) not in seen and len(chain) < 12:
        seen.add(id(current))
        chain.append(
            {
                "relation": relation,
                "class": type(current).__name__,
                "message": str(current),
                "traceback": "".join(
                    traceback.format_exception(type(current), current, current.__traceback__)
                ),
            }
        )
        if current.__cause__ is not None:
            current = current.__cause__
            relation = "cause"
        elif current.__context__ is not None and not current.__suppress_context__:
            current = current.__context__
            relation = "context"
        else:
            current = None
    return sanitize_trace_value(
        {
            "class": type(error).__name__,
            "message": str(error),
            "chain": chain,
        }
    )


def get_request_trace(request: Any) -> Optional["RequestTrace"]:
    return getattr(getattr(request, "state", None), "request_trace", None)


class RequestTrace:
    def __init__(self, *, log_id: str, method: str, path: str) -> None:
        self.log_id = str(log_id or "")
        self.method = str(method or "").upper()
        self.path = str(path or "")
        self.started_at = time.time()
        self._started_perf = time.perf_counter()
        self._lock = threading.RLock()
        self._sequence = 0
        self._stages: list[dict[str, Any]] = []
        self._stage_by_id: dict[str, dict[str, Any]] = {}
        self._poll_groups: dict[str, tuple[str, str]] = {}
        self._dropped_stages = 0
        self._truncated = False

    def _elapsed_ms(self) -> float:
        return round((time.perf_counter() - self._started_perf) * 1000.0, 3)

    def start_stage(
        self,
        *,
        layer: str,
        kind: str,
        name: str,
        parent_id: Optional[str] = None,
        attempt: Any = None,
        request: Any = None,
        details: Any = None,
    ) -> Optional[str]:
        with self._lock:
            if len(self._stages) >= MAX_TRACE_STAGES:
                self._dropped_stages += 1
                self._truncated = True
                return None
            self._sequence += 1
            stage_id = f"s{self._sequence}-{uuid.uuid4().hex[:6]}"
            stage: dict[str, Any] = {
                "id": stage_id,
                "parent_id": str(parent_id) if parent_id else None,
                "seq": self._sequence,
                "layer": str(layer or "service"),
                "kind": str(kind or "stage"),
                "name": str(name or "stage"),
                "status": "running",
                "started_at": time.time(),
                "offset_ms": self._elapsed_ms(),
            }
            if attempt is not None:
                stage["attempt"] = sanitize_trace_value(attempt)
            if request is not None:
                stage["request"] = sanitize_trace_value(request)
            if details is not None:
                stage["details"] = sanitize_trace_value(details)
            self._stages.append(stage)
            self._stage_by_id[stage_id] = stage
            return stage_id

    def finish_stage(
        self,
        stage_id: Optional[str],
        *,
        status: str = "succeeded",
        response: Any = None,
        error: BaseException | str | dict | None = None,
        details: Any = None,
        aggregate: Any = None,
    ) -> None:
        if not stage_id:
            return
        with self._lock:
            stage = self._stage_by_id.get(stage_id)
            if stage is None:
                return
            finished_at = time.time()
            stage["status"] = str(status or "succeeded")
            stage["finished_at"] = finished_at
            stage["duration_ms"] = round(
                max(0.0, finished_at - float(stage.get("started_at") or finished_at))
                * 1000.0,
                3,
            )
            if response is not None:
                stage["response"] = sanitize_trace_value(response)
            if error is not None:
                stage["error"] = (
                    sanitize_trace_value(error)
                    if isinstance(error, dict)
                    else exception_snapshot(error)
                )
            if details is not None:
                existing = stage.get("details")
                if isinstance(existing, dict) and isinstance(details, Mapping):
                    merged = dict(existing)
                    merged.update(sanitize_trace_value(details))
                    stage["details"] = merged
                else:
                    stage["details"] = sanitize_trace_value(details)
            if aggregate is not None:
                stage["aggregate"] = sanitize_trace_value(aggregate)

    def add_stage(
        self,
        *,
        layer: str,
        kind: str,
        name: str,
        status: str = "succeeded",
        parent_id: Optional[str] = None,
        attempt: Any = None,
        request: Any = None,
        response: Any = None,
        error: BaseException | str | dict | None = None,
        details: Any = None,
    ) -> Optional[str]:
        stage_id = self.start_stage(
            layer=layer,
            kind=kind,
            name=name,
            parent_id=parent_id,
            attempt=attempt,
            request=request,
            details=details,
        )
        self.finish_stage(stage_id, status=status, response=response, error=error)
        return stage_id

    def record_poll(
        self,
        *,
        parent_id: Optional[str],
        status_key: str,
        request: Any,
        response: Any,
        duration_ms: float,
        failed: bool = False,
    ) -> Optional[str]:
        group_key = str(parent_id or "root")
        safe_response = sanitize_trace_value(response)
        with self._lock:
            previous = self._poll_groups.get(group_key)
            if previous and previous[0] == str(status_key):
                stage = self._stage_by_id.get(previous[1])
                if stage is not None:
                    aggregate = stage.setdefault(
                        "aggregate",
                        {
                            "count": 1,
                            "first_response": copy.deepcopy(stage.get("response")),
                            "last_response": copy.deepcopy(stage.get("response")),
                            "total_duration_ms": float(stage.get("duration_ms") or 0.0),
                        },
                    )
                    aggregate["count"] = int(aggregate.get("count") or 1) + 1
                    aggregate["last_response"] = copy.deepcopy(safe_response)
                    aggregate["total_duration_ms"] = round(
                        float(aggregate.get("total_duration_ms") or 0.0)
                        + float(duration_ms or 0.0),
                        3,
                    )
                    stage["response"] = safe_response
                    stage["finished_at"] = time.time()
                    stage["duration_ms"] = aggregate["total_duration_ms"]
                    stage["status"] = "failed" if failed else "succeeded"
                    return str(stage.get("id") or "") or None

        stage_id = self.start_stage(
            layer="adobe",
            kind="poll",
            name="Adobe task poll",
            parent_id=parent_id,
            request=request,
            details={"status_key": str(status_key)},
        )
        if stage_id:
            with self._lock:
                stage = self._stage_by_id.get(stage_id)
                if stage is not None:
                    stage["response"] = safe_response
                    stage["status"] = "failed" if failed else "succeeded"
                    stage["finished_at"] = time.time()
                    stage["duration_ms"] = round(float(duration_ms or 0.0), 3)
                    stage["aggregate"] = {
                        "count": 1,
                        "first_response": copy.deepcopy(safe_response),
                        "last_response": copy.deepcopy(safe_response),
                        "total_duration_ms": round(float(duration_ms or 0.0), 3),
                    }
                self._poll_groups[group_key] = (str(status_key), stage_id)
        return stage_id

    @staticmethod
    def _summarize_large_value(value: Any) -> dict[str, Any]:
        encoded = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        return {
            "truncated": True,
            "kind": "structured_data",
            "original_size_bytes": len(encoded),
            "sha256": _sha256_bytes(encoded),
        }

    @classmethod
    def _fit_trace_size(cls, payload: dict[str, Any]) -> dict[str, Any]:
        def encoded_size() -> int:
            return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))

        original_size = encoded_size()
        if original_size <= MAX_TRACE_BYTES:
            return payload

        payload["truncated"] = True
        payload["original_size_bytes"] = original_size
        # Account for this field while shrinking so the final metadata cannot
        # push an otherwise fitted trace back over the configured limit.
        payload["stored_size_bytes"] = 0
        stages = payload.get("stages") if isinstance(payload.get("stages"), list) else []
        final_adobe_response_id = next(
            (
                str(stage.get("id") or "")
                for stage in reversed(stages)
                if stage.get("layer") == "adobe" and stage.get("response") is not None
            ),
            "",
        )
        final_error_id = next(
            (
                str(stage.get("id") or "")
                for stage in reversed(stages)
                if stage.get("error") is not None
            ),
            "",
        )
        for stage in stages:
            if encoded_size() <= MAX_TRACE_BYTES:
                break
            if stage.get("status") == "failed" or stage.get("kind") == "exception":
                continue
            for key in ("response", "request", "details"):
                if key in stage:
                    stage[key] = cls._summarize_large_value(stage[key])
        for stage in stages:
            if encoded_size() <= MAX_TRACE_BYTES:
                break
            if stage.get("status") == "failed" or stage.get("kind") == "exception":
                continue
            aggregate = stage.get("aggregate")
            if isinstance(aggregate, dict):
                for key in ("first_response", "last_response"):
                    if key in aggregate:
                        aggregate[key] = cls._summarize_large_value(aggregate[key])
        for stage in stages:
            if encoded_size() <= MAX_TRACE_BYTES:
                break
            stage_id = str(stage.get("id") or "")
            for key in ("request", "details", "aggregate"):
                if key in stage:
                    stage[key] = cls._summarize_large_value(stage[key])
            if stage_id != final_adobe_response_id and "response" in stage:
                stage["response"] = cls._summarize_large_value(stage["response"])
            if stage_id != final_error_id and "error" in stage:
                stage["error"] = cls._summarize_large_value(stage["error"])
        if encoded_size() > MAX_TRACE_BYTES:
            for stage in stages:
                if encoded_size() <= MAX_TRACE_BYTES:
                    break
                stage_id = str(stage.get("id") or "")
                if stage_id in {final_adobe_response_id, final_error_id}:
                    continue
                preserved = {
                    key: stage.get(key)
                    for key in (
                        "id",
                        "parent_id",
                        "seq",
                        "layer",
                        "kind",
                        "name",
                        "status",
                        "started_at",
                        "finished_at",
                        "offset_ms",
                        "duration_ms",
                    )
                    if key in stage
                }
                preserved["data_omitted"] = True
                stage.clear()
                stage.update(preserved)
        for _ in range(3):
            payload["stored_size_bytes"] = encoded_size()
        return payload

    def finalize(
        self,
        *,
        outcome: str,
        final_error: BaseException | str | dict | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            finished_at = time.time()
            for stage in self._stages:
                if stage.get("status") == "running":
                    stage["status"] = "interrupted"
                    stage["finished_at"] = finished_at
                    stage["duration_ms"] = round(
                        max(
                            0.0,
                            finished_at - float(stage.get("started_at") or finished_at),
                        )
                        * 1000.0,
                        3,
                    )
            payload: dict[str, Any] = {
                "schema_version": TRACE_SCHEMA_VERSION,
                "log_id": self.log_id,
                "method": self.method,
                "path": self.path,
                "outcome": str(outcome or "failed"),
                "started_at": self.started_at,
                "finished_at": finished_at,
                "duration_ms": round(
                    max(0.0, finished_at - self.started_at) * 1000.0,
                    3,
                ),
                "truncated": bool(self._truncated),
                "dropped_stages": int(self._dropped_stages),
                "stages": copy.deepcopy(self._stages),
            }
            if final_error is not None:
                payload["final_error"] = (
                    sanitize_trace_value(final_error)
                    if isinstance(final_error, dict)
                    else exception_snapshot(final_error)
                )
        return self._fit_trace_size(payload)


class RequestTraceStore:
    def __init__(self, directory: Path, *, max_items: int = 5000) -> None:
        self._directory = Path(directory)
        self._max_items = max(200, int(max_items or 5000))
        self._lock = threading.Lock()
        self._directory.mkdir(parents=True, exist_ok=True)

    def _path_for(self, log_id: str) -> Optional[Path]:
        safe_id = str(log_id or "").strip()
        if not _SAFE_ID_RE.fullmatch(safe_id):
            return None
        return self._directory / f"{safe_id}.json.gz"

    def _prune_locked(self) -> None:
        files = sorted(
            self._directory.glob("*.json.gz"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for stale_path in files[self._max_items :]:
            try:
                stale_path.unlink()
            except FileNotFoundError:
                pass

    def save(self, log_id: str, payload: dict[str, Any]) -> bool:
        target = self._path_for(log_id)
        if target is None:
            return False
        temp_path = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        with self._lock:
            try:
                with gzip.open(temp_path, "wb", compresslevel=6) as stream:
                    stream.write(encoded)
                os.replace(temp_path, target)
                self._prune_locked()
                return True
            finally:
                try:
                    if temp_path.exists():
                        temp_path.unlink()
                except Exception:
                    pass

    def get(self, log_id: str) -> Optional[dict[str, Any]]:
        target = self._path_for(log_id)
        if target is None or not target.exists():
            return None
        try:
            with self._lock:
                with gzip.open(target, "rt", encoding="utf-8") as stream:
                    payload = json.load(stream)
            return payload if isinstance(payload, dict) else None
        except (OSError, ValueError, json.JSONDecodeError):
            return None
