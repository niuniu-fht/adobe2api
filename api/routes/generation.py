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
from core.models.payloads import gpt_image_pixels_from_ratio, size_from_ratio
from core.models.image_limits import (
    MAX_INPUT_IMAGES,
    MAX_SINGLE_IMAGE_BYTES,
    MAX_TOTAL_IMAGE_BYTES,
    ImageInputLimitError,
    add_input_image_bytes,
    validate_input_image_count,
)


SEEDANCE2_RATIOS = {
    "auto",
    "adaptive",
    "21:9",
    "16:9",
    "4:3",
    "1:1",
    "3:4",
    "9:16",
}
SEEDANCE_OFFICIAL_MODEL_ALIASES = {
    "seedance-2.0": "seedance2",
    "seedance-2-0": "seedance2",
    "seedance-2-0-260128": "seedance2",
    "dreamina-seedance-2-0": "seedance2",
    "dreamina-seedance-2-0-260128": "seedance2",
    "seedance-2.0-fast": "seedance2-fast",
    "seedance-2-0-fast": "seedance2-fast",
    "seedance-2-0-fast-260128": "seedance2-fast",
    "dreamina-seedance-2-0-fast": "seedance2-fast",
    "dreamina-seedance-2-0-fast-260128": "seedance2-fast",
}
SEEDANCE_OFFICIAL_MODEL_IDS = {
    "seedance2": "dreamina-seedance-2-0-260128",
    "seedance2-fast": "dreamina-seedance-2-0-fast-260128",
}
SEEDANCE_VIDEO_MAX_BYTES = 50 * 1024 * 1024
SEEDANCE_AUDIO_MAX_BYTES = 50 * 1024 * 1024
SEEDANCE_IMAGE_MAX_BYTES = 50 * 1024 * 1024
SEEDANCE_MAX_MULTIMODAL_REFERENCES = 9
GROK_VIDEO_MODELS = {
    "grok-imagine-video",
    "grok-imagine-video-1.5",
}
GROK_VIDEO_RATIOS = {"16:9", "9:16", "4:3", "3:4", "1:1"}
SEEDANCE_MEDIA_MIME_TYPES = {
    "video": {
        "video/mp4",
        "video/quicktime",
        "video/x-m4v",
    },
    "audio": {
        "audio/aac",
        "audio/aiff",
        "audio/m4a",
        "audio/mp4",
        "audio/mpeg",
        "audio/wav",
        "audio/x-aiff",
        "audio/x-m4a",
        "audio/x-wav",
    },
}
SEEDANCE_MEDIA_EXTENSION_MIMES = {
    "video": {
        ".m4v": "video/x-m4v",
        ".mov": "video/quicktime",
        ".mp4": "video/mp4",
    },
    "audio": {
        ".aac": "audio/aac",
        ".aif": "audio/aiff",
        ".aiff": "audio/aiff",
        ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
    },
}

VIDEO_MODEL_DIMENSION_FIELDS = (
    "duration",
    "seconds",
    "ratio",
    "aspect_ratio",
    "aspectRatio",
)


def reject_video_model_dimensions(data: dict) -> None:
    supplied = [field for field in VIDEO_MODEL_DIMENSION_FIELDS if field in data]
    if supplied:
        raise ValueError(
            "duration and ratio are encoded in model; remove: "
            + ", ".join(supplied)
        )


def _normalize_seedance_media_mime(
    media_type: str, mime_type: str, source: str = ""
) -> str:
    kind = str(media_type or "").strip().lower()
    if kind not in SEEDANCE_MEDIA_MIME_TYPES:
        raise ValueError("media_type must be video or audio")

    normalized = str(mime_type or "").split(";", 1)[0].strip().lower()
    aliases = {
        "audio/mp3": "audio/mpeg",
        "audio/mpeg3": "audio/mpeg",
        "audio/x-mpeg-3": "audio/mpeg",
        "video/mov": "video/quicktime",
    }
    normalized = aliases.get(normalized, normalized)
    suffix = Path(str(source or "").split("?", 1)[0]).suffix.lower()
    guessed = SEEDANCE_MEDIA_EXTENSION_MIMES[kind].get(suffix)
    if normalized in {"", "application/octet-stream", "binary/octet-stream"}:
        normalized = guessed or ("video/mp4" if kind == "video" else "audio/mpeg")
    elif normalized not in SEEDANCE_MEDIA_MIME_TYPES[kind] and guessed:
        normalized = guessed
    if normalized not in SEEDANCE_MEDIA_MIME_TYPES[kind]:
        supported = ", ".join(sorted(SEEDANCE_MEDIA_MIME_TYPES[kind]))
        raise ValueError(f"unsupported {kind} content type {normalized}; use {supported}")
    return normalized


