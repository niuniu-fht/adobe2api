from concurrent.futures import ThreadPoolExecutor, as_completed
import asyncio
import base64
import binascii
import io
import json
import re
import secrets
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
import requests
from starlette.concurrency import run_in_threadpool

try:
    from PIL import Image
except Exception:
    Image = None

from api.schemas import GenerateRequest
from core.entity_store import entity_store
from core.adobe_client import (
    AdobeRequestError,
    ContentPolicyError,
    ReferenceImageRequiredError,
)
from core.config_mgr import config_manager
from core.models.openai_images import (
    OpenAIImageRequestError,
    build_legacy_image_options,
    build_native_gpt_image_options,
    encode_image_response_item,
    is_native_gpt_image_model,
    normalize_openai_gemini_model_id,
)
from core.models.gemini import (
    GEMINI_IMAGE_MODELS,
    GEMINI_MODEL_ALIASES,
    GeminiRequestError,
    build_gemini_generate_response,
    gemini_model_resource,
    gemini_model_resources,
    normalize_gemini_model_id,
    parse_gemini_generate_request,
)
from core.models.payloads import (
    gpt_image_pixels_from_ratio,
    random_image_seed,
    size_from_ratio,
)
from core.models.image_limits import (
    MAX_INPUT_IMAGES,
    MAX_SINGLE_IMAGE_BYTES,
    MAX_TOTAL_IMAGE_BYTES,
    ImageInputLimitError,
    add_input_image_bytes,
    validate_input_image_count,
)
from core.request_trace import (
    binary_summary,
    get_request_trace,
    sanitize_trace_value,
)


def generate_with_reference_recovery(
    *,
    source_image_ids: list[str],
    expected_image_count: int,
    generate_with_ids: Callable[[list[str]], list[dict]],
    reupload_all: Callable[[], list[str]],
    cancel_check: Callable[[], None],
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[dict], list[str]]:
    try:
        return generate_with_ids(source_image_ids), source_image_ids
    except ReferenceImageRequiredError:
        for delay in (0.5, 1.0, 2.0):
            cancel_check()
            sleep(delay)
            try:
                return generate_with_ids(source_image_ids), source_image_ids
            except ReferenceImageRequiredError:
                continue

    replacement_ids = reupload_all()
    if len(replacement_ids) != expected_image_count or any(
        not str(image_id or "").strip() for image_id in replacement_ids
    ):
        raise AdobeRequestError(
            "reference image re-upload incomplete; generation was not started"
        )
    return generate_with_ids(replacement_ids), replacement_ids


