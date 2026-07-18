from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

from .catalog import DEFAULT_MODEL_ID, MODEL_CATALOG, SUPPORTED_RATIOS
from .gemini import GEMINI_IMAGE_MODELS, normalize_gemini_model_id
from .openai_images import (
    normalize_openai_gemini_model_id,
    parse_openai_gemini_size,
)


def _compatible_model_resolution(
    data: dict, size_resolution: str | None = None
) -> str:
    explicit = str(
        data.get("output_resolution") or data.get("image_size") or ""
    ).strip().upper()
    if explicit in {"1K", "2K", "4K"}:
        return explicit
    if size_resolution in {"1K", "2K", "4K"}:
        return str(size_resolution)
    quality = str(data.get("quality") or "").strip().lower()
    if quality in {"4k", "ultra", "high"}:
        return "4K"
    if quality in {"1k", "standard", "low"}:
        return "1K"
    return "2K"


def _resolve_compatible_model_id(
    model_id: str,
    *,
    aspect_ratio: str,
    output_resolution: str,
) -> str | None:
    canonical_model_id = normalize_gemini_model_id(
        model_id
    ) or normalize_openai_gemini_model_id(model_id)
    if canonical_model_id is None:
        return None
    family_prefix = GEMINI_IMAGE_MODELS[canonical_model_id]["family_prefix"]
    for candidate_id, config in MODEL_CATALOG.items():
        if not candidate_id.startswith(f"{family_prefix}-"):
            continue
        if str(config.get("aspect_ratio") or "") != aspect_ratio:
            continue
        if str(config.get("output_resolution") or "").upper() != output_resolution:
            continue
        return candidate_id
    for candidate_id in MODEL_CATALOG:
        if candidate_id.startswith(f"{family_prefix}-"):
            return candidate_id
    return None


def resolve_model(model_id: Optional[str]) -> dict:
    if not model_id:
        return MODEL_CATALOG[DEFAULT_MODEL_ID]
    compatible_model_id = _resolve_compatible_model_id(
        model_id,
        aspect_ratio="1:1",
        output_resolution="2K",
    )
    if compatible_model_id:
        return MODEL_CATALOG[compatible_model_id]
    if model_id not in MODEL_CATALOG:
        raise HTTPException(status_code=400, detail=f"Invalid model: {model_id}")
    return MODEL_CATALOG[model_id]


def ratio_from_size(size: str) -> str:
    mapping = {
        "1024x1024": "1:1",
        "1536x1536": "1:1",
        "2048x2048": "1:1",
        "1024x1792": "9:16",
        "1536x2752": "9:16",
        "1792x1024": "16:9",
        "2752x1536": "16:9",
        "2048x1536": "4:3",
        "1536x2048": "3:4",
    }
    return mapping.get(str(size or "").strip(), "1:1")


def resolve_ratio_and_resolution(
    data: dict, model_id: Optional[str]
) -> tuple[str, str, str]:
    canonical_model_id = normalize_gemini_model_id(
        model_id
    ) or normalize_openai_gemini_model_id(model_id)
    compatible_size = (
        parse_openai_gemini_size(data.get("size"))
        if canonical_model_id
        else None
    )
    size_ratio = compatible_size[0] if compatible_size else ""
    size_resolution = compatible_size[1] if compatible_size else None
    ratio = (
        str(data.get("aspect_ratio") or "").strip()
        or size_ratio
        or ratio_from_size(data.get("size", "1024x1024"))
    )
    if ratio not in SUPPORTED_RATIOS:
        ratio = "1:1"

    if model_id and canonical_model_id:
        output_resolution = _compatible_model_resolution(data, size_resolution)
        resolved_model_id = _resolve_compatible_model_id(
            model_id,
            aspect_ratio=ratio,
            output_resolution=output_resolution,
        )
        if resolved_model_id is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported aspect ratio for model {model_id}: {ratio}",
            )
        return ratio, output_resolution, model_id

    resolved_model_id = model_id or DEFAULT_MODEL_ID
    if resolved_model_id not in MODEL_CATALOG:
        resolved_model_id = DEFAULT_MODEL_ID
    model_conf = MODEL_CATALOG[resolved_model_id]

    output_resolution = model_conf["output_resolution"]
    if not model_id:
        quality = str(data.get("quality", "2k")).lower()
        if quality in ("4k", "ultra"):
            output_resolution = "4K"
        elif quality in ("hd", "2k"):
            output_resolution = "2K"
        else:
            output_resolution = "1K"

    model_ratio = model_conf.get("aspect_ratio")
    if model_ratio:
        ratio = model_ratio

    return ratio, output_resolution, resolved_model_id