def load_seedance_media_reference(
    raw_value: str,
    media_type: str,
    *,
    proxies: dict | None = None,
) -> tuple[bytes, str]:
    """Load an official-style media value into memory for Adobe asset upload."""
    kind = str(media_type or "").strip().lower()
    if kind not in {"video", "audio"}:
        raise ValueError("media_type must be video or audio")
    value = str(raw_value or "").strip()
    if not value:
        raise ValueError(f"{kind}_url.url is required")

    max_bytes = (
        SEEDANCE_VIDEO_MAX_BYTES if kind == "video" else SEEDANCE_AUDIO_MAX_BYTES
    )
    mime_type = ""
    if value.startswith("data:"):
        head, sep, body = value.partition(",")
        if not sep or ";base64" not in head.lower():
            raise ValueError(f"{kind} data URL must be base64 encoded")
        mime_type = head[5:].split(";", 1)[0].strip()
        try:
            media_bytes = base64.b64decode(re.sub(r"\s+", "", body), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"invalid base64 {kind} data") from exc
    elif value.lower().startswith(("http://", "https://")):
        try:
            response = requests.get(
                value,
                timeout=60,
                proxies=proxies,
                stream=True,
            )
        except requests.RequestException as exc:
            raise ValueError(f"failed to fetch {kind}_url: {exc}") from exc
        try:
            if response.status_code != 200:
                raise ValueError(
                    f"failed to fetch {kind}_url: HTTP {response.status_code}"
                )
            content_length = str(response.headers.get("content-length") or "").strip()
            if content_length:
                try:
                    declared_size = int(content_length)
                except ValueError:
                    declared_size = 0
                if declared_size > max_bytes:
                    raise ValueError(
                        f"{kind} is too large, max {max_bytes // (1024 * 1024)}MB"
                    )
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(
                        f"{kind} is too large, max {max_bytes // (1024 * 1024)}MB"
                    )
                chunks.append(chunk)
            media_bytes = b"".join(chunks)
            mime_type = str(response.headers.get("content-type") or "")
        finally:
            response.close()
    else:
        try:
            media_bytes = base64.b64decode(
                re.sub(r"\s+", "", value), validate=True
            )
        except (binascii.Error, ValueError) as exc:
            raise ValueError(
                f"{kind} must be an http/https URL, data URL, or base64 string"
            ) from exc

    if not media_bytes:
        raise ValueError(f"{kind} is empty")
    if len(media_bytes) > max_bytes:
        raise ValueError(f"{kind} is too large, max {max_bytes // (1024 * 1024)}MB")
    return media_bytes, _normalize_seedance_media_mime(kind, mime_type, value)


def normalize_seedance_official_model(
    model: str, video_model_catalog: dict | None = None
) -> str:
    value = str(model or "").strip().lower()
    if value in {"seedance2", "seedance2-fast"}:
        return value
    alias = SEEDANCE_OFFICIAL_MODEL_ALIASES.get(value, "")
    if alias:
        return alias
    conf = (video_model_catalog or {}).get(value) or {}
    if str(conf.get("engine") or "") in {"seedance2", "seedance2-fast"}:
        return value
    return ""