def build_generation_router(
    *,
    store,
    token_manager,
    client,
    image_task_coordinator,
    generated_dir: Path,
    model_catalog: dict,
    video_model_catalog: dict,
    supported_ratios: set,
    resolve_model: Callable[[str | None], dict],
    resolve_ratio_and_resolution: Callable[[dict, str | None], tuple[str, str, str]],
    require_service_api_key: Callable[[Request], None],
    set_request_task_progress: Callable[..., None],
    run_with_token_retries: Callable[..., Any],
    set_request_error_detail: Callable[..., str],
    set_request_preview: Callable[[Request, str, str], None],
    public_image_url: Callable[[Request, str], str],
    public_generated_url: Callable[[Request, str], str],
    resolve_video_options: Callable[[dict], tuple[bool, str, str]],
    load_input_images: Callable[[Any], list[tuple[bytes, str]]],
    prepare_video_source_image: Callable[[bytes, str, str], tuple[bytes, str]],
    video_ext_from_meta: Callable[[dict], str],
    extract_prompt_from_messages: Callable[[Any], str],
    sse_chat_stream: Callable[[dict], Any],
    on_generated_file_written: Callable[[Path, int, int], None],
    quota_error_cls,
    auth_error_cls,
    upstream_temp_error_cls,
    logger,
) -> APIRouter:
    router = APIRouter()
    entity_ref_re = re.compile(r"@entity:([^\s@]+)")
    remote_image_error_message = "输入图片下载失败，请确认图片 URL 可公开访问"

    def _image_config_int(key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(config_manager.get(key, default) or default)
        except Exception:
            value = default
        return max(minimum, min(maximum, value))

    def _register_image_queue(
        request: Request,
        *,
        model: str,
        prompt: str,
        output_count: int,
    ) -> str:
        queue_id = image_task_coordinator.register_request(
            log_id=str(getattr(request.state, "log_id", "") or ""),
            path=str(request.url.path),
            model=model,
            prompt_preview=prompt.replace("\r", " ").replace("\n", " ")[:180],
            output_count=output_count,
        )
        request.state.image_queue_id = queue_id
        request.state.generated_output_paths = []
        request.state.generated_output_paths_lock = threading.Lock()
        request.state.image_temp_dirs = []
        return queue_id

    def _track_generated_path(request: Request, path: Path) -> None:
        lock = getattr(request.state, "generated_output_paths_lock", None)
        paths = getattr(request.state, "generated_output_paths", None)
        if not isinstance(paths, list):
            paths = []
            request.state.generated_output_paths = paths
        if lock is None:
            paths.append(path)
            return
        with lock:
            paths.append(path)

    def _cleanup_generated_paths(request: Request) -> None:
        paths = getattr(request.state, "generated_output_paths", None)
        if not isinstance(paths, list):
            return
        for path in list(paths):
            try:
                old_size = int(Path(path).stat().st_size) if Path(path).exists() else 0
                Path(path).unlink(missing_ok=True)
                Path(f"{path}.part").unlink(missing_ok=True)
                if old_size > 0:
                    on_generated_file_written(Path(path), old_size, 0)
            except Exception:
                pass
        paths.clear()

    def _cleanup_image_temp_dirs(request: Request) -> None:
        temp_dirs = getattr(request.state, "image_temp_dirs", None)
        if not isinstance(temp_dirs, list):
            return
        for path in list(temp_dirs):
            try:
                shutil.rmtree(Path(path), ignore_errors=True)
            except Exception:
                pass
        temp_dirs.clear()

    def _spool_edit_source_images(
        request: Request,
        input_images: list[tuple[bytes, str]],
    ) -> list[tuple[Path, str]]:
        if not input_images:
            return []
        queue_id = str(getattr(request.state, "image_queue_id", "") or "image")
        temp_dir = Path(tempfile.mkdtemp(prefix=f"adobe2api-{queue_id[:16]}-"))
        request.state.image_temp_dirs.append(temp_dir)
        results: list[tuple[Path, str]] = []
        suffixes = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
        }
        for index, (image_bytes, mime_type) in enumerate(input_images):
            normalized_mime = str(mime_type or "image/jpeg").lower()
            final_path = temp_dir / f"reference-{index}{suffixes.get(normalized_mime, '.img')}"
            part_path = final_path.with_name(f"{final_path.name}.part")
            part_path.write_bytes(image_bytes)
            part_path.replace(final_path)
            results.append((final_path, normalized_mime))
        return results

    def _finish_image_queue(
        request: Request, *, succeeded: bool, error: Any = None
    ) -> None:
        queue_id = str(getattr(request.state, "image_queue_id", "") or "")
        if not queue_id:
            return
        if not succeeded:
            image_task_coordinator.cancel_request(queue_id, error)
            _cleanup_generated_paths(request)
        _cleanup_image_temp_dirs(request)
        image_task_coordinator.finish_request(
            queue_id, succeeded=succeeded, error=error
        )

    async def _watch_image_disconnect(request: Request, queue_id: str) -> None:
        while True:
            if await request.is_disconnected():
                image_task_coordinator.cancel_request(
                    queue_id, "client disconnected"
                )
                return
            await asyncio.sleep(0.5)

    def _start_image_operation(request: Request, name: str) -> Any:
        trace = get_request_trace(request)
        if trace is None:
            return None
        stage_id = trace.start_stage(
            layer="service",
            kind="operation",
            name=name,
            parent_id=getattr(request.state, "trace_request_stage_id", None),
        )
        request.state.trace_operation_stage_id = stage_id
        return stage_id

    def _trace_auth(request: Request) -> None:
        trace = get_request_trace(request)
        if trace is None:
            require_service_api_key(request)
            return
        stage_id = trace.start_stage(
            layer="service",
            kind="authentication",
            name="校验服务 API Key",
            parent_id=getattr(request.state, "trace_operation_stage_id", None),
        )
        try:
            require_service_api_key(request)
        except Exception as exc:
            request.state.trace_final_error = exc
            trace.finish_stage(stage_id, status="failed", error=exc)
            if isinstance(exc, HTTPException):
                _remember_trace_response(
                    request,
                    int(exc.status_code or 500),
                    {"detail": exc.detail},
                    error=exc,
                )
            raise
        trace.finish_stage(stage_id, status="succeeded")

    def _remember_trace_response(
        request: Request,
        status_code: int,
        content: Any,
        *,
        error: Any = None,
    ) -> None:
        payload = {
            "status_code": int(status_code),
            "headers": {"content-type": "application/json"},
            "body": sanitize_trace_value(content),
        }
        request.state.trace_response_payload = payload
        effective_error = error
        if effective_error is None and isinstance(content, dict):
            error_obj = content.get("error")
            if isinstance(error_obj, dict):
                effective_error = error_obj.get("message") or error_obj
            elif error_obj:
                effective_error = error_obj
        if effective_error is not None:
            request.state.trace_final_error = effective_error
        if int(status_code) >= 400:
            _finish_image_queue(
                request,
                succeeded=False,
                error=effective_error or f"HTTP {status_code}",
            )
        trace = get_request_trace(request)
        if trace is not None:
            trace.finish_stage(
                getattr(request.state, "trace_operation_stage_id", None),
                status="failed" if int(status_code) >= 400 else "succeeded",
                response=payload,
                error=effective_error if int(status_code) >= 400 else None,
            )

    def _traced_json_response(
        request: Request,
        *,
        status_code: int,
        content: Any,
        error: Any = None,
    ) -> JSONResponse:
        _remember_trace_response(
            request,
            status_code,
            content,
            error=error,
        )
        return JSONResponse(status_code=status_code, content=content)

    def _remember_existing_response(
        request: Request,
        response: JSONResponse,
        *,
        error: Any = None,
    ) -> JSONResponse:
        try:
            body = json.loads(response.body.decode("utf-8"))
        except Exception:
            body = response.body.decode("utf-8", errors="replace")
        _remember_trace_response(
            request,
            int(response.status_code),
            body,
            error=error,
        )
        return response

    def _nanoid(size: int = 21) -> str:
        alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_"
        return "".join(secrets.choice(alphabet) for _ in range(size))

    def _entity_name(item: dict) -> str:
        entity_value = item.get("entityValue")
        if isinstance(entity_value, dict):
            name = str(entity_value.get("displayName") or "").strip()
            if name:
                return name
        return str(item.get("name") or item.get("displayName") or "").strip()

    def _entity_urn(item: dict) -> str:
        for key in ("id", "urn", "entityId", "entityUrn"):
            val = str(item.get(key) or "").strip()
            if val:
                return val
        entity = item.get("entity")
        if isinstance(entity, dict):
            return _entity_urn(entity)
        return ""

    def _is_gpt_image_model_or_alias(model_id: str | None) -> bool:
        model_id = str(model_id or "").strip()
        if normalize_openai_gemini_model_id(model_id):
            return False
        if is_native_gpt_image_model(model_id):
            return True
        return bool(
            model_id
            and model_id not in model_catalog
            and client.is_gpt_image_model_alias(model_id)
        )

    def _gpt_image_quality_for_model(
        model_conf: dict, model_id: str | None
    ) -> str | None:
        if str(model_conf.get("upstream_model_id") or "") != "gpt-image":
            return None
        return str(
            model_conf.get("gpt_image_quality")
            or client.get_gpt_image_quality(model_id)
            or client.gpt_image_quality
        )

    def _build_gpt_image_alias_options(data: dict, model_id: str | None):
        response_model = str(model_id or "gpt-image-2").strip() or "gpt-image-2"
        image_options = build_native_gpt_image_options(
            data,
            model_id_override="gpt-image-2",
            response_model=response_model,
            upstream_model_version="2",
        )
        model_conf = {
            "upstream_model_id": image_options.upstream_model_id,
            "upstream_model_version": image_options.upstream_model_version,
            "gpt_image_quality": client.get_gpt_image_quality(response_model),
        }
        return image_options, model_conf, response_model

    def _entity_names_from_prompt(raw_prompt: str) -> list[str]:
        matches = list(entity_ref_re.finditer(raw_prompt or ""))
        names: list[str] = []
        for match in matches:
            name = match.group(1).strip()
            if name and name not in names:
                names.append(name)
        return names

    def _sync_entity_by_name(name: str) -> list[dict]:
        found: list[dict] = []
        for token_info in token_manager.list_active_account_tokens():
            token = str(token_info.get("token") or "").strip()
            account_id = str(token_info.get("account_id") or "").strip()
            if not token or not account_id:
                continue
            try:
                entities = client.list_entities(token, limit=100)
            except Exception:
                continue
            for item in entities:
                item_name = _entity_name(item)
                if item_name != name:
                    continue
                urn = _entity_urn(item)
                if not urn:
                    continue
                found.append(
                    entity_store.upsert(
                        entity_id=urn,
                        name=item_name,
                        entity_type=str(item.get("entityType") or item.get("type") or ""),
                        account_id=account_id,
                        account_name=str(token_info.get("account_name") or ""),
                        account_email=str(token_info.get("account_email") or ""),
                    )
                )
        return found

    def _resolve_entity_bindings(raw_prompt: str) -> tuple[str, list[dict]]:
        refs: list[dict] = []
        account_id = ""
        for name in _entity_names_from_prompt(raw_prompt):
            matches = entity_store.find_by_name(name)
            if not matches:
                matches = _sync_entity_by_name(name)
            account_ids = {
                str(item.get("account_id") or "").strip()
                for item in matches
                if str(item.get("account_id") or "").strip()
            }
            if not matches:
                raise HTTPException(status_code=400, detail=f"entity not found: {name}")
            if len(account_ids) > 1:
                raise HTTPException(
                    status_code=400,
                    detail=f"entity name is ambiguous across accounts: {name}",
                )
            if len(matches) > 1 and len({str(item.get("id") or "") for item in matches}) > 1:
                raise HTTPException(
                    status_code=400,
                    detail=f"entity name is ambiguous: {name}",
                )
            current_account = next(iter(account_ids), "")
            if not current_account:
                raise HTTPException(status_code=400, detail=f"entity has no account: {name}")
            if account_id and account_id != current_account:
                raise HTTPException(
                    status_code=400,
                    detail="entities in one prompt must belong to the same Adobe account",
                )
            account_id = current_account
            refs.append(
                {
                    "name": name,
                    "urn": str(matches[0].get("id") or "").strip(),
                    "account_id": account_id,
                }
            )
        return account_id, refs

    def _resolve_kling_entity_refs(
        token: str,
        raw_prompt: str,
        bound_refs: list[dict] | None = None,
    ) -> tuple[str, list[dict]]:
        matches = list(entity_ref_re.finditer(raw_prompt or ""))
        if not matches:
            return raw_prompt, []
        if bound_refs is not None:
            by_name = {str(item.get("name") or "").strip(): item for item in bound_refs}
        else:
            entities = client.list_entities(token, limit=100)
            by_name = {_entity_name(item): item for item in entities if _entity_name(item)}
        refs: list[dict] = []
        replacements: dict[str, str] = {}
        for match in matches:
            name = match.group(1).strip()
            if name in replacements:
                continue
            item = by_name.get(name)
            if not item:
                raise HTTPException(status_code=400, detail=f"entity not found: {name}")
            urn = str(item.get("urn") or "").strip() if bound_refs is not None else _entity_urn(item)
            if not urn:
                raise HTTPException(status_code=400, detail=f"entity has no urn: {name}")
            mention_id = _nanoid()
            replacements[name] = mention_id
            refs.append({"name": name, "urn": urn, "mention_id": mention_id})

        def replace_match(match: re.Match) -> str:
            return f"@{replacements[match.group(1).strip()]}"

        return entity_ref_re.sub(replace_match, raw_prompt), refs

    @router.get("/v1/models")
    def list_models(request: Request):
        require_service_api_key(request)
        data = []
        for model_id, conf in model_catalog.items():
            data.append(
                {
                    "id": model_id,
                    "object": "model",
                    "owned_by": "adobe2api",
                    "description": conf["description"],
                }
            )
        for model_id, conf in video_model_catalog.items():
            if bool(conf.get("hidden", False)):
                continue
            data.append(
                {
                    "id": model_id,
                    "object": "model",
                    "owned_by": "adobe2api",
                    "description": conf["description"],
                }
            )
        data.append(
            {
                "id": "gpt-image-2",
                "object": "model",
                "owned_by": "adobe2api",
                "description": "OpenAI Images compatible alias for Firefly GPT Image 2",
            }
        )
        for model_id, quality in client.gpt_image_model_qualities.items():
            if model_id == "gpt-image-2":
                continue
            data.append(
                {
                    "id": model_id,
                    "object": "model",
                    "owned_by": "adobe2api",
                    "description": f"Custom OpenAI Images alias for gpt-image-2 ({quality})",
                }
            )
        compatible_models = {
            **{
                model_id: conf["description"]
                for model_id, conf in GEMINI_IMAGE_MODELS.items()
            },
            **{
                alias: GEMINI_IMAGE_MODELS[canonical_id]["description"]
                for alias, canonical_id in GEMINI_MODEL_ALIASES.items()
            },
            **{
                f"gpt-image-{model_id}": (
                    f"OpenAI Images compatible alias for {conf['display_name']}"
                )
                for model_id, conf in GEMINI_IMAGE_MODELS.items()
            },
        }
        existing_ids = {item["id"] for item in data}
        for model_id, description in compatible_models.items():
            if model_id in existing_ids:
                continue
            data.append(
                {
                    "id": model_id,
                    "object": "model",
                    "owned_by": "google",
                    "description": description,
                }
            )
        return {"object": "list", "data": data}

    @router.get("/v1beta/models")
    def list_gemini_models(request: Request, pageSize: int | None = None):
        require_service_api_key(request)
        models = gemini_model_resources()
        if pageSize is not None:
            models = models[: max(1, min(int(pageSize), 1000))]
        return {"models": models}

    @router.get("/v1beta/models/{model_id}")
    def get_gemini_model(model_id: str, request: Request):
        require_service_api_key(request)
        resource = gemini_model_resource(model_id)
        if resource is None:
            return _gemini_error_response(404, f"model not found: {model_id}")
        return resource

    def _gemini_error_status(status_code: int) -> str:
        return {
            400: "INVALID_ARGUMENT",
            401: "UNAUTHENTICATED",
            403: "PERMISSION_DENIED",
            404: "NOT_FOUND",
            429: "RESOURCE_EXHAUSTED",
            500: "INTERNAL",
            502: "UNAVAILABLE",
            503: "UNAVAILABLE",
            504: "DEADLINE_EXCEEDED",
        }.get(int(status_code), "UNKNOWN")

    def _gemini_error_response(status_code: int, message: str) -> JSONResponse:
        return JSONResponse(
            status_code=status_code,
            content={
                "error": {
                    "code": status_code,
                    "message": str(message),
                    "status": _gemini_error_status(status_code),
                }
            },
        )

    def _openai_image_error_response(exc: OpenAIImageRequestError) -> JSONResponse:
        error_payload = {
            "message": str(exc),
            "type": "invalid_request_error",
        }
        if exc.param:
            error_payload["param"] = exc.param
        return JSONResponse(status_code=400, content={"error": error_payload})

    def _openai_http_exception_response(exc: HTTPException) -> JSONResponse | None:
        detail = exc.detail
        if not isinstance(detail, dict):
            return None
        status_code = int(exc.status_code or 500)
        if isinstance(detail.get("error"), dict):
            error_payload = dict(detail["error"])
        else:
            error_payload = dict(detail)
        error_payload["message"] = str(
            error_payload.get("message") or "Request failed"
        )
        error_payload.setdefault(
            "type",
            "invalid_request_error" if 400 <= status_code < 500 else "server_error",
        )
        return JSONResponse(status_code=status_code, content={"error": error_payload})

    max_openai_edit_body_bytes = (MAX_TOTAL_IMAGE_BYTES * 4 // 3) + (8 * 1024 * 1024)

    def _validate_openai_edit_content_length(request: Request) -> None:
        raw_content_length = str(request.headers.get("content-length") or "").strip()
        if not raw_content_length:
            return
        try:
            content_length = int(raw_content_length)
        except ValueError:
            return
        if content_length > max_openai_edit_body_bytes:
            raise HTTPException(
                status_code=413,
                detail="request body is too large for 200MB of input images",
            )

    def _normalize_edit_image_mime(mime_type: str) -> str:
        normalized = str(mime_type or "").split(";", 1)[0].strip().lower()
        if normalized == "image/jpg":
            normalized = "image/jpeg"
        if normalized not in {"image/jpeg", "image/png", "image/webp"}:
            normalized = "image/jpeg"
        return normalized

    def _normalize_edit_image(image_bytes: bytes, mime_type: str) -> tuple[bytes, str]:
        normalized_mime = _normalize_edit_image_mime(mime_type)
        if Image is None:
            return image_bytes, normalized_mime
        try:
            with Image.open(io.BytesIO(image_bytes)) as source:
                source.load()
                actual_mime = {
                    "JPEG": "image/jpeg",
                    "PNG": "image/png",
                    "WEBP": "image/webp",
                }.get(str(source.format or "").upper())
                is_animated = bool(
                    getattr(source, "is_animated", False)
                    and int(getattr(source, "n_frames", 1) or 1) > 1
                )
                if (
                    actual_mime
                    and actual_mime == normalized_mime
                    and source.mode in {"RGB", "RGBA"}
                    and not is_animated
                ):
                    return image_bytes, actual_mime
                has_alpha = "A" in source.getbands() or "transparency" in source.info
                converted = source.convert("RGBA" if has_alpha else "RGB")
                output = io.BytesIO()
                converted.save(output, format="PNG")
                return output.getvalue(), "image/png"
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"unsupported or invalid image format: {exc}",
            ) from exc

    def _decode_edit_data_url(raw_value: str) -> tuple[bytes, str]:
        head, sep, body = raw_value.partition(",")
        if not sep:
            raise HTTPException(status_code=400, detail="invalid image data URL")
        mime_type = "image/jpeg"
        mime_part = head[5:]
        if ";" in mime_part:
            mime_type = (mime_part.split(";", 1)[0] or "image/jpeg").strip()
        elif mime_part:
            mime_type = mime_part.strip()
        if ";base64" not in head:
            raise HTTPException(
                status_code=400,
                detail="image data URL must be base64 encoded",
            )
        try:
            return base64.b64decode(body, validate=True), mime_type
        except binascii.Error:
            raise HTTPException(status_code=400, detail="invalid base64 image data")

    def _load_edit_image_string(raw_value: str) -> tuple[bytes, str]:
        image_ref = str(raw_value or "").strip()
        if not image_ref:
            raise HTTPException(status_code=400, detail="image is required")
        if image_ref.startswith("data:"):
            image_bytes, mime_type = _decode_edit_data_url(image_ref)
        elif image_ref.lower().startswith(("http://", "https://")):
            try:
                resp = requests.get(
                    image_ref,
                    timeout=30,
                    proxies=client._requests_proxies(),
                )
            except requests.RequestException as exc:
                raise HTTPException(
                    status_code=400,
                    detail=remote_image_error_message,
                ) from exc
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=400,
                    detail=remote_image_error_message,
                )
            image_bytes = resp.content
            mime_type = resp.headers.get("content-type") or "image/jpeg"
        else:
            try:
                image_bytes = base64.b64decode(image_ref, validate=True)
            except binascii.Error:
                raise HTTPException(
                    status_code=400,
                    detail="image must be a URL, data URL, or base64 string",
                )
            mime_type = "image/jpeg"

        if not image_bytes:
            raise HTTPException(status_code=400, detail="image is empty")
        image_bytes, mime_type = _normalize_edit_image(image_bytes, mime_type)
        if len(image_bytes) > MAX_SINGLE_IMAGE_BYTES:
            raise HTTPException(status_code=400, detail="image too large, max 30MB")
        return image_bytes, mime_type

    def _extract_edit_image_urls(raw_images: Any) -> list[str]:
        urls: list[str] = []

        def append_value(value: Any) -> None:
            if len(urls) >= MAX_INPUT_IMAGES:
                return
            if isinstance(value, list):
                for item in value:
                    append_value(item)
                return
            if isinstance(value, dict):
                for key in ("image", "url", "image_url"):
                    if key in value:
                        append_value(value.get(key))
                return
            image_ref = str(value or "").strip()
            if image_ref.lower().startswith(("http://", "https://")):
                urls.append(image_ref)

        append_value(raw_images)
        return urls

    def _set_raw_edit_log_context(
        request: Request,
        data: dict,
        raw_images: Any = None,
    ) -> None:
        try:
            prompt = str(data.get("prompt") or "").strip()
            resolution = str(
                data.get("size")
                or data.get("output_resolution")
                or data.get("aspect_ratio")
                or ""
            ).strip()
            request.state.log_model = (
                str(data.get("model") or "gpt-image-2").strip() or "gpt-image-2"
            )
            request.state.log_prompt = prompt or None
            request.state.log_prompt_preview = (
                prompt.replace("\r", " ").replace("\n", " ").strip()[:180] or None
            )
            request.state.log_resolution = resolution or None
            request.state.log_request_type = "edits"
            param_parts = []
            for key in (
                "n",
                "size",
                "aspect_ratio",
                "output_resolution",
                "response_format",
                "output_format",
                "quality",
                "output_compression",
            ):
                value = data.get(key)
                if value not in (None, ""):
                    param_parts.append(f"{key}={value}")
            request.state.log_request_params = ", ".join(param_parts)[:240] or None
            request.state.log_input_image_urls = (
                _extract_edit_image_urls(raw_images) or None
            )
        except Exception:
            pass

    def _load_edit_image_value(raw_value: Any) -> tuple[bytes, str]:
        if isinstance(raw_value, dict):
            image_value = raw_value.get("image") or raw_value.get("url")
            image_url = raw_value.get("image_url")
            if isinstance(image_url, dict):
                image_value = image_value or image_url.get("url")
            else:
                image_value = image_value or image_url
            image_value = image_value or raw_value.get("b64_json")
            image_value = image_value or raw_value.get("base64")
            return _load_edit_image_string(str(image_value or ""))
        return _load_edit_image_string(str(raw_value or ""))

    async def _parse_openai_edit_request(
        request: Request,
    ) -> tuple[dict, list[tuple[bytes, str]]]:
        trace = get_request_trace(request)
        trace_request_stage_id = getattr(
            request.state, "trace_request_stage_id", None
        )
        _validate_openai_edit_content_length(request)
        content_type = str(request.headers.get("content-type") or "").lower()
        if "application/json" in content_type:
            try:
                data = await request.json()
            except Exception as exc:
                if trace is not None:
                    trace.finish_stage(
                        trace_request_stage_id,
                        status="failed",
                        error=exc,
                    )
                raise HTTPException(status_code=400, detail="invalid JSON body")
            if not isinstance(data, dict):
                if trace is not None:
                    trace.finish_stage(
                        trace_request_stage_id,
                        status="failed",
                        error="request body must be JSON",
                        details={"body": sanitize_trace_value(data)},
                    )
                raise HTTPException(status_code=400, detail="request body must be JSON")
            raw_images = (
                data.get("image")
                or data.get("images")
                or data.get("image_url")
                or data.get("image_urls")
            )
            _set_raw_edit_log_context(request, data, raw_images)
            image_values = raw_images if isinstance(raw_images, list) else [raw_images]
            image_values = [value for value in image_values if value]
            try:
                validate_input_image_count(len(image_values))
            except ImageInputLimitError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=str(exc),
                ) from exc

            def load_json_images() -> list[tuple[bytes, str]]:
                loaded_images: list[tuple[bytes, str]] = []
                total_image_bytes = 0
                for value in image_values:
                    loaded_image = _load_edit_image_value(value)
                    try:
                        total_image_bytes = add_input_image_bytes(
                            total_image_bytes, len(loaded_image[0])
                        )
                    except ImageInputLimitError as exc:
                        raise HTTPException(status_code=400, detail=str(exc)) from exc
                    loaded_images.append(loaded_image)
                return loaded_images

            input_images = await run_in_threadpool(load_json_images)
            if trace is not None:
                trace.finish_stage(
                    trace_request_stage_id,
                    status="succeeded",
                    details={
                        "body": sanitize_trace_value(data),
                        "input_images": [
                            binary_summary(
                                image_bytes,
                                content_type=image_mime,
                            )
                            for image_bytes, image_mime in input_images
                        ],
                    },
                )
            return data, input_images

        form = await request.form()
        form_snapshot: list[dict[str, Any]] = []
        for field_name, field_value in form.multi_items():
            if hasattr(field_value, "read"):
                form_snapshot.append(
                    {
                        "name": str(field_name),
                        "file": {
                            "filename": str(
                                getattr(field_value, "filename", "") or ""
                            ),
                            "content_type": str(
                                getattr(field_value, "content_type", "") or ""
                            ),
                        },
                    }
                )
            else:
                form_snapshot.append(
                    {
                        "name": str(field_name),
                        "value": sanitize_trace_value(field_value, key=str(field_name)),
                    }
                )
        data = {
            "model": form.get("model") or "gpt-image-2",
            "prompt": form.get("prompt"),
            "size": form.get("size"),
            "n": form.get("n"),
            "response_format": form.get("response_format"),
            "output_format": form.get("output_format"),
            "output_compression": form.get("output_compression"),
        }
        image_values = list(form.getlist("image"))
        if not image_values:
            image_values = list(form.getlist("images"))
        if not image_values:
            image_values = list(form.getlist("image[]"))
        _set_raw_edit_log_context(request, data, image_values)
        try:
            validate_input_image_count(len(image_values))
        except ImageInputLimitError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        input_images: list[tuple[bytes, str]] = []
        total_image_bytes = 0
        for value in image_values:
            if hasattr(value, "read"):
                image_bytes = await value.read()
                mime_type = getattr(value, "content_type", None) or "image/jpeg"
                if not image_bytes:
                    raise HTTPException(status_code=400, detail="image is empty")
                if len(image_bytes) > MAX_SINGLE_IMAGE_BYTES:
                    raise HTTPException(
                        status_code=400,
                        detail="image too large, max 30MB",
                    )
                loaded_image = _normalize_edit_image(image_bytes, str(mime_type))
            else:
                loaded_image = await run_in_threadpool(
                    lambda: _load_edit_image_value(value)
                )
            try:
                total_image_bytes = add_input_image_bytes(
                    total_image_bytes, len(loaded_image[0])
                )
            except ImageInputLimitError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            input_images.append(loaded_image)

        if trace is not None:
            trace.finish_stage(
                trace_request_stage_id,
                status="succeeded",
                details={
                    "multipart_fields": form_snapshot,
                    "parsed_parameters": sanitize_trace_value(data),
                    "input_images": [
                        binary_summary(
                            image_bytes,
                            content_type=image_mime,
                        )
                        for image_bytes, image_mime in input_images
                    ],
                },
            )
        return data, input_images

    def _generate_openai_image_items(
        *,
        request: Request,
        token: str | None,
        prompt: str,
        image_options,
        model_conf: dict,
        source_image_ids: list[str] | None = None,
        distributed_tokens: bool = False,
        result_cache: Optional[dict[int, dict]] = None,
        seed_cache: Optional[dict[int, int]] = None,
    ) -> list[dict]:
        source_image_ids = source_image_ids or []
        result_cache = result_cache if result_cache is not None else {}
        seed_cache = seed_cache if seed_cache is not None else {}
        cache_lock = threading.Lock()
        queue_id = str(getattr(request.state, "image_queue_id", "") or "")
        trace = get_request_trace(request)
        trace_group_id = None
        if trace is not None:
            trace_group_id = trace.start_stage(
                layer="service",
                kind="generation_batch",
                name="生成 OpenAI Images 响应项",
                parent_id=getattr(request.state, "trace_operation_stage_id", None),
                details={
                    "image_count": image_options.n,
                    "source_image_count": len(source_image_ids),
                },
            )

        def _image_progress_cb(output_index: int, selected_token: str, update: dict):
            task_status = str(update.get("task_status") or "IN_PROGRESS").upper()
            queue_state = {
                "IN_PROGRESS": "WAITING_POLL",
                "COMPLETED": "COMPLETED",
                "FAILED": "FAILED",
            }.get(task_status, task_status)
            retry_after = update.get("retry_after")
            if task_status == "RATE_LIMITED" and retry_after:
                image_task_coordinator.note_token_cooldown(
                    selected_token, float(retry_after)
                )
            image_task_coordinator.update_output(
                queue_id,
                output_index,
                state=queue_state,
                token=selected_token,
                upstream_job_id=update.get("upstream_job_id"),
                retry_count=update.get("retry_count"),
                next_run_at=(
                    time.time() + float(retry_after)
                    if retry_after not in (None, "")
                    else None
                ),
                rate_limit_wait_seconds=update.get("rate_limit_wait_seconds"),
                download_attempt=update.get("download_attempt"),
                error=update.get("error"),
            )
            set_request_task_progress(
                request,
                task_status="IN_PROGRESS" if queue_state != "FAILED" else "FAILED",
                task_progress=update.get("task_progress"),
                upstream_job_id=update.get("upstream_job_id"),
                retry_after=retry_after,
                error=update.get("error"),
            )

        def _wait_for_token_cooldown(output_index: int, selected_token: str) -> None:
            while True:
                image_task_coordinator.raise_if_cancelled(queue_id)
                remaining = image_task_coordinator.token_cooldown_remaining(
                    selected_token
                )
                if remaining <= 0:
                    return
                image_task_coordinator.update_output(
                    queue_id,
                    output_index,
                    state="RATE_LIMITED",
                    token=selected_token,
                    next_run_at=time.time() + remaining,
                    rate_limit_wait_seconds=remaining,
                )
                image_task_coordinator.wait(queue_id, remaining)

        def _generation_token_candidates() -> list[str]:
            try:
                candidates = [
                    str(item.get("token") or "").strip()
                    for item in token_manager.list_active_account_tokens()
                    if isinstance(item, dict)
                ]
                candidates = [token for token in candidates if token]
                if candidates:
                    return candidates
            except Exception:
                pass
            try:
                candidate = str(
                    token_manager.get_available(
                        strategy=client.token_rotation_strategy
                    )
                    or ""
                ).strip()
            except Exception:
                candidate = ""
            return [candidate] if candidate else []

        def _generate_response_item_impl(
            response_index: int,
            trace_output_id: Optional[str],
            selected_token: str,
        ) -> tuple[int, dict]:
            with cache_lock:
                cached = result_cache.get(response_index)
            if cached is not None:
                return response_index, cached

            job_id = uuid.uuid4().hex
            out_path = generated_dir / f"{job_id}.png"
            old_size = 0
            try:
                if out_path.exists():
                    old_size = int(out_path.stat().st_size)
            except Exception:
                old_size = 0

            with cache_lock:
                fixed_seed = seed_cache.setdefault(
                    response_index, random_image_seed()
                )
            _wait_for_token_cooldown(response_index, selected_token)
            try:
                token_meta = token_manager.get_meta_by_value(selected_token) or {}
            except Exception:
                token_meta = {}
            token_limit = _image_config_int(
                "image_per_token_concurrency", 3, 1, 10
            )
            with image_task_coordinator.token_slot(
                selected_token,
                limit=token_limit,
                request_id=queue_id,
                output_index=response_index,
            ):
                image_task_coordinator.update_output(
                    queue_id,
                    response_index,
                    state="SUBMITTING",
                    token=selected_token,
                    account_name=(
                        token_meta.get("token_account_name")
                        or token_meta.get("token_account_email")
                    ),
                )
                image_bytes, _meta = client.generate(
                    token=selected_token,
                    prompt=prompt,
                    aspect_ratio=image_options.aspect_ratio,
                    output_resolution=image_options.output_resolution,
                    upstream_model_id=str(
                        model_conf.get("upstream_model_id") or "gemini-flash"
                    ),
                    upstream_model_version=str(
                        model_conf.get("upstream_model_version") or "nano-banana-2"
                    ),
                    quality_level=_gpt_image_quality_for_model(
                        model_conf, image_options.response_model
                    ),
                    detail_level=model_conf.get("detail_level"),
                    seed=fixed_seed,
                    source_image_ids=source_image_ids,
                    requested_size=image_options.requested_size,
                    timeout=client.generate_timeout,
                    out_path=out_path,
                    progress_cb=lambda update: _image_progress_cb(
                        response_index, selected_token, update
                    ),
                    trace=trace,
                    trace_parent_id=trace_output_id or trace_group_id,
                    cancel_check=lambda: image_task_coordinator.raise_if_cancelled(
                        queue_id
                    ),
                    io_call=image_task_coordinator.run_io,
                    wait_cb=lambda delay: image_task_coordinator.wait(
                        queue_id, delay
                    ),
                )
            if image_bytes is not None:
                out_path.write_bytes(image_bytes)
            new_size = int(out_path.stat().st_size) if out_path.exists() else 0
            _track_generated_path(request, out_path)
            image_url = public_image_url(request, job_id)
            set_request_preview(request, image_url, kind="image")
            image_file_bytes = (
                out_path.read_bytes()
                if image_options.response_format == "b64_json"
                else b""
            )
            item = encode_image_response_item(
                image_file_bytes,
                image_url=image_url,
                response_format=image_options.response_format,
                output_format=image_options.output_format,
                output_compression=image_options.output_compression,
            )
            if image_options.response_format == "b64_json":
                out_path.unlink(missing_ok=True)
            else:
                on_generated_file_written(out_path, old_size, new_size)
            with cache_lock:
                result_cache[response_index] = item
            image_task_coordinator.update_output(
                queue_id,
                response_index,
                state="COMPLETED",
                token=selected_token,
            )
            return response_index, item

        def _generate_response_item(response_index: int) -> tuple[int, dict]:
            trace_output_id = None
            if trace is not None:
                trace_output_id = trace.start_stage(
                    layer="service",
                    kind="output",
                    name=f"生成第 {response_index + 1} 张图片",
                    parent_id=trace_group_id,
                    attempt={
                        "output_index": response_index,
                        "output_count": image_options.n,
                    },
                )
            try:
                if distributed_tokens:
                    assigned_token = ""
                    attempted_tokens: set[str] = set()

                    def select_output_token() -> Optional[str]:
                        nonlocal assigned_token
                        if assigned_token:
                            image_task_coordinator.release_token_assignment(
                                assigned_token
                            )
                            assigned_token = ""
                        selected = image_task_coordinator.assign_token(
                            _generation_token_candidates(),
                            exclude=attempted_tokens,
                        )
                        if selected:
                            assigned_token = selected
                            attempted_tokens.add(selected)
                        return selected

                    try:
                        result = run_with_token_retries(
                            request=request,
                            operation_name=f"images.output.{response_index}",
                            run_once=lambda selected_token: _generate_response_item_impl(
                                response_index, trace_output_id, selected_token
                            ),
                            token_selector=select_output_token,
                        )
                    finally:
                        if assigned_token:
                            image_task_coordinator.release_token_assignment(
                                assigned_token
                            )
                else:
                    if not token:
                        raise HTTPException(
                            status_code=503,
                            detail="No active tokens available in the pool",
                        )
                    result = _generate_response_item_impl(
                        response_index, trace_output_id, token
                    )
            except ContentPolicyError:
                image_task_coordinator.cancel_request(queue_id, "图片不安全")
                image_task_coordinator.update_output(
                    queue_id,
                    response_index,
                    state="FAILED",
                    error="图片不安全",
                )
                raise
            except Exception as exc:
                image_task_coordinator.update_output(
                    queue_id,
                    response_index,
                    state="FAILED",
                    error=exc,
                )
                if trace is not None:
                    trace.finish_stage(
                        trace_output_id,
                        status="failed",
                        error=exc,
                    )
                    trace.finish_stage(
                        trace_group_id,
                        status="failed",
                        error=exc,
                    )
                raise
            if trace is not None:
                trace.finish_stage(
                    trace_output_id,
                    status="succeeded",
                    response={"item": sanitize_trace_value(result[1])},
                )
            return result

        pending_indices = [
            index for index in range(image_options.n) if index not in result_cache
        ]
        request_limit = _image_config_int(
            "image_per_request_concurrency", 4, 1, 10
        )
        try:
            image_task_coordinator.run_indexed(
                request_id=queue_id,
                indices=pending_indices,
                worker=lambda index: _generate_response_item(index)[1],
                max_parallel=request_limit,
            )
        except Exception as exc:
            if trace is not None:
                trace.finish_stage(
                    trace_group_id,
                    status="failed",
                    error=exc,
                    details={"generated_count": len(result_cache)},
                )
            raise

        if len(result_cache) != image_options.n:
            raise AdobeRequestError(
                f"image generation incomplete: expected {image_options.n}, got {len(result_cache)}"
            )
        result_items = [result_cache[index] for index in range(image_options.n)]
        if trace is not None:
            trace.finish_stage(
                trace_group_id,
                status="succeeded",
                details={"generated_count": len(result_items)},
            )
        return result_items

    def _upload_edit_source_images(
        token: str,
        input_images: list[tuple[Any, str]],
        request: Request,
    ) -> list[str]:
        if not input_images:
            return []
        trace = get_request_trace(request)
        queue_id = str(getattr(request.state, "image_queue_id", "") or "")
        image_task_coordinator.set_request_state(queue_id, "UPLOADING")
        image_task_coordinator.set_all_output_state(queue_id, "UPLOADING")
        trace_group_id = None
        if trace is not None:
            trace_group_id = trace.start_stage(
                layer="service",
                kind="upload_batch",
                name="上传 edits 参考图",
                parent_id=getattr(
                    request.state, "trace_token_attempt_id", None
                ),
                details={"image_count": len(input_images)},
            )

        def upload_one(index: int, item: tuple[Any, str]) -> tuple[int, str]:
            image_bytes = (
                item[0].read_bytes()
                if isinstance(item[0], Path)
                else bytes(item[0])
            )
            trace_image_id = None
            if trace is not None:
                trace_image_id = trace.start_stage(
                    layer="service",
                    kind="upload_item",
                    name=f"准备上传第 {index + 1} 张参考图",
                    parent_id=trace_group_id,
                    attempt={"image_index": index},
                    request={
                        "image": binary_summary(
                            image_bytes,
                            filename=(item[0].name if isinstance(item[0], Path) else None),
                            content_type=item[1] or "image/jpeg",
                        )
                    },
                )
            try:
                def upload_progress(update: dict) -> None:
                    state = str(update.get("task_status") or "UPLOADING")
                    retry_after = update.get("retry_after")
                    if state.upper() == "RATE_LIMITED" and retry_after:
                        image_task_coordinator.note_token_cooldown(
                            token, float(retry_after)
                        )
                    image_task_coordinator.set_request_state(
                        queue_id, state, error=update.get("error")
                    )
                    image_task_coordinator.set_all_output_state(
                        queue_id,
                        state,
                        error=update.get("error"),
                        next_run_at=(
                            time.time() + float(retry_after)
                            if retry_after not in (None, "")
                            else None
                        ),
                        rate_limit_wait_seconds=update.get(
                            "rate_limit_wait_seconds"
                        ),
                        retry_count=update.get("retry_count"),
                    )

                token_limit = _image_config_int(
                    "image_per_token_concurrency", 3, 1, 10
                )
                with image_task_coordinator.token_slot(
                    token,
                    limit=token_limit,
                    request_id=queue_id,
                    output_index=index,
                ):
                    image_id = client.upload_image(
                        token,
                        image_bytes,
                        item[1] or "image/jpeg",
                        trace=trace,
                        trace_parent_id=trace_image_id or trace_group_id,
                        progress_cb=upload_progress,
                        cancel_check=lambda: image_task_coordinator.raise_if_cancelled(
                            queue_id
                        ),
                        io_call=image_task_coordinator.run_io,
                        wait_cb=lambda delay: image_task_coordinator.wait(
                            queue_id, delay
                        ),
                    )
                if not str(image_id or "").strip():
                    raise AdobeRequestError(
                        "upload image succeeded but no image id returned"
                    )
            except Exception as exc:
                if trace is not None:
                    trace.finish_stage(trace_image_id, status="failed", error=exc)
                    trace.finish_stage(trace_group_id, status="failed", error=exc)
                raise
            if trace is not None:
                trace.finish_stage(
                    trace_image_id,
                    status="succeeded",
                    response={"storage_image_id": image_id},
                )
            return index, image_id

        max_workers = min(3, len(input_images))
        if max_workers <= 1:
            result = [
                upload_one(index, item)[1]
                for index, item in enumerate(input_images)
            ]
            if len(result) != len(input_images) or any(
                not str(image_id or "").strip() for image_id in result
            ):
                raise AdobeRequestError(
                    "reference image upload incomplete; generation was not started"
                )
            if trace is not None:
                trace.finish_stage(
                    trace_group_id,
                    status="succeeded",
                    details={"uploaded_count": len(result)},
                )
            image_task_coordinator.set_all_output_state(queue_id, "QUEUED")
            return result
        indexed_images = list(enumerate(input_images))
        source_pairs: list[tuple[int, str]] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(
                    upload_one,
                    idx,
                    item,
                )
                for idx, item in indexed_images
            ]
            for future in as_completed(futures):
                source_pairs.append(future.result())
        result = [
            image_id
            for _idx, image_id in sorted(source_pairs, key=lambda pair: pair[0])
        ]
        if len(result) != len(input_images) or any(
            not str(image_id or "").strip() for image_id in result
        ):
            raise AdobeRequestError(
                "reference image upload incomplete; generation was not started"
            )
        if trace is not None:
            trace.finish_stage(
                trace_group_id,
                status="succeeded",
                details={"uploaded_count": len(result)},
            )
        image_task_coordinator.set_all_output_state(queue_id, "QUEUED")
        return result

    def _log_resolution_from_image_options(image_options: Any) -> str | None:
        requested_size = getattr(image_options, "requested_size", None)
        if isinstance(requested_size, dict):
            width = int(requested_size.get("width") or 0)
            height = int(requested_size.get("height") or 0)
            if width > 0 and height > 0:
                return f"{width}x{height}"

        ratio = str(getattr(image_options, "aspect_ratio", "") or "").strip()
        output_resolution = str(
            getattr(image_options, "output_resolution", "") or ""
        ).strip()
        try:
            if bool(getattr(image_options, "is_native_gpt_image", False)):
                pixel_size = gpt_image_pixels_from_ratio(ratio, output_resolution)
            else:
                pixel_size = size_from_ratio(ratio, output_resolution)
            if isinstance(pixel_size, dict):
                width = int(pixel_size.get("width") or 0)
                height = int(pixel_size.get("height") or 0)
                if width > 0 and height > 0:
                    return f"{width}x{height}"
        except Exception:
            pass
        if output_resolution and ratio:
            return f"{output_resolution} {ratio}"
        return output_resolution or ratio or None

    def _compact_image_request_params(
        data: dict,
        image_options: Any,
        *,
        input_image_count: int = 0,
    ) -> str | None:
        params = {
            "n": getattr(image_options, "n", data.get("n", 1)),
            "size": data.get("size") or _log_resolution_from_image_options(image_options),
            "ratio": getattr(image_options, "aspect_ratio", None),
            "response_format": getattr(image_options, "response_format", None),
            "output_format": getattr(image_options, "output_format", None),
        }
        for key in ("quality", "output_compression"):
            if data.get(key) not in (None, ""):
                params[key] = data.get(key)
        if input_image_count > 0:
            params["images"] = input_image_count
        parts = [
            f"{key}={value}"
            for key, value in params.items()
            if value is not None and value != ""
        ]
        return ", ".join(parts)[:240] or None

    def _set_image_log_context(
        request: Request,
        *,
        data: dict,
        prompt: str,
        image_options: Any,
        request_type: str,
        input_image_count: int = 0,
    ) -> None:
        try:
            request.state.log_model = (
                str(getattr(image_options, "response_model", "") or "").strip()
                or str(data.get("model") or "").strip()
                or None
            )
            request.state.log_prompt = str(prompt or "").strip() or None
            request.state.log_prompt_preview = (
                str(prompt or "").replace("\r", " ").replace("\n", " ").strip()[:180]
                or None
            )
            request.state.log_resolution = _log_resolution_from_image_options(
                image_options
            )
            request.state.log_request_type = request_type
            request.state.log_request_params = _compact_image_request_params(
                data,
                image_options,
                input_image_count=input_image_count,
            )
        except Exception:
            pass

    def _gemini_generate_content_impl(
        model_id: str,
        data: dict,
        request: Request,
    ):
        require_service_api_key(request)
        try:
            gemini_options = parse_gemini_generate_request(data, model_id)
            if gemini_options.aspect_ratio not in supported_ratios:
                raise GeminiRequestError(
                    f"unsupported imageConfig.aspectRatio: {gemini_options.aspect_ratio}"
                )
            family_prefix = GEMINI_IMAGE_MODELS[
                gemini_options.canonical_model_id
            ]["family_prefix"]
            model_conf = next(
                (
                    conf
                    for candidate_id, conf in model_catalog.items()
                    if candidate_id.startswith(f"{family_prefix}-")
                    and str(conf.get("aspect_ratio") or "")
                    == gemini_options.aspect_ratio
                    and str(conf.get("output_resolution") or "").upper()
                    == gemini_options.image_size
                ),
                None,
            )
            if model_conf is None:
                model_conf = next(
                    (
                        conf
                        for candidate_id, conf in model_catalog.items()
                        if candidate_id.startswith(f"{family_prefix}-")
                    ),
                    None,
                )
            if model_conf is None:
                raise GeminiRequestError(f"model not found: {model_id}")
            image_options = build_legacy_image_options(
                {
                    "n": gemini_options.candidate_count,
                    "response_format": "b64_json",
                    "output_format": "png",
                },
                ratio=gemini_options.aspect_ratio,
                output_resolution=gemini_options.image_size,
                resolved_model_id=gemini_options.canonical_model_id,
            )
        except GeminiRequestError as exc:
            return _gemini_error_response(400, str(exc))
        except OpenAIImageRequestError as exc:
            return _gemini_error_response(400, str(exc))

        _set_image_log_context(
            request,
            data={
                "model": gemini_options.canonical_model_id,
                "n": gemini_options.candidate_count,
                "aspect_ratio": gemini_options.aspect_ratio,
                "output_resolution": gemini_options.image_size,
            },
            prompt=gemini_options.prompt,
            image_options=image_options,
            request_type="gemini.generateContent",
            input_image_count=len(gemini_options.input_images),
        )

        try:
            set_request_task_progress(
                request, task_status="IN_PROGRESS", task_progress=0.0
            )

            def _run_once(token: str):
                source_image_ids = _upload_edit_source_images(
                    token, gemini_options.input_images, request
                )
                response_items = _generate_openai_image_items(
                    request=request,
                    token=token,
                    prompt=gemini_options.prompt,
                    image_options=image_options,
                    model_conf=model_conf,
                    source_image_ids=source_image_ids,
                )
                return build_gemini_generate_response(
                    model_id=gemini_options.canonical_model_id,
                    images_base64=[item["b64_json"] for item in response_items],
                    response_id=uuid.uuid4().hex,
                )

            return run_with_token_retries(
                request=request,
                operation_name="models.generateContent",
                run_once=_run_once,
            )
        except HTTPException as exc:
            detail = exc.detail
            if isinstance(detail, dict):
                detail = detail.get("message") or str(detail)
            return _gemini_error_response(int(exc.status_code or 500), str(detail))
        except quota_error_cls:
            return _gemini_error_response(429, "Token quota exhausted")
        except auth_error_cls:
            return _gemini_error_response(401, "Token invalid or expired")
        except upstream_temp_error_cls as exc:
            return _gemini_error_response(
                int(getattr(exc, "status_code", 503) or 503), str(exc)
            )
        except Exception as exc:
            logger.exception(
                "Unhandled error in Gemini generateContent model=%s", model_id
            )
            set_request_error_detail(
                request,
                error=exc,
                status_code=500,
                error_type="server_error",
                include_traceback=True,
            )
            return _gemini_error_response(500, str(exc))

    @router.post("/v1beta/models/{model_id}:generateContent")
    def gemini_generate_content(model_id: str, data: dict, request: Request):
        return _gemini_generate_content_impl(model_id, data, request)

    @router.post("/v1beta/models/{model_id}:streamGenerateContent")
    def gemini_stream_generate_content(model_id: str, data: dict, request: Request):
        response = _gemini_generate_content_impl(model_id, data, request)
        if isinstance(response, JSONResponse):
            return response

        def _stream():
            import json

            yield f"data: {json.dumps(response, ensure_ascii=False)}\n\n"

        return StreamingResponse(_stream(), media_type="text/event-stream")

    @router.post("/v1/images/generations")
    async def openai_generate(data: dict, request: Request):
        _start_image_operation(request, "处理 /v1/images/generations")
        _trace_auth(request)

        prompt = str(data.get("prompt") or "").strip()
        trace = get_request_trace(request)
        validation_stage_id = None
        if trace is not None:
            validation_stage_id = trace.start_stage(
                layer="service",
                kind="validation",
                name="校验 generation 请求参数",
                parent_id=getattr(request.state, "trace_operation_stage_id", None),
                details={"parameters": sanitize_trace_value(data)},
            )
        if not prompt:
            content = {
                "error": {
                    "message": "prompt is required",
                    "type": "invalid_request_error",
                }
            }
            if trace is not None:
                trace.finish_stage(
                    validation_stage_id,
                    status="failed",
                    error="prompt is required",
                )
            return _traced_json_response(
                request,
                status_code=400,
                content=content,
            )

        model_id = str(data.get("model") or "").strip()
        if model_id in video_model_catalog:
            content = {
                "error": {
                    "message": "Use /v1/chat/completions for video generation",
                    "type": "invalid_request_error",
                }
            }
            if trace is not None:
                trace.finish_stage(
                    validation_stage_id,
                    status="failed",
                    error="Use /v1/chat/completions for video generation",
                )
            return _traced_json_response(
                request,
                status_code=400,
                content=content,
            )
        if trace is not None:
            trace.finish_stage(validation_stage_id, status="succeeded")
        model_stage_id = None
        if trace is not None:
            model_stage_id = trace.start_stage(
                layer="service",
                kind="model_resolution",
                name="解析 GPT Image 模型与尺寸",
                parent_id=getattr(request.state, "trace_operation_stage_id", None),
                details={"requested_model": model_id or None},
            )
        try:
            if _is_gpt_image_model_or_alias(model_id):
                image_options, model_conf, resolved_model_id = (
                    _build_gpt_image_alias_options(data, model_id or "gpt-image-2")
                )
            else:
                ratio, output_resolution, resolved_model_id = (
                    resolve_ratio_and_resolution(data, model_id or None)
                )
                model_conf = resolve_model(resolved_model_id)
                image_options = build_legacy_image_options(
                    data,
                    ratio=ratio,
                    output_resolution=output_resolution,
                    resolved_model_id=resolved_model_id,
                )
        except OpenAIImageRequestError as exc:
            if trace is not None:
                trace.finish_stage(model_stage_id, status="failed", error=exc)
            error_payload = {
                "message": str(exc),
                "type": "invalid_request_error",
            }
            if exc.param:
                error_payload["param"] = exc.param
            return _traced_json_response(
                request,
                status_code=400,
                content={"error": error_payload},
                error=exc,
            )

        if trace is not None:
            trace.finish_stage(
                model_stage_id,
                status="succeeded",
                response={
                    "resolved_model_id": resolved_model_id,
                    "image_options": asdict(image_options),
                    "upstream": {
                        "model_id": model_conf.get("upstream_model_id"),
                        "model_version": model_conf.get("upstream_model_version"),
                    },
                },
            )

        _set_image_log_context(
            request,
            data=data,
            prompt=prompt,
            image_options=image_options,
            request_type="generation",
        )

        queue_id = _register_image_queue(
            request,
            model=str(image_options.response_model or resolved_model_id),
            prompt=prompt,
            output_count=image_options.n,
        )
        disconnect_task = asyncio.create_task(
            _watch_image_disconnect(request, queue_id)
        )

        try:
            set_request_task_progress(
                request, task_status="IN_PROGRESS", task_progress=0.0
            )

            response_items = await run_in_threadpool(
                lambda: _generate_openai_image_items(
                    request=request,
                    token=None,
                    prompt=prompt,
                    image_options=image_options,
                    model_conf=model_conf,
                    distributed_tokens=True,
                )
            )
            result = {
                "created": int(time.time()),
                "data": response_items,
            }
            if not image_options.is_native_gpt_image:
                result["model"] = resolved_model_id
            if trace is not None:
                trace.finish_stage(
                    getattr(request.state, "trace_operation_stage_id", None),
                    status="succeeded",
                    response={"result": sanitize_trace_value(result)},
                )
            _finish_image_queue(request, succeeded=True)
            return result

        except ContentPolicyError as exc:
            set_request_task_progress(
                request,
                task_status="FAILED",
                task_progress=0.0,
                error="图片不安全",
            )
            return _traced_json_response(
                request,
                status_code=400,
                content={
                    "error": {
                        "message": "图片不安全",
                        "type": "invalid_request_error",
                    }
                },
                error=exc,
            )
        except quota_error_cls:
            error_code = str(
                getattr(request.state, "log_error_code", "") or ""
            ) or set_request_error_detail(
                request,
                error="Token quota exhausted",
                status_code=429,
                error_type="rate_limit_error",
                include_traceback=False,
            )
            set_request_task_progress(
                request,
                task_status="FAILED",
                task_progress=0.0,
                error="Token quota exhausted",
            )
            content = {
                "error": {
                    "message": "Token quota exhausted",
                    "type": "rate_limit_error",
                    "code": error_code,
                }
            }
            return _traced_json_response(
                request,
                status_code=429,
                content=content,
            )
        except auth_error_cls:
            error_code = str(
                getattr(request.state, "log_error_code", "") or ""
            ) or set_request_error_detail(
                request,
                error="Token invalid or expired",
                status_code=401,
                error_type="invalid_request_error",
                include_traceback=False,
            )
            set_request_task_progress(
                request,
                task_status="FAILED",
                task_progress=0.0,
                error="Token invalid or expired",
            )
            content = {
                "error": {
                    "message": "Token invalid or expired",
                    "type": "invalid_request_error",
                    "code": error_code,
                }
            }
            return _traced_json_response(
                request,
                status_code=401,
                content=content,
            )
        except upstream_temp_error_cls as exc:
            error_code = str(
                getattr(request.state, "log_error_code", "") or ""
            ) or set_request_error_detail(
                request,
                error=str(exc),
                status_code=getattr(exc, "status_code", 502) or 502,
                error_type="server_error",
                include_traceback=False,
            )
            set_request_task_progress(
                request,
                task_status="FAILED",
                task_progress=0.0,
                error=str(exc),
            )
            status_code = getattr(exc, "status_code", 502) or 502
            content = {
                "error": {
                    "message": str(exc),
                    "type": "server_error",
                    "code": error_code,
                }
            }
            return _traced_json_response(
                request,
                status_code=status_code,
                content=content,
                error=exc,
            )
        except HTTPException as exc:
            passthrough = _openai_http_exception_response(exc)
            if passthrough is not None:
                return _remember_existing_response(request, passthrough, error=exc)
            status_code = int(exc.status_code or 500)
            err_type = (
                "invalid_request_error" if 400 <= status_code < 500 else "server_error"
            )
            set_request_task_progress(
                request,
                task_status="FAILED",
                task_progress=0.0,
                error=str(exc.detail),
            )
            content = {
                "error": {
                    "message": str(exc.detail),
                    "type": err_type,
                }
            }
            return _traced_json_response(
                request,
                status_code=status_code,
                content=content,
                error=exc,
            )
        except Exception as exc:
            logger.exception("Unhandled error in /v1/images/generations")
            error_code = str(
                getattr(request.state, "log_error_code", "") or ""
            ) or set_request_error_detail(
                request,
                error=str(exc),
                status_code=500,
                error_type="server_error",
                include_traceback=True,
            )
            set_request_task_progress(
                request,
                task_status="FAILED",
                task_progress=0.0,
                error=str(exc),
            )
            content = {
                "error": {
                    "message": str(exc),
                    "type": "server_error",
                    "code": error_code,
                }
            }
            return _traced_json_response(
                request,
                status_code=500,
                content=content,
                error=exc,
            )
        finally:
            disconnect_task.cancel()

    @router.post("/v1/images/edits")
    async def openai_edit(request: Request):
        _start_image_operation(request, "处理 /v1/images/edits")
        _trace_auth(request)
        trace = get_request_trace(request)
        try:
            data, input_images = await _parse_openai_edit_request(request)
        except HTTPException as exc:
            if trace is not None:
                trace.finish_stage(
                    getattr(request.state, "trace_request_stage_id", None),
                    status="failed",
                    error=exc,
                )
            passthrough = _openai_http_exception_response(exc)
            if passthrough is not None:
                return _remember_existing_response(request, passthrough, error=exc)
            status_code = int(exc.status_code or 400)
            error_type = (
                "invalid_request_error"
                if 400 <= status_code < 500
                else "server_error"
            )
            error_code = set_request_error_detail(
                request,
                error=str(exc.detail),
                status_code=status_code,
                error_type=error_type,
                include_traceback=False,
            )
            set_request_task_progress(
                request,
                task_status="FAILED",
                task_progress=0.0,
                error=str(exc.detail),
            )
            content = {
                "error": {
                    "message": str(exc.detail),
                    "type": error_type,
                    "code": error_code,
                }
            }
            return _traced_json_response(
                request,
                status_code=status_code,
                content=content,
                error=exc,
            )

        prompt = str(data.get("prompt") or "").strip()
        validation_stage_id = None
        if trace is not None:
            validation_stage_id = trace.start_stage(
                layer="service",
                kind="validation",
                name="校验 edits 请求参数",
                parent_id=getattr(request.state, "trace_operation_stage_id", None),
                details={
                    "parameters": sanitize_trace_value(data),
                    "input_image_count": len(input_images),
                },
            )
        if not prompt:
            content = {
                "error": {
                    "message": "prompt is required",
                    "type": "invalid_request_error",
                }
            }
            if trace is not None:
                trace.finish_stage(
                    validation_stage_id,
                    status="failed",
                    error="prompt is required",
                )
            return _traced_json_response(
                request,
                status_code=400,
                content=content,
            )
        if not input_images:
            content = {
                "error": {
                    "message": "image is required",
                    "type": "invalid_request_error",
                    "param": "image",
                }
            }
            if trace is not None:
                trace.finish_stage(
                    validation_stage_id,
                    status="failed",
                    error="image is required",
                )
            return _traced_json_response(
                request,
                status_code=400,
                content=content,
            )

        model_id = str(data.get("model") or "gpt-image-2").strip()
        try:
            request.state.log_model = model_id or "gpt-image-2"
            request.state.log_prompt = prompt or None
            request.state.log_prompt_preview = (
                prompt.replace("\r", " ").replace("\n", " ").strip()[:180] or None
            )
        except Exception:
            pass
        if model_id in video_model_catalog:
            content = {
                "error": {
                    "message": "Video models are not supported for image edits",
                    "type": "invalid_request_error",
                }
            }
            if trace is not None:
                trace.finish_stage(
                    validation_stage_id,
                    status="failed",
                    error="Video models are not supported for image edits",
                )
            return _traced_json_response(
                request,
                status_code=400,
                content=content,
            )
        if trace is not None:
            trace.finish_stage(validation_stage_id, status="succeeded")
        model_stage_id = None
        if trace is not None:
            model_stage_id = trace.start_stage(
                layer="service",
                kind="model_resolution",
                name="解析 edits 模型与尺寸",
                parent_id=getattr(request.state, "trace_operation_stage_id", None),
                details={"requested_model": model_id},
            )

        try:
            if _is_gpt_image_model_or_alias(model_id):
                data["model"] = model_id
                image_options, model_conf, resolved_model_id = (
                    _build_gpt_image_alias_options(data, model_id or "gpt-image-2")
                )
            else:
                ratio, output_resolution, resolved_model_id = (
                    resolve_ratio_and_resolution(data, model_id or None)
                )
                model_conf = resolve_model(resolved_model_id)
                image_options = build_legacy_image_options(
                    data,
                    ratio=ratio,
                    output_resolution=output_resolution,
                    resolved_model_id=resolved_model_id,
                )
        except OpenAIImageRequestError as exc:
            if trace is not None:
                trace.finish_stage(model_stage_id, status="failed", error=exc)
            response = _openai_image_error_response(exc)
            return _remember_existing_response(request, response, error=exc)

        if trace is not None:
            trace.finish_stage(
                model_stage_id,
                status="succeeded",
                response={
                    "resolved_model_id": resolved_model_id,
                    "image_options": asdict(image_options),
                    "upstream": {
                        "model_id": model_conf.get("upstream_model_id"),
                        "model_version": model_conf.get("upstream_model_version"),
                    },
                },
            )

        _set_image_log_context(
            request,
            data=data,
            prompt=prompt,
            image_options=image_options,
            request_type="edits",
            input_image_count=len(input_images),
        )

        queue_id = _register_image_queue(
            request,
            model=str(image_options.response_model or resolved_model_id),
            prompt=prompt,
            output_count=image_options.n,
        )
        result_cache: dict[int, dict] = {}
        seed_cache: dict[int, int] = {}
        source_image_ids_cache: list[str] = []
        bound_edit_account: dict[str, str] = {}
        disconnect_task = asyncio.create_task(
            _watch_image_disconnect(request, queue_id)
        )

        try:
            set_request_task_progress(
                request, task_status="IN_PROGRESS", task_progress=0.0
            )
            spooled_input_images = _spool_edit_source_images(
                request, input_images
            )

            def _select_edit_token() -> Optional[str]:
                if not bound_edit_account:
                    selected = str(
                        token_manager.get_available(
                            strategy=client.token_rotation_strategy
                        )
                        or ""
                    ).strip()
                    if not selected:
                        return None
                    try:
                        meta = token_manager.get_meta_by_value(selected) or {}
                    except Exception:
                        meta = {}
                    bound_edit_account.update(
                        {
                            "account_id": str(meta.get("token_account_id") or ""),
                            "refresh_profile_id": str(meta.get("refresh_profile_id") or ""),
                            "initial_token": selected,
                        }
                    )
                    return selected

                account_id = bound_edit_account.get("account_id") or ""
                if account_id:
                    selected = token_manager.get_available_for_account(
                        account_id,
                        strategy=client.token_rotation_strategy,
                    )
                    return str(selected or "").strip() or None
                profile_id = bound_edit_account.get("refresh_profile_id") or ""
                if profile_id and hasattr(
                    token_manager, "get_available_for_refresh_profile"
                ):
                    selected = token_manager.get_available_for_refresh_profile(
                        profile_id,
                        strategy=client.token_rotation_strategy,
                    )
                    return str(selected or "").strip() or None
                return bound_edit_account.get("initial_token") or None

            def _run_once(token: str):
                nonlocal source_image_ids_cache
                if len(result_cache) == image_options.n:
                    response_items = [
                        result_cache[index] for index in range(image_options.n)
                    ]
                    return {
                        "created": int(time.time()),
                        "data": response_items,
                    }
                if not source_image_ids_cache:
                    source_image_ids_cache = _upload_edit_source_images(
                        token,
                        spooled_input_images,
                        request,
                    )
                source_image_ids = list(source_image_ids_cache)
                if len(source_image_ids) != len(input_images) or any(
                    not str(image_id or "").strip()
                    for image_id in source_image_ids
                ):
                    raise AdobeRequestError(
                        "reference image upload incomplete; generation was not started"
                    )

                def generate_with_ids(ids: list[str]) -> list[dict]:
                    return _generate_openai_image_items(
                        request=request,
                        token=token,
                        prompt=prompt,
                        image_options=image_options,
                        model_conf=model_conf,
                        source_image_ids=ids,
                        result_cache=result_cache,
                        seed_cache=seed_cache,
                    )

                response_items, source_image_ids = generate_with_reference_recovery(
                    source_image_ids=source_image_ids,
                    expected_image_count=len(spooled_input_images),
                    generate_with_ids=generate_with_ids,
                    reupload_all=lambda: _upload_edit_source_images(
                            token,
                            spooled_input_images,
                            request,
                    ),
                    cancel_check=lambda: image_task_coordinator.raise_if_cancelled(
                        queue_id
                    ),
                    sleep=lambda delay: image_task_coordinator.wait(
                        queue_id, delay
                    ),
                )
                source_image_ids_cache = list(source_image_ids)
                response_payload = {
                    "created": int(time.time()),
                    "data": response_items,
                }
                if not image_options.is_native_gpt_image:
                    response_payload["model"] = resolved_model_id
                return response_payload

            result = await run_in_threadpool(
                lambda: run_with_token_retries(
                    request=request,
                    operation_name="images.edits",
                    run_once=_run_once,
                    token_selector=_select_edit_token,
                )
            )
            if trace is not None:
                trace.finish_stage(
                    getattr(request.state, "trace_operation_stage_id", None),
                    status="succeeded",
                    response={"result": sanitize_trace_value(result)},
                )
            _finish_image_queue(request, succeeded=True)
            return result

        except ContentPolicyError as exc:
            set_request_task_progress(
                request,
                task_status="FAILED",
                task_progress=0.0,
                error="图片不安全",
            )
            return _traced_json_response(
                request,
                status_code=400,
                content={
                    "error": {
                        "message": "图片不安全",
                        "type": "invalid_request_error",
                    }
                },
                error=exc,
            )
        except quota_error_cls:
            error_code = str(
                getattr(request.state, "log_error_code", "") or ""
            ) or set_request_error_detail(
                request,
                error="Token quota exhausted",
                status_code=429,
                error_type="rate_limit_error",
                include_traceback=False,
            )
            set_request_task_progress(
                request,
                task_status="FAILED",
                task_progress=0.0,
                error="Token quota exhausted",
            )
            content = {
                "error": {
                    "message": "Token quota exhausted",
                    "type": "rate_limit_error",
                    "code": error_code,
                }
            }
            return _traced_json_response(
                request,
                status_code=429,
                content=content,
            )
        except auth_error_cls:
            error_code = str(
                getattr(request.state, "log_error_code", "") or ""
            ) or set_request_error_detail(
                request,
                error="Token invalid or expired",
                status_code=401,
                error_type="invalid_request_error",
                include_traceback=False,
            )
            set_request_task_progress(
                request,
                task_status="FAILED",
                task_progress=0.0,
                error="Token invalid or expired",
            )
            content = {
                "error": {
                    "message": "Token invalid or expired",
                    "type": "invalid_request_error",
                    "code": error_code,
                }
            }
            return _traced_json_response(
                request,
                status_code=401,
                content=content,
            )
        except upstream_temp_error_cls as exc:
            error_code = str(
                getattr(request.state, "log_error_code", "") or ""
            ) or set_request_error_detail(
                request,
                error=str(exc),
                status_code=getattr(exc, "status_code", 502) or 502,
                error_type="server_error",
                include_traceback=False,
            )
            set_request_task_progress(
                request,
                task_status="FAILED",
                task_progress=0.0,
                error=str(exc),
            )
            status_code = getattr(exc, "status_code", 502) or 502
            content = {
                "error": {
                    "message": str(exc),
                    "type": "server_error",
                    "code": error_code,
                }
            }
            return _traced_json_response(
                request,
                status_code=status_code,
                content=content,
                error=exc,
            )
        except HTTPException as exc:
            passthrough = _openai_http_exception_response(exc)
            if passthrough is not None:
                return _remember_existing_response(request, passthrough, error=exc)
            status_code = int(exc.status_code or 500)
            err_type = (
                "invalid_request_error" if 400 <= status_code < 500 else "server_error"
            )
            set_request_task_progress(
                request,
                task_status="FAILED",
                task_progress=0.0,
                error=str(exc.detail),
            )
            content = {
                "error": {
                    "message": str(exc.detail),
                    "type": err_type,
                }
            }
            return _traced_json_response(
                request,
                status_code=status_code,
                content=content,
                error=exc,
            )
        except Exception as exc:
            logger.exception("Unhandled error in /v1/images/edits")
            error_code = str(
                getattr(request.state, "log_error_code", "") or ""
            ) or set_request_error_detail(
                request,
                error=str(exc),
                status_code=500,
                error_type="server_error",
                include_traceback=True,
            )
            set_request_task_progress(
                request,
                task_status="FAILED",
                task_progress=0.0,
                error=str(exc),
            )
            content = {
                "error": {
                    "message": str(exc),
                    "type": "server_error",
                    "code": error_code,
                }
            }
            return _traced_json_response(
                request,
                status_code=500,
                content=content,
                error=exc,
            )
        finally:
            disconnect_task.cancel()

    @router.post("/api/v1/generate")
    def create_job(data: GenerateRequest, request: Request):
        require_service_api_key(request)

        prompt = data.prompt.strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt cannot be empty")

        ratio = data.aspect_ratio.strip() or "16:9"
        if ratio not in supported_ratios:
            raise HTTPException(status_code=400, detail="unsupported aspect ratio")

        output_resolution = (data.output_resolution or "2K").upper()
        if output_resolution not in {"1K", "2K", "4K"}:
            raise HTTPException(status_code=400, detail="unsupported output_resolution")

        model_conf = resolve_model(data.model)
        if data.model:
            output_resolution = model_conf["output_resolution"]

        job = store.create(prompt=prompt, aspect_ratio=ratio)

        def runner(job_id: str):
            store.update(job_id, status="running", progress=5.0)
            max_attempts = client.retry_max_attempts if client.retry_enabled else 1
            max_attempts = max(1, int(max_attempts))
            last_error = "No active tokens available in the pool"

            for attempt in range(1, max_attempts + 1):
                token = token_manager.get_available(
                    strategy=client.token_rotation_strategy
                )
                if not token:
                    break

                try:
                    out_path = generated_dir / f"{job_id}.png"
                    old_size = 0
                    try:
                        if out_path.exists():
                            old_size = int(out_path.stat().st_size)
                    except Exception:
                        old_size = 0

                    image_bytes, meta = client.generate(
                        token=token,
                        prompt=prompt,
                        aspect_ratio=ratio,
                        output_resolution=output_resolution,
                        upstream_model_id=str(
                            model_conf.get("upstream_model_id") or "gemini-flash"
                        ),
                        upstream_model_version=str(
                            model_conf.get("upstream_model_version") or "nano-banana-2"
                        ),
                        quality_level=_gpt_image_quality_for_model(model_conf, data.model),
                        detail_level=model_conf.get("detail_level"),
                        out_path=out_path,
                    )
                    if image_bytes is not None:
                        out_path.write_bytes(image_bytes)
                    new_size = int(out_path.stat().st_size) if out_path.exists() else 0
                    on_generated_file_written(out_path, old_size, new_size)
                    progress = float(meta.get("progress") or 100.0)
                    image_url = public_image_url(request, job_id)
                    store.update(
                        job_id,
                        status="succeeded",
                        progress=max(progress, 100.0),
                        image_url=image_url,
                    )
                    return
                except quota_error_cls:
                    token_manager.report_exhausted(token)
                    last_error = "Token quota exhausted."
                    retryable = attempt < max_attempts
                except auth_error_cls:
                    token_manager.report_invalid(token)
                    last_error = "Token invalid or expired."
                    retryable = attempt < max_attempts
                except upstream_temp_error_cls as exc:
                    last_error = str(exc)
                    retryable = (
                        attempt < max_attempts
                        and client.should_retry_temporary_error(exc)
                    )
                except Exception as exc:
                    store.update(job_id, status="failed", error=str(exc))
                    return

                if retryable:
                    delay = client._retry_delay_for_attempt(attempt)
                    if delay > 0:
                        time.sleep(delay)
                    continue
                break

            store.update(job_id, status="failed", error=last_error)

        threading.Thread(target=runner, args=(job.id,), daemon=True).start()

        return {"task_id": job.id, "status": job.status}

    @router.get("/api/v1/generate/{task_id}")
    def get_job(task_id: str, request: Request):
        require_service_api_key(request)

        job = store.get(task_id)
        if not job:
            raise HTTPException(status_code=404, detail="task not found")
        return asdict(job)

    @router.post("/v1/chat/completions")
    def chat_completions(data: dict, request: Request):
        require_service_api_key(request)

        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": "This endpoint is disabled. Use /v1/images/generations for image generation or /v1/images/edits for image editing.",
                    "type": "invalid_request_error",
                }
            },
        )

        prompt = extract_prompt_from_messages(data.get("messages") or [])
        if not prompt:
            prompt = str(data.get("prompt") or "").strip()
        if not prompt:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": "messages or prompt is required",
                        "type": "invalid_request_error",
                    }
                },
            )

        model_id = str(data.get("model") or "").strip()
        if (
            model_id.startswith("firefly-sora2")
            or model_id.startswith("firefly-veo31-fast")
            or model_id.startswith("firefly-veo31-")
            or model_id.startswith("firefly-kling-")
        ) and model_id not in video_model_catalog:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": "Invalid video model. Use /v1/models to get supported firefly-sora2-*, firefly-veo31-*, firefly-veo31-fast-* or firefly-kling-* models",
                        "type": "invalid_request_error",
                    }
                },
            )
        video_conf = video_model_catalog.get(model_id)
        is_video_model = video_conf is not None
        is_gpt_image_alias_model = (
            not is_video_model and _is_gpt_image_model_or_alias(model_id)
        )
        resolved_model_id = model_id if is_video_model else None
        ratio = "9:16"
        output_resolution = "2K"
        image_options = None
        image_model_conf: dict = {}
        duration = int(video_conf["duration"]) if video_conf else 12
        video_resolution = (
            str(video_conf.get("resolution") or "720p") if video_conf else "720p"
        )
        if video_conf:
            ratio = str(video_conf.get("aspect_ratio") or ratio)
        video_engine = str(video_conf.get("engine") or "sora2") if video_conf else ""
        generate_audio = True
        negative_prompt = ""
        video_reference_mode = (
            str(video_conf.get("reference_mode") or "frame") if video_conf else "frame"
        )
        if is_video_model:
            resolved_video_options = resolve_video_options(data)
            if (
                isinstance(resolved_video_options, tuple)
                and len(resolved_video_options) == 3
            ):
                generate_audio, negative_prompt, requested_reference_mode = (
                    resolved_video_options
                )
                if "reference_mode" not in (video_conf or {}):
                    video_reference_mode = requested_reference_mode
            else:
                generate_audio, negative_prompt = resolved_video_options
            if not any(k in data for k in ("generate_audio", "generateAudio")):
                generate_audio = bool(video_conf.get("generate_audio", generate_audio))
        elif is_gpt_image_alias_model:
            image_options, image_model_conf, resolved_model_id = (
                _build_gpt_image_alias_options(data, model_id or "gpt-image-2")
            )
            ratio = image_options.aspect_ratio
            output_resolution = image_options.output_resolution
        else:
            ratio, output_resolution, resolved_model_id = resolve_ratio_and_resolution(
                data, model_id or None
            )
            image_model_conf = resolve_model(resolved_model_id)

        try:
            entity_account_id = ""
            kling_bound_refs: list[dict] | None = None
            if video_engine == "kling-o3":
                entity_account_id, kling_bound_refs = _resolve_entity_bindings(prompt)
            input_images = load_input_images(data.get("messages") or [])
            set_request_task_progress(
                request, task_status="IN_PROGRESS", task_progress=0.0
            )

            def _run_once(token: str):
                source_image_ids: list[str] = []
                image_url = ""
                response_content = ""

                if is_video_model:
                    if (
                        video_engine == "veo31-standard"
                        and video_reference_mode == "image"
                    ):
                        max_video_inputs = 3
                    else:
                        max_video_inputs = (
                            2
                            if video_engine
                            in {"veo31-fast", "veo31-standard", "kling-o3", "kling3"}
                            else 1
                        )
                    if len(input_images) > max_video_inputs:
                        raise HTTPException(
                            status_code=400,
                            detail=f"video model supports at most {max_video_inputs} input image(s)",
                        )
                    for image_bytes, _image_mime in input_images[:max_video_inputs]:
                        prepared_bytes, prepared_mime = prepare_video_source_image(
                            image_bytes,
                            ratio,
                            video_resolution,
                        )
                        source_image_ids.append(
                            client.upload_image(token, prepared_bytes, prepared_mime)
                        )

                    def _video_progress_cb(update: dict):
                        set_request_task_progress(
                            request,
                            task_status=str(update.get("task_status") or "IN_PROGRESS"),
                            task_progress=update.get("task_progress"),
                            upstream_job_id=update.get("upstream_job_id"),
                            retry_after=update.get("retry_after"),
                            error=update.get("error"),
                        )

                    job_id = uuid.uuid4().hex
                    tmp_path = generated_dir / f"{job_id}.video.tmp"
                    old_size = 0
                    try:
                        if tmp_path.exists():
                            old_size = int(tmp_path.stat().st_size)
                    except Exception:
                        old_size = 0

                    video_prompt = prompt
                    entity_refs = None
                    if video_engine == "kling-o3":
                        video_prompt, entity_refs = _resolve_kling_entity_refs(
                            token, prompt, kling_bound_refs
                        )

                    video_bytes, video_meta = client.generate_video(
                        token=token,
                        video_conf=video_conf or {},
                        prompt=video_prompt,
                        aspect_ratio=ratio,
                        duration=duration,
                        source_image_ids=source_image_ids,
                        entity_refs=entity_refs,
                        timeout=max(int(client.generate_timeout), 600),
                        negative_prompt=negative_prompt,
                        generate_audio=generate_audio,
                        reference_mode=video_reference_mode,
                        out_path=tmp_path,
                        progress_cb=_video_progress_cb,
                    )
                    video_ext = video_ext_from_meta(video_meta)
                    filename = f"{job_id}.{video_ext}"
                    out_path = generated_dir / filename
                    if video_bytes is not None:
                        out_path.write_bytes(video_bytes)
                    elif tmp_path.exists():
                        tmp_path.replace(out_path)
                    new_size = int(out_path.stat().st_size) if out_path.exists() else 0
                    on_generated_file_written(out_path, old_size, new_size)
                    image_url = public_generated_url(request, filename)
                    set_request_preview(request, image_url, kind="video")
                    response_content = (
                        f"```html\n<video src='{image_url}' controls></video>\n```"
                    )
                else:
                    for image_bytes, image_mime in input_images:
                        source_image_ids.append(
                            client.upload_image(
                                token, image_bytes, image_mime or "image/jpeg"
                            )
                        )

                    def _image_progress_cb(update: dict):
                        set_request_task_progress(
                            request,
                            task_status=str(update.get("task_status") or "IN_PROGRESS"),
                            task_progress=update.get("task_progress"),
                            upstream_job_id=update.get("upstream_job_id"),
                            retry_after=update.get("retry_after"),
                            error=update.get("error"),
                        )

                    job_id = uuid.uuid4().hex
                    out_path = generated_dir / f"{job_id}.png"
                    old_size = 0
                    try:
                        if out_path.exists():
                            old_size = int(out_path.stat().st_size)
                    except Exception:
                        old_size = 0

                    image_bytes, _meta = client.generate(
                        token=token,
                        prompt=prompt,
                        aspect_ratio=ratio,
                        output_resolution=output_resolution,
                        upstream_model_id=str(
                            image_model_conf.get("upstream_model_id") or "gemini-flash"
                        ),
                        upstream_model_version=str(
                            image_model_conf.get("upstream_model_version")
                            or "nano-banana-2"
                        ),
                        quality_level=_gpt_image_quality_for_model(image_model_conf, resolved_model_id),
                        detail_level=image_model_conf.get("detail_level"),
                        source_image_ids=source_image_ids,
                        requested_size=(
                            image_options.requested_size if image_options else None
                        ),
                        timeout=client.generate_timeout,
                        out_path=out_path,
                        progress_cb=_image_progress_cb,
                    )
                    if image_bytes is not None:
                        out_path.write_bytes(image_bytes)
                    new_size = int(out_path.stat().st_size) if out_path.exists() else 0
                    on_generated_file_written(out_path, old_size, new_size)
                    image_url = public_image_url(request, job_id)
                    set_request_preview(request, image_url, kind="image")
                    response_content = f"![Generated Image]({image_url})"

                response_payload = {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": resolved_model_id,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": response_content,
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    },
                }
                if bool(data.get("stream", False)):
                    return StreamingResponse(
                        sse_chat_stream(response_payload),
                        media_type="text/event-stream",
                    )
                return response_payload

            token_selector = None
            if entity_account_id:
                token_selector = lambda: token_manager.get_available_for_account(
                    entity_account_id, strategy=client.token_rotation_strategy
                )
            return run_with_token_retries(
                request=request,
                operation_name="chat.completions",
                run_once=_run_once,
                token_selector=token_selector,
            )
        except quota_error_cls:
            error_code = str(
                getattr(request.state, "log_error_code", "") or ""
            ) or set_request_error_detail(
                request,
                error="Token quota exhausted",
                status_code=429,
                error_type="rate_limit_error",
                include_traceback=False,
            )
            set_request_task_progress(
                request,
                task_status="FAILED",
                task_progress=0.0,
                error="Token quota exhausted",
            )
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "message": "Token quota exhausted",
                        "type": "rate_limit_error",
                        "code": error_code,
                    }
                },
            )
        except auth_error_cls:
            error_code = str(
                getattr(request.state, "log_error_code", "") or ""
            ) or set_request_error_detail(
                request,
                error="Token invalid or expired",
                status_code=401,
                error_type="authentication_error",
                include_traceback=False,
            )
            set_request_task_progress(
                request,
                task_status="FAILED",
                task_progress=0.0,
                error="Token invalid or expired",
            )
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "message": "Token invalid or expired",
                        "type": "authentication_error",
                        "code": error_code,
                    }
                },
            )
        except upstream_temp_error_cls as exc:
            error_code = str(
                getattr(request.state, "log_error_code", "") or ""
            ) or set_request_error_detail(
                request,
                error=exc,
                status_code=503,
                error_type="server_error",
                include_traceback=False,
            )
            set_request_task_progress(
                request, task_status="FAILED", task_progress=0.0, error=str(exc)
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": str(exc),
                        "type": "server_error",
                        "code": error_code,
                    }
                },
            )
        except HTTPException as exc:
            passthrough = _openai_http_exception_response(exc)
            if passthrough is not None:
                return passthrough
            err_type = (
                "invalid_request_error"
                if 400 <= int(exc.status_code) < 500
                else "server_error"
            )
            error_code = set_request_error_detail(
                request,
                error=str(exc.detail),
                status_code=exc.status_code,
                error_type=err_type,
                include_traceback=False,
            )
            set_request_task_progress(
                request, task_status="FAILED", task_progress=0.0, error=str(exc.detail)
            )
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "error": {
                        "message": str(exc.detail),
                        "type": err_type,
                        "code": error_code,
                    }
                },
            )
        except Exception as exc:
            error_code = set_request_error_detail(
                request,
                error=exc,
                status_code=500,
                error_type="server_error",
                include_traceback=True,
            )
            logger.exception(
                "Unhandled error in /v1/chat/completions log_id=%s model=%s resolved_model=%s is_video_model=%s",
                getattr(request.state, "log_id", ""),
                model_id,
                resolved_model_id,
                is_video_model,
            )
            set_request_task_progress(
                request, task_status="FAILED", task_progress=0.0, error=str(exc)
            )
            return JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "message": str(exc),
                        "type": "server_error",
                        "code": error_code,
                    }
                },
            )

    return router
