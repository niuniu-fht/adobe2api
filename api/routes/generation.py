from concurrent.futures import ThreadPoolExecutor, as_completed
import base64
import binascii
import re
import secrets
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
import requests
from starlette.concurrency import run_in_threadpool

from api.schemas import GenerateRequest
from core.entity_store import entity_store
from core.models.openai_images import (
    OpenAIImageRequestError,
    build_legacy_image_options,
    build_native_gpt_image_options,
    encode_image_response_item,
    image_generation_batch_sizes,
    is_native_gpt_image_model,
)


def build_generation_router(
    *,
    store,
    token_manager,
    client,
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
        return {"object": "list", "data": data}

    def _openai_image_error_response(exc: OpenAIImageRequestError) -> JSONResponse:
        error_payload = {
            "message": str(exc),
            "type": "invalid_request_error",
        }
        if exc.param:
            error_payload["param"] = exc.param
        return JSONResponse(status_code=400, content={"error": error_payload})

    def _validate_openai_edit_content_length(request: Request) -> None:
        raw_content_length = str(request.headers.get("content-length") or "").strip()
        if not raw_content_length:
            return
        try:
            content_length = int(raw_content_length)
        except ValueError:
            return
        max_body_bytes = 80 * 1024 * 1024
        if content_length > max_body_bytes:
            raise HTTPException(
                status_code=413,
                detail="request body too large, max 80MB",
            )

    def _normalize_edit_image_mime(mime_type: str) -> str:
        normalized = str(mime_type or "").split(";", 1)[0].strip().lower()
        if normalized == "image/jpg":
            normalized = "image/jpeg"
        if normalized not in {"image/jpeg", "image/png", "image/webp"}:
            normalized = "image/jpeg"
        return normalized

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
            resp = requests.get(image_ref, timeout=30)
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=400,
                    detail=f"Failed to fetch image: {resp.status_code}",
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
        if len(image_bytes) > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="image too large, max 10MB")
        return image_bytes, _normalize_edit_image_mime(mime_type)

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
        _validate_openai_edit_content_length(request)
        content_type = str(request.headers.get("content-type") or "").lower()
        if "application/json" in content_type:
            try:
                data = await request.json()
            except Exception:
                raise HTTPException(status_code=400, detail="invalid JSON body")
            if not isinstance(data, dict):
                raise HTTPException(status_code=400, detail="request body must be JSON")
            raw_images = (
                data.get("image")
                or data.get("images")
                or data.get("image_url")
                or data.get("image_urls")
            )
            image_values = raw_images if isinstance(raw_images, list) else [raw_images]
            input_images = await run_in_threadpool(
                lambda: [
                    _load_edit_image_value(value) for value in image_values if value
                ]
            )
            if len(input_images) > 6:
                raise HTTPException(
                    status_code=400,
                    detail="image edits support at most 6 input images",
                )
            return data, input_images

        form = await request.form()
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

        input_images: list[tuple[bytes, str]] = []
        for value in image_values:
            if hasattr(value, "read"):
                image_bytes = await value.read()
                mime_type = getattr(value, "content_type", None) or "image/jpeg"
                if not image_bytes:
                    raise HTTPException(status_code=400, detail="image is empty")
                if len(image_bytes) > 10 * 1024 * 1024:
                    raise HTTPException(
                        status_code=400,
                        detail="image too large, max 10MB",
                    )
                input_images.append(
                    (image_bytes, _normalize_edit_image_mime(str(mime_type)))
                )
            else:
                input_images.append(
                    await run_in_threadpool(lambda: _load_edit_image_value(value))
                )
            if len(input_images) > 6:
                raise HTTPException(
                    status_code=400,
                    detail="image edits support at most 6 input images",
                )

        return data, input_images

    def _generate_openai_image_items(
        *,
        request: Request,
        token: str,
        prompt: str,
        image_options,
        model_conf: dict,
        source_image_ids: list[str] | None = None,
    ) -> list[dict]:
        source_image_ids = source_image_ids or []

        def _image_progress_cb(update: dict):
            set_request_task_progress(
                request,
                task_status=str(update.get("task_status") or "IN_PROGRESS"),
                task_progress=update.get("task_progress"),
                upstream_job_id=update.get("upstream_job_id"),
                retry_after=update.get("retry_after"),
                error=update.get("error"),
            )

        def _generate_response_item(response_index: int) -> tuple[int, dict]:
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
                aspect_ratio=image_options.aspect_ratio,
                output_resolution=image_options.output_resolution,
                upstream_model_id=str(
                    model_conf.get("upstream_model_id") or "gemini-flash"
                ),
                upstream_model_version=str(
                    model_conf.get("upstream_model_version") or "nano-banana-2"
                ),
                quality_level=(
                    client.gpt_image_quality
                    if str(model_conf.get("upstream_model_id") or "") == "gpt-image"
                    else None
                ),
                detail_level=model_conf.get("detail_level"),
                source_image_ids=source_image_ids,
                requested_size=image_options.requested_size,
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
            return response_index, item

        def _generate_response_batch(
            start_index: int, batch_size: int
        ) -> list[tuple[int, dict]]:
            return [
                _generate_response_item(start_index + offset)
                for offset in range(batch_size)
            ]

        batch_sizes = image_generation_batch_sizes(image_options.n)
        response_pairs: list[tuple[int, dict]] = []
        if len(batch_sizes) <= 1:
            response_pairs = _generate_response_batch(
                0, batch_sizes[0] if batch_sizes else 0
            )
        else:
            with ThreadPoolExecutor(max_workers=len(batch_sizes)) as executor:
                futures = []
                start_index = 0
                for batch_size in batch_sizes:
                    futures.append(
                        executor.submit(
                            _generate_response_batch,
                            start_index,
                            batch_size,
                        )
                    )
                    start_index += batch_size
                for future in as_completed(futures):
                    response_pairs.extend(future.result())

        return [
            item
            for _idx, item in sorted(response_pairs, key=lambda pair: pair[0])
        ]

    def _upload_edit_source_images(
        token: str,
        input_images: list[tuple[bytes, str]],
    ) -> list[str]:
        if not input_images:
            return []
        max_workers = min(3, len(input_images))
        if max_workers <= 1:
            return [
                client.upload_image(token, image_bytes, image_mime or "image/jpeg")
                for image_bytes, image_mime in input_images
            ]
        indexed_images = list(enumerate(input_images))
        source_pairs: list[tuple[int, str]] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(
                    lambda idx=idx, item=item: (
                        idx,
                        client.upload_image(
                            token,
                            item[0],
                            item[1] or "image/jpeg",
                        ),
                    )
                )
                for idx, item in indexed_images
            ]
            for future in as_completed(futures):
                source_pairs.append(future.result())
        return [
            image_id
            for _idx, image_id in sorted(source_pairs, key=lambda pair: pair[0])
        ]

    @router.post("/v1/images/generations")
    def openai_generate(data: dict, request: Request):
        require_service_api_key(request)

        prompt = str(data.get("prompt") or "").strip()
        if not prompt:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": "prompt is required",
                        "type": "invalid_request_error",
                    }
                },
            )

        model_id = str(data.get("model") or "").strip()
        if model_id in video_model_catalog:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": "Use /v1/chat/completions for video generation",
                        "type": "invalid_request_error",
                    }
                },
            )
        try:
            if is_native_gpt_image_model(model_id):
                image_options = build_native_gpt_image_options(data)
                model_conf = {
                    "upstream_model_id": image_options.upstream_model_id,
                    "upstream_model_version": image_options.upstream_model_version,
                }
                resolved_model_id = image_options.response_model
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
            error_payload = {
                "message": str(exc),
                "type": "invalid_request_error",
            }
            if exc.param:
                error_payload["param"] = exc.param
            return JSONResponse(status_code=400, content={"error": error_payload})

        try:
            set_request_task_progress(
                request, task_status="IN_PROGRESS", task_progress=0.0
            )

            def _run_once(token: str):
                response_items = _generate_openai_image_items(
                    request=request,
                    token=token,
                    prompt=prompt,
                    image_options=image_options,
                    model_conf=model_conf,
                )

                response_payload = {
                    "created": int(time.time()),
                    "data": response_items,
                }
                if not image_options.is_native_gpt_image:
                    response_payload["model"] = resolved_model_id
                return response_payload

            return run_with_token_retries(
                request=request,
                operation_name="images.generations",
                run_once=_run_once,
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
                error_type="invalid_request_error",
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
                        "type": "invalid_request_error",
                        "code": error_code,
                    }
                },
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
            return JSONResponse(
                status_code=getattr(exc, "status_code", 502) or 502,
                content={
                    "error": {
                        "message": str(exc),
                        "type": "server_error",
                        "code": error_code,
                    }
                },
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

    @router.post("/v1/images/edits")
    async def openai_edit(request: Request):
        require_service_api_key(request)
        try:
            data, input_images = await _parse_openai_edit_request(request)
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "error": {
                        "message": str(exc.detail),
                        "type": "invalid_request_error",
                    }
                },
            )

        prompt = str(data.get("prompt") or "").strip()
        if not prompt:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": "prompt is required",
                        "type": "invalid_request_error",
                    }
                },
            )
        if not input_images:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": "image is required",
                        "type": "invalid_request_error",
                        "param": "image",
                    }
                },
            )

        model_id = str(data.get("model") or "gpt-image-2").strip()
        if model_id in video_model_catalog:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": "Video models are not supported for image edits",
                        "type": "invalid_request_error",
                    }
                },
            )

        try:
            if is_native_gpt_image_model(model_id):
                data["model"] = model_id
                image_options = build_native_gpt_image_options(data)
                model_conf = {
                    "upstream_model_id": image_options.upstream_model_id,
                    "upstream_model_version": image_options.upstream_model_version,
                }
                resolved_model_id = image_options.response_model
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
            return _openai_image_error_response(exc)

        try:
            set_request_task_progress(
                request, task_status="IN_PROGRESS", task_progress=0.0
            )

            def _run_once(token: str):
                source_image_ids = _upload_edit_source_images(token, input_images)
                response_items = _generate_openai_image_items(
                    request=request,
                    token=token,
                    prompt=prompt,
                    image_options=image_options,
                    model_conf=model_conf,
                    source_image_ids=source_image_ids,
                )
                response_payload = {
                    "created": int(time.time()),
                    "data": response_items,
                }
                if not image_options.is_native_gpt_image:
                    response_payload["model"] = resolved_model_id
                return response_payload

            return await run_in_threadpool(
                lambda: run_with_token_retries(
                    request=request,
                    operation_name="images.edits",
                    run_once=_run_once,
                )
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
                error_type="invalid_request_error",
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
                        "type": "invalid_request_error",
                        "code": error_code,
                    }
                },
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
            return JSONResponse(
                status_code=getattr(exc, "status_code", 502) or 502,
                content={
                    "error": {
                        "message": str(exc),
                        "type": "server_error",
                        "code": error_code,
                    }
                },
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
                        quality_level=(
                            client.gpt_image_quality
                            if str(model_conf.get("upstream_model_id") or "") == "gpt-image"
                            else None
                        ),
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
        resolved_model_id = model_id if is_video_model else None
        ratio = "9:16"
        output_resolution = "2K"
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
        else:
            ratio, output_resolution, resolved_model_id = resolve_ratio_and_resolution(
                data, model_id or None
            )
        image_model_conf = (
            resolve_model(resolved_model_id) if not is_video_model else {}
        )

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
                        quality_level=(
                            client.gpt_image_quality
                            if str(image_model_conf.get("upstream_model_id") or "")
                            == "gpt-image"
                            else None
                        ),
                        detail_level=image_model_conf.get("detail_level"),
                        source_image_ids=source_image_ids,
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