def parse_seedance_official_request(data: dict, video_model_catalog: dict) -> dict:
    requested_model = str(data.get("model") or "").strip().lower()
    model = normalize_seedance_official_model(requested_model, video_model_catalog)
    if not model or model not in video_model_catalog:
        raise ValueError(
            "model must be a Seedance official ID or sd2-{4s|6s|8s}-{16x9|9x16}-{720p|1080p}, "
            "or sd2-fast-{4s|6s|8s}-{16x9|9x16}-{480p|720p}"
        )

    content = data.get("content")
    if not isinstance(content, list) or not content:
        raise ValueError("content must be a non-empty array")

    prompt_parts: list[str] = []
    image_refs: list[dict] = []
    video_refs: list[dict] = []
    audio_refs: list[dict] = []
    for item in content:
        if not isinstance(item, dict):
            raise ValueError("content items must be objects")
        item_type = str(item.get("type") or "").strip().lower()
        if item_type == "text":
            text = str(item.get("text") or "").strip()
            if text:
                prompt_parts.append(text)
            continue
        if item_type == "image_url":
            image_value = item.get("image_url")
            if isinstance(image_value, dict):
                image_url = str(image_value.get("url") or "").strip()
            else:
                image_url = str(image_value or "").strip()
            if not image_url:
                raise ValueError("image_url.url is required")
            role = str(item.get("role") or "").strip().lower()
            if role not in {"", "first_frame", "last_frame", "reference_image"}:
                raise ValueError(
                    "image_url.role must be first_frame, last_frame or reference_image"
                )
            image_refs.append({"url": image_url, "role": role})
            continue
        if item_type in {"video_url", "audio_url"}:
            value = item.get(item_type)
            if isinstance(value, dict):
                media_url = str(value.get("url") or "").strip()
            else:
                media_url = str(value or "").strip()
            if not media_url:
                raise ValueError(f"{item_type}.url is required")
            expected_role = "reference_video" if item_type == "video_url" else "reference_audio"
            role = str(item.get("role") or "").strip().lower()
            if role != expected_role:
                raise ValueError(f"{item_type}.role must be {expected_role}")
            target = video_refs if item_type == "video_url" else audio_refs
            target.append({"url": media_url, "role": role})
            continue
        if item_type == "draft_task":
            raise ValueError("draft_task is not supported by the Adobe Firefly bridge")
        raise ValueError(
            "content.type must be text, image_url, video_url or audio_url"
        )

    frame_refs = [
        item for item in image_refs if item["role"] in {"first_frame", "last_frame"}
    ]
    media_refs = [item for item in image_refs if item["role"] == "reference_image"]
    untyped_refs = [item for item in image_refs if not item["role"]]
    if frame_refs and (media_refs or video_refs or audio_refs):
        raise ValueError(
            "first/last frame roles cannot be mixed with multimodal references"
        )
    if frame_refs and len(frame_refs) > 2:
        raise ValueError("at most two frame images are supported")
    if media_refs and len(media_refs) > 9:
        raise ValueError("at most nine reference images are supported")
    if len(video_refs) > 3:
        raise ValueError("at most three reference videos are supported")
    if len(audio_refs) > 3:
        raise ValueError("at most three reference audio files are supported")
    if len(media_refs) + len(video_refs) + len(audio_refs) > SEEDANCE_MAX_MULTIMODAL_REFERENCES:
        raise ValueError("at most nine multimodal references are supported by Adobe")
    if audio_refs and not (media_refs or video_refs):
        raise ValueError("reference_audio requires at least one image or video reference")
    if untyped_refs:
        if frame_refs or media_refs or video_refs or audio_refs:
            raise ValueError("typed and untyped image roles cannot be mixed")
        if len(image_refs) > 2:
            raise ValueError("untyped image_url content supports at most two images")
        for item in untyped_refs:
            item["role"] = "first_frame"
        if len(untyped_refs) == 2:
            untyped_refs[1]["role"] = "last_frame"
    if sum(item["role"] == "first_frame" for item in image_refs) > 1:
        raise ValueError("only one first_frame image is supported")
    if sum(item["role"] == "last_frame" for item in image_refs) > 1:
        raise ValueError("only one last_frame image is supported")

    conf = video_model_catalog[model]
    is_fixed_preset = bool(conf.get("fixed_parameters", False))
    if is_fixed_preset:
        reject_video_model_dimensions(data)
    default_ratio = str(conf.get("aspect_ratio") or "adaptive") if is_fixed_preset else "adaptive"
    ratio = str(data.get("ratio") or default_ratio).strip().lower()
    if ratio == "adaptive":
        upstream_ratio = "auto"
    else:
        upstream_ratio = ratio
    if ratio not in SEEDANCE2_RATIOS:
        raise ValueError(
            "ratio must be 16:9, 4:3, 1:1, 3:4, 9:16, 21:9 or adaptive"
        )

    resolution = str(
        data.get("resolution") or conf.get("resolution") or "720p"
    ).strip().lower()
    supported = tuple(
        str(value).lower() for value in conf.get("supported_resolutions", ())
    )
    if resolution not in supported:
        raise ValueError(
            f"resolution {resolution} is not supported by {model}; use {', '.join(supported)}"
        )

    raw_duration = data.get("duration")
    if raw_duration is None:
        raw_duration = conf.get("duration", 5)
    try:
        duration = int(raw_duration)
    except (TypeError, ValueError) as exc:
        raise ValueError("duration must be an integer between 4 and 15") from exc
    if duration < 4 or duration > 15:
        raise ValueError("duration must be an integer between 4 and 15")
    if is_fixed_preset:
        if ratio != str(conf.get("aspect_ratio") or ""):
            raise ValueError(
                f"model {model} fixes ratio to {conf.get('aspect_ratio')}"
            )
        if resolution != str(conf.get("resolution") or ""):
            raise ValueError(
                f"model {model} fixes resolution to {conf.get('resolution')}"
            )
        if duration != int(conf.get("duration") or 0):
            raise ValueError(
                f"model {model} fixes duration to {conf.get('duration')} seconds"
            )
    if data.get("frames") is not None:
        raise ValueError("frames is not supported by the Adobe Firefly bridge")
    if bool(data.get("watermark", False)):
        raise ValueError("watermark=true is not supported by the Adobe Firefly bridge")
    if bool(data.get("return_last_frame", False)):
        raise ValueError(
            "return_last_frame=true is not supported by the Adobe Firefly bridge"
        )
    if bool(data.get("camera_fixed", False)):
        raise ValueError("camera_fixed=true is not supported by Seedance 2.0")
    if str(data.get("service_tier") or "default").lower() != "default":
        raise ValueError("only service_tier=default is supported by the Adobe bridge")

    seed = data.get("seed")
    if seed is not None:
        try:
            seed = int(seed)
        except (TypeError, ValueError) as exc:
            raise ValueError("seed must be an integer between 0 and 4294967295") from exc
        if seed < 0 or seed > 4294967295:
            raise ValueError("seed must be an integer between 0 and 4294967295")

    prompt = "\n".join(prompt_parts).strip()
    if not prompt and not image_refs and not video_refs:
        raise ValueError("content must include text, image_url or video_url")
    if len(prompt) > 2500:
        raise ValueError("prompt must be at most 2500 characters")
    callback_url = str(data.get("callback_url") or "").strip()
    if callback_url and not callback_url.lower().startswith(("http://", "https://")):
        raise ValueError("callback_url must use http or https")
    return {
        "model": model,
        "official_model": SEEDANCE_OFFICIAL_MODEL_IDS[
            str(conf.get("canonical_model") or model)
        ],
        "response_model": (
            requested_model
            if is_fixed_preset
            else SEEDANCE_OFFICIAL_MODEL_IDS[model]
        ),
        "prompt": prompt,
        "ratio": ratio,
        "upstream_ratio": upstream_ratio,
        "resolution": resolution,
        "duration": duration,
        "generate_audio": bool(data.get("generate_audio", True)),
        "seed": seed,
        "image_refs": image_refs,
        "video_refs": video_refs,
        "audio_refs": audio_refs,
        "callback_url": callback_url or None,
        "user": str(data.get("user") or "").strip() or None,
    }


def _grok_input_url(value: Any, field_name: str) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    if str(value.get("file_id") or "").strip():
        raise ValueError(f"{field_name}.file_id is not supported by this bridge; use url")
    return str(value.get("url") or value.get("image_url") or "").strip()


def parse_grok_video_request(data: dict, video_model_catalog: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("request body must be an object")

    requested_model = str(data.get("model") or "grok-imagine-video").strip().lower()
    seedance_preset = video_model_catalog.get(requested_model)
    is_seedance_preset = bool(
        isinstance(seedance_preset, dict)
        and seedance_preset.get("fixed_parameters")
        and str(seedance_preset.get("engine") or "")
        in {"seedance2", "seedance2-fast"}
    )
    if requested_model not in GROK_VIDEO_MODELS and not is_seedance_preset:
        raise ValueError(
            "model must be grok-imagine-video, grok-imagine-video-1.5, or an sd2-* / sd2-fast-* fixed model"
        )

    if is_seedance_preset:
        reject_video_model_dimensions(data)

    default_duration = int(seedance_preset.get("duration") or 8) if is_seedance_preset else 8
    raw_duration = data.get(
        "duration",
        data.get("seconds", default_duration),
    )
    try:
        duration = int(raw_duration)
    except (TypeError, ValueError) as exc:
        raise ValueError("duration must be an integer between 4 and 15") from exc
    if duration < 4 or duration > 15:
        raise ValueError("duration must be an integer between 4 and 15")

    default_ratio = (
        str(seedance_preset.get("aspect_ratio") or "16:9")
        if is_seedance_preset
        else "16:9"
    )
    ratio = str(
        data.get("aspect_ratio") or data.get("ratio") or default_ratio
    ).strip()
    if ratio not in GROK_VIDEO_RATIOS:
        raise ValueError("aspect_ratio must be one of: 16:9, 9:16, 4:3, 3:4, 1:1")

    default_resolution = (
        str(seedance_preset.get("resolution") or "480p")
        if is_seedance_preset
        else "480p"
    )
    resolution = str(
        data.get("resolution") or data.get("output_resolution") or default_resolution
    ).strip().lower()
    if resolution not in {"480p", "720p", "1080p"}:
        raise ValueError("resolution must be 480p, 720p or 1080p")
    internal_model = (
        requested_model
        if is_seedance_preset
        else ("seedance2" if resolution == "1080p" else "seedance2-fast")
    )

    prompt = str(data.get("prompt") or "").strip()
    image_url = _grok_input_url(data.get("image"), "image")
    reference_values = data.get("reference_images") or []
    if not isinstance(reference_values, list):
        raise ValueError("reference_images must be an array")
    if len(reference_values) > 9:
        raise ValueError("reference_images supports at most nine images")
    reference_urls = [
        _grok_input_url(value, f"reference_images[{index}]")
        for index, value in enumerate(reference_values)
    ]
    if any(not value for value in reference_urls):
        raise ValueError("reference_images.url is required")
    if image_url and reference_urls:
        raise ValueError("image and reference_images cannot be used in the same request")
    if not prompt and not image_url:
        raise ValueError("prompt is required unless image is provided")
    if reference_urls and not prompt:
        raise ValueError("prompt is required when reference_images is provided")

    content: list[dict] = []
    if prompt:
        content.append({"type": "text", "text": prompt})
    if image_url:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": image_url},
                "role": "first_frame",
            }
        )
    for value in reference_urls:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": value},
                "role": "reference_image",
            }
        )

    bridge_request = {
        "model": internal_model,
        "content": content,
        "resolution": resolution,
        "generate_audio": bool(data.get("generate_audio", True)),
        "seed": data.get("seed"),
        "user": data.get("user"),
    }
    if not is_seedance_preset:
        bridge_request["duration"] = duration
        bridge_request["ratio"] = ratio
    normalized = parse_seedance_official_request(
        bridge_request, video_model_catalog
    )
    normalized["response_model"] = requested_model
    normalized["grok_model"] = requested_model
    return normalized


def build_grok_video_task_response(task) -> dict:
    status = str(task.status or "queued").lower()
    if status == "succeeded":
        return {
            "status": "done",
            "progress": 100,
            "video": {
                "url": task.video_url,
                "duration": int(task.duration),
                "respect_moderation": True,
            },
            "model": task.model,
        }
    if status == "failed":
        raw_error = task.error if isinstance(task.error, dict) else {}
        raw_code = str(raw_error.get("code") or "internal_error")
        code_map = {
            "InvalidInput": "invalid_argument",
            "AuthenticationError": "permission_denied",
            "QuotaExceeded": "failed_precondition",
            "UpstreamUnavailable": "service_unavailable",
        }
        return {
            "status": "failed",
            "error": {
                "code": code_map.get(raw_code, "internal_error"),
                "message": str(raw_error.get("message") or "video generation failed"),
            },
        }
    return {
        "status": "pending",
        "progress": max(0, min(99, int(round(float(task.progress or 0))))),
        "model": task.model,
    }


def resolve_video_request_parameters(
    data: dict, video_conf: dict
) -> tuple[int, str, str, int | None]:
    duration = int(video_conf.get("duration") or 8)
    ratio = str(video_conf.get("aspect_ratio") or "16:9")
    resolution = str(video_conf.get("resolution") or "720p").lower()
    seed = None

    if bool(video_conf.get("fixed_parameters", False)):
        reject_video_model_dimensions(data)
        fixed_duration = int(video_conf.get("duration") or duration)
        fixed_ratio = str(video_conf.get("aspect_ratio") or ratio)
        fixed_resolution = str(video_conf.get("resolution") or resolution).lower()
        requested_resolution = str(
            data.get("resolution") or data.get("output_resolution") or fixed_resolution
        ).strip().lower()
        if requested_resolution != fixed_resolution:
            raise ValueError(f"model fixes resolution to {fixed_resolution}")
        raw_seed = data.get("seed")
        if raw_seed is not None and str(raw_seed).strip() != "":
            try:
                seed = int(raw_seed)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "seed must be an integer between 0 and 4294967295"
                ) from exc
            if seed < 0 or seed > 4294967295:
                raise ValueError("seed must be an integer between 0 and 4294967295")
        return fixed_duration, fixed_ratio, fixed_resolution, seed

    engine = str(video_conf.get("engine") or "")
    if engine not in {"seedance2", "seedance2-fast"}:
        return duration, ratio, resolution, seed

    try:
        duration = int(data.get("duration", duration))
    except (TypeError, ValueError) as exc:
        raise ValueError("duration must be an integer between 4 and 15") from exc
    if duration < 4 or duration > 15:
        raise ValueError("duration must be an integer between 4 and 15")

    ratio = str(
        data.get("aspect_ratio") or data.get("aspectRatio") or ratio
    ).strip()
    if ratio not in SEEDANCE2_RATIOS:
        raise ValueError(
            "aspect_ratio must be one of: auto, 21:9, 16:9, 4:3, 1:1, 3:4, 9:16"
        )

    resolution = str(
        data.get("resolution")
        or data.get("output_resolution")
        or resolution
    ).strip().lower()
    supported_resolutions = tuple(
        str(value).lower()
        for value in video_conf.get("supported_resolutions", ("480p", "720p"))
    )
    if resolution not in supported_resolutions:
        raise ValueError(
            f"resolution must be one of: {', '.join(supported_resolutions)}"
        )

    raw_seed = data.get("seed")
    if raw_seed is not None and str(raw_seed).strip() != "":
        try:
            seed = int(raw_seed)
        except (TypeError, ValueError) as exc:
            raise ValueError("seed must be an integer between 0 and 4294967295") from exc
        if seed < 0 or seed > 4294967295:
            raise ValueError("seed must be an integer between 0 and 4294967295")

    return duration, ratio, resolution, seed


def build_generation_router(
    *,
    store,
    seedance_task_store,
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
    load_input_images: Callable[..., list[tuple[bytes, str]]],
    prepare_video_source_image: Callable[[bytes, str, str], tuple[bytes, str]],
    video_ext_from_meta: Callable[[dict], str],
    extract_prompt_from_messages: Callable[[Any], str],
    sse_chat_stream: Callable[[dict], Any],
    on_generated_file_written: Callable[[Path, int, int], None],
    update_deferred_request_log: Callable[..., None],
    quota_error_cls,
    auth_error_cls,
    upstream_temp_error_cls,
    logger,
) -> APIRouter:
    router = APIRouter()
    entity_ref_re = re.compile(r"@entity:([^\s@]+)")
    remote_image_error_message = "输入图片下载失败，请确认图片 URL 可公开访问"

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
                    "owned_by": str(conf.get("provider") or "adobe2api"),
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
                    "owned_by": str(conf.get("provider") or "adobe2api"),
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
        if len(image_bytes) > MAX_SINGLE_IMAGE_BYTES:
            raise HTTPException(status_code=400, detail="image too large, max 30MB")
        return image_bytes, _normalize_edit_image_mime(mime_type)

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
                loaded_image = (
                    image_bytes,
                    _normalize_edit_image_mime(str(mime_type)),
                )
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
                quality_level=_gpt_image_quality_for_model(model_conf, image_options.response_model),
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
                    token, gemini_options.input_images
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
            error_payload = {
                "message": str(exc),
                "type": "invalid_request_error",
            }
            if exc.param:
                error_payload["param"] = exc.param
            return JSONResponse(status_code=400, content={"error": error_payload})

        _set_image_log_context(
            request,
            data=data,
            prompt=prompt,
            image_options=image_options,
            request_type="generation",
        )

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
        except HTTPException as exc:
            passthrough = _openai_http_exception_response(exc)
            if passthrough is not None:
                return passthrough
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
            return JSONResponse(
                status_code=status_code,
                content={
                    "error": {
                        "message": str(exc.detail),
                        "type": err_type,
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
            passthrough = _openai_http_exception_response(exc)
            if passthrough is not None:
                return passthrough
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
            return JSONResponse(
                status_code=status_code,
                content={
                    "error": {
                        "message": str(exc.detail),
                        "type": error_type,
                        "code": error_code,
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
        try:
            request.state.log_model = model_id or "gpt-image-2"
            request.state.log_prompt = prompt or None
            request.state.log_prompt_preview = (
                prompt.replace("\r", " ").replace("\n", " ").strip()[:180] or None
            )
        except Exception:
            pass
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
            return _openai_image_error_response(exc)

        _set_image_log_context(
            request,
            data=data,
            prompt=prompt,
            image_options=image_options,
            request_type="edits",
            input_image_count=len(input_images),
        )

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
        except HTTPException as exc:
            passthrough = _openai_http_exception_response(exc)
            if passthrough is not None:
                return passthrough
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
            return JSONResponse(
                status_code=status_code,
                content={
                    "error": {
                        "message": str(exc.detail),
                        "type": err_type,
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

    def _seedance_task_response(task) -> dict:
        payload = {
            "id": task.id,
            "model": task.model,
            "status": task.status,
            "error": task.error,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "content": {"video_url": task.video_url} if task.video_url else None,
            "resolution": task.resolution,
            "ratio": task.ratio,
            "duration": task.duration,
            "framespersecond": 24,
            "progress": round(float(task.progress or 0), 2),
        }
        if task.seed is not None:
            payload["seed"] = task.seed
        if task.user:
            payload["user"] = task.user
        return payload

    def _bind_deferred_task_log(request: Request, task, normalized: dict) -> None:
        log_id = str(getattr(task, "log_id", None) or "").strip()
        if not log_id:
            return
        request.state.log_deferred = True
        request.state.log_task_id = task.id
        request.state.log_task_status = "IN_PROGRESS"
        request.state.log_task_progress = 0.0
        request.state.log_model = task.model
        request.state.log_prompt = task.prompt or None
        request.state.log_prompt_preview = (
            task.prompt.replace("\r", " ").replace("\n", " ")[:180]
            if task.prompt
            else None
        )
        request.state.log_resolution = task.resolution
        request.state.log_request_params = (
            f"duration={task.duration}, aspect_ratio={task.ratio}, "
            f"resolution={task.resolution}, generate_audio={task.generate_audio}"
        )
        input_urls = [
            str(item.get("url") or "")
            for item in normalized.get("image_refs", [])
            if str(item.get("url") or "").lower().startswith(("http://", "https://"))
        ]
        request.state.log_input_image_urls = input_urls or None
        update_deferred_request_log(
            log_id,
            {
                "task_id": task.id,
                "task_status": "IN_PROGRESS",
                "task_progress": 0.0,
                "status_code": 202,
                "model": task.model,
                "prompt": task.prompt or None,
                "prompt_preview": request.state.log_prompt_preview,
                "resolution": task.resolution,
                "request_params": request.state.log_request_params,
                "input_image_urls": request.state.log_input_image_urls,
                "started_at": float(getattr(task, "log_started_at", 0.0) or time.time()),
            },
            final=False,
        )

    def _notify_seedance_callback(task_id: str) -> None:
        task = seedance_task_store.get(task_id)
        if not task or not task.callback_url:
            return
        try:
            requests.post(
                task.callback_url,
                json=_seedance_task_response(task),
                timeout=10,
            )
        except Exception as exc:
            logger.warning("Seedance callback failed task_id=%s error=%s", task_id, exc)

    def _run_seedance_official_task(
        task_id: str, normalized: dict, output_url_root: str
    ) -> None:
        initial_task = seedance_task_store.get(task_id)
        log_id = str(getattr(initial_task, "log_id", None) or "").strip()
        task_started_at = float(
            getattr(initial_task, "log_started_at", 0.0) or time.time()
        )
        resolution_weight = {"480p": 0, "720p": 60, "1080p": 150}.get(
            str(normalized.get("resolution") or "").lower(), 60
        )
        estimated_total_sec = max(
            120.0,
            150.0 + resolution_weight + max(0, int(normalized["duration"]) - 4) * 15,
        )
        last_progress = 1.0

        def update_async_log(
            *,
            task_status: str,
            progress: float | None = None,
            upstream_job_id: str | None = None,
            preview_url: str | None = None,
            error: str | None = None,
            status_code: int | None = None,
            final: bool = False,
        ) -> None:
            if not log_id:
                return
            patch: dict[str, Any] = {
                "task_id": task_id,
                "task_status": task_status,
                "updated_at": time.time(),
            }
            if progress is not None:
                patch["task_progress"] = round(float(progress), 2)
            if upstream_job_id:
                patch["upstream_job_id"] = str(upstream_job_id)
            if preview_url:
                patch["preview_url"] = preview_url
                patch["preview_kind"] = "video"
            if error:
                patch["error"] = str(error)[:240]
            if status_code is not None:
                patch["status_code"] = int(status_code)
            update_deferred_request_log(log_id, patch, final=final)

        seedance_task_store.update(task_id, status="running", progress=1.0)
        update_async_log(task_status="IN_PROGRESS", progress=1.0)
        _notify_seedance_callback(task_id)
        content_parts: list[dict] = []
        if normalized["prompt"]:
            content_parts.append({"type": "text", "text": normalized["prompt"]})
        image_refs = list(normalized["image_refs"])
        if image_refs:
            role_order = {"first_frame": 0, "last_frame": 1, "reference_image": 2}
            image_refs.sort(key=lambda item: role_order.get(item["role"], 3))
            content_parts.extend(
                {
                    "type": "image_url",
                    "image_url": {"url": item["url"]},
                }
                for item in image_refs
            )
        messages = [{"role": "user", "content": content_parts}]

        try:
            input_images = load_input_images(
                messages,
                max_items=9,
                max_bytes=SEEDANCE_IMAGE_MAX_BYTES,
            )
            input_videos = [
                load_seedance_media_reference(
                    item["url"], "video", proxies=client._requests_proxies()
                )
                for item in normalized["video_refs"]
            ]
            input_audios = [
                load_seedance_media_reference(
                    item["url"], "audio", proxies=client._requests_proxies()
                )
                for item in normalized["audio_refs"]
            ]
        except Exception as exc:
            detail = str(getattr(exc, "detail", None) or exc)
            seedance_task_store.update(
                task_id,
                status="failed",
                error={"code": "InvalidInput", "message": detail},
            )
            update_async_log(
                task_status="FAILED",
                error=detail,
                status_code=422,
                final=True,
            )
            _notify_seedance_callback(task_id)
            return

        reference_mode = (
            "image"
            if any(item["role"] == "reference_image" for item in image_refs)
            else "frame"
        )
        model_conf = video_model_catalog[normalized["model"]]
        max_attempts = client.retry_max_attempts if client.retry_enabled else 1
        max_attempts = max(1, int(max_attempts))
        last_error = "No active tokens available in the pool"
        last_code = "NoAvailableToken"

        for attempt in range(1, max_attempts + 1):
            token = token_manager.get_available(strategy=client.token_rotation_strategy)
            if not token:
                break
            try:
                source_image_ids: list[str] = []
                for image_bytes, image_mime in input_images:
                    if reference_mode == "image" or normalized["upstream_ratio"] == "auto":
                        prepared_bytes = image_bytes
                        prepared_mime = image_mime or "image/jpeg"
                    else:
                        prepared_bytes, prepared_mime = prepare_video_source_image(
                            image_bytes,
                            normalized["upstream_ratio"],
                            normalized["resolution"],
                        )
                    source_image_ids.append(
                        client.upload_image(token, prepared_bytes, prepared_mime)
                    )
                source_video_ids = [
                    client.upload_video(token, media_bytes, media_mime)
                    for media_bytes, media_mime in input_videos
                ]
                source_audio_ids = [
                    client.upload_audio(token, media_bytes, media_mime)
                    for media_bytes, media_mime in input_audios
                ]

                def progress_cb(update: dict) -> None:
                    nonlocal last_progress
                    raw_progress = update.get("task_progress")
                    try:
                        progress = float(raw_progress)
                    except (TypeError, ValueError):
                        progress = 0.0
                    if progress <= 0:
                        elapsed = max(0.0, time.time() - task_started_at)
                        progress = min(95.0, 3.0 + (elapsed / estimated_total_sec) * 92.0)
                    progress = min(99.0, max(last_progress, progress))
                    last_progress = progress
                    upstream_job_id = str(update.get("upstream_job_id") or "").strip()
                    patch: dict = {
                        "status": "running",
                        "progress": progress,
                    }
                    if upstream_job_id:
                        patch["upstream_job_id"] = upstream_job_id
                    seedance_task_store.update(task_id, **patch)
                    update_async_log(
                        task_status="IN_PROGRESS",
                        progress=progress,
                        upstream_job_id=upstream_job_id or None,
                    )

                tmp_path = generated_dir / f"{task_id}.video.tmp"
                video_bytes, video_meta = client.generate_video(
                    token=token,
                    video_conf=model_conf,
                    prompt=normalized["prompt"],
                    aspect_ratio=normalized["upstream_ratio"],
                    duration=normalized["duration"],
                    resolution=normalized["resolution"],
                    source_image_ids=source_image_ids,
                    source_video_ids=source_video_ids,
                    source_audio_ids=source_audio_ids,
                    timeout=max(int(client.generate_timeout), 600),
                    generate_audio=normalized["generate_audio"],
                    reference_mode=reference_mode,
                    seed=normalized["seed"],
                    out_path=tmp_path,
                    progress_cb=progress_cb,
                )
                video_ext = video_ext_from_meta(video_meta)
                filename = f"{task_id}.{video_ext}"
                out_path = generated_dir / filename
                old_size = int(out_path.stat().st_size) if out_path.exists() else 0
                if video_bytes is not None:
                    out_path.write_bytes(video_bytes)
                elif tmp_path.exists():
                    tmp_path.replace(out_path)
                new_size = int(out_path.stat().st_size) if out_path.exists() else 0
                on_generated_file_written(out_path, old_size, new_size)
                token_manager.report_success(token)
                seedance_task_store.update(
                    task_id,
                    status="succeeded",
                    progress=100.0,
                    video_url=f"{output_url_root}{filename}",
                    error=None,
                )
                video_url = f"{output_url_root}{filename}"
                completed_task = seedance_task_store.get(task_id)
                update_async_log(
                    task_status="COMPLETED",
                    progress=100.0,
                    upstream_job_id=(
                        str(getattr(completed_task, "upstream_job_id", None) or "")
                        or None
                    ),
                    preview_url=video_url,
                    status_code=200,
                    final=True,
                )
                _notify_seedance_callback(task_id)
                return
            except quota_error_cls as exc:
                token_manager.report_exhausted(token)
                last_error = str(exc)
                last_code = "QuotaExceeded"
                retryable = attempt < max_attempts
            except auth_error_cls as exc:
                token_manager.report_invalid(token)
                last_error = str(exc)
                last_code = "AuthenticationError"
                retryable = attempt < max_attempts
            except upstream_temp_error_cls as exc:
                last_error = str(exc)
                last_code = "UpstreamUnavailable"
                retryable = attempt < max_attempts and client.should_retry_temporary_error(
                    exc
                )
            except Exception as exc:
                last_error = str(getattr(exc, "detail", None) or exc)
                last_code = "GenerationFailed"
                retryable = False

            if retryable:
                delay = client._retry_delay_for_attempt(attempt)
                if delay > 0:
                    time.sleep(delay)
                continue
            break

        seedance_task_store.update(
            task_id,
            status="failed",
            error={"code": last_code, "message": last_error},
        )
        failed_task = seedance_task_store.get(task_id)
        update_async_log(
            task_status="FAILED",
            progress=last_progress,
            upstream_job_id=(
                str(getattr(failed_task, "upstream_job_id", None) or "") or None
            ),
            error=last_error,
            status_code=502,
            final=True,
        )
        _notify_seedance_callback(task_id)

    @router.post("/api/v3/contents/generations/tasks")
    def create_seedance_official_task(data: dict, request: Request):
        require_service_api_key(request)
        try:
            normalized = parse_seedance_official_request(data, video_model_catalog)
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "InvalidParameter",
                        "message": str(exc),
                    }
                },
            )

        log_started_at = time.time()
        task = seedance_task_store.create(
            model=normalized["response_model"],
            local_model=normalized["model"],
            prompt=normalized["prompt"],
            ratio=normalized["ratio"],
            resolution=normalized["resolution"],
            duration=normalized["duration"],
            generate_audio=normalized["generate_audio"],
            seed=normalized["seed"],
            callback_url=normalized["callback_url"],
            user=normalized["user"],
            log_id=str(getattr(request.state, "log_id", "") or "") or None,
            log_started_at=log_started_at,
            api_style="seedance",
        )
        _bind_deferred_task_log(request, task, normalized)
        output_url_root = public_generated_url(request, "OUTPUT_FILE").replace(
            "OUTPUT_FILE", ""
        )
        threading.Thread(
            target=_run_seedance_official_task,
            args=(task.id, normalized, output_url_root),
            daemon=True,
        ).start()
        return {"id": task.id}

    @router.post("/v1/videos")
    @router.post("/v1/videos/generations")
    def create_grok_video_task(data: dict, request: Request):
        require_service_api_key(request)
        try:
            normalized = parse_grok_video_request(data, video_model_catalog)
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_argument",
                        "message": str(exc),
                    }
                },
            )

        log_started_at = time.time()
        task = seedance_task_store.create(
            model=normalized["response_model"],
            local_model=normalized["model"],
            prompt=normalized["prompt"],
            ratio=normalized["ratio"],
            resolution=normalized["resolution"],
            duration=normalized["duration"],
            generate_audio=normalized["generate_audio"],
            seed=normalized["seed"],
            callback_url=None,
            user=normalized["user"],
            log_id=str(getattr(request.state, "log_id", "") or "") or None,
            log_started_at=log_started_at,
            api_style="grok",
        )
        _bind_deferred_task_log(request, task, normalized)
        output_url_root = public_generated_url(request, "OUTPUT_FILE").replace(
            "OUTPUT_FILE", ""
        )
        threading.Thread(
            target=_run_seedance_official_task,
            args=(task.id, normalized, output_url_root),
            daemon=True,
        ).start()
        return {"request_id": task.id}

    @router.get("/v1/videos/{request_id}")
    def get_grok_video_task(request_id: str, request: Request):
        require_service_api_key(request)
        task = seedance_task_store.get(request_id)
        if not task:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "invalid_argument",
                        "message": "video request not found",
                    }
                },
            )
        return build_grok_video_task_response(task)

    @router.get("/api/v3/contents/generations/tasks/{task_id}")
    def get_seedance_official_task(task_id: str, request: Request):
        require_service_api_key(request)
        task = seedance_task_store.get(task_id)
        if not task:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "TaskNotFound",
                        "message": "video generation task not found",
                    }
                },
            )
        return _seedance_task_response(task)

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

        requested_model_id = str(data.get("model") or "").strip()
        requested_video_conf = video_model_catalog.get(requested_model_id)
        if not requested_video_conf or bool(requested_video_conf.get("hidden", False)):
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": "This endpoint only accepts video models. Use /v1/images/generations for image generation or /v1/images/edits for image editing.",
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

        model_id = requested_model_id
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
        video_seed = None
        if video_conf:
            ratio = str(video_conf.get("aspect_ratio") or ratio)
        video_engine = str(video_conf.get("engine") or "sora2") if video_conf else ""
        if video_conf:
            try:
                duration, ratio, video_resolution, video_seed = (
                    resolve_video_request_parameters(data, video_conf)
                )
            except ValueError as exc:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": {
                            "message": str(exc),
                            "type": "invalid_request_error",
                        }
                    },
                )
            prompt_max_length = int(video_conf.get("prompt_max_length") or 0)
            if prompt_max_length and len(prompt) > prompt_max_length:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": {
                            "message": f"prompt must be at most {prompt_max_length} characters",
                            "type": "invalid_request_error",
                        }
                    },
                )
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
                    elif (
                        video_engine in {"seedance2", "seedance2-fast"}
                        and video_reference_mode == "image"
                    ):
                        max_video_inputs = 9
                    else:
                        max_video_inputs = (
                            2
                            if video_engine
                            in {
                                "seedance2",
                                "seedance2-fast",
                                "veo31-fast",
                                "veo31-standard",
                                "kling-o3",
                                "kling3",
                            }
                            else 1
                        )
                    if len(input_images) > max_video_inputs:
                        raise HTTPException(
                            status_code=400,
                            detail=f"video model supports at most {max_video_inputs} input image(s)",
                        )
                    for image_bytes, _image_mime in input_images[:max_video_inputs]:
                        if (
                            video_engine in {"seedance2", "seedance2-fast"}
                            and video_reference_mode == "image"
                        ):
                            prepared_bytes = image_bytes
                            prepared_mime = _image_mime or "image/jpeg"
                        else:
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
                        resolution=video_resolution,
                        source_image_ids=source_image_ids,
                        entity_refs=entity_refs,
                        timeout=max(int(client.generate_timeout), 600),
                        negative_prompt=negative_prompt,
                        generate_audio=generate_audio,
                        reference_mode=video_reference_mode,
                        seed=video_seed,
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
